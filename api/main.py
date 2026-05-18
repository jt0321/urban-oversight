"""
api/main.py — FastAPI observability + query surface

GET  /                              health
GET  /tracts/{geoid}                demographics + latest score + source breakdown
GET  /tracts/{geoid}/points         surveillance points in tract
GET  /tracts/{geoid}/images         mapillary images + analysis
GET  /scores                        all tract scores, sortable, filterable
GET  /correlations                  precomputed demographic correlations
GET  /ingest/log                    ingestion run history
POST /pipeline/run                  trigger full pipeline run
POST /pipeline/analyze/{geoid}      trigger vision pass for one tract
"""

import os
from typing import Optional
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware

from db.store import get_conn, init_db

app = FastAPI(
    title="Urban Oversight",
    description="Civic data pipeline: surveillance infrastructure density × Census demographics",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def q(sql: str, params: list = []) -> list[dict]:
    con = get_conn()
    try:
        return con.execute(sql, params).fetchdf().to_dict(orient="records")
    finally:
        con.close()


@app.on_event("startup")
def startup():
    init_db()


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# ── Tract detail ──────────────────────────────────────────────────────────────

@app.get("/tracts/{geoid}")
def get_tract(geoid: str, vintage: int = 2022):
    tract = q("SELECT * FROM census_tracts WHERE geoid = ?", [geoid])
    if not tract:
        raise HTTPException(404, "Tract not found")

    demographics = q(
        "SELECT * FROM tract_demographics WHERE geoid = ? AND vintage = ?",
        [geoid, vintage]
    )

    score = q("""
        SELECT * FROM tract_scores
        WHERE geoid = ?
        ORDER BY computed_at DESC
        LIMIT 1
    """, [geoid])

    sources = q("""
        SELECT source, COUNT(*) as count, COUNT(DISTINCT point_type) as types
        FROM surveillance_points
        WHERE geoid = ?
        GROUP BY source
    """, [geoid])

    t = tract[0]
    t.pop("geometry_wkt", None)  # don't return raw WKT in default response

    return {
        "geoid": geoid,
        "tract": t,
        "demographics": demographics[0] if demographics else None,
        "surveillance": score[0] if score else None,
        "sources": sources,
    }


@app.get("/tracts/{geoid}/points")
def get_tract_points(
    geoid: str,
    source: Optional[str] = None,
    point_type: Optional[str] = None,
    verified_only: bool = False,
):
    where = ["geoid = ?"]
    params = [geoid]

    if source:
        where.append("source = ?")
        params.append(source)
    if point_type:
        where.append("point_type = ?")
        params.append(point_type)
    if verified_only:
        where.append("verified = TRUE")

    rows = q(f"""
        SELECT id, lat, lon, point_type, source, confidence,
               detection_method, verified, ingested_at
        FROM surveillance_points
        WHERE {' AND '.join(where)}
        ORDER BY source, point_type
    """, params)

    return {"geoid": geoid, "count": len(rows), "points": rows}


@app.get("/tracts/{geoid}/images")
def get_tract_images(geoid: str, analyzed_only: bool = False):
    where = "WHERE geoid = ?"
    params = [geoid]
    if analyzed_only:
        where += " AND analyzed = TRUE"

    rows = q(f"""
        SELECT id, lat, lon, bearing, captured_at,
               mapillary_url, thumb_url, analyzed, detections
        FROM mapillary_images
        {where}
        ORDER BY detections DESC
    """, params)

    return {"geoid": geoid, "count": len(rows), "images": rows}


# ── Scores ────────────────────────────────────────────────────────────────────

@app.get("/scores")
def list_scores(
    county_fips: Optional[str] = None,
    min_score: Optional[float] = None,
    limit: int = Query(100, le=500),
    vintage: int = 2022,
):
    where = ["ts.composite_score IS NOT NULL"]
    params = []

    if county_fips:
        where.append("ct.county_fips = ?")
        params.append(county_fips)
    if min_score is not None:
        where.append("ts.composite_score >= ?")
        params.append(min_score)

    rows = q(f"""
        SELECT DISTINCT ON (ts.geoid)
            ts.geoid,
            ct.tract_name,
            ts.composite_score,
            ts.known_density,
            ts.detected_density,
            ts.source_coverage,
            ts.point_count,
            ts.images_analyzed,
            td.median_income,
            td.poverty_rate,
            td.pct_black,
            td.pct_hispanic
        FROM tract_scores ts
        JOIN census_tracts ct ON ts.geoid = ct.geoid
        LEFT JOIN tract_demographics td ON ts.geoid = td.geoid AND td.vintage = ?
        WHERE {' AND '.join(where)}
        ORDER BY ts.geoid, ts.computed_at DESC, ts.composite_score DESC
        LIMIT ?
    """, [vintage] + params + [limit])

    return {"count": len(rows), "tracts": rows}


# ── Correlations ──────────────────────────────────────────────────────────────

@app.get("/correlations")
def get_correlations(vintage: int = 2022):
    from scoring.correlations import compute_correlations
    return compute_correlations(vintage)


# ── Ingest log ────────────────────────────────────────────────────────────────

@app.get("/ingest/log")
def ingest_log(limit: int = Query(20, le=100)):
    rows = q("""
        SELECT * FROM ingest_log
        ORDER BY started_at DESC
        LIMIT ?
    """, [limit])
    return {"count": len(rows), "log": rows}


# ── Pipeline triggers ─────────────────────────────────────────────────────────

_running = False


@app.post("/pipeline/run")
def trigger_run(
    background_tasks: BackgroundTasks,
    skip_census_geo: bool = True,
    skip_vision: bool = True,
):
    global _running
    if _running:
        return {"status": "already_running"}

    def _run():
        global _running
        _running = True
        try:
            from pipeline.runner import run_pipeline
            run_pipeline(skip_census_geo=skip_census_geo, skip_vision=skip_vision)
        finally:
            _running = False

    background_tasks.add_task(_run)
    return {"status": "triggered", "skip_census_geo": skip_census_geo, "skip_vision": skip_vision}


@app.post("/pipeline/analyze/{geoid}")
def trigger_vision(geoid: str, background_tasks: BackgroundTasks, limit: int = 20):
    def _run():
        from detection.vision import run_vision_pass
        result = run_vision_pass(tract_geoid=geoid, limit=limit)
        # Re-score after vision pass
        from scoring.density import compute_tract_score
        compute_tract_score(geoid)
        return result

    background_tasks.add_task(_run)
    return {"status": "triggered", "geoid": geoid, "image_limit": limit}
