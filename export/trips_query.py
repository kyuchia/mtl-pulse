"""
Shared query + trip-assembly logic for MTL Pulse.

Both `export/export_trips.py` (CLI) and `api/main.py` (HTTP) call into here, so the
JSON they produce is identical by construction rather than by careful copying.

The public entry points are:

    build_trips(conn, layers, ...)               -> dict   (the exact trips.json shape)
    build_trips_with_summary(conn, layers, ...)  -> (dict, summary)
    get_range(conn)                              -> dict   (what data actually exists)

`build_trips` returns the documented JSON shape and nothing else — no counters, no
debug keys — because it is serialised straight to the client. The CLI needs per-layer
counts for its console report, so it calls the `_with_summary` variant instead; the
summary is a side channel, deliberately kept out of the payload.

Output shape (see the restart brief, "Data Format: data/trips.json"):

{
  "start_time": "2026-04-14T23:15:19-04:00",
  "end_time":   "...",
  "duration_sec": 6099,
  "modes": ["aircraft", "bus"],
  "trips": [
    {
      "mode": "bus",              # frontend looks this up in MODES for colour
      "id": "39221",              # vehicle_id or icao24
      "label": "747",             # route_id or callsign
      "path": [[lon, lat], ...],
      "timestamps": [0, 23, 47, ...]    # seconds relative to start_time
    },
    ...
  ]
}
"""

import time
from collections import defaultdict
from datetime import timedelta

from psycopg2.extras import RealDictCursor

# Per-layer table mapping. Adding a mode here is most of what a new layer needs,
# provided its table also has fetched_at / latitude / longitude columns.
LAYERS = {
    "bus": {
        "table": "vehicle_positions",
        "id_col": "vehicle_id",
        "label_col": "route_id",
    },
    "aircraft": {
        "table": "aircraft_positions",
        "id_col": "icao24",
        "label_col": "callsign",
    },
}

VALID_LAYERS = tuple(LAYERS)


class NoDataError(Exception):
    """Raised when every requested layer came back empty.

    Carries the list of empty layer names so callers can report which ones missed
    without re-running the queries.
    """

    def __init__(self, empty):
        self.empty = list(empty)
        super().__init__(f"no rows in any requested layer: {', '.join(self.empty)}")


def build_where(layer, *, start=None, end=None, hours=None, route=None,
                include_ground=False):
    """Build the WHERE fragments + bound parameters for one layer."""
    where = ["latitude IS NOT NULL", "longitude IS NOT NULL"]
    params = []

    if hours is not None:
        where.append("fetched_at >= NOW() - INTERVAL %s")
        params.append(f"{hours} hours")
    if start:
        where.append("fetched_at >= %s")
        params.append(start)
    if end:
        where.append("fetched_at <= %s")
        params.append(end)

    if layer == "bus" and route:
        where.append("route_id = %s")
        params.append(route)

    # Aircraft parked on the apron have no meaningful trail and clump into a blob
    # over Dorval, so they are filtered out unless explicitly requested.
    if layer == "aircraft" and not include_ground:
        where.append("(on_ground IS NULL OR on_ground = false)")

    return where, params


def fetch_layer(conn, layer, **filters):
    """Fetch raw position rows for one layer, ordered by vehicle then time."""
    cfg = LAYERS[layer]
    where, params = build_where(layer, **filters)

    query = f"""
        SELECT fetched_at, {cfg['id_col']} AS uid, {cfg['label_col']} AS label,
               latitude, longitude
        FROM {cfg['table']}
        WHERE {' AND '.join(where)}
        ORDER BY {cfg['id_col']}, fetched_at
    """

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def build_trips_with_summary(conn, layers, *, start=None, end=None, hours=None,
                             route=None, min_points=3, include_ground=False):
    """Assemble the trips payload, plus a summary for console reporting.

    Returns (output, summary). `summary` carries per-layer (kept, skipped, points)
    counts, the names of any empty layers, and the raw start/end datetimes — the CLI
    prints the datetimes directly, so it needs the objects rather than the ISO strings
    that go into the payload.

    Raises NoDataError if every requested layer is empty.
    """
    raw = {}
    empty = []
    for layer in layers:
        rows = fetch_layer(conn, layer, start=start, end=end, hours=hours,
                           route=route, include_ground=include_ground)
        if rows:
            raw[layer] = rows
        else:
            empty.append(layer)

    if not raw:
        raise NoDataError(empty)

    # Every layer shares one timeline, so the origin is taken across all of them.
    all_times = [r["fetched_at"] for rows in raw.values() for r in rows]
    start_time = min(all_times)
    end_time = max(all_times)
    duration_sec = (end_time - start_time).total_seconds()

    trips = []
    per_layer = {}
    for layer, rows in raw.items():
        by_id = defaultdict(list)
        for r in rows:
            by_id[r["uid"]].append(r)

        kept = skipped = 0
        for uid, points in by_id.items():
            if len(points) < min_points:
                skipped += 1
                continue
            trips.append({
                "mode": layer,
                "id": uid,
                "label": (points[0]["label"] or "").strip() or None,
                "path": [[p["longitude"], p["latitude"]] for p in points],
                "timestamps": [(p["fetched_at"] - start_time).total_seconds()
                               for p in points],
            })
            kept += 1
        per_layer[layer] = (kept, skipped, sum(len(p) for p in by_id.values()))

    output = {
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_sec": duration_sec,
        "modes": sorted(raw.keys()),
        "trips": trips,
    }

    summary = {
        "per_layer": per_layer,
        "empty": empty,
        "start_time": start_time,
        "end_time": end_time,
        "duration_sec": duration_sec,
    }
    return output, summary


