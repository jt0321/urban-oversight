"""
ingestion/mapillary.py — Mapillary street-level imagery

Queries images within a tract bounding box, stores metadata.
Vision analysis is a separate step (detection/vision.py).

Free API token: https://www.mapillary.com/developer/api-documentation
"""

import os
import uuid
import httpx
from datetime import datetime, timezone
from db.store import get_conn
from db.spatial import tract_bbox, point_to_tract

MAPILLARY_TOKEN = os.getenv("MAPILLARY_TOKEN", "")
MAPILLARY_API = "https://graph.mapillary.com/images"

# Max images to sample per tract — keeps costs manageable
IMAGES_PER_TRACT = int(os.getenv("IMAGES_PER_TRACT", "20"))


def fetch_tract_images(geoid: str, limit: int = IMAGES_PER_TRACT) -> list[dict]:
    """Fetch Mapillary images within a tract's bounding box."""
    if not MAPILLARY_TOKEN:
        raise RuntimeError("MAPILLARY_TOKEN not set")

    bbox = tract_bbox(geoid)
    if not bbox:
        print(f"[mapillary] no bbox found for tract {geoid}")
        return []

    min_lon, min_lat, max_lon, max_lat = bbox
    bbox_str = f"{min_lon},{min_lat},{max_lon},{max_lat}"

    params = {
        "access_token": MAPILLARY_TOKEN,
        "fields": "id,geometry,captured_at,compass_angle,thumb_256_url,thumb_1024_url",
        "bbox": bbox_str,
        "limit": limit,
    }

    with httpx.Client(timeout=30.0) as client:
        r = client.get(MAPILLARY_API, params=params)
        r.raise_for_status()

    data = r.json()
    return data.get("data", [])


def load_mapillary_for_tract(geoid: str) -> int:
    images = fetch_tract_images(geoid)
    if not images:
        return 0

    con = get_conn()
    count = 0
    now = datetime.now(timezone.utc)

    for img in images:
        img_id = img.get("id", "")
        geometry = img.get("geometry", {})
        coords = geometry.get("coordinates", [None, None])
        lon, lat = coords[0], coords[1]

        if lat is None or lon is None:
            continue

        captured_raw = img.get("captured_at")
        captured_at = None
        if captured_raw:
            try:
                captured_at = datetime.fromtimestamp(
                    captured_raw / 1000, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        con.execute("""
            INSERT OR IGNORE INTO mapillary_images
              (id, geoid, lat, lon, bearing, captured_at,
               mapillary_url, thumb_url, analyzed, detections)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            img_id, geoid, lat, lon,
            img.get("compass_angle"),
            captured_at,
            f"https://www.mapillary.com/app/?pKey={img_id}",
            img.get("thumb_1024_url") or img.get("thumb_256_url"),
            False, 0,
        ])
        count += 1

    con.close()
    return count


def load_mapillary_for_county(county_fips: str = None) -> int:
    """Load imagery for all tracts in a county."""
    con = get_conn()
    where = f"WHERE county_fips = ?" if county_fips else ""
    params = [county_fips] if county_fips else []
    tracts = con.execute(
        f"SELECT geoid FROM census_tracts {where}", params
    ).fetchall()
    con.close()

    total = 0
    for (geoid,) in tracts:
        n = load_mapillary_for_tract(geoid)
        total += n
        if n:
            print(f"[mapillary] tract {geoid}: {n} images")

    print(f"[mapillary] total images loaded: {total}")
    return total
