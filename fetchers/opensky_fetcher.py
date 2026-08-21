"""
OpenSky aircraft positions for the Montréal area → PostgreSQL.

Credentials come from the Keychain (see fetchers/secrets.py); anonymous access is
supported but announced as unfit for an overnight run, because it is:

    anonymous : 400 credits/day  → 2.2 h at 20s polling
    OAuth2    : 4000 credits/day → 22.2 h at 20s polling

The quota is spent per request regardless of whether aircraft are found, and once it
is gone every response is HTTP 429 that the client library reports as an empty sky.
Remaining credits are read from the X-Rate-Limit-Remaining header on every response
and checked against the planned run length, so exhaustion is predicted hours ahead
instead of discovered the next morning.
"""

import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
import requests
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from health import FetchHealth
from secrets import describe, get_secret

BBOX = {"min_lat": 45.20, "max_lat": 45.85, "min_lon": -74.20, "max_lon": -73.20}
PG_DSN = os.environ.get("PG_DSN", "dbname=mtl_pulse")
# 20s matches the bus fetcher: aircraft do not need finer sampling than buses, and a
# shared cadence keeps the replay timeline clean.
POLL_INTERVAL_SEC = int(os.environ.get("OPENSKY_INTERVAL_SEC", 20))
# How long an unattended run is expected to last; drives the quota projection.
PLAN_HOURS = float(os.environ.get("MTL_PLAN_HOURS", 12))

QUOTA = {"anonymous": 400, "oauth2": 4000}


def _attach_hook(api):
    """Capture status + remaining credits from every response.

    opensky_api swallows both: on 429 it logs at DEBUG and returns None, which is
    indistinguishable from "no aircraft in the box". A response hook on its session is
    the only way to see what actually happened without spending an extra request.
    """
    def _capture(resp, *a, **kw):
        api._mtl_last_status = resp.status_code
        api._mtl_retry_after = resp.headers.get("X-Rate-Limit-Retry-After-Seconds")
        rem = resp.headers.get("X-Rate-Limit-Remaining")
        if rem is not None:
            try:
                api._mtl_remaining = int(rem)
            except ValueError:
                pass
        return resp
    try:
        api._session.hooks.setdefault("response", []).append(_capture)
    except Exception as e:
        print(f"⚠️  could not attach status hook ({e}); 429s will be invisible",
              file=sys.stderr)


def reset_session(api):
    """Discard the HTTP session and build a fresh one.

    Called after a detected sleep. The library re-applies auth headers from its
    TokenManager on every request, so a new session inherits authentication; only the
    response hook has to be re-attached.
    """
    try:
        api._session.close()
    except Exception:
        pass
    api._session = requests.Session()
    _attach_hook(api)


def make_api():
    try:
        from opensky_api import OpenSkyApi
    except ImportError:
        print("❌ install first: pip install git+https://github.com/openskynetwork/"
              "opensky-api.git#subdirectory=python", file=sys.stderr)
        sys.exit(1)

    cid, cid_src = get_secret("OPENSKY_CLIENT_ID")
    cs, cs_src = get_secret("OPENSKY_CLIENT_SECRET")

    if cid and cs:
        api = OpenSkyApi(client_id=cid, client_secret=cs)
        api._mtl_mode = "oauth2"
        print("✅ OpenSky: OAuth2")
        print(f"   {describe('client id', cid, cid_src)}")
        print(f"   {describe('client secret', cs, cs_src)}")
    else:
        api = OpenSkyApi()
        api._mtl_mode = "anonymous"
        print("⚠️  OpenSky: ANONYMOUS (no credentials in env or Keychain)")

    api._mtl_last_status = None
    api._mtl_retry_after = None
    api._mtl_remaining = None
    _attach_hook(api)
    return api


def quota_banner(api, interval, plan_hours):
    """State up front whether this configuration can survive the planned run.

    A doomed overnight run should announce itself before it is walked away from, not
    after — last night's aircraft gap was a 400-credit quota spent in 1.7 h and only
    noticed eleven hours later.
    """
    mode = getattr(api, "_mtl_mode", "anonymous")
    credits = QUOTA.get(mode, 400)
    runtime_h = credits * interval / 3600
    needed = int(plan_hours * 3600 / interval)

    print(f"   quota: {mode} = {credits:,} credits/day")
    print(f"   at {interval}s polling that is {runtime_h:.1f} h of continuous collection")
    print(f"   planned run {plan_hours:.1f} h needs ~{needed:,} credits", end="")
    if credits >= needed:
        print(f"  ✅ headroom {credits - needed:,}")
        return True
    print(f"  ❌ SHORT BY {needed - credits:,}")
    print()
    print("   " + "!" * 66)
    print(f"   !!  THIS RUN WILL RUN OUT OF QUOTA AFTER ~{runtime_h:.1f} h")
    print(f"   !!  After that every poll returns HTTP 429, which the client library")
    print(f"   !!  reports as an empty sky — the data simply stops with no error.")
    if mode == "anonymous":
        print(f"   !!  Fix: store OAuth2 credentials in the Keychain (4,000/day).")
    else:
        print(f"   !!  Fix: raise the interval, or shorten the planned run.")
    print("   " + "!" * 66)
    print()
    return False


