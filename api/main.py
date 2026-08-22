"""
MTL Pulse backend.

Serves the same trip JSON that `export/export_trips.py` writes to disk, so the browser
can change its time window without anyone re-running the export script. Both sides call
into `export/trips_query.py`; this module adds only HTTP concerns.

Run from the project root:

    uvicorn api.main:app --reload --port 8000

Then open http://localhost:8000/docs/ — the page is served by this same app, which also
retires the old "you must start the server from the project root" trap.
"""

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
from psycopg2 import pool as pgpool
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "export"))
sys.path.insert(0, str(ROOT / "simulate"))

from trips_query import NoDataError, VALID_LAYERS, build_trips, get_range  # noqa: E402
import gtfs_sim  # noqa: E402

# Recorded layers come from Postgres; simulated ones are materialised from archived
# static GTFS on the fly. They are mergeable: /api/trips?layer=all returns both kinds on
# one shared timeline, so the frontend cannot tell which is which.
DB_LAYERS = tuple(VALID_LAYERS)                  # bus, aircraft
SIM_LAYERS = tuple(gtfs_sim.MODES)               # metro, train, rem
ALL_LAYERS = DB_LAYERS + SIM_LAYERS

PG_DSN = os.environ.get("PG_DSN", "dbname=mtl_pulse")

# Point budget for a single /api/trips response. The frontend was benchmarked smooth at
# ~178k points, so the default is roughly 2x that: generous enough that no realistic
# window trips it, tight enough that an unbounded query fails fast with a clear message
# instead of streaming ~1M points into the browser. The CLI is deliberately NOT capped —
# writing a huge file to disk is a legitimate thing to want.
MAX_POINTS = int(os.environ.get("MTL_MAX_POINTS", 400_000))

app = FastAPI(title="MTL Pulse API", version="1.0")

# A one-hour peak window is ~178k points; the JSON compresses roughly 5-10x, which is
# worth it even over localhost.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Localhost only — this is a local dev tool, not something to expose.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- database

# Built lazily so the app still boots (and still serves web/) when Postgres is down.
_POOL = None


def _get_pool():
    global _POOL
    if _POOL is None:
        _POOL = pgpool.ThreadedConnectionPool(minconn=1, maxconn=8, dsn=PG_DSN)
    return _POOL


@contextmanager
def db_conn():
    """Borrow a pooled connection, always returning it in a clean state.

    Every query here is read-only, so the transaction is rolled back on the way out
    either way — otherwise a connection returns to the pool holding an idle
    transaction, or a failed one that poisons the next request that borrows it.
    """
    try:
        pool = _get_pool()
    except psycopg2.OperationalError as e:
        raise HTTPException(status_code=503,
                            detail=f"cannot connect to PostgreSQL ({PG_DSN}): {e}")

    conn = pool.getconn()
    try:
        yield conn
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------- layer parsing

def _resolve_layers(layer):
    """Turn the `layer` parameter into an ordered, de-duplicated list of layer names.

    Accepts a single name, one of the `both`/`all` aliases, or a comma-separated list
    mixing recorded and simulated layers (e.g. `metro,rem,train`). Aliases may appear
    inside a list too, so `both,metro` is the recorded pair plus metro.

    Order follows ALL_LAYERS rather than the order given, so the same set always produces
    the same payload regardless of how it was spelled.
    """
    raw = [part.strip() for part in (layer or "").split(",")]
    raw = [part for part in raw if part]
    if not raw:
        raise HTTPException(status_code=400, detail="layer must not be empty")

    want = set()
    for part in raw:
        if part == "both":
            want.update(DB_LAYERS)
        elif part == "all":
            want.update(ALL_LAYERS)
        elif part in ALL_LAYERS:
            want.add(part)
        else:
            raise HTTPException(
                status_code=400,
                detail=(f"unknown layer {part!r}. Valid: {', '.join(ALL_LAYERS)}, "
                        f"both, all — or a comma-separated list of them"),
            )
    return [l for l in ALL_LAYERS if l in want]


# ---------------------------------------------------------------- simulated layers

def _sim_window(start, end, hours):
    """Resolve the absolute window the simulator needs.

    Unlike a DB query, a schedule can be materialised for any instant in history, so an
    unbounded request has no natural answer — it is rejected rather than guessed at.
    """
    if start and end:
        return gtfs_sim.parse_dt(start), gtfs_sim.parse_dt(end)
    if hours is not None:
        now = datetime.now(gtfs_sim.TZ)
        return now - timedelta(hours=hours), now
    raise HTTPException(
        status_code=400,
        detail=("simulated layers (metro, train, rem) need an explicit window: "
                "pass start and end, or hours"),
    )


