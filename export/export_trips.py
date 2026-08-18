"""
Export PostgreSQL positions as deck.gl TripsLayer JSON (多運具版).

用法:
    python export/export_trips.py --start "2026-04-15 08:00-04:00" --end "2026-04-15 09:00-04:00"
    python export/export_trips.py --hours 1
    python export/export_trips.py --layer bus            # 只要公車
    python export/export_trips.py --layer aircraft       # 只要飛機
    python export/export_trips.py --route 747            # 只看機場線 (僅 bus)

預設 --layer both: 公車 + 飛機一起 export 到同一個檔，共用同一條時間軸。

輸出格式:
{
  "start_time": "2026-04-15T08:00:14-04:00",
  "end_time":   "...",
  "duration_sec": 3581,
  "modes": ["bus", "aircraft"],
  "trips": [
    {
      "mode": "bus",              # 前端據此決定顏色 (見 index.html 的 MODES)
      "id": "39221",              # vehicle_id 或 icao24
      "label": "747",             # route_id 或 callsign
      "path": [[lon, lat], ...],
      "timestamps": [0, 23, 47, ...]    # 相對 start_time 的秒數
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


def build_where(args, layer):
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

    if layer == "bus" and args.route:
        where.append("route_id = %s")
        params.append(args.route)

    # 停在停機坪的飛機沒有軌跡可言, 預設濾掉
    if layer == "aircraft" and not args.include_ground:
        where.append("(on_ground IS NULL OR on_ground = false)")

    return where, params


def fetch_layer(conn, layer, args):
    cfg = LAYERS[layer]
    where, params = build_where(args, layer)

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", choices=["bus", "aircraft", "both"], default="both")
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--route", type=str, default=None,
                        help="只看指定路線 (僅 bus 有效)")
    parser.add_argument("--min-points", type=int, default=3,
                        help="少於這個點數的軌跡會被丟掉 (預設 3)")
    parser.add_argument("--include-ground", action="store_true",
                        help="飛機層保留 on_ground=true 的點 (預設濾掉)")
    parser.add_argument("--out", type=str, default="data/trips.json")
    args = parser.parse_args()

    layers = ["bus", "aircraft"] if args.layer == "both" else [args.layer]

    conn = psycopg2.connect(PG_DSN)
    try:
        raw = {}
        for layer in layers:
            rows = fetch_layer(conn, layer, args)
            if rows:
                raw[layer] = rows
            else:
                print(f"⚠️  {layer}: 這個時間區間沒有資料, 跳過")
    finally:
        conn.close()

    if not raw:
        print("❌ 兩層都沒資料")
        sys.exit(1)

    # 所有層共用同一條時間軸, 所以 start_time 要跨層取
    all_times = [r["fetched_at"] for rows in raw.values() for r in rows]
    start_time = min(all_times)
    end_time = max(all_times)
    duration_sec = (end_time - start_time).total_seconds()

    trips = []
    summary = {}
    for layer, rows in raw.items():
        by_id = defaultdict(list)
        for r in rows:
            by_id[r["uid"]].append(r)

        kept = skipped = 0
        for uid, points in by_id.items():
            if len(points) < args.min_points:
                skipped += 1
                continue
            trips.append({
                "mode": layer,
                "id": uid,
                "label": (points[0]["label"] or "").strip() or None,
                "path": [[p["longitude"], p["latitude"]] for p in points],
                "timestamps": [(p["fetched_at"] - start_time).total_seconds() for p in points],
            })
            kept += 1
        summary[layer] = (kept, skipped, sum(len(p) for p in by_id.values()))

    output = {
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_sec": duration_sec,
        "modes": sorted(raw.keys()),
        "trips": trips,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    size_mb = os.path.getsize(args.out) / 1024 / 1024
    print(f"✅ {args.out}  ({size_mb:.1f} MB)")
    print(f"   時間區間: {start_time}  →  {end_time}")
    print(f"   長度: {duration_sec:.0f} 秒 ({duration_sec/60:.1f} 分鐘)")
    for layer, (kept, skipped, pts) in summary.items():
        print(f"   {layer:9s} 軌跡 {kept:,}  (跳過 {skipped} 條太短)  點數 {pts:,}")


if __name__ == "__main__":
    main()