def build_trips(conn, layers, **filters):
    """Assemble the trips payload in the exact documented JSON shape."""
    output, _ = build_trips_with_summary(conn, layers, **filters)
    return output


# Memoised because the query is a bucketed COUNT over ~1M rows: fine once, wasteful on
# every cold page load. The recorded tables only change while a fetcher is running, so a
# few minutes of staleness costs nothing.
BUSIEST_TTL_SEC = 300
_BUSIEST_CACHE = {"at": 0.0, "value": None, "computed": False}


def busiest_hour(conn, ttl=BUSIEST_TTL_SEC):
    """The one-hour window holding the most recorded rows, across all recorded layers.

    This is the sane default window when simulated modes are in play: a schedule can be
    materialised for any instant, so the only real constraint is where recorded data
    exists, and the densest hour is the one worth looking at. Derived from the data
    rather than hardcoded to a clock time, so it stays correct as new data is collected.
    """
    now = time.monotonic()
    if _BUSIEST_CACHE["computed"] and (now - _BUSIEST_CACHE["at"]) < ttl:
        return _BUSIEST_CACHE["value"]

    counts = defaultdict(int)
    with conn.cursor() as cur:
        # Same guard as get_range: this machine's PostgreSQL 17 has a broken install path
        # and its parallel workers die on `could not open directory ".../timezonesets"`.
        # A GROUP BY over ~1M rows is precisely the shape that tips the planner parallel.
        cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        for cfg in LAYERS.values():
            cur.execute(f"""
                SELECT date_trunc('hour', fetched_at) AS bucket, COUNT(*) AS n
                FROM {cfg['table']}
                WHERE fetched_at IS NOT NULL
                GROUP BY 1
            """)
            for bucket, n in cur.fetchall():
                counts[bucket] += n

    value = None
    if counts:
        bucket, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        value = {
            "start": bucket.isoformat(),
            "end": (bucket + timedelta(hours=1)).isoformat(),
            "rows": n,
        }

    _BUSIEST_CACHE.update({"at": now, "value": value, "computed": True})
    return value


def get_range(conn):
    """Report what data actually exists, per layer, plus the shared overlap.

    The overlap matters: the two tables currently intersect for only ~1.7 hours, so a
    client that defaults its window to the bus range alone will show an empty aircraft
    layer and look broken.
    """
    layers = {}
    bounds = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # COUNT(*) over ~1M rows tips the planner into a parallel plan, and this
        # machine's PostgreSQL 17 has a broken install path — its parallel workers die
        # with `could not open directory ".../timezonesets"`. Aggregates over one
        # column are fast enough single-threaded (MIN/MAX use the fetched_at index),
        # so the guard costs nothing here. SET LOCAL is scoped to this transaction and
        # is undone by the caller's rollback, so it never leaks to a pooled connection.
        cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")

        for layer, cfg in LAYERS.items():
            cur.execute(f"""
                SELECT COUNT(*) AS rows,
                       MIN(fetched_at) AS min_time,
                       MAX(fetched_at) AS max_time
                FROM {cfg['table']}
            """)
            r = cur.fetchone()
            layers[layer] = {
                "table": cfg["table"],
                "rows": r["rows"],
                "min_time": r["min_time"].isoformat() if r["min_time"] else None,
                "max_time": r["max_time"].isoformat() if r["max_time"] else None,
            }
            if r["min_time"] and r["max_time"]:
                bounds.append((r["min_time"], r["max_time"]))

    # Intersect on the datetime objects, not the ISO strings — string ordering only
    # happens to work while every row carries the same UTC offset.
    overlap = None
    if len(bounds) == len(LAYERS) and bounds:
        lo = max(b[0] for b in bounds)
        hi = min(b[1] for b in bounds)
        if lo < hi:
            overlap = {"start": lo.isoformat(), "end": hi.isoformat()}

    return {
        "layers": layers,
        "overlap": overlap,
        # For recorded-only layers `overlap` is the right default; when a simulated mode
        # is requested the overlap is meaningless (it only knows the recorded tables), so
        # the client uses busiest_hour instead.
        "busiest_hour": busiest_hour(conn),
    }
