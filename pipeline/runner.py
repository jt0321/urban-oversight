"""
pipeline/runner.py — orchestrates full ingestion + scoring run

Sequence:
  1. Census geometries (if not already loaded)
  2. ACS demographics
  3. EFF Atlas
  4. OSM
  5. LA city portals
  6. Deduplication
  7. Tract scoring
  (8. Mapillary + vision — separate, triggered manually or on schedule)
"""

import os
import uuid
from datetime import datetime, timezone
from db.store import get_conn, init_db

COUNTY_FIPS = os.getenv("TARGET_COUNTY", "06037")
ACS_VINTAGE = int(os.getenv("ACS_VINTAGE", "2022"))


def _log_ingest(source: str, status: str, records: int, notes: str = ""):
    con = get_conn()
    con.execute("""
        INSERT INTO ingest_log (id, source, started_at, finished_at, status, records_written, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        str(uuid.uuid4()), source,
        datetime.now(timezone.utc).isoformat(),
        datetime.now(timezone.utc).isoformat(),
        status, records, notes,
    ])
    con.close()


def run_pipeline(
    skip_census_geo: bool = False,
    skip_vision: bool = True,
) -> dict:
    init_db()
    summary = {}

    # 1. Census geometries
    if not skip_census_geo:
        try:
            from ingestion.census import load_tiger_geometries
            n = load_tiger_geometries(COUNTY_FIPS)
            _log_ingest("census_tiger", "success", n)
            summary["census_geo"] = n
        except Exception as e:
            _log_ingest("census_tiger", "failed", 0, str(e))
            summary["census_geo"] = f"error: {e}"
            print(f"[runner] census geo failed: {e}")

    # 2. ACS demographics
    try:
        from ingestion.census import load_acs_demographics
        n = load_acs_demographics(COUNTY_FIPS, ACS_VINTAGE)
        _log_ingest("census_acs", "success", n)
        summary["acs"] = n
    except Exception as e:
        _log_ingest("census_acs", "failed", 0, str(e))
        summary["acs"] = f"error: {e}"

    # 3. EFF Atlas
    try:
        from ingestion.eff_atlas import load_eff_atlas
        n = load_eff_atlas()
        _log_ingest("eff", "success", n)
        summary["eff"] = n
    except FileNotFoundError as e:
        _log_ingest("eff", "skipped", 0, str(e))
        summary["eff"] = "skipped (CSV not found)"
    except Exception as e:
        _log_ingest("eff", "failed", 0, str(e))
        summary["eff"] = f"error: {e}"

    # 4. OSM
    try:
        from ingestion.osm import load_osm
        n = load_osm()
        _log_ingest("osm", "success", n)
        summary["osm"] = n
    except Exception as e:
        _log_ingest("osm", "failed", 0, str(e))
        summary["osm"] = f"error: {e}"

    # 5. LA city portals
    try:
        from ingestion.city_portals.la import load_la
        n = load_la()
        _log_ingest("city_la", "success", n)
        summary["city_la"] = n
    except Exception as e:
        _log_ingest("city_la", "failed", 0, str(e))
        summary["city_la"] = f"error: {e}"

    # 6. Deduplication
    try:
        from detection.deduplication import run_deduplication
        dedup = run_deduplication()
        summary["deduplication"] = dedup
    except Exception as e:
        summary["deduplication"] = f"error: {e}"

    # 7. Scoring
    try:
        from scoring.density import score_all_tracts
        n = score_all_tracts(COUNTY_FIPS)
        summary["scored_tracts"] = n
    except Exception as e:
        summary["scored_tracts"] = f"error: {e}"

    # 8. Vision (opt-in)
    if not skip_vision:
        try:
            from detection.vision import run_vision_pass
            vision = run_vision_pass()
            summary["vision"] = vision
        except Exception as e:
            summary["vision"] = f"error: {e}"

    print(f"[runner] complete — {summary}")
    return summary
