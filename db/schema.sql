-- ============================================================
-- MTL Pulse Schema (PostgreSQL + PostGIS)
-- ============================================================
-- 用法:  psql mtl_pulse < schema.sql
--
-- 設計重點:
--   - vehicle_positions: STM 公車即時位置
--   - aircraft_positions: OpenSky 飛機即時位置
--   - 兩張表都有 PostGIS geometry 欄位 (geom), 自動由 lat/lon 計算
--   - 在 fetched_at 上有 B-tree 索引 (時間範圍查詢)
--   - 在 geom 上有 GiST 空間索引 (地理範圍查詢)
-- ============================================================

-- 確保 PostGIS 已啟用 (重複執行不會出錯)
CREATE EXTENSION IF NOT EXISTS postgis;

-- ------------------------------------------------------------
-- 公車表 (STM)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vehicle_positions (
    id              BIGSERIAL PRIMARY KEY,
    fetched_at      TIMESTAMPTZ NOT NULL,
    feed_timestamp  BIGINT,
    vehicle_id      TEXT,
    trip_id         TEXT,
    route_id        TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    bearing         REAL,
    speed           REAL,
    stop_id         TEXT,
    current_status  INTEGER,
    occupancy       INTEGER,
    -- PostGIS 地理欄位, 自動由 lat/lon 算出 (SRID 4326 = WGS84 經緯度)
    geom            GEOGRAPHY(POINT, 4326)
                    GENERATED ALWAYS AS (
                        CASE
                            WHEN latitude IS NOT NULL AND longitude IS NOT NULL
                            THEN ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
                            ELSE NULL
                        END
                    ) STORED
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_vp_fetched_at ON vehicle_positions(fetched_at);
CREATE INDEX IF NOT EXISTS idx_vp_route_id   ON vehicle_positions(route_id);
CREATE INDEX IF NOT EXISTS idx_vp_vehicle_id ON vehicle_positions(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_vp_geom       ON vehicle_positions USING GIST(geom);

-- ------------------------------------------------------------
-- 飛機表 (OpenSky)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aircraft_positions (
    id              BIGSERIAL PRIMARY KEY,
    fetched_at      TIMESTAMPTZ NOT NULL,
    state_time      BIGINT,
    icao24          TEXT,
    callsign        TEXT,
    origin_country  TEXT,
    longitude       DOUBLE PRECISION,
    latitude        DOUBLE PRECISION,
    baro_altitude   REAL,    -- meters
    geo_altitude    REAL,    -- meters
    on_ground       BOOLEAN,
    velocity        REAL,    -- m/s
    heading         REAL,    -- degrees (true_track)
    vertical_rate   REAL,    -- m/s
    geom            GEOGRAPHY(POINT, 4326)
                    GENERATED ALWAYS AS (
                        CASE
                            WHEN latitude IS NOT NULL AND longitude IS NOT NULL
                            THEN ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
                            ELSE NULL
                        END
                    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_ap_fetched_at ON aircraft_positions(fetched_at);
CREATE INDEX IF NOT EXISTS idx_ap_icao24     ON aircraft_positions(icao24);
CREATE INDEX IF NOT EXISTS idx_ap_callsign   ON aircraft_positions(callsign);
CREATE INDEX IF NOT EXISTS idx_ap_geom       ON aircraft_positions USING GIST(geom);

-- ------------------------------------------------------------
-- 完成
-- ------------------------------------------------------------
\echo '✅ Schema created. Tables:'
\dt
