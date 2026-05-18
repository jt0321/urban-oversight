"""
ingestion/census.py — Census TIGER geometries + ACS demographics

Two operations:
  1. load_tiger_geometries(county_fips)  — downloads tract shapefile, loads WKT to DB
  2. load_acs_demographics(county_fips, vintage)  — Census API → tract_demographics

Free Census API key: https://api.census.gov/data/key_signup.html
"""

import os
import uuid
import zipfile
import tempfile
import requests
import geopandas as gpd
from datetime import datetime, timezone
from db.store import get_conn

CENSUS_API_KEY = os.getenv("CENSUS_API_KEY", "")
STATE_FIPS = os.getenv("STATE_FIPS", "06")        # California
COUNTY_FIPS = os.getenv("TARGET_COUNTY", "06037") # LA County


def load_tiger_geometries(county_fips: str = COUNTY_FIPS) -> int:
    """
    Download Census TIGER tract shapefile for a county and load geometries into DB.
    Returns number of tracts loaded.

    TIGER URL pattern:
    https://www2.census.gov/geo/tiger/TIGER2022/TRACT/tl_2022_{state_fips}_tract.zip
    """
    state_fips = county_fips[:2]
    url = f"https://www2.census.gov/geo/tiger/TIGER2022/TRACT/tl_2022_{state_fips}_tract.zip"

    print(f"[census] downloading TIGER shapefile from {url}")
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "tract.zip")
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmpdir)

        shp_files = [f for f in os.listdir(tmpdir) if f.endswith(".shp")]
        if not shp_files:
            raise RuntimeError("No .shp file found in TIGER zip")

        gdf = gpd.read_file(os.path.join(tmpdir, shp_files[0]))

    # Filter to target county
    gdf = gdf[gdf["COUNTYFP"] == county_fips[2:]].copy()
    gdf = gdf.to_crs("EPSG:4326")  # ensure WGS84

    con = get_conn()
    count = 0
    for _, row in gdf.iterrows():
        geoid = row["GEOID"]
        area_sqkm = row.geometry.area * (111.32 ** 2)  # rough degree-to-km² at mid-latitudes
        wkt = row.geometry.wkt

        con.execute("""
            INSERT OR REPLACE INTO census_tracts
              (geoid, state_fips, county_fips, tract_name, area_sqkm, geometry_wkt)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            geoid, geoid[:2], geoid[:5],
            row.get("NAME", ""), area_sqkm, wkt
        ])
        count += 1

    con.close()
    print(f"[census] loaded {count} tract geometries for county {county_fips}")
    return count


def load_acs_demographics(county_fips: str = COUNTY_FIPS, vintage: int = 2022) -> int:
    """
    Pull ACS 5-year estimates for all tracts in a county.
    Variables:
      B01003_001E  total population
      B19013_001E  median household income
      B17001_002E  below poverty level
      B02001_002E  white alone
      B02001_003E  black alone
      B03001_003E  hispanic/latino
      B02001_005E  asian alone
      B25003_002E  owner occupied
      B25003_003E  renter occupied
    """
    if not CENSUS_API_KEY:
        raise RuntimeError("CENSUS_API_KEY not set")

    state_fips = county_fips[:2]
    county_only = county_fips[2:]

    variables = ",".join([
        "B01003_001E",  # total pop
        "B19013_001E",  # median income
        "B17001_002E",  # below poverty
        "B02001_002E",  # white
        "B02001_003E",  # black
        "B03001_003E",  # hispanic
        "B02001_005E",  # asian
        "B25003_002E",  # owner occupied
        "B25003_003E",  # renter occupied
    ])

    url = f"https://api.census.gov/data/{vintage}/acs/acs5"
    params = {
        "get": f"NAME,{variables}",
        "for": "tract:*",
        "in": f"state:{state_fips} county:{county_only}",
        "key": CENSUS_API_KEY,
    }

    print(f"[census] fetching ACS {vintage} for county {county_fips}")
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()

    data = r.json()
    headers = data[0]
    rows = data[1:]

    def idx(name):
        return headers.index(name)

    def safe_float(val, divisor=1.0):
        try:
            v = float(val)
            return None if v < 0 else v / divisor
        except (TypeError, ValueError):
            return None

    con = get_conn()
    count = 0

    for row in rows:
        state = row[idx("state")]
        county = row[idx("county")]
        tract = row[idx("tract")]
        geoid = f"{state}{county}{tract}"

        total_pop = safe_float(row[idx("B01003_001E")])
        median_income = safe_float(row[idx("B19013_001E")])
        below_poverty = safe_float(row[idx("B17001_002E")])
        white = safe_float(row[idx("B02001_002E")])
        black = safe_float(row[idx("B02001_003E")])
        hispanic = safe_float(row[idx("B03001_003E")])
        asian = safe_float(row[idx("B02001_005E")])
        owner = safe_float(row[idx("B25003_002E")])
        renter = safe_float(row[idx("B25003_003E")])

        poverty_rate = (below_poverty / total_pop) if total_pop and below_poverty else None
        pct_white = (white / total_pop) if total_pop and white else None
        pct_black = (black / total_pop) if total_pop and black else None
        pct_hispanic = (hispanic / total_pop) if total_pop and hispanic else None
        pct_asian = (asian / total_pop) if total_pop and asian else None
        total_housing = (owner or 0) + (renter or 0)
        pct_renter = (renter / total_housing) if total_housing and renter else None

        con.execute("""
            INSERT OR REPLACE INTO tract_demographics
              (geoid, vintage, total_pop, median_income, poverty_rate,
               pct_white, pct_black, pct_hispanic, pct_asian, pct_renter)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            geoid, vintage,
            int(total_pop) if total_pop else None,
            int(median_income) if median_income else None,
            poverty_rate, pct_white, pct_black, pct_hispanic, pct_asian, pct_renter,
        ])
        count += 1

    con.close()
    print(f"[census] loaded demographics for {count} tracts (vintage {vintage})")
    return count
