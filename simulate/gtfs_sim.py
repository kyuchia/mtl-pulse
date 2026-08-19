"""
GTFS static → simulated vehicle trips, in the same JSON shape the recorded layers use.

    python simulate/gtfs_sim.py --mode all --start "2026-04-14 23:15-04:00" \
                                --end "2026-04-15 00:57-04:00" --out data/trips_sim.json

One simulator, three modes:

    metro  ← STM feed,        route_type 1  (4 lines; STM publishes no realtime GPS)
    train  ← exo trains feed, route_type 2  (realtime exists but needs an application)
    rem    ← REM feed,        route_type 0  (realtime feed is alerts-only)

Output is identical in shape to export/export_trips.py, so the frontend and /api/trips
consume it unchanged. `mode` is metro/train/rem, `id` is trip_id, and `label` is
route_id — for REM the branch identity MUST come from route_id, because all three of its
route_color values are near-identical green (73A400 / 72A300 / 73A400) and cannot tell
A1/A3/A4 apart.

Three things here are less obvious than they look; each is explained at its function:
service-date substitution, service days that run past midnight, and distance along shape
when the feed omits shape_dist_traveled.
"""

import argparse
import bisect
import csv
import io
import json
import math
import os
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Montreal")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GTFS_DIR = os.path.join(ROOT, "gtfs")

# mode → (agency archive dir, GTFS route_type to keep)
MODES = {
    "metro": {"agency": "stm",        "route_types": {"1"}},
    "train": {"agency": "exo_trains", "route_types": {"2"}},
    "rem":   {"agency": "rem",        "route_types": {"0"}},
}

WEEKDAY_COLS = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"]


# ----------------------------------------------------------------- loading

def _rows(zf, name):
    if name not in zf.namelist():
        return
    with zf.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig"))


def _gtfs_date(s):
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


class Gtfs:
    """One parsed feed, filtered to the route_types a mode cares about."""

    def __init__(self, zip_path, route_types):
        self.path = zip_path
        zf = zipfile.ZipFile(zip_path)

        self.routes = {}
        for r in _rows(zf, "routes.txt"):
            if r["route_type"] in route_types:
                self.routes[r["route_id"]] = r

        self.trips = {}
        for r in _rows(zf, "trips.txt"):
            if r["route_id"] in self.routes:
                self.trips[r["trip_id"]] = r

        # stop_times, grouped per trip and sorted by stop_sequence
        st = defaultdict(list)
        for r in _rows(zf, "stop_times.txt"):
            if r["trip_id"] in self.trips:
                st[r["trip_id"]].append(r)
        self.stop_times = {}
        for tid, rows in st.items():
            rows.sort(key=lambda r: int(r["stop_sequence"]))
            self.stop_times[tid] = rows

        self.stops = {r["stop_id"]: r for r in _rows(zf, "stops.txt")}

        # shapes, only those referenced by the kept trips
        wanted = {t.get("shape_id") for t in self.trips.values() if t.get("shape_id")}
        pts = defaultdict(list)
        for r in _rows(zf, "shapes.txt"):
            if r["shape_id"] in wanted:
                pts[r["shape_id"]].append((
                    int(r["shape_pt_sequence"]),
                    float(r["shape_pt_lat"]),
                    float(r["shape_pt_lon"]),
                    _f(r.get("shape_dist_traveled")),
                ))
        self.shapes = {}
        for sid, p in pts.items():
            p.sort(key=lambda x: x[0])
            self.shapes[sid] = [(lat, lon, d) for _, lat, lon, d in p]

        self.calendar = list(_rows(zf, "calendar.txt"))
        self.calendar_dates = list(_rows(zf, "calendar_dates.txt"))

        # every date carrying an explicit exception — used to avoid substituting onto a
        # holiday or a special-service day
        self.exception_dates = {_gtfs_date(r["date"]) for r in self.calendar_dates}

        spans = [(_gtfs_date(r["start_date"]), _gtfs_date(r["end_date"]))
                 for r in self.calendar if r.get("start_date")]
        if spans:
            self.span_start = min(s for s, _ in spans)
            self.span_end = max(e for _, e in spans)
        elif self.exception_dates:
            self.span_start = min(self.exception_dates)
            self.span_end = max(self.exception_dates)
        else:
            self.span_start = self.span_end = None

        # caches
        self._cum = {}
        self._proj = {}
        zf.close()

    # ---- shape geometry ----

    def cumulative(self, shape_id):
        """(lats, lons, cumulative_metres) for a shape, computed once."""
        if shape_id not in self._cum:
            pts = self.shapes[shape_id]
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            cum = [0.0]
            for i in range(1, len(pts)):
                cum.append(cum[-1] + _metres(lats[i-1], lons[i-1], lats[i], lons[i]))
            self._cum[shape_id] = (lats, lons, cum)
        return self._cum[shape_id]

    def has_shape_dist(self, shape_id):
        pts = self.shapes.get(shape_id) or []
        return bool(pts) and all(p[2] is not None for p in pts)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _metres(lat1, lon1, lat2, lon2):
    """Equirectangular approximation — negligible error at city scale, fast."""
    mlat = math.radians((lat1 + lat2) / 2)
    dx = math.radians(lon2 - lon1) * math.cos(mlat) * 6371000
    dy = math.radians(lat2 - lat1) * 6371000
    return math.hypot(dx, dy)


