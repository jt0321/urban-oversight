"""
detection/vision.py — Claude vision model → structured surveillance detections

For each unanalyzed Mapillary image:
  1. Fetch the thumbnail URL
  2. Send to Claude with a structured prompt
  3. Parse response into detection records
  4. Write detected points to surveillance_points with confidence=low
  5. Mark image as analyzed
"""

import os
import json
import uuid
import httpx
import anthropic
from datetime import datetime, timezone
from db.store import get_conn
from db.spatial import point_to_tract

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are a surveillance infrastructure analyst reviewing street-level photographs.
Identify any surveillance or monitoring infrastructure visible in the image.

Respond ONLY with a JSON object, no markdown:
{
  "detections": [
    {
      "type": "camera|alpr|cctv|traffic|red_light|gunshot_detection|unknown",
      "confidence": "high|medium|low",
      "description": "brief description of what you see",
      "mounted_on": "pole|building|traffic_light|overpass|unknown"
    }
  ],
  "total_count": <integer>,
  "notes": "<any relevant context about the scene, e.g. dense urban, residential, freeway>"
}

If no surveillance infrastructure is visible, return {"detections": [], "total_count": 0, "notes": "..."}.
Do not guess. Only report what is clearly visible."""


def fetch_image_bytes(url: str) -> bytes:
    with httpx.Client(timeout=30.0) as client_http:
        r = client_http.get(url)
        r.raise_for_status()
        return r.content


def analyze_image(image_url: str) -> dict:
    """Send a Mapillary thumbnail to Claude vision and return structured detections."""
    try:
        image_bytes = fetch_image_bytes(image_url)
    except Exception as e:
        return {"detections": [], "total_count": 0, "notes": f"image fetch failed: {e}"}

    import base64
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    }
                },
                {
                    "type": "text",
                    "text": "Identify surveillance infrastructure in this street-level image."
                }
            ]
        }],
    )

    text = response.content[0].text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"detections": [], "total_count": 0, "notes": "parse error", "raw": text}


def write_detections(image_row: dict, analysis: dict) -> int:
    """Write vision-detected points to surveillance_points."""
    detections = analysis.get("detections", [])
    if not detections:
        return 0

    con = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    for det in detections:
        point_id = str(uuid.uuid4())
        raw = {
            "mapillary_image_id": image_row["id"],
            "description": det.get("description"),
            "mounted_on": det.get("mounted_on"),
            "scene_notes": analysis.get("notes"),
        }

        con.execute("""
            INSERT OR IGNORE INTO surveillance_points
              (id, geoid, lat, lon, point_type, source, source_id,
               confidence, detection_method, verified, ingested_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            point_id,
            image_row.get("geoid"),
            image_row["lat"],
            image_row["lon"],
            det.get("type", "unknown"),
            "mapillary_detected",
            image_row["id"],
            det.get("confidence", "low"),
            "vision_model",
            False,
            now,
            str(raw),
        ])
        count += 1

    con.close()
    return count


def run_vision_pass(
    tract_geoid: str = None,
    limit: int = 50,
) -> dict:
    """
    Analyze unanalyzed Mapillary images.
    Optionally filter to a specific tract.
    Returns summary of detections.
    """
    con = get_conn()
    where = "WHERE analyzed = FALSE"
    params = []
    if tract_geoid:
        where += " AND geoid = ?"
        params.append(tract_geoid)

    images = con.execute(
        f"SELECT * FROM mapillary_images {where} LIMIT ?",
        params + [limit]
    ).fetchdf().to_dict(orient="records")
    con.close()

    total_detections = 0
    images_processed = 0

    for img in images:
        thumb_url = img.get("thumb_url")
        if not thumb_url:
            continue

        analysis = analyze_image(thumb_url)
        n_detections = write_detections(img, analysis)
        total_detections += n_detections
        images_processed += 1

        # Update image record
        con = get_conn()
        con.execute("""
            UPDATE mapillary_images
            SET analyzed = TRUE,
                analysis_json = ?,
                detections = ?
            WHERE id = ?
        """, [str(analysis), n_detections, img["id"]])
        con.close()

        print(f"[vision] image {img['id']}: {n_detections} detections")

    return {
        "images_processed": images_processed,
        "total_detections": total_detections,
    }
