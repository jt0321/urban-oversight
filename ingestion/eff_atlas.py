"""
ingestion/eff_atlas.py — EFF Atlas of Surveillance dataset

The Atlas is a static CSV/dataset maintained by EFF documenting surveillance
technology deployments by city/agency. Download from:
  https://atlasofsurveillance.org/

Place the CSV at data/eff_atlas.csv — columns vary by export but typically include:
  City, State, Agency, Technology, Latitude, Longitude, Source, Year

This ingester normalizes to SurveillancePoint schema with confidence=high.
"""

import os
import uuid
import csv
from datetime import datetime, timezone
from db.store import get_conn
from db.spatial import point_to_tract

EFF_CSV_PATH = os.getenv("EFF_CSV_PATH", "data/eff_atlas.csv")

# Map EFF technology labels to our point_type vocabulary
TECH_TYPE_MAP = {
    "automated license plate reader": "alpr",
    "alpr": "alpr",
    "flock safety": "flock",
    "cctv": "cctv",
    "surveillance camera": "camera",
    "camera": "camera",
    "red light camera": "red_light",
    "traffic camera": "traffic",
    "stingray": "cell_site_simulator",
    "cell site simulator": "cell_site_simulator",
    "face recognition": "face_recognition",
    "facial recognition": "face_recognition",
    "gunshot detection": "gunshot_detection",
    "shotspotter": "gunshot_detection",
    "drone": "drone",
}


def normalize_type(tech_label: str) -> str:
    if not tech_label:
        return "unknown"
    key = tech_label.strip().lower()
    for pattern, mapped in TECH_TYPE_MAP.items():
        if pattern in key:
            return mapped
    return "unknown"


def load_eff_atlas(
    csv_path: str = EFF_CSV_PATH,
    target_state: str = "CA",
) -> int:
    """
    Load EFF Atlas CSV into surveillance_points.
    Filters to target_state. Assigns tract geoids via spatial join.
    Returns number of records written.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"EFF Atlas CSV not found at {csv_path}. "
            "Download from https://atlasofsurveillance.org/ and place at data/eff_atlas.csv"
        )

    con = get_conn()
    count = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            state = row.get("State", "").strip()
            if target_state and state != target_state:
                continue

            # Latitude/longitude — EFF CSV may use different column names
            lat_raw = row.get("Latitude") or row.get("lat") or row.get("LAT")
            lon_raw = row.get("Longitude") or row.get("lon") or row.get("LON")

            try:
                lat = float(lat_raw)
                lon = float(lon_raw)
            except (TypeError, ValueError):
                skipped += 1
                continue

            # Skip clearly invalid coords
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                skipped += 1
                continue

            technology = row.get("Technology") or row.get("technology") or ""
            agency = row.get("Agency") or row.get("agency") or ""
            city = row.get("City") or row.get("city") or ""
            source_url = row.get("Source") or row.get("source") or ""
            year = row.get("Year") or row.get("year") or ""

            point_type = normalize_type(technology)
            geoid = point_to_tract(lat, lon)

            point_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"eff_{lat}_{lon}_{technology}_{agency}"
            ))

            raw = {
                "agency": agency,
                "city": city,
                "technology": technology,
                "source_url": source_url,
                "year": year,
                "state": state,
            }

            con.execute("""
                INSERT OR IGNORE INTO surveillance_points
                  (id, geoid, lat, lon, point_type, source, source_id,
                   confidence, detection_method, verified, ingested_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                point_id, geoid, lat, lon,
                point_type, "eff",
                row.get("id") or point_id,
                "high", "open_data", False,
                now, str(raw),
            ])
            count += 1

    con.close()
    print(f"[eff] loaded {count} points, skipped {skipped} (missing/invalid coords)")
    return count
