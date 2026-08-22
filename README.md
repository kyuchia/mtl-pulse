# sillage

**Montréal's transit network, drawn as the trails its vehicles leave behind.**

### → **[Live demo](https://kyuchia.github.io/sillage/)**

*sillage* (French: the wake a boat leaves on water) collects live vehicle positions from STM
buses and OpenSky aircraft into a PostGIS spatiotemporal database, simulates métro, commuter
rail and REM from static GTFS schedules, and replays any time window as an animated trail map
built with deck.gl and MapLibre.

The live demo runs on curated frozen scenes — no backend. Run it locally and it queries the
database directly.

---

## What it does

Two Python fetchers poll open APIs on a loop and write every position fix into PostgreSQL. A
FastAPI backend pulls any time window out of the database, merges it with schedule-simulated
modes on one shared timeline, and hands the browser the JSON that deck.gl's `TripsLayer`
expects. The page then plays that window back — scrub the timeline, change playback speed,
adjust how long the trails linger, toggle modes on and off.

Because everything is stored rather than streamed, you can replay 8am rush hour as many times
as you like, or jump to 3am to watch the night network.

Five modes render together: **bus** and **aircraft** are recorded; **métro**, **train** and
**REM** are interpolated from published schedules.

---

## Stack

| Layer | Choice |
|---|---|
| Ingestion | Python + `gtfs-realtime-bindings` + `opensky-api` |
| Storage | PostgreSQL 17 + PostGIS 3.6 |
| API | FastAPI + uvicorn |
| Simulation | GTFS static (`shapes.txt` + `stop_times.txt`) interpolation |
| Map | MapLibre GL JS |
| Layers | deck.gl (`TripsLayer`, `ScatterplotLayer`) |
| Basemap | CARTO Dark Matter |

MapLibre and CARTO were picked over Mapbox specifically because neither requires a credit card.

---

## Setup

### Prerequisites

