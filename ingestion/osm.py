"""
ingestion/osm.py — OpenStreetMap Overpass API

Queries for surveillance infrastructure nodes within a bounding box.
OSM tag: man_made=surveillance (plus subtypes via surveillance=* tag)

Free, no key needed. Be polite — cache results, don't hammer.
"""

import uuid
import httpx
import time
from datetime import datetime, timezone
from db.store import get_conn
from db.spatial import point_to_tract

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# LA County bounding box (south, west, north, east)
LA_BBOX = (33.70, -118.67, 34.82, -117.65)

OSM_SURVEILLANCE_TYPES = {
    "camera": "camera",
    "traffic": "traffic",
    "ALPR": "alpr",
    "guard": "unknown",
    "outdoor": "camera",
    "indoor": "camera",
    "public": "camera",
}


def build_overpass_query(bbox: tuple) -> str:
    s, w, n, e = bbox
    return f"""
[out:json][timeout:60];
(
  node["man_made"="surveillance"]({s},{w},{n},{e});
  node["highway"="speed_camera"]({s},{w},{n},{e});
);
out body;
"""


def fetch_osm(bbox: tuple = LA_BBOX, timeout: float = 90.0) -> list[dict]:
    query = build_overpass_query(bbox)
    print(f"[osm] querying Overpass for bbox {bbox}")

    with httpx.Client(timeout=timeout) as client:
        r = client.post(OVERPASS_URL, data={"data": query})
        r.raise_for_status()

    data = r.json()
    return data.get("elements", [])


def normalize_osm_type(tags: dict) -> str:
    surveillance_tag = tags.get("surveillance", "")
    highway_tag = tags.get("highway", "")

    if highway_tag == "speed_camera":
        return "traffic"
    mapped = OSM_SURVEILLANCE_TYPES.get(surveillance_tag)
    return mapped or "camera"


def load_osm(bbox: tuple = LA_BBOX) -> int:
    elements = fetch_osm(bbox)
    con = get_conn()
    count = 0
    now = datetime.now(timezone.utc).isoformat()

    for el in elements:
        if el.get("type") != "node":
            continue

        lat = el.get("lat")
        lon = el.get("lon")
        osm_id = str(el.get("id", ""))
        tags = el.get("tags", {})

        if lat is None or lon is None:
            continue

        point_type = normalize_osm_type(tags)
        geoid = point_to_tract(lat, lon)

        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"osm_{osm_id}"))

        con.execute("""
            INSERT OR IGNORE INTO surveillance_points
              (id, geoid, lat, lon, point_type, source, source_id,
               confidence, detection_method, verified, ingested_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            point_id, geoid, lat, lon,
            point_type, "osm", osm_id,
            "medium", "osm", False,
            now, str(tags),
        ])
        count += 1

    con.close()
    print(f"[osm] loaded {count} surveillance nodes")
    return count
