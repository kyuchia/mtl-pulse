"""
STM Realtime Bus Position Fetcher (PostgreSQL 版)
--------------------------------------------------
跟 stm_fetcher.py 一樣, 但寫入 PostgreSQL 而不是 SQLite.

使用前:
1. PostgreSQL 17 + PostGIS 已啟用
2. 已建立 mtl_pulse database 和 vehicle_positions 表 (跑過 schema.sql)
3. export STM_API_KEY="你的key"
4. (可選) export PG_DSN="dbname=mtl_pulse"   ← 預設就是這個, 通常不用設

跑:
    python stm_fetcher_pg.py
"""

import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import execute_values
import requests
from google.transit import gtfs_realtime_pb2

# ---------- 設定 ----------
API_KEY = os.environ.get("STM_API_KEY", "PASTE_YOUR_KEY_HERE")
PG_DSN = os.environ.get("PG_DSN", "dbname=mtl_pulse")
VEHICLE_URL = "https://api.stm.info/pub/od/gtfs-rt/ic/v2/vehiclePositions"
POLL_INTERVAL_SEC = 20
# --------------------------


def fetch_vehicle_positions(api_key: str):
    try:
        resp = requests.get(VEHICLE_URL, headers={"apikey": api_key}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ⚠️  HTTP error: {e}", file=sys.stderr)
        return None

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(resp.content)
    except Exception as e:
        print(f"  ⚠️  Parse error: {e}", file=sys.stderr)
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
    if API_KEY == "PASTE_YOUR_KEY_HERE":
        print("❌ 請先設定 STM_API_KEY 環境變數")
        sys.exit(1)

    try:
        conn = psycopg2.connect(PG_DSN)
    except psycopg2.OperationalError as e:
        print(f"❌ 無法連線 PostgreSQL: {e}", file=sys.stderr)
        print(f"   檢查: brew services list   (postgresql@17 應該是 started)")
        sys.exit(1)

    print(f"✅ PostgreSQL 連線成功 (DSN: {PG_DSN})")
    print(f"✅ 每 {POLL_INTERVAL_SEC} 秒抓一次. Ctrl+C 停止.\n")

    total = 0
    try:
        while True:
            t0 = time.time()
            feed = fetch_vehicle_positions(API_KEY)
            if feed is not None:
                try:
                    n = save_feed(conn, feed)
                    total += n
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"[{now}] +{n:4d} vehicles  (累計 {total:,})")
                except Exception as e:
                    print(f"  ⚠️  DB error: {e}", file=sys.stderr)
                    conn.rollback()
            elapsed = time.time() - t0
            time.sleep(max(0, POLL_INTERVAL_SEC - elapsed))
    except KeyboardInterrupt:
        print(f"\n🛑 停止. 本次共寫入 {total:,} 筆.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
