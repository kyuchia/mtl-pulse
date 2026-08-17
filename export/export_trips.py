"""
Export PostgreSQL bus positions as deck.gl TripsLayer JSON.

用法:
    python export_trips_json.py --hours 1
    python export_trips_json.py --start "2026-04-15 08:00" --end "2026-04-15 09:00"
    python export_trips_json.py --hours 0.5 --route 747 --out trips_747.json

輸出格式 (deck.gl TripsLayer):
{
  "start_time": "2026-04-15T08:00:00+00:00",   # ISO, 給前端顯示用
  "duration_sec": 3600,                          # 整段時間長度
  "trips": [
    {
      "vehicle_id": "...",
      "route_id": "...",
      "path": [[lon, lat], ...],
      "timestamps": [0, 23, 47, ...]             # 相對 start_time 的秒數
    },
    ...
  ]
}
"""

import argparse
import json
import os
import sys
from collections import defaultdict

import psycopg2
from psycopg2.extras import RealDictCursor

PG_DSN = os.environ.get("PG_DSN", "dbname=mtl_pulse")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--route", type=str, default=None)
    parser.add_argument("--min-points", type=int, default=3,
                        help="少於這個點數的軌跡會被丟掉 (預設 3)")
    parser.add_argument("--out", type=str, default="data/trips.json")
    args = parser.parse_args()

    where = ["latitude IS NOT NULL", "longitude IS NOT NULL"]
    params = []

    if args.hours is not None:
        where.append("fetched_at >= NOW() - INTERVAL %s")
        params.append(f"{args.hours} hours")
    if args.start:
        where.append("fetched_at >= %s")
        params.append(args.start)
    if args.end:
        where.append("fetched_at <= %s")
        params.append(args.end)
    if args.route:
        where.append("route_id = %s")
        params.append(args.route)

    query = f"""
        SELECT fetched_at, vehicle_id, route_id, latitude, longitude
        FROM vehicle_positions
        WHERE {' AND '.join(where)}
        ORDER BY vehicle_id, fetched_at
    """

    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("⚠️  沒有資料")
        sys.exit(1)

    # 找全段的最早時間, 用來算相對秒數
    start_time = min(r["fetched_at"] for r in rows)
    end_time = max(r["fetched_at"] for r in rows)
    duration_sec = (end_time - start_time).total_seconds()

    # 依 vehicle_id 分組
    by_vehicle = defaultdict(list)
    for r in rows:
        by_vehicle[r["vehicle_id"]].append(r)

    trips = []
    skipped = 0
    for vid, points in by_vehicle.items():
        if len(points) < args.min_points:
            skipped += 1
            continue
        # 已經是按時間排序的 (SQL ORDER BY)
        path = [[p["longitude"], p["latitude"]] for p in points]
        timestamps = [(p["fetched_at"] - start_time).total_seconds() for p in points]
        trips.append({
            "vehicle_id": vid,
            "route_id": points[0]["route_id"],
            "path": path,
            "timestamps": timestamps,
        })

    output = {
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_sec": duration_sec,
        "trips": trips,
    }

    with open(args.out, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    size_mb = os.path.getsize(args.out) / 1024 / 1024
    print(f"✅ {args.out}  ({size_mb:.1f} MB)")
    print(f"   時間區間: {start_time}  →  {end_time}")
    print(f"   長度: {duration_sec:.0f} 秒 ({duration_sec/60:.1f} 分鐘)")
    print(f"   軌跡數: {len(trips):,}  (跳過 {skipped} 條太短的)")
    print(f"   總點數: {sum(len(t['path']) for t in trips):,}")


if __name__ == "__main__":
    main()