# ------------------------------------------------- service date resolution

def resolve_service_ids(gtfs, service_date):
    """service_ids running on `service_date`, per the GTFS spec.

    calendar.txt supplies the weekly pattern bounded by start_date/end_date;
    calendar_dates.txt then adds (exception_type 1) or removes (2) individual dates.
    Exceptions are applied after the base pattern, so a removal always wins.
    """
    col = WEEKDAY_COLS[service_date.weekday()]
    active = set()
    for r in gtfs.calendar:
        if r.get(col) != "1":
            continue
        if not (_gtfs_date(r["start_date"]) <= service_date <= _gtfs_date(r["end_date"])):
            continue
        active.add(r["service_id"])

    for r in gtfs.calendar_dates:
        if _gtfs_date(r["date"]) != service_date:
            continue
        if r["exception_type"] == "1":
            active.add(r["service_id"])
        elif r["exception_type"] == "2":
            active.discard(r["service_id"])
    return active


def effective_date(gtfs, service_date, verbose=True):
    """The date whose schedule we actually read for `service_date`.

    A feed downloaded today describes a service period that starts months after the
    April 2026 replay data was recorded, so `service_date` usually falls outside it.
    Rather than matching an abstract "weekday schedule", pick a REAL date inside the
    feed's span, which keeps one single code path: everything downstream just calls
    this and then does an ordinary date lookup.

    The substitute must match on all of:
      - day of week          (Tuesday service differs from Sunday service)
      - inside the span      (otherwise resolve_service_ids finds nothing)
      - no calendar_dates exception on it (avoids holidays and one-off special service)
      - same DST offset      (an EDT source date mapped onto an EST substitute would
                              shift the entire timeline by an hour)
    and among the candidates, is nearest to service_date.
    """
    if gtfs.span_start is None:
        return service_date
    if gtfs.span_start <= service_date <= gtfs.span_end:
        return service_date

    want_dow = service_date.weekday()
    want_off = datetime(service_date.year, service_date.month, service_date.day,
                        12, tzinfo=TZ).utcoffset()

    best = None
    d = gtfs.span_start
    while d <= gtfs.span_end:
        if d.weekday() == want_dow and d not in gtfs.exception_dates:
            off = datetime(d.year, d.month, d.day, 12, tzinfo=TZ).utcoffset()
            if off == want_off:
                dist = abs((d - service_date).days)
                if best is None or dist < best[0]:
                    best = (dist, d)
        d += timedelta(days=1)

    if best is None:
        # No clean same-weekday, same-DST, exception-free date exists. Fall back to the
        # same weekday inside the span, ignoring the exception filter, and say so.
        d = gtfs.span_start
        while d <= gtfs.span_end:
            if d.weekday() == want_dow:
                dist = abs((d - service_date).days)
                if best is None or dist < best[0]:
                    best = (dist, d)
            d += timedelta(days=1)
        if best and verbose:
            print(f"   ⚠️  {service_date} → {best[1]}: no exception-free candidate, "
                  f"relaxed the holiday filter", file=sys.stderr)

    if best is None:
        return service_date

    if verbose:
        dow = service_date.strftime("%A")
        print(f"   ↪︎  service date {service_date} ({dow}) is outside the feed span "
              f"{gtfs.span_start}→{gtfs.span_end}; using {best[1]} "
              f"(same weekday, same DST offset, {best[0]} days away)", file=sys.stderr)
    return best[1]


