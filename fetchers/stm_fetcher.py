"""
STM realtime bus positions → PostgreSQL.

Requires:
  1. PostgreSQL 17 + PostGIS
  2. the mtl_pulse database and vehicle_positions table (see db/schema.sql)
  3. an STM API key, read from the environment or the macOS Keychain:
         security add-generic-password -a "$USER" -s mtl-pulse-stm -T /usr/bin/security -w
     (env STM_API_KEY still wins if set)
  4. optional: export PG_DSN="dbname=mtl_pulse"   <- already the default

Run:
    python fetchers/stm_fetcher.py
"""

import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import execute_values
import requests
from google.transit import gtfs_realtime_pb2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from health import FetchHealth
from secrets import describe, get_secret

# ---------- config ----------
PG_DSN = os.environ.get("PG_DSN", "dbname=mtl_pulse")
VEHICLE_URL = "https://api.stm.info/pub/od/gtfs-rt/ic/v2/vehiclePositions"
POLL_INTERVAL_SEC = 20
# --------------------------


def fetch_vehicle_positions(api_key: str, health=None):
    """Fetch one GTFS-RT frame. Failures are classified, not just printed, so the exit
    summary and the log can say WHICH kind of failure dominated a bad run."""
    try:
        resp = requests.get(VEHICLE_URL, headers={"apikey": api_key}, timeout=10)
    except requests.Timeout as e:
        if health: health.record_failure("http_timeout", str(e))
        return None
    except requests.ConnectionError as e:
        # What a post-sleep dead socket looks like.
        if health: health.record_failure("http_connection", str(e))
        return None
    except requests.RequestException as e:
        if health: health.record_failure("http_other", str(e))
        return None

    if resp.status_code != 200:
        kind = "http_429_rate_limited" if resp.status_code == 429 else f"http_{resp.status_code}"
        if health: health.record_failure(kind, f"{resp.status_code} {resp.reason}")
        return None

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(resp.content)
    except Exception as e:
        if health: health.record_failure("parse", str(e))
        return None
    return feed


def save_feed(conn, feed) -> int:
    fetched_at = datetime.now(timezone.utc)
    feed_ts = feed.header.timestamp
    rows = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        rows.append((
            fetched_at,
            feed_ts,
            v.vehicle.id if v.HasField("vehicle") else None,
            v.trip.trip_id if v.HasField("trip") else None,
            v.trip.route_id if v.HasField("trip") else None,
            v.position.latitude if v.HasField("position") else None,
            v.position.longitude if v.HasField("position") else None,
            v.position.bearing if v.HasField("position") and v.position.HasField("bearing") else None,
            v.position.speed if v.HasField("position") and v.position.HasField("speed") else None,
            v.stop_id if v.HasField("stop_id") else None,
            v.current_status if v.HasField("current_status") else None,
            v.occupancy_status if v.HasField("occupancy_status") else None,
        ))

    if rows:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO vehicle_positions
                (fetched_at, feed_timestamp, vehicle_id, trip_id, route_id,
                 latitude, longitude, bearing, speed, stop_id, current_status, occupancy)
                VALUES %s
            """, rows)
        conn.commit()
    return len(rows)


def main():
    # Environment first, then the Keychain — so the key lives in neither the repo nor
    # the launchd plist. See fetchers/secrets.py.
    api_key, key_src = get_secret(
        "STM_API_KEY", required=True,
        hint="Store it once with:\n"
             "     security add-generic-password -a \"$USER\" -s mtl-pulse-stm "
             "-T /usr/bin/security -w")

    try:
        conn = psycopg2.connect(PG_DSN)
    except psycopg2.OperationalError as e:
        print(f"❌ cannot connect to PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"✅ PostgreSQL connected (DSN: {PG_DSN})")
    print(f"   {describe('STM api key', api_key, key_src)}")
    print(f"✅ Polling every {POLL_INTERVAL_SEC}s. Ctrl+C to stop.\n")

    # No on_wake handler: this fetcher calls requests.get() per poll, which builds and
    # closes its own connection each time, so there is no long-lived socket to go stale
    # across a sleep. The OpenSky fetcher does hold a session and does reset it.
    health = FetchHealth("stm", POLL_INTERVAL_SEC, unit="vehicles")
    print(f"📝 log: {health.log_path}\n")

    total = 0
    reason = "stopped by user"
    try:
        while True:
            t0 = time.time()
            health.begin_poll()
            feed = fetch_vehicle_positions(api_key, health)
            if feed is not None:
                try:
                    n = save_feed(conn, feed)
                    total += n
                    health.record_success(n)
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"[{now}] +{n:4d} vehicles  (total {total:,})")
                except Exception as e:
                    health.record_failure("db", str(e))
                    conn.rollback()
            health.check_stale()
            if health.heartbeat_due():
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
        health.finish(reason)
        conn.close()


if __name__ == "__main__":
    main()
