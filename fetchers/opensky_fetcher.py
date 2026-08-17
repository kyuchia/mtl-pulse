"""
OpenSky 蒙特婁地區飛機位置抓取 (PostgreSQL 版)
"""

import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import execute_values

BBOX = {"min_lat": 45.20, "max_lat": 45.85, "min_lon": -74.20, "max_lon": -73.20}
PG_DSN = os.environ.get("PG_DSN", "dbname=mtl_pulse")
POLL_INTERVAL_SEC = 15


def make_api():
    try:
        from opensky_api import OpenSkyApi
    except ImportError:
        print("❌ 請先安裝: pip install git+https://github.com/openskynetwork/opensky-api.git#subdirectory=python", file=sys.stderr)
        sys.exit(1)

    cid = os.environ.get("OPENSKY_CLIENT_ID")
    cs = os.environ.get("OPENSKY_CLIENT_SECRET")
    if cid and cs:
        print("✅ 認證模式 (OAuth2)")
        return OpenSkyApi(client_id=cid, client_secret=cs)
    print("⚠️  匿名模式")
    return OpenSkyApi()


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
        print(f"❌ 無法連線 PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"✅ PostgreSQL 連線成功")
    print(f"✅ Bounding box: {BBOX}")
    print(f"✅ 每 {POLL_INTERVAL_SEC} 秒抓一次. Ctrl+C 停止.\n")

    bbox = (BBOX["min_lat"], BBOX["max_lat"], BBOX["min_lon"], BBOX["max_lon"])
    total = 0

    try:
        while True:
            t0 = time.time()
            try:
                states = api.get_states(bbox=bbox)
                fetched_at = datetime.now(timezone.utc)
                n = save_states(conn, states, fetched_at)
                total += n
                now = datetime.now().strftime("%H:%M:%S")
                if n > 0:
                    print(f"[{now}] +{n:3d} aircraft  (累計 {total:,})")
                else:
                    print(f"[{now}]   0 aircraft")
            except Exception as e:
                print(f"  ⚠️  Error: {e}", file=sys.stderr)
                conn.rollback()

            elapsed = time.time() - t0
            time.sleep(max(0, POLL_INTERVAL_SEC - elapsed))
    except KeyboardInterrupt:
        print(f"\n🛑 停止. 本次共寫入 {total:,} 筆.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
