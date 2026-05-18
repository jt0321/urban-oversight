"""
db/spatial.py — spatial join helpers

Primary operation: given (lat, lon), return the census tract geoid.
Uses DuckDB spatial extension (ST_Within, ST_Point, ST_GeomFromText).
"""

import os
from typing import Optional
from db.store import get_conn


def point_to_tract(lat: float, lon: float) -> Optional[str]:
    """
    Return the census tract geoid containing (lat, lon).
    Returns None if no tract found (outside loaded area or missing geometry).
    """
    con = get_conn()
    try:
        result = con.execute("""
            SELECT geoid
            FROM census_tracts
            WHERE ST_Within(
                ST_Point(?, ?),
                ST_GeomFromText(geometry_wkt)
            )
            LIMIT 1
        """, [lon, lat]).fetchone()  # ST_Point is (lon, lat) per GeoJSON convention
        return result[0] if result else None
    finally:
        con.close()


def batch_assign_tracts(table: str, id_col: str = "id"):
    """
    Update all rows in `table` where geoid IS NULL by doing a spatial join
    against census_tracts. Runs in-database — no Python loop needed.
    """
    con = get_conn()
    try:
        con.execute(f"""
            UPDATE {table}
            SET geoid = (
                SELECT ct.geoid
                FROM census_tracts ct
                WHERE ST_Within(
                    ST_Point({table}.lon, {table}.lat),
                    ST_GeomFromText(ct.geometry_wkt)
                )
                LIMIT 1
            )
            WHERE geoid IS NULL
        """)
        updated = con.execute(f"SELECT changes()").fetchone()[0]
        print(f"[spatial] assigned tracts to {updated} rows in {table}")
    finally:
        con.close()


def tract_bbox(geoid: str) -> Optional[tuple[float, float, float, float]]:
    """
    Return (min_lon, min_lat, max_lon, max_lat) bounding box for a tract.
    Used for Mapillary API queries.
    """
    con = get_conn()
    try:
        result = con.execute("""
            SELECT
                ST_XMin(ST_GeomFromText(geometry_wkt)) as min_lon,
                ST_YMin(ST_GeomFromText(geometry_wkt)) as min_lat,
                ST_XMax(ST_GeomFromText(geometry_wkt)) as max_lon,
                ST_YMax(ST_GeomFromText(geometry_wkt)) as max_lat
            FROM census_tracts
            WHERE geoid = ?
        """, [geoid]).fetchone()
        return tuple(result) if result else None
    finally:
        con.close()