# ------------------------------------------------------ time and midnight

def _service_midnight(day):
    """GTFS anchor for a service day: noon minus 12 hours, local time.

    The spec defines stop_times relative to noon-minus-12h rather than to midnight
    precisely so that a DST transition does not shift every trip on that day.
    """
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=TZ)
    return noon - timedelta(hours=12)


def _secs(hhmmss):
    """GTFS time → seconds since the service day's anchor. Hours may exceed 24."""
    if not hhmmss:
        return None
    parts = hhmmss.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


# ------------------------------------------------------------ projection

def _project_dist(gtfs, shape_id, lat, lon, min_dist):
    """Distance along a shape of the nearest point to (lat, lon), at or after min_dist.

    Used when a feed omits shape_dist_traveled — STM's does, for both stop_times.txt and
    shapes.txt, so every metro stop is projected this way. exo and REM both publish
    shape_dist_traveled and never reach this path.

    The min_dist floor matters: metro shapes pass near the same point twice on loops and
    near-parallel segments, and an unconstrained nearest-point search can snap a later
    stop backwards, producing a trip that jumps in reverse.
    """
    key = (shape_id, lat, lon, round(min_dist))
    hit = gtfs._proj.get(key)
    if hit is not None:
        return hit

    lats, lons, cum = gtfs.cumulative(shape_id)
    best_d, best_dist = None, None
    for i in range(len(lats) - 1):
        if cum[i + 1] < min_dist:
            continue
        d, along = _point_seg(lat, lon, lats[i], lons[i], lats[i + 1], lons[i + 1])
        pos = cum[i] + along
        if pos < min_dist:
            pos = min_dist
        if best_d is None or d < best_d:
            best_d, best_dist = d, pos
    if best_dist is None:
        best_dist = cum[-1]
    gtfs._proj[key] = best_dist
    return best_dist


def _point_seg(plat, plon, alat, alon, blat, blon):
    """(distance from point to segment, distance along segment of the foot) in metres."""
    mlat = math.radians((alat + blat) / 2)
    def xy(la, lo):
        return (math.radians(lo) * math.cos(mlat) * 6371000,
                math.radians(la) * 6371000)
    px, py = xy(plat, plon)
    ax, ay = xy(alat, alon)
    bx, by = xy(blat, blon)
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy
    if L2 == 0:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
    fx, fy = ax + t * vx, ay + t * vy
    return math.hypot(px - fx, py - fy), t * math.sqrt(L2)


def _pos_at(gtfs, shape_id, dist):
    """(lon, lat) at a distance along the shape, linearly interpolated."""
    lats, lons, cum = gtfs.cumulative(shape_id)
    if dist <= 0:
        return (lons[0], lats[0])
    if dist >= cum[-1]:
        return (lons[-1], lats[-1])
    i = bisect.bisect_right(cum, dist) - 1
    i = max(0, min(i, len(cum) - 2))
    seg = cum[i + 1] - cum[i]
    f = 0.0 if seg <= 0 else (dist - cum[i]) / seg
    return (lons[i] + (lons[i + 1] - lons[i]) * f,
            lats[i] + (lats[i + 1] - lats[i]) * f)


