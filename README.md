# MTL Pulse

Real-time visualization of Montréal's public transit — a Montréal take on the Taiwan Pulse project.

Collects live vehicle positions from STM (buses) and OpenSky (aircraft) into a PostGIS spatiotemporal database, then replays any time window as an animated trail map built with deck.gl and MapLibre.


---

## What it does

Two Python fetchers poll open APIs on a loop and write every position fix into PostgreSQL. A separate export step pulls any time window out of the database and shapes it into the JSON that deck.gl's `TripsLayer` expects. The web page then plays that window back — scrub the timeline, change playback speed, adjust how long the trails linger.

Because everything is stored rather than streamed, you can replay 8am rush hour as many times as you like, or jump to 3am to watch the night network.

---

## Stack

| Layer | Choice |
|---|---|
| Ingestion | Python + `gtfs-realtime-bindings` + `opensky-api` |
| Storage | PostgreSQL 17 + PostGIS 3.6 |
| Export | psycopg2 → JSON |
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

### Install

```bash
git clone https://github.com/kyuchia/mtl-pulse.git
cd mtl-pulse

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

createdb mtl_pulse
psql mtl_pulse < db/schema.sql
```

### Collect data

```bash
export STM_API_KEY="your-key-here"
python fetchers/stm_fetcher.py       # buses, every 20s
python fetchers/opensky_fetcher.py   # aircraft, every 15s — run in a second terminal
```

Leave these running for as long as you want to capture. An hour of morning rush hour gives roughly 1,200 vehicles and 180,000 position fixes.

On macOS, `caffeinate -i &` keeps the machine awake while collecting.

### Visualize

```bash
# Export a time window (note the -04:00 offset — data is stored in EDT)
python export/export_trips.py --start "2026-04-15 08:00-04:00" --end "2026-04-15 09:00-04:00"

# Serve the page (fetch() won't work over file://)
# serve from the project root so the page can reach data/
python -m http.server 8000
```

Open http://localhost:8000/web/

---

## Export options

```bash
python export/export_trips.py --hours 1                    # last hour
python export/export_trips.py --route 747                  # single route
python export/export_trips.py --min-points 5               # drop short fragments
python export/export_trips.py --out data/custom.json       # custom output path
```

---

## Data model

Two tables, both with a `GENERATED` PostGIS geography column derived from lat/lon, plus a GiST index for spatial queries and a B-tree on `fetched_at` for time-range scans. See `db/schema.sql`.

A sample spatial query — buses near Place-des-Arts in the last hour, by route:

```sql
SELECT route_id, COUNT(DISTINCT vehicle_id)
FROM vehicle_positions
WHERE fetched_at > NOW() - INTERVAL '1 hour'
  AND ST_DWithin(geom, ST_MakePoint(-73.5772, 45.5048)::geography, 500)
GROUP BY route_id ORDER BY 2 DESC;
```

---

## Notes on the trail rendering

`TripsLayer` wants each vehicle as one record with parallel `path` and `timestamps` arrays. Timestamps are stored as seconds relative to the window's start rather than Unix epoch values — epoch milliseconds are large enough that float64 precision starts to bite when deck.gl interpolates.

`TripsLayer` draws the trail but not the vehicle itself, so the page also computes each vehicle's current position by binary-searching its timestamp array and interpolating between the two bracketing fixes. Those go into a `ScatterplotLayer` as the moving heads.

---

## Data sources

| Mode | Source | Status |
|---|---|---|
| STM bus | GTFS-RT | Live |
| Aircraft | OpenSky | Live |
| STM métro | — | No realtime GPS (underground); planned via static GTFS interpolation |
| exo commuter rail | GTFS-RT (application required) | Planned |
| RTL / STL | GTFS-RT (application required) | Planned |
| REM | Via exo/ARTM | Not yet public |

STM does not publish realtime métro positions — trains are underground and the signalling system is closed. The plan is to simulate them from the static GTFS schedule (`shapes.txt` + `stop_times.txt`), interpolating where each train should be at a given moment.

---

## Roadmap

- [ ] Rework the color scheme — per-route hashing produces visual noise at 200+ routes
- [ ] Add the aircraft layer to the web view
- [ ] FastAPI backend so the page can query time windows directly
- [ ] Métro layer via static GTFS interpolation
- [ ] exo commuter rail
- [ ] Live mode (polling or WebSocket) alongside replay

---

## License

MIT

Transit data © STM, used under their open data terms. Aircraft data © [The OpenSky Network](https://opensky-network.org). Basemap © CARTO, © OpenStreetMap contributors.
