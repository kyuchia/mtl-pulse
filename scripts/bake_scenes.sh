#!/usr/bin/env bash
# ============================================================
# Bake curated scenes for GitHub Pages.
#
# The Pages site is static: no Postgres, no FastAPI. This script pulls a few
# hand-picked windows out of the LOCAL API and freezes them as JSON under
# docs/scenes/, which IS committed to git (unlike data/, which is ignored).
#
# Requires the API running first:
#     uvicorn api.main:app --port 8000
#
# Then:
#     ./scripts/bake_scenes.sh
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p docs/scenes

bake () {
  local slug="$1"; shift
  printf '→ %-22s' "$slug"
  if curl -sfG "http://localhost:8000/api/trips" "$@" -o "docs/scenes/$slug.json"; then
    echo "ok"
  else
    echo "FAILED (uvicorn not running, empty window, or 413 — try a narrower window)"
    rm -f "docs/scenes/$slug.json"
  fi
}

# August windows sit inside the reference overnight run (2026-08-19 16:00 →
# 08-20 12:00, no gaps). The April window is the only span where the 2026-04
# recording has bus AND aircraft together (v7 caveat), hence layer=both.
bake morning-peak --data-urlencode "layer=all" \
  --data-urlencode "start=2026-08-20 08:00-04:00" \
  --data-urlencode "end=2026-08-20 09:00-04:00"

bake evening-peak --data-urlencode "layer=all" \
  --data-urlencode "start=2026-08-19 17:00-04:00" \
  --data-urlencode "end=2026-08-19 18:00-04:00"

bake night-3am --data-urlencode "layer=all" \
  --data-urlencode "start=2026-08-20 03:00-04:00" \
  --data-urlencode "end=2026-08-20 04:00-04:00"

bake april-first-night --data-urlencode "layer=both" \
  --data-urlencode "start=2026-04-14 23:15-04:00" \
  --data-urlencode "end=2026-04-15 00:57-04:00"

# Shrink and index: round coordinates to 5 decimals (~1.1 m, well under GPS
# error), timestamps to 0.1 s (sampling is 20 s), then write the manifest the
# frontend's scene picker reads.
python3 - << 'PY'
import json, os

LABELS = {
    "morning-peak":      "Morning peak - all five modes",
    "evening-peak":      "Evening peak - the crowded hour",
    "night-3am":         "03:00 - night buses",
    "april-first-night": "April 2026 - the first recorded night",
}
ORDER = ["morning-peak", "evening-peak", "night-3am", "april-first-night"]

scenes = []
for slug in ORDER:
    p = f"docs/scenes/{slug}.json"
    if not os.path.exists(p):
        continue
    with open(p) as f:
        d = json.load(f)
    for t in d["trips"]:
        t["path"] = [[round(x, 5), round(y, 5)] for x, y in t["path"]]
        t["timestamps"] = [round(ts, 1) for ts in t["timestamps"]]
    with open(p, "w") as f:
        json.dump(d, f, separators=(",", ":"))
    scenes.append({
        "slug": slug,
        "file": f"{slug}.json",
        "label": LABELS.get(slug, slug),
        "start": d["start_time"],
        "end": d["end_time"],
        "modes": d["modes"],
        "points": sum(len(t["path"]) for t in d["trips"]),
        "mb": round(os.path.getsize(p) / 1048576, 1),
    })

with open("docs/scenes/manifest.json", "w") as f:
    json.dump({"scenes": scenes}, f, indent=1)

print()
for s in scenes:
    print(f"   {s['slug']:22s} {s['mb']:5.1f} MB  {s['points']:>8,} pts  {','.join(s['modes'])}")
print("\n✓ docs/scenes/manifest.json written")
print("  Now: git add docs/scenes && git commit && git push")
PY