def trip_stop_profile(gtfs, trip_id):
    """[(seconds_since_anchor, distance_along_shape)] for one trip, monotonic in both.

    Uses shape_dist_traveled when the feed publishes it (exo, REM) and projects stops
    onto the polyline when it does not (STM).
    """
    trip = gtfs.trips[trip_id]
    shape_id = trip.get("shape_id")
    rows = gtfs.stop_times.get(trip_id) or []
    if not shape_id or shape_id not in gtfs.shapes or len(rows) < 2:
        return None, shape_id

    _, _, cum = gtfs.cumulative(shape_id)
    total = cum[-1]
    use_native = gtfs.has_shape_dist(shape_id) and any(
        _f(r.get("shape_dist_traveled")) is not None for r in rows)

    # native shape_dist_traveled is in the feed's own units; rescale onto metres so the
    # geometry lookup and the stop distances share one scale
    if use_native:
        shape_max = max(p[2] for p in gtfs.shapes[shape_id])
        scale = (total / shape_max) if shape_max else 1.0

    profile = []
    prev_d = 0.0
    for r in rows:
        t = _secs(r.get("departure_time") or r.get("arrival_time"))
        if t is None:
            continue
        d = None
        if use_native:
            raw = _f(r.get("shape_dist_traveled"))
            if raw is not None:
                d = raw * scale
        if d is None:
            stop = gtfs.stops.get(r["stop_id"])
            if not stop:
                continue
            d = _project_dist(gtfs, shape_id, float(stop["stop_lat"]),
                              float(stop["stop_lon"]), prev_d)
        d = max(prev_d, min(d, total))
        if profile and t <= profile[-1][0]:
            # duplicate or non-increasing timestamp; keep the geometry moving forward
            continue
        profile.append((t, d))
        prev_d = d

    return (profile if len(profile) >= 2 else None), shape_id


# ------------------------------------------------------------ simulation

# Parsed feeds are cached per (zip path, route types). The STM archive is ~42 MB and
# parsing it takes seconds, which is fine once per process but not once per HTTP request.
# Gtfs is read-only after construction apart from its own memoisation dicts, so sharing
# one instance across the API's threadpool is safe enough for a local dev tool.
_FEED_CACHE = {}


def load_gtfs(zip_path, route_types):
    key = (zip_path, frozenset(route_types))
    if key not in _FEED_CACHE:
        _FEED_CACHE[key] = Gtfs(zip_path, route_types)
    return _FEED_CACHE[key]


def simulate(mode, start_dt, end_dt, interval=20, gtfs_dir=None, route=None,
             verbose=True):
    """Materialise one mode over an absolute wall-clock window.

    Returns (trips, stats). Each trip is
        {"mode", "id", "label", "path": [[lon,lat],...], "times": [datetime,...]}
    with absolute datetimes; the caller re-bases them onto a shared timeline.
    """
    cfg = MODES[mode]
    zip_path = _latest_zip(cfg["agency"], gtfs_dir)
    gtfs = load_gtfs(zip_path, cfg["route_types"])

    stats = {"trips_considered": 0, "trips_emitted": 0, "no_shape": 0,
             "no_profile": 0, "feed": os.path.relpath(zip_path, ROOT),
             "service_days": []}

    # C3: a trip that departs at 25:30 on the 14th belongs to the 14th's service day but
    # runs on the 15th in wall-clock terms. Slicing by calendar day would drop it, so the
    # candidate service days are expanded backwards by one and everything is filtered by
    # absolute timestamp at the end.
    days = []
    d = start_dt.astimezone(TZ).date() - timedelta(days=1)
    last = end_dt.astimezone(TZ).date()
    while d <= last:
        days.append(d)
        d += timedelta(days=1)

    out = []
    for day in days:
        eff = effective_date(gtfs, day, verbose=verbose)
        service_ids = resolve_service_ids(gtfs, eff)
        if not service_ids:
            continue
        anchor = _service_midnight(day)
        lo = (start_dt - anchor).total_seconds()
        hi = (end_dt - anchor).total_seconds()
        stats["service_days"].append({
            "day": day.isoformat(), "effective": eff.isoformat(),
            "substituted": eff != day, "services": len(service_ids),
        })

        for tid, trip in gtfs.trips.items():
            if trip.get("service_id") not in service_ids:
                continue
            if route and trip.get("route_id") != route:
                continue
            rows = gtfs.stop_times.get(tid) or []
            if len(rows) < 2:
                continue
            t0 = _secs(rows[0].get("departure_time") or rows[0].get("arrival_time"))
            t1 = _secs(rows[-1].get("arrival_time") or rows[-1].get("departure_time"))
            if t0 is None or t1 is None or t1 < lo or t0 > hi:
                continue          # cheap reject before the expensive projection
            stats["trips_considered"] += 1

            if not trip.get("shape_id"):
                stats["no_shape"] += 1
                continue
            profile, shape_id = trip_stop_profile(gtfs, tid)
            if not profile:
                stats["no_profile"] += 1
                continue

            path, times = [], []
            first, lastt = profile[0][0], profile[-1][0]
            # align samples to the interval grid so separate trips share sample instants
            t = max(first, math.ceil(lo / interval) * interval)
            while t <= min(lastt, hi):
                dist = _interp_dist(profile, t)
                path.append(list(_pos_at(gtfs, shape_id, dist)))
                times.append(anchor + timedelta(seconds=t))
                t += interval

            if len(path) >= 2:
                out.append({
                    "mode": mode,
                    "id": tid,
                    "label": trip.get("route_id"),
                    "path": path,
                    "times": times,
                })
                stats["trips_emitted"] += 1

    return out, stats