def _add_simulated(payload, sim_layers, start, end, hours, route):
    """Materialise simulated modes and merge them onto one shared timeline.

    Both sides are re-based onto a single origin so `timestamps` stay comparable across
    recorded and simulated trips — deck.gl interpolates every layer against one
    `currentTime`, so a per-source origin would desynchronise them.

    The re-basing is a scalar shift per trip, not a per-point datetime conversion: a
    peak window carries ~178k points and rebuilding a datetime for each costs more than
    the query that produced them.
    """
    lo, hi = _sim_window(start, end, hours)

    sim_trips = []
    for mode in sim_layers:
        try:
            trips, _stats = gtfs_sim.simulate(mode, lo, hi, route=route, verbose=False)
        except SystemExit as e:
            # _latest_zip raises SystemExit when a feed was never archived
            raise HTTPException(status_code=503, detail=str(e))
        sim_trips.extend(trips)

    if not sim_trips and payload is None:
        return None

    # epoch seconds throughout — cheap to compare and to shift
    sim_epochs = [[t.timestamp() for t in tr["times"]] for tr in sim_trips]
    candidates = [e[0] for e in sim_epochs if e]
    db_start_epoch = None
    if payload is not None:
        db_start_epoch = datetime.fromisoformat(payload["start_time"]).timestamp()
        candidates.append(db_start_epoch)
    if not candidates:
        return payload
    origin = min(candidates)

    trips_out = []
    if payload is not None:
        shift = db_start_epoch - origin        # one scalar for every recorded trip
        for t in payload["trips"]:
            if shift:
                t["timestamps"] = [ts + shift for ts in t["timestamps"]]
            trips_out.append(t)

    for tr, epochs in zip(sim_trips, sim_epochs):
        trips_out.append({
            "mode": tr["mode"],
            "id": tr["id"],
            "label": tr["label"],
            "path": tr["path"],
            "timestamps": [e - origin for e in epochs],
        })

    ends = [max(e) for e in sim_epochs if e]
    if payload is not None:
        ends.append(datetime.fromisoformat(payload["end_time"]).timestamp())
    end_epoch = max(ends)

    return {
        "start_time": datetime.fromtimestamp(origin, gtfs_sim.TZ).isoformat(),
        "end_time": datetime.fromtimestamp(end_epoch, gtfs_sim.TZ).isoformat(),
        "duration_sec": end_epoch - origin,
        "modes": sorted({t["mode"] for t in trips_out}),
        "trips": trips_out,
    }


# ---------------------------------------------------------------- API

@app.get("/api/trips")
def api_trips(
    start: str | None = Query(None, description='e.g. "2026-04-14 23:15-04:00"'),
    end: str | None = Query(None),
    hours: float | None = Query(None, description="last N hours; needs a live fetcher"),
    layer: str = Query("both", description=("bus | aircraft | metro | train | rem | both | all, "
                                        "or a comma-separated list e.g. metro,rem")),
    route: str | None = Query(None, description="filter to one route (bus layer only)"),
    # ge=0 rather than ge=1 so the CLI's `--min-points 0` has an exact HTTP equivalent;
    # 0 and 1 both keep everything, since a trace group is never empty.
    min_points: int = Query(3, ge=0, description="drop traces shorter than this"),
    include_ground: bool = Query(False, description="keep parked aircraft"),
):
    """Return the same JSON shape `export/export_trips.py` writes to data/trips.json.

    `layer` accepts the recorded layers (bus, aircraft), the simulated ones
    (metro, train, rem), `both` (the two recorded layers — unchanged default), or `all`.
    """
    want = _resolve_layers(layer)

    db_layers = [l for l in want if l in DB_LAYERS]
    sim_layers = [l for l in want if l in SIM_LAYERS]

    payload = None
    db_empty = []
    if db_layers:
        with db_conn() as conn:
            try:
                payload = build_trips(
                    conn, db_layers,
                    start=start, end=end, hours=hours, route=route,
                    min_points=min_points, include_ground=include_ground,
                )
            except NoDataError as e:
                # Only fatal when nothing else was asked for; with simulated layers in
                # play an empty DB window is a partial result, not a failure.
                if not sim_layers:
                    raise HTTPException(
                        status_code=404,
                        detail=f"no data for this query (empty layers: {', '.join(e.empty)})",
                    )
                db_empty = e.empty
            except (psycopg2.DataError, psycopg2.errors.InvalidDatetimeFormat) as e:
                # Almost always an unparseable start/end string; Postgres does the
                # parsing, so this is where a bad timestamp surfaces.
                raise HTTPException(status_code=400, detail=f"bad query parameter: {e}")

    if sim_layers:
        payload = _add_simulated(payload, sim_layers, start, end, hours, route)

    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"no data for this query (empty layers: {', '.join(db_empty or want)})",
        )

    total_points = sum(len(t["path"]) for t in payload["trips"])
    if total_points > MAX_POINTS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"payload too large: {total_points:,} points exceeds the "
                f"{MAX_POINTS:,} budget. Narrow the window (start/end or hours), "
                f"pick a single layer, or raise MTL_MAX_POINTS."
            ),
        )

    # Returned as an explicit Response so FastAPI skips jsonable_encoder — recursing a
    # 178k-point payload field by field costs more than the query itself.
    return JSONResponse(content=payload)


@app.get("/api/range")
def api_range():
    """Report what data actually exists, so the client can pick a sane default window.

    Includes `overlap`, the window where every layer has data at once. The bus and
    aircraft tables currently intersect for only ~1.7 hours, so defaulting to the bus
    range alone would render an empty aircraft layer and look like a bug.
    """
    with db_conn() as conn:
        return get_range(conn)


@app.get("/api/health")
def api_health():
    """Cheap liveness probe that also reports whether Postgres is actually reachable."""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"status": "ok", "database": "up", "dsn": PG_DSN}
    except HTTPException as e:
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "database": "down",
                                     "detail": e.detail})


# ---------------------------------------------------------------- static

# `docs/index.html` falls back to fetching ../data/trips.json, which resolves to /data/
# when the page is served from /docs/ — so both directories are mounted, and the static
# workflow keeps working through this server too.
DATA_DIR = ROOT / "data"
if DATA_DIR.is_dir():
    app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

# Served as /docs/ because GitHub Pages publishes from main:/docs — one directory feeds
# both the local API mode and the static Pages deployment, so they cannot drift.
app.mount("/docs", StaticFiles(directory=str(ROOT / "docs"), html=True), name="docs")


@app.get("/")
def root():
    return RedirectResponse(url="/docs/")