- PostgreSQL 17 with PostGIS (`brew install postgresql@17 postgis`)
- Python 3.12
- An STM API key from the [STM developer portal](https://portail.developpeurs.stm.info/apihub)
- An OpenSky OAuth2 client (free) — anonymous access is capped at 400 requests/day, which is
  under two hours of polling

### Install

```bash
git clone https://github.com/kyuchia/sillage.git
cd sillage

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

createdb mtl_pulse
psql mtl_pulse < db/schema.sql
```

### Credentials

Secrets live in the macOS Keychain, never in a file or a launchd plist:

```bash
security add-generic-password -a "$USER" -s mtl-pulse-stm -T /usr/bin/security -U -w
./scripts/store_opensky_credentials.sh ~/Downloads/credentials.json
```

### Collect data

```bash
python fetchers/stm_fetcher.py       # buses, every 20s
python fetchers/opensky_fetcher.py   # aircraft, every 20s — second terminal
```

An hour of morning rush hour gives roughly 1,200 vehicles and 180,000 position fixes.

For unattended overnight runs, use the launchd agents instead — they survive terminal closure
and hold a sleep assertion tied to the fetcher's own lifetime:

```bash
sudo pmset -c sleep 0 disksleep 0            # AC only; on battery it still sleeps
cp launchd/ca.mtlpulse.*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ca.mtlpulse.stm.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ca.mtlpulse.opensky.plist
```

> **Do not use `caffeinate -i &`.** A standalone `caffeinate` belongs to the shell that
> launched it and dies with the terminal, leaving nothing asserting sleep prevention. That
> cost a full night of collection. The correct form is `caffeinate -i <command>`, which is
> how the launchd agents invoke the fetchers.

The fetchers report their own health: sleep/wake detection, stale-write and low-yield
warnings, per-run logs under `fetchers/logs/`, and an exit summary that refuses to call a
degraded run healthy.

### Static GTFS (for the simulated modes)

```bash
python scripts/fetch_gtfs.py     # archives STM / exo / REM feeds under gtfs/<agency>/<date>/
```

### Visualize

```bash
uvicorn api.main:app --reload --port 8000
```

Open **http://localhost:8000/docs/** — the API serves the page itself.

Any URL parameter is forwarded straight to the API:

```
/docs/?layer=all&start=2026-08-20%2008:00-04:00&end=2026-08-20%2009:00-04:00
```

`layer` accepts `bus`, `aircraft`, `metro`, `train`, `rem`, the aliases `both` and `all`, or a
comma-separated list such as `metro,rem`.

---

## The published demo

GitHub Pages serves `docs/` statically, so the live site has no database behind it. A handful
of hand-picked windows are frozen into `docs/scenes/` and chosen from a dropdown:

```bash
uvicorn api.main:app --port 8000 &    # scenes are baked from the local API
./scripts/bake_scenes.sh
```

Locally the app still prefers the live API; baked scenes are a fallback, and any fallback says
so in the panel rather than quietly showing different data.

---

## Data model

Two tables, both with a `GENERATED` PostGIS geography column derived from lat/lon, plus a GiST
index for spatial queries and a B-tree on `fetched_at` for time-range scans. See
`db/schema.sql`.

A sample spatial query — buses near Place-des-Arts in the last hour, by route:

```sql
SELECT route_id, COUNT(DISTINCT vehicle_id)
FROM vehicle_positions
WHERE fetched_at > NOW() - INTERVAL '1 hour'
  AND ST_DWithin(geom, ST_MakePoint(-73.5772, 45.5048)::geography, 500)
GROUP BY route_id ORDER BY 2 DESC;
```

---

## Notes on the rendering

`TripsLayer` wants each vehicle as one record with parallel `path` and `timestamps` arrays.
Timestamps are seconds relative to the window's start rather than Unix epoch values — epoch
milliseconds are large enough that float64 precision starts to bite when deck.gl interpolates.

`TripsLayer` draws the trail but not the vehicle itself, so the page also computes each
vehicle's current position by binary-searching its timestamp array and interpolating between
the two bracketing fixes. Those become a `ScatterplotLayer` of moving heads.

**Colour is assigned by mode, and brightness by speed** — never by route number, which turned
200+ routes into rainbow soup. Where an agency publishes a brand colour it is used verbatim:
the métro's four STM line colours and REM's `#73A400`. Bus, the densest layer, is deliberately
achromatic so it reads as texture rather than competing with the lines. Every pairing is gated
by `scripts/check_colours.js`, which compares full colour ramps by CIEDE2000 and fails below a
minimum perceptual distance.

---

## Data sources

| Mode | Source | Status |
|---|---|---|
| STM bus | GTFS-RT | Live |
| Aircraft | OpenSky (OAuth2) | Live |
| STM métro | Static GTFS interpolation | Simulated — no realtime GPS exists |
| REM | Static GTFS interpolation | Simulated — realtime feed is alerts-only |
| exo commuter rail | Static GTFS interpolation | Simulated — realtime needs an application |
| RTL / STL | GTFS-RT (application required) | Not implemented |

STM does not publish realtime métro positions — trains are underground and the signalling
system is closed. REM's published GTFS-Realtime feed contains service alerts only, with no
vehicle positions at all. Both are therefore simulated from the static schedule, interpolating
where each train should be at a given moment.

---

## Roadmap

- [x] Colour scheme reworked — by mode and speed, not per-route hashing
- [x] Aircraft layer
- [x] FastAPI backend so the page can query time windows directly
- [x] Métro, REM and commuter rail via static GTFS interpolation
- [x] Unattended overnight collection with health monitoring
- [x] Published demo on GitHub Pages
- [ ] 3D extrusion and hover tooltips
- [ ] exo commuter rail realtime (application submitted)
- [ ] Live mode (WebSocket) alongside replay

---

## License

MIT

Transit data © STM and © exo/ARTM, used under their open data terms. REM GTFS © CDPQ Infra
(CC-BY-4.0). Aircraft data © [The OpenSky Network](https://opensky-network.org). Basemap
© CARTO, © OpenStreetMap contributors.
