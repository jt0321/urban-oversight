"""
scoring/correlations.py — Pearson correlations between surveillance density and demographics

Computes pairwise correlations between composite_score and:
  median_income, poverty_rate, pct_white, pct_black,
  pct_hispanic, pct_asian, pct_renter

Results cached in DB as JSON. Surfaced via GET /correlations.
"""

import math
from datetime import datetime, timezone
from db.store import get_conn


def pearson_r(xs: list, ys: list) -> float | None:
    """Pearson correlation coefficient for two equal-length lists."""
    n = len(xs)
    if n < 3:
        return None

    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))

    if den_x == 0 or den_y == 0:
        return None

    return round(num / (den_x * den_y), 4)


DEMOGRAPHIC_FIELDS = [
    "median_income",
    "poverty_rate",
    "pct_white",
    "pct_black",
    "pct_hispanic",
    "pct_asian",
    "pct_renter",
]


def compute_correlations(vintage: int = 2022) -> dict:
    """
    Join latest tract scores with demographics and compute correlations.
    Returns dict of {field: {r, n, caveat}}.
    """
    con = get_conn()

    # Get latest score per tract
    rows = con.execute("""
        SELECT DISTINCT ON (ts.geoid)
            ts.geoid,
            ts.composite_score,
            td.median_income,
            td.poverty_rate,
            td.pct_white,
            td.pct_black,
            td.pct_hispanic,
            td.pct_asian,
            td.pct_renter,
            ts.source_coverage
        FROM tract_scores ts
        JOIN tract_demographics td ON ts.geoid = td.geoid AND td.vintage = ?
        WHERE ts.composite_score IS NOT NULL
        ORDER BY ts.geoid, ts.computed_at DESC
    """, [vintage]).fetchdf().to_dict(orient="records")
    con.close()

    scores = [r["composite_score"] for r in rows]
    results = {}

    for field in DEMOGRAPHIC_FIELDS:
        pairs = [
            (scores[i], rows[i][field])
            for i in range(len(rows))
            if rows[i][field] is not None
        ]
        if not pairs:
            results[field] = {"r": None, "n": 0, "caveat": "no data"}
            continue

        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        r = pearson_r(xs, ys)

        # Flag tracts with low source coverage — sparse data inflates correlations
        low_coverage = sum(1 for row in rows if row.get("source_coverage", 0) < 2)
        caveat = None
        if low_coverage / len(rows) > 0.3:
            caveat = f"{low_coverage} of {len(rows)} tracts have low source coverage — interpret with caution"

        results[field] = {
            "r": r,
            "n": len(pairs),
            "caveat": caveat,
        }

    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "vintage": vintage,
        "tract_count": len(rows),
        "correlations": results,
    }
