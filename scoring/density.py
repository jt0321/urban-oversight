"""
scoring/density.py — per-tract surveillance density + composite score

Composite score formula:
  known_density    (documented points / sqkm)  × 0.5
  detected_density (vision detections / images) × 0.3
  source_coverage  (0–1 normalized)             × 0.2

All raw components stored — never just the composite.
"""

from datetime import datetime, timezone
from db.store import get_conn

# Sources counted toward source_coverage
ALL_SOURCES = {"osm", "eff", "city_la", "mapillary_detected"}
MAX_SOURCES = len(ALL_SOURCES)

WEIGHTS = {
    "known_density": 0.5,
    "detected_density": 0.3,
    "source_coverage": 0.2,
}


def compute_tract_score(geoid: str) -> dict | None:
    con = get_conn()

    tract = con.execute(
        "SELECT area_sqkm FROM census_tracts WHERE geoid = ?", [geoid]
    ).fetchone()

    if not tract or not tract[0]:
        con.close()
        return None

    area_sqkm = tract[0]

    # Known points (excluding vision-detected)
    known = con.execute("""
        SELECT COUNT(*) as n, COUNT(DISTINCT source) as sources
        FROM surveillance_points
        WHERE geoid = ?
          AND source != 'mapillary_detected'
    """, [geoid]).fetchone()

    known_count = known[0] or 0
    source_count = known[1] or 0

    # Vision-detected
    vision = con.execute("""
        SELECT
            COUNT(*) as detections,
            (SELECT COUNT(*) FROM mapillary_images WHERE geoid = ? AND analyzed = TRUE) as images
        FROM surveillance_points
        WHERE geoid = ? AND source = 'mapillary_detected'
    """, [geoid, geoid]).fetchone()

    detections = vision[0] or 0
    images_analyzed = vision[1] or 0

    known_density = known_count / area_sqkm if area_sqkm else 0
    detected_density = detections / images_analyzed if images_analyzed else 0
    source_coverage_norm = source_count / MAX_SOURCES

    # Normalize densities to a 0–10 scale (rough calibration, adjust with real data)
    known_norm = min(known_density / 5.0, 1.0) * 10
    detected_norm = min(detected_density / 3.0, 1.0) * 10
    coverage_norm = source_coverage_norm * 10

    composite = (
        known_norm * WEIGHTS["known_density"]
        + detected_norm * WEIGHTS["detected_density"]
        + coverage_norm * WEIGHTS["source_coverage"]
    )

    now = datetime.now(timezone.utc).isoformat()

    con.execute("""
        INSERT INTO tract_scores
          (geoid, computed_at, known_density, detected_density,
           source_coverage, images_analyzed, point_count, composite_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        geoid, now,
        round(known_density, 4),
        round(detected_density, 4),
        source_count,
        images_analyzed,
        known_count,
        round(composite, 4),
    ])
    con.close()

    return {
        "geoid": geoid,
        "known_density": known_density,
        "detected_density": detected_density,
        "source_coverage": source_count,
        "images_analyzed": images_analyzed,
        "point_count": known_count,
        "composite_score": composite,
    }


def score_all_tracts(county_fips: str = None) -> int:
    con = get_conn()
    where = "WHERE county_fips = ?" if county_fips else ""
    params = [county_fips] if county_fips else []
    tracts = con.execute(
        f"SELECT geoid FROM census_tracts {where}", params
    ).fetchall()
    con.close()

    count = 0
    for (geoid,) in tracts:
        result = compute_tract_score(geoid)
        if result:
            count += 1

    print(f"[scoring] scored {count} tracts")
    return count
