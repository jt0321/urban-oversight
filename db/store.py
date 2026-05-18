"""
db/store.py — DuckDB schema + init

Tables:
  census_tracts         tract geometries (WKT) + metadata
  tract_demographics    ACS data per tract per vintage
  surveillance_points   normalized camera/infrastructure points
  mapillary_images      street-level image metadata + analysis
  tract_scores          computed density + composite scores
  ingest_log            one record per source ingestion run
"""

import os
import duckdb

DB_PATH = os.getenv("DB_PATH", "urban_oversight.duckdb")


def get_conn() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(DB_PATH)
    con.execute("INSTALL spatial; LOAD spatial;")
    return con


def init_db():
    con = get_conn()

    con.execute("""
        CREATE TABLE IF NOT EXISTS census_tracts (
            geoid           VARCHAR PRIMARY KEY,   -- 11-digit FIPS
            state_fips      VARCHAR NOT NULL,
            county_fips     VARCHAR NOT NULL,
            tract_name      VARCHAR,
            area_sqkm       DOUBLE,
            geometry_wkt    VARCHAR NOT NULL       -- WKT polygon
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS tract_demographics (
            geoid               VARCHAR NOT NULL,
            vintage             INTEGER NOT NULL,  -- ACS year
            total_pop           INTEGER,
            median_income       INTEGER,
            poverty_rate        DOUBLE,
            pct_white           DOUBLE,
            pct_black           DOUBLE,
            pct_hispanic        DOUBLE,
            pct_asian           DOUBLE,
            pct_renter          DOUBLE,
            svi                 DOUBLE,            -- CDC Social Vulnerability Index, nullable
            PRIMARY KEY (geoid, vintage)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS surveillance_points (
            id                  VARCHAR PRIMARY KEY,
            geoid               VARCHAR,           -- tract, nullable until spatial join
            lat                 DOUBLE NOT NULL,
            lon                 DOUBLE NOT NULL,
            point_type          VARCHAR NOT NULL,  -- camera | alpr | cctv | red_light | traffic | flock | unknown
            source              VARCHAR NOT NULL,  -- osm | eff | city_la | mapillary_detected
            source_id           VARCHAR,           -- original id in source system
            confidence          VARCHAR NOT NULL,  -- high | medium | low
            detection_method    VARCHAR NOT NULL,  -- manual | osm | open_data | vision_model
            verified            BOOLEAN DEFAULT FALSE,
            ingested_at         TIMESTAMPTZ NOT NULL,
            raw_json            JSON
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS mapillary_images (
            id                  VARCHAR PRIMARY KEY,
            geoid               VARCHAR,
            lat                 DOUBLE NOT NULL,
            lon                 DOUBLE NOT NULL,
            bearing             DOUBLE,
            captured_at         TIMESTAMPTZ,
            mapillary_url       VARCHAR,
            thumb_url           VARCHAR,
            analyzed            BOOLEAN DEFAULT FALSE,
            analysis_json       JSON,              -- raw vision model output
            detections          INTEGER DEFAULT 0  -- count of surveillance items detected
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS tract_scores (
            geoid               VARCHAR NOT NULL,
            computed_at         TIMESTAMPTZ NOT NULL,
            known_density       DOUBLE,            -- documented points per sqkm
            detected_density    DOUBLE,            -- vision-detected per images analyzed
            source_coverage     INTEGER,           -- number of sources that contributed
            images_analyzed     INTEGER DEFAULT 0,
            point_count         INTEGER DEFAULT 0,
            composite_score     DOUBLE,
            PRIMARY KEY (geoid, computed_at)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS ingest_log (
            id                  VARCHAR PRIMARY KEY,
            source              VARCHAR NOT NULL,
            started_at          TIMESTAMPTZ NOT NULL,
            finished_at         TIMESTAMPTZ,
            status              VARCHAR NOT NULL,  -- success | partial | failed
            records_written     INTEGER DEFAULT 0,
            notes               VARCHAR
        )
    """)

    con.close()
    print(f"[db] initialized at {DB_PATH}")


if __name__ == "__main__":
    init_db()
