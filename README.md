# Urban Oversight

A civic data pipeline that maps surveillance infrastructure density across census tracts and correlates it with demographic data.

Pulls from four public sources — EFF Atlas, OpenStreetMap, LA City open data, and Mapillary street imagery — deduplicates across them, and uses Claude vision to detect additional cameras from street-level photos. Scores each tract on a composite density index, then exposes everything through a queryable API.

---

## What It Produces

For every census tract in a target county:

- **Surveillance point inventory** — cameras, ALPRs, red-light cameras, gunshot detectors, drones, and more, each tagged with source, type, confidence, and detection method
- **Composite density score** — documented density + vision-detected density + source coverage, weighted and normalized to 0–10
- **Demographic correlations** — Pearson r between the density score and median income, poverty rate, and racial/ethnic composition

---

## Data Sources

| Source | What it provides | Key |
|---|---|---|
| Census TIGER | Tract geometries (WKT polygons) | Free |
| Census ACS 5-year | Demographics per tract (income, poverty, race, renter rate) | Free (sign up) |
| EFF Atlas of Surveillance | Documented surveillance deployments by agency | CSV download |
| OpenStreetMap Overpass | `man_made=surveillance` nodes | None needed |
| LA City open data | Traffic cameras, red-light cameras | None needed |
| Mapillary | Street-level imagery for vision analysis | Free token |

---

## Architecture

```
pipeline/runner.py
    ├── ingestion/census.py          TIGER geometries + ACS demographics
    ├── ingestion/eff_atlas.py       EFF Atlas CSV → surveillance_points
    ├── ingestion/osm.py             Overpass API → surveillance_points
    ├── ingestion/city_portals/la.py LA Socrata open data → surveillance_points
    ├── ingestion/mapillary.py       Street imagery metadata → mapillary_images
    ├── detection/deduplication.py   Cross-source merge (25m threshold)
    ├── detection/vision.py          Claude vision → surveillance_points
    └── scoring/density.py           Per-tract composite score

scoring/correlations.py              Pearson r: score × demographics
db/store.py                          DuckDB schema + init
db/spatial.py                        ST_Within tract assignment
api/main.py                          FastAPI surface
```

---

## Scoring

```
composite_score = known_density    × 0.5
               + detected_density  × 0.3
               + source_coverage   × 0.2
```

- **known_density** — documented points (OSM + EFF + city portals) per km²
- **detected_density** — Claude-detected items per Mapillary image analyzed
- **source_coverage** — number of distinct sources contributing to the tract (0–4)

All raw components are stored separately. The composite is one query on top of them.

---

## API

```
GET  /                              health check
GET  /tracts/{geoid}                demographics + latest score + source breakdown
GET  /tracts/{geoid}/points         surveillance points (filter by source, type, verified)
GET  /tracts/{geoid}/images         Mapillary images + vision analysis results
GET  /scores                        all tract scores (filter by county, min_score)
GET  /correlations                  Pearson r between score and demographics
GET  /ingest/log                    ingestion run history
POST /pipeline/run                  trigger full pipeline (flags: skip_census_geo, skip_vision)
POST /pipeline/analyze/{geoid}      run vision pass for one tract, then re-score
```

### Example: tract detail

```json
GET /tracts/06037204600

{
  "geoid": "06037204600",
  "tract": { "tract_name": "2046", "area_sqkm": 1.83, ... },
  "demographics": {
    "median_income": 42100,
    "poverty_rate": 0.24,
    "pct_black": 0.31,
    "pct_hispanic": 0.47
  },
  "surveillance": {
    "composite_score": 7.2,
    "known_density": 5.46,
    "detected_density": 1.3,
    "source_coverage": 3,
    "point_count": 10,
    "images_analyzed": 20
  },
  "sources": [
    { "source": "city_la", "count": 6, "types": 2 },
    { "source": "osm",     "count": 3, "types": 1 },
    { "source": "eff",     "count": 1, "types": 1 }
  ]
}
```

---

## Setup

**Dependencies:**
```bash
pip install -r requirements.txt
cp .env.example .env
# fill in keys (see table below)
```

**EFF Atlas CSV** (not auto-downloaded — requires manual export):
Download from [atlasofsurveillance.org](https://atlasofsurveillance.org/) and place at `data/eff_atlas.csv`.

**Initialize DB and run:**
```bash
# Initialize schema
python -c "from db.store import init_db; init_db()"

# Full pipeline run (census geo only needed once)
python -m pipeline.runner

# Or via API
uvicorn api.main:app --reload
# POST /pipeline/run
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | required | Claude API key (used for vision analysis) |
| `CENSUS_API_KEY` | required | census.gov API key — [sign up here](https://api.census.gov/data/key_signup.html) |
| `MAPILLARY_TOKEN` | required | Mapillary developer token |
| `DB_PATH` | `urban_oversight.duckdb` | DuckDB file path |
| `TARGET_COUNTY` | `06037` | County FIPS code (default: LA County) |
| `PIPELINE_INTERVAL_HOURS` | `24` | Scheduler run interval |
| `IMAGES_PER_TRACT` | `20` | Max Mapillary images sampled per tract |
| `EFF_CSV_PATH` | `data/eff_atlas.csv` | Path to EFF Atlas CSV |

---

## Stack

| Layer | Technology |
|---|---|
| Storage | DuckDB + spatial extension |
| Spatial | GeoPandas, Shapely, PyProj |
| Vision | Anthropic Claude SDK |
| API | FastAPI |
| HTTP | httpx, requests |
| Runtime | Python 3.11+ |

---

## Notes

**Why DuckDB?** The spatial extension handles `ST_Within` tract assignments and bounding box queries without standing up PostGIS. Swappable for a proper spatial database if the dataset grows.

**Why not LLM for scoring?** The composite score formula is deterministic and auditable — weights are explicit and the components are stored separately. The model earns its place in vision detection, where rule-based logic can't read a photo.

**Deduplication:** When two sources report a point within 25 meters of each other, the higher-priority source wins (`city_la > eff > osm > mapillary_detected`). The lower-priority record is flagged rather than deleted.

**Source coverage caveat:** Tracts with fewer than two contributing sources will have unreliable scores. The `/correlations` endpoint flags this when more than 30% of tracts fall below that threshold.