def save_states(conn, states_obj, fetched_at) -> int:
    if not states_obj or not states_obj.states:
        return 0

    rows = []
    for s in states_obj.states:
        rows.append((
            fetched_at,
            states_obj.time,
            s.icao24,
            (s.callsign or "").strip(),
            s.origin_country,
            s.longitude,
            s.latitude,
            s.baro_altitude,
            s.geo_altitude,
            bool(s.on_ground),
            s.velocity,
            s.true_track,
            s.vertical_rate,
        ))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO aircraft_positions
            (fetched_at, state_time, icao24, callsign, origin_country,
             longitude, latitude, baro_altitude, geo_altitude, on_ground,
             velocity, heading, vertical_rate)
            VALUES %s
        """, rows)
    conn.commit()
    return len(rows)


def main():
    api = make_api()
    try:
        conn = psycopg2.connect(PG_DSN)
    except psycopg2.OperationalError as e:
        print(f"❌ cannot connect to PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"✅ PostgreSQL connected")
    print(f"✅ Bounding box: {BBOX}")
    quota_banner(api, POLL_INTERVAL_SEC, PLAN_HOURS)
    print(f"✅ Polling every {POLL_INTERVAL_SEC}s. Ctrl+C to stop.\n")

    bbox = (BBOX["min_lat"], BBOX["max_lat"], BBOX["min_lon"], BBOX["max_lon"])
    health = FetchHealth("opensky", POLL_INTERVAL_SEC, unit="aircraft",
                         on_wake=lambda: reset_session(api))
    print(f"📝 log: {health.log_path}\n")

    total = 0
    reason = "stopped by user"
    quota_hit = False
    low_credit_warned = False
    plan_end = time.monotonic() + PLAN_HOURS * 3600

    try:
        while True:
            t0 = time.time()
            health.begin_poll()
            try:
                api._mtl_last_status = None
                states = api.get_states(bbox=bbox)
                status = api._mtl_last_status
                fetched_at = datetime.now(timezone.utc)

                if status == 429:
                    retry = api._mtl_retry_after
                    if not quota_hit:
                        quota_hit = True
                        hrs = f"{int(retry)/3600:.1f} h" if retry else "unknown"
                        health.shout(
                            "OPENSKY QUOTA EXHAUSTED (HTTP 429)",
                            [f"retry-after: {retry}s ({hrs})",
                             f"mode: {getattr(api, '_mtl_mode', '?')}"
                             f"  quota: {QUOTA.get(getattr(api,'_mtl_mode','anonymous')):,}/day",
                             "Every further poll returns 'no aircraft' until the quota resets.",
                             "This is NOT an empty sky and NOT a sleep event."],
                        )
                    health.record_failure("http_429_quota", f"retry_after={retry}")
                elif status is not None and status != 200:
                    health.record_failure(f"http_{status}", "non-200 from OpenSky")
                elif states is None:
                    health.record_failure("empty_response", "library returned None")
                else:
                    n = save_states(conn, states, fetched_at)
                    total += n
                    rem = api._mtl_remaining
                    health.record_success(n, note=f"credits_left={rem}" if rem is not None else "")
                    now = datetime.now().strftime("%H:%M:%S")
                    tail = f"  [{rem:,} credits left]" if rem is not None else ""
                    print(f"[{now}] +{n:3d} aircraft  (total {total:,}){tail}" if n
                          else f"[{now}]   0 aircraft{tail}")

                # Predict exhaustion instead of discovering it. Counting polls after the
                # fact is how last night's failure was found; this warns hours ahead.
                rem = api._mtl_remaining
                if rem is not None and not quota_hit:
                    left_sec = max(0.0, plan_end - time.monotonic())
                    needed = int(left_sec / POLL_INTERVAL_SEC)
                    if needed and rem < needed and not low_credit_warned:
                        low_credit_warned = True
                        runs_out_in = rem * POLL_INTERVAL_SEC / 3600
                        health.shout(
                            f"QUOTA WILL RUN OUT BEFORE THE PLANNED RUN ENDS",
                            [f"{rem:,} credits left, but {needed:,} needed for the remaining "
                             f"{left_sec/3600:.1f} h",
                             f"at {POLL_INTERVAL_SEC}s polling that is ~{runs_out_in:.1f} h from now "
                             f"(~{datetime.now().strftime('%H:%M')} + {runs_out_in:.1f} h)",
                             "Raise the interval now, or accept a short run."],
                        )
            except Exception as e:
                health.record_failure(type(e).__name__, str(e))
                conn.rollback()

            health.check_stale()
            if health.heartbeat_due():
                rem = api._mtl_remaining
                health.log("INFO", f"credits_remaining={rem}")
                health.heartbeat()
            elapsed = time.time() - t0
            time.sleep(max(0, POLL_INTERVAL_SEC - elapsed))
    except KeyboardInterrupt:
        reason = "stopped by user"
    except Exception as e:
        reason = f"crashed: {type(e).__name__}: {e}"
        health.log("ERROR", reason)
        raise
    finally:
        rem = getattr(api, "_mtl_remaining", None)
        if rem is not None:
            print(f"\n   OpenSky credits remaining at exit: {rem:,}")
            health.log("INFO", f"credits_remaining_at_exit={rem}")
        health.finish(reason)
        conn.close()


if __name__ == "__main__":
    main()
