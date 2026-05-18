"""
ingestion/city_portals/la.py — LA City open data

Sources:
  LADOT Traffic Cameras
    https://data.lacity.org/Transportation/ATSAC-Traffic-Signal-Control-Traffic-Cameras/efgh-...
  LA Red Light Cameras (via LAPD / city records)

Both are fetched from the LA open data Socrata API (no key needed for public datasets).
"""

import uuid
import httpx
from datetime import datetime, timezone
from db.store import get_conn
from db.spatial import point_to_tract

# Socrata dataset endpoints — update IDs if LA changes them
DATASETS = {
    "traffic_cameras": {
        "url": "https://data.lacity.org/resource/i6c3-rz9p.json",
        "lat_col": "latitude",
        "lon_col": "longitude",
        "type": "traffic",
        "confidence": "high",
    },
    "red_light_cameras": {
        "url": "https://data.lacity.org/resource/t4nf-hmjg.json",
        "lat_col": "lat",
        "lon_col": "lon",
        "type": "red_light",
        "confidence": "high",
    },
}


def fetch_dataset(url: str, limit: int = 5000) -> list[dict]:
    params = {"$limit": limit}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
    return r.json()


def load_la(datasets: dict = DATASETS) -> int:
    con = get_conn()
    total = 0
    now = datetime.now(timezone.utc).isoformat()

    for dataset_name, config in datasets.items():
        print(f"[la] fetching {dataset_name}")
        try:
            rows = fetch_dataset(config["url"])
        except Exception as e:
            print(f"[la] failed to fetch {dataset_name}: {e}")
            continue

        count = 0
        for row in rows:
            try:
                lat = float(row.get(config["lat_col"]) or row.get("location", {}).get("latitude", 0))
                lon = float(row.get(config["lon_col"]) or row.get("location", {}).get("longitude", 0))
            except (TypeError, ValueError):
                continue

            if lat == 0 and lon == 0:
                continue

            source_id = str(row.get("objectid") or row.get("id") or row.get("_id", ""))
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"la_{dataset_name}_{source_id}_{lat}_{lon}"))
            geoid = point_to_tract(lat, lon)

            con.execute("""
                INSERT OR IGNORE INTO surveillance_points
                  (id, geoid, lat, lon, point_type, source, source_id,
                   confidence, detection_method, verified, ingested_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                point_id, geoid, lat, lon,
                config["type"], "city_la", source_id,
                config["confidence"], "open_data", True,
                now, str(row),
            ])
            count += 1

        print(f"[la] {dataset_name}: {count} points loaded")
        total += count

    con.close()
    return total