def _interp_dist(profile, t):
    """Distance along shape at time t, linear between the bracketing stop_times."""
    times = [p[0] for p in profile]
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return profile[0][1]
    if i >= len(profile) - 1:
        return profile[-1][1]
    t0, d0 = profile[i]
    t1, d1 = profile[i + 1]
    if t1 == t0:
        return d0
    return d0 + (d1 - d0) * (t - t0) / (t1 - t0)


def _latest_zip(agency, gtfs_dir=None):
    base = os.path.join(gtfs_dir or GTFS_DIR, agency)
    if not os.path.isdir(base):
        raise SystemExit(f"❌ no archived feed for '{agency}'. "
                         f"Run: python scripts/fetch_gtfs.py --agency {agency}")
    days = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    for day in reversed(days):
        p = os.path.join(base, day, "gtfs.zip")
        if os.path.isfile(p):
            return p
    raise SystemExit(f"❌ no gtfs.zip under {base}")


# ----------------------------------------------------------------- output

def to_payload(all_trips, start_dt=None):
    """Re-base absolute times onto one shared timeline, in the documented JSON shape."""
    if not all_trips:
        return None
    lo = min(min(t["times"]) for t in all_trips)
    hi = max(max(t["times"]) for t in all_trips)
    if start_dt is not None:
        lo = min(lo, start_dt)
    trips = [{
        "mode": t["mode"],
        "id": t["id"],
        "label": t["label"],
        "path": t["path"],
        "timestamps": [(x - lo).total_seconds() for x in t["times"]],
    } for t in all_trips]
    return {
        "start_time": lo.isoformat(),
        "end_time": hi.isoformat(),
        "duration_sec": (hi - lo).total_seconds(),
        "modes": sorted({t["mode"] for t in all_trips}),
        "trips": trips,
    }


def parse_dt(s):
    """Accept the same forms as export_trips.py; assume Montréal if no offset given."""
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"❌ cannot parse datetime: {s!r}")
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["metro", "train", "rem", "all"], default="all")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--interval", type=int, default=20,
                    help="seconds between position samples (default 20, matching the bus fetcher)")
    ap.add_argument("--route", default=None, help="only this route_id")
    ap.add_argument("--out", default="data/trips_sim.json")
    args = ap.parse_args()

    start_dt, end_dt = parse_dt(args.start), parse_dt(args.end)
    if end_dt <= start_dt:
        raise SystemExit("❌ --end must be after --start")

    modes = ["metro", "train", "rem"] if args.mode == "all" else [args.mode]

    all_trips, summary = [], {}
    for mode in modes:
        trips, stats = simulate(mode, start_dt, end_dt,
                                interval=args.interval, route=args.route)
        all_trips.extend(trips)
        summary[mode] = stats
        subs = [s for s in stats["service_days"] if s["substituted"]]
        print(f"   {mode:6s} {stats['trips_emitted']:5,} trips  "
              f"({stats['no_shape']} no shape, {stats['no_profile']} unusable)  "
              f"{len(subs)}/{len(stats['service_days'])} service days substituted")

    payload = to_payload(all_trips)
    if payload is None:
        print("❌ no trips in this window", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    pts = sum(len(t["path"]) for t in payload["trips"])
    size_mb = os.path.getsize(args.out) / 1024 / 1024
    print(f"✅ {args.out}  ({size_mb:.1f} MB)")
    print(f"   Range:  {payload['start_time']}  →  {payload['end_time']}")
    print(f"   Length: {payload['duration_sec']:.0f} sec "
          f"({payload['duration_sec']/60:.1f} min)")
    print(f"   Modes:  {', '.join(payload['modes'])}  "
          f"trips {len(payload['trips']):,}  points {pts:,}")


if __name__ == "__main__":
    main()
