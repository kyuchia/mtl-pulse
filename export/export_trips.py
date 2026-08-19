"""
Export PostgreSQL positions as deck.gl TripsLayer JSON (multi-mode).

Usage:
    python export/export_trips.py --start "2026-04-15 08:00-04:00" --end "2026-04-15 09:00-04:00"
    python export/export_trips.py --hours 1
    python export/export_trips.py --layer bus            # buses only
    python export/export_trips.py --layer aircraft       # aircraft only
    python export/export_trips.py --route 747            # airport line only (bus layer)

--layer defaults to "both": buses and aircraft are exported into one file, sharing a
single timeline.

The query and trip-assembly logic lives in export/trips_query.py, shared by this CLI and
api/main.py, so the JSON both sides produce is identical by construction. See that
module's docstring for the output format.
"""

import argparse
import json
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trips_query import NoDataError, VALID_LAYERS, build_trips_with_summary

PG_DSN = os.environ.get("PG_DSN", "dbname=mtl_pulse")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", choices=["bus", "aircraft", "both"], default="both")
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--route", type=str, default=None,
                        help="only this route (bus layer only)")
    parser.add_argument("--min-points", type=int, default=3,
                        help="drop traces with fewer points than this (default 3)")
    parser.add_argument("--include-ground", action="store_true",
                        help="keep on_ground=true aircraft points (filtered out by default)")
    parser.add_argument("--out", type=str, default="data/trips.json")
    args = parser.parse_args()

    layers = list(VALID_LAYERS) if args.layer == "both" else [args.layer]

    conn = psycopg2.connect(PG_DSN)
    try:
        output, summary = build_trips_with_summary(
            conn, layers,
            start=args.start,
            end=args.end,
            hours=args.hours,
            route=args.route,
            min_points=args.min_points,
            include_ground=args.include_ground,
        )
    except NoDataError as e:
        for layer in e.empty:
            print(f"⚠️  {layer}: no data in this time range, skipping")
        print("❌ no data in any layer")
        sys.exit(1)
    finally:
        conn.close()

    for layer in summary["empty"]:
        print(f"⚠️  {layer}: no data in this time range, skipping")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    start_time = summary["start_time"]
    end_time = summary["end_time"]
    duration_sec = summary["duration_sec"]

    size_mb = os.path.getsize(args.out) / 1024 / 1024
    print(f"✅ {args.out}  ({size_mb:.1f} MB)")
    print(f"   Range:  {start_time}  →  {end_time}")
    print(f"   Length: {duration_sec:.0f} sec ({duration_sec/60:.1f} min)")
    for layer, (kept, skipped, pts) in summary["per_layer"].items():
        print(f"   {layer:9s} trips {kept:,}  (skipped {skipped} too short)  points {pts:,}")


if __name__ == "__main__":
    main()
