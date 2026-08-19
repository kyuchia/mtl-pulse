"""
Download and archive static GTFS feeds, one dated directory per download.

    python scripts/fetch_gtfs.py                 # all agencies
    python scripts/fetch_gtfs.py --agency rem    # just one
    python scripts/fetch_gtfs.py --list          # show what is already archived

Layout:

    gtfs/<agency>/<YYYY-MM-DD>/gtfs.zip
    gtfs/<agency>/<YYYY-MM-DD>/meta.json

Why archive by date instead of overwriting a single file: a GTFS feed describes the
schedule in effect during a bounded window (its calendar.txt span). Replay data recorded
in April can only be matched honestly against the feed that was actually in effect in
April. We cannot retrofit that for the existing 2026-04 recording — today's feeds start
in May at the earliest — but from now on every archived feed keeps its own span, so
future replays can resolve against the right one and the service-date substitution in
simulate/gtfs_sim.py becomes unnecessary.

An existing dated directory is never overwritten; re-running on the same day is a no-op
unless --force is passed, which writes a `.N` suffixed sibling rather than clobbering.
"""

import argparse
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone

AGENCIES = {
    "stm": {
        "url": "https://www.stm.info/sites/default/files/gtfs/gtfs_stm.zip",
        "note": "STM — buses and the four metro lines (route_type 1 = metro)",
    },
    "exo_trains": {
        "url": "https://www.rtm.quebec/xdata/trains/google_transit.zip",
        "note": "exo — commuter rail (CC-BY-4.0)",
    },
    "rem": {
        "url": "https://gtfs.gpmmom.ca/gtfs/gtfs.zip",
        "note": "REM — light metro, lines A1/A3/A4 (CC-BY-4.0)",
    },
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GTFS_DIR = os.path.join(ROOT, "gtfs")

UA = "mtl-pulse/1.0 (+https://github.com/kyuchia/mtl-pulse)"


def calendar_span(zf):
    """Return (min start_date, max end_date) across calendar.txt and calendar_dates.txt.

    Both files are optional in GTFS; a feed may express service entirely through
    calendar_dates.txt, so the span is taken across whichever exist.
    """
    starts, ends = [], []
    names = set(zf.namelist())

    if "calendar.txt" in names:
        import csv
        with zf.open("calendar.txt") as fh:
            for row in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")):
                if row.get("start_date"):
                    starts.append(row["start_date"].strip())
                if row.get("end_date"):
                    ends.append(row["end_date"].strip())

    if "calendar_dates.txt" in names:
        import csv
        with zf.open("calendar_dates.txt") as fh:
            for row in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")):
                d = (row.get("date") or "").strip()
                if d:
                    starts.append(d)
                    ends.append(d)

    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def feed_stats(zf):
    """Cheap row counts for the files the simulator cares about."""
    import csv
    out = {}
    for name in ("routes.txt", "trips.txt", "shapes.txt", "stop_times.txt", "stops.txt"):
        if name not in zf.namelist():
            out[name] = None
            continue
        with zf.open(name) as fh:
            # subtract the header
            out[name] = max(0, sum(1 for _ in io.TextIOWrapper(fh, "utf-8-sig")) - 1)
    return out


def fetch(agency, force=False):
    cfg = AGENCIES[agency]
    url = cfg["url"]
    day = datetime.now().strftime("%Y-%m-%d")
    dest_dir = os.path.join(GTFS_DIR, agency, day)

    if os.path.isdir(dest_dir) and not force:
        print(f"  ↩︎  {agency}: {day} already archived, skipping (use --force for a new copy)")
        return dest_dir, False

    if os.path.isdir(dest_dir) and force:
        n = 1
        while os.path.isdir(f"{dest_dir}.{n}"):
            n += 1
        dest_dir = f"{dest_dir}.{n}"

    print(f"  ↓  {agency}: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        blob = resp.read()

    # Validate before writing anything — a truncated or HTML error page must not land in
    # the archive looking like a real feed.
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        bad = zf.testzip()
        if bad:
            raise zipfile.BadZipFile(f"corrupt member: {bad}")
    except zipfile.BadZipFile as e:
        print(f"  ❌ {agency}: not a valid zip ({e}); {len(blob):,} bytes discarded",
              file=sys.stderr)
        return None, False

    required = {"trips.txt", "stop_times.txt", "routes.txt"}
    missing = required - set(zf.namelist())
    if missing:
        print(f"  ❌ {agency}: missing {', '.join(sorted(missing))}; discarded", file=sys.stderr)
        return None, False

    start, end = calendar_span(zf)
    meta = {
        "agency": agency,
        "source_url": url,
        "note": cfg["note"],
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size_bytes": len(blob),
        "calendar_start": start,
        "calendar_end": end,
        "has_shapes": "shapes.txt" in zf.namelist(),
        "files": sorted(zf.namelist()),
        "row_counts": feed_stats(zf),
    }

    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, "gtfs.zip"), "wb") as f:
        f.write(blob)
    with open(os.path.join(dest_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    span = f"{start} → {end}" if start else "no calendar dates"
    print(f"  ✅ {agency}: {len(blob)/1024:.0f} KB  span {span}  "
          f"shapes={'yes' if meta['has_shapes'] else 'NO'}  → {os.path.relpath(dest_dir, ROOT)}")
    return dest_dir, True


def latest_dir(agency):
    """Most recent archived directory for an agency, or None."""
    base = os.path.join(GTFS_DIR, agency)
    if not os.path.isdir(base):
        return None
    days = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    return os.path.join(base, days[-1]) if days else None


def list_archive():
    if not os.path.isdir(GTFS_DIR):
        print("nothing archived yet — run without --list to download")
        return
    for agency in sorted(AGENCIES):
        base = os.path.join(GTFS_DIR, agency)
        if not os.path.isdir(base):
            print(f"{agency:12s} —")
            continue
        for day in sorted(os.listdir(base)):
            mpath = os.path.join(base, day, "meta.json")
            if not os.path.isfile(mpath):
                continue
            m = json.load(open(mpath))
            print(f"{agency:12s} {day}  {m['size_bytes']/1024:7.0f} KB  "
                  f"span {m['calendar_start']} → {m['calendar_end']}  "
                  f"trips={m['row_counts'].get('trips.txt')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agency", choices=sorted(AGENCIES), default=None,
                    help="only this agency (default: all)")
    ap.add_argument("--force", action="store_true",
                    help="download again even if today is already archived")
    ap.add_argument("--list", action="store_true", help="show what is already archived")
    args = ap.parse_args()

    if args.list:
        list_archive()
        return

    todo = [args.agency] if args.agency else sorted(AGENCIES)
    ok = 0
    for agency in todo:
        try:
            path, fresh = fetch(agency, force=args.force)
            if path:
                ok += 1
        except Exception as e:
            print(f"  ❌ {agency}: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"\n{ok}/{len(todo)} agencies available under gtfs/")


if __name__ == "__main__":
    main()
