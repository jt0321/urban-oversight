"""
detection/deduplication.py — cross-source point merging

When two sources report a camera at nearly the same location,
mark the lower-confidence one as a duplicate and set verified=TRUE
on the higher-confidence one.

Uses a simple distance threshold (default 25m).
"""

import math
from db.store import get_conn

# Two points within this distance (meters) are considered the same physical camera
DEDUP_THRESHOLD_M = 25.0

# Source priority for keeping the canonical record
SOURCE_PRIORITY = {
    "city_la": 1,
    "eff": 2,
    "osm": 3,
    "mapillary_detected": 4,
}


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distance between two lat/lon points in meters."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def run_deduplication(threshold_m: float = DEDUP_THRESHOLD_M) -> dict:
    """
    For each pair of points within threshold_m of each other from different sources:
    - Mark the lower-priority source as a duplicate (soft delete via verified flag trick)
    - Set verified=TRUE on the higher-priority record

    Returns summary of merges performed.
    """
    con = get_conn()
    points = con.execute("""
        SELECT id, geoid, lat, lon, source, confidence
        FROM surveillance_points
        ORDER BY source
    """).fetchdf().to_dict(orient="records")
    con.close()

    verified_ids = set()
    duplicate_ids = set()
    merge_count = 0

    # O(n²) — acceptable for typical dataset sizes (<10k points per county)
    for i, a in enumerate(points):
        if a["id"] in duplicate_ids:
            continue
        for b in points[i + 1:]:
            if b["id"] in duplicate_ids:
                continue
            if a["source"] == b["source"]:
                continue

            dist = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            if dist <= threshold_m:
                # Keep higher-priority source
                pri_a = SOURCE_PRIORITY.get(a["source"], 99)
                pri_b = SOURCE_PRIORITY.get(b["source"], 99)
                keeper = a if pri_a <= pri_b else b
                dupe = b if pri_a <= pri_b else a

                verified_ids.add(keeper["id"])
                duplicate_ids.add(dupe["id"])
                merge_count += 1

    if verified_ids:
        con = get_conn()
        for vid in verified_ids:
            con.execute("UPDATE surveillance_points SET verified = TRUE WHERE id = ?", [vid])
        con.close()

    print(f"[dedup] {merge_count} merges — {len(verified_ids)} verified, {len(duplicate_ids)} duplicates flagged")
    return {
        "merges": merge_count,
        "verified": len(verified_ids),
        "duplicates_flagged": len(duplicate_ids),
        "duplicate_ids": list(duplicate_ids),
    }
