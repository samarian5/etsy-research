"""Read models for the dashboard. All SQL lives here."""
from __future__ import annotations

import sqlite3
from typing import Any

LATEST = """
WITH latest AS (
  -- LEAD over a newest-first partition gives the *previous* observation, which is
  -- what makes a bi-daily refresh useful: favorites gained since the last crawl.
  SELECT s.*,
         ROW_NUMBER() OVER (PARTITION BY s.listing_id ORDER BY s.captured_at DESC) AS rn,
         COUNT(*)     OVER (PARTITION BY s.listing_id) AS obs_count,
         LEAD(s.favorites) OVER (PARTITION BY s.listing_id ORDER BY s.captured_at DESC) AS fav_prev,
         LEAD(s.price_usd) OVER (PARTITION BY s.listing_id ORDER BY s.captured_at DESC) AS price_prev
  FROM snapshot s
),
occ AS (
  SELECT o.listing_id,
         MIN(o.page * 1000 + o.position) AS best_rank_raw,
         MIN(o.page)                    AS best_page,
         GROUP_CONCAT(DISTINCT k.phrase) AS keywords
  FROM occurrence o JOIN keyword k ON k.id = o.keyword_id
  GROUP BY o.listing_id
)
SELECT l.listing_id, l.url, l.shop_name, l.shop_url,
       s.title, s.thumbnail_url, s.price_usd, s.price_original_usd,
       s.price_min_usd, s.price_max_usd, s.is_on_sale, s.is_range,
       s.favorites, s.listing_review_count, s.listing_rating,
       s.shop_review_count, s.shop_rating, s.is_bestseller, s.is_star_seller,
       s.free_shipping, s.description_chars, s.captured_at, s.obs_count,
       o.best_page, o.keywords,
       CASE WHEN s.fav_prev IS NULL THEN NULL ELSE s.favorites - s.fav_prev END AS fav_delta,
       CASE WHEN s.price_prev IS NULL OR s.price_prev = 0 THEN NULL
            ELSE s.price_usd - s.price_prev END AS price_delta,
       (SELECT 1 FROM shortlist sh WHERE sh.listing_id = l.listing_id) AS shortlisted
FROM latest s
JOIN listing l ON l.listing_id = s.listing_id
LEFT JOIN occ o ON o.listing_id = s.listing_id
WHERE s.rn = 1
"""

SORTS = {
    "favorites": "COALESCE(favorites, -1) DESC",
    "price_asc": "COALESCE(price_usd, 9e9) ASC",
    "price_desc": "COALESCE(price_usd, -1) DESC",
    "reviews": "COALESCE(listing_review_count, -1) DESC",
    "rank": "COALESCE(best_page, 999) ASC, COALESCE(favorites, -1) DESC",
    "newest": "captured_at DESC",
    "momentum": "COALESCE(fav_delta, -9e9) DESC",
    "title": "title ASC",
}

# The "Status" dropdown. Each value is a predicate over the latest snapshot.
STATUS = {
    "on_sale": "s.is_on_sale = 1",
    "bestseller": "s.is_bestseller = 1",
    "star": "s.is_star_seller = 1",
    "free_shipping": "s.free_shipping = 1",
    "shortlisted": "l.listing_id IN (SELECT listing_id FROM shortlist)",
    "price_drop": "s.price_prev IS NOT NULL AND s.price_prev > 0 AND s.price_usd < s.price_prev",
    "rising": "s.fav_prev IS NOT NULL AND s.favorites > s.fav_prev",
    "no_description": "COALESCE(s.description_chars, 0) = 0",
}

PRICE_BANDS = {
    "": (None, None),
    "under15": (None, 15),
    "15to30": (15, 30),
    "30to60": (30, 60),
    "over60": (60, None),
}


def _filters(q: dict[str, Any]) -> tuple[str, list[Any]]:
    where, params = [], []
    if q.get("keyword"):
        where.append(
            "l.listing_id IN (SELECT o.listing_id FROM occurrence o JOIN keyword k "
            "ON k.id = o.keyword_id WHERE k.phrase = ?)")
        params.append(q["keyword"])
    if q.get("q"):
        where.append("l.listing_id IN (SELECT listing_id FROM listing_fts "
                     "WHERE listing_fts MATCH ?)")
        params.append(f'"{q["q"].replace(chr(34), "")}"*')
    if q.get("min_favorites"):
        where.append("COALESCE(s.favorites, 0) >= ?")
        params.append(int(q["min_favorites"]))
    if q.get("min_reviews"):
        where.append("COALESCE(s.listing_review_count, 0) >= ?")
        params.append(int(q["min_reviews"]))
    if q.get("min_price"):
        where.append("COALESCE(s.price_usd, 0) >= ?")
        params.append(float(q["min_price"]))
    if q.get("max_price"):
        where.append("COALESCE(s.price_usd, 9e9) <= ?")
        params.append(float(q["max_price"]))
    if q.get("band") in PRICE_BANDS and q["band"]:
        lo, hi = PRICE_BANDS[q["band"]]
        if lo is not None:
            where.append("COALESCE(s.price_usd, 0) >= ?")
            params.append(float(lo))
        if hi is not None:
            where.append("COALESCE(s.price_usd, 9e9) <= ?")
            params.append(float(hi))
    if q.get("status") in STATUS:
        where.append(STATUS[q["status"]])
    # legacy single-purpose params still honoured (bookmarks, CLI, tests)
    if q.get("on_sale"):
        where.append("s.is_on_sale = 1")
    if q.get("bestseller"):
        where.append("s.is_bestseller = 1")
    if q.get("shortlisted"):
        where.append("l.listing_id IN (SELECT listing_id FROM shortlist)")
    if q.get("seen_twice"):
        where.append("s.obs_count >= 2")
    return (" AND ".join(where), params)


def results(con: sqlite3.Connection, q: dict[str, Any], limit: int = 200,
            offset: int = 0) -> list[sqlite3.Row]:
    where, params = _filters(q)
    sql = f"{LATEST} {'AND ' + where if where else ''} ORDER BY {SORTS.get(q.get('sort'), SORTS['favorites'])} LIMIT ? OFFSET ?"
    return con.execute(sql, (*params, limit, offset)).fetchall()


def results_count(con: sqlite3.Connection, q: dict[str, Any]) -> int:
    where, params = _filters(q)
    sql = f"SELECT COUNT(*) FROM ({LATEST} {'AND ' + where if where else ''})"
    return con.execute(sql, params).fetchone()[0]


FACET_PREDS = {
    "on_sale": "is_on_sale = 1",
    "bestseller": "is_bestseller = 1",
    "star": "is_star_seller = 1",
    "free_shipping": "free_shipping = 1",
    "shortlisted": "shortlisted = 1",
    "price_drop": "price_delta IS NOT NULL AND price_delta < 0",
    "rising": "fav_delta IS NOT NULL AND fav_delta > 0",
    "seen_twice": "obs_count >= 2",
}


def facets(con: sqlite3.Connection) -> dict[str, Any]:
    """Counts behind each dropdown choice, so a filter never leads to a dead end.

    One pass over the latest-snapshot CTE: nine separate COUNT(*) queries cost
    ~0.8 s on 1.6k rows, this costs ~0.12 s.
    """
    out: dict[str, Any] = {"keywords": [], "counts": {}}
    for row in con.execute("""
            SELECT k.phrase, COUNT(DISTINCT o.listing_id) AS n
            FROM keyword k LEFT JOIN occurrence o ON o.keyword_id = k.id
            GROUP BY k.phrase ORDER BY k.phrase"""):
        out["keywords"].append({"phrase": row["phrase"], "n": row["n"]})
    sums = ", ".join(f"SUM(CASE WHEN {pred} THEN 1 ELSE 0 END) AS {name}"
                     for name, pred in FACET_PREDS.items())
    row = con.execute(f"SELECT {sums} FROM ({LATEST}) t").fetchone()
    out["counts"] = {name: (row[name] or 0) for name in FACET_PREDS}
    return out




def keywords(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("""
        SELECT k.*,
          (SELECT COUNT(*) FROM run r WHERE r.keyword_id = k.id) AS run_count,
          (SELECT status FROM run r WHERE r.keyword_id = k.id
             ORDER BY COALESCE(started_at, '') DESC LIMIT 1) AS last_status,
          (SELECT COALESCE(finished_at, started_at) FROM run r WHERE r.keyword_id = k.id
             ORDER BY COALESCE(started_at, '') DESC LIMIT 1) AS last_run_at,
          (SELECT unique_listings FROM run r WHERE r.keyword_id = k.id
             ORDER BY COALESCE(started_at, '') DESC LIMIT 1) AS last_listings,
          (SELECT COUNT(DISTINCT o.listing_id) FROM occurrence o WHERE o.keyword_id = k.id) AS total_listings
        FROM keyword k ORDER BY k.phrase
    """).fetchall()


def runs(con: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return con.execute("""
        SELECT r.*, k.phrase FROM run r JOIN keyword k ON k.id = r.keyword_id
        ORDER BY COALESCE(r.started_at, '') DESC LIMIT ?""", (limit,)).fetchall()


def listing_detail(con: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None:
    return con.execute(f"{LATEST} AND s.listing_id = ?", (listing_id,)).fetchone()


def price_history(con: sqlite3.Connection, listing_id: str, limit: int = 60) -> list[sqlite3.Row]:
    return con.execute("""
        SELECT captured_at, price_usd, favorites, listing_review_count
        FROM snapshot WHERE listing_id = ? ORDER BY captured_at ASC LIMIT ?""",
        (listing_id, limit)).fetchall()


def stats(con: sqlite3.Connection) -> dict:
    row = con.execute("""
        SELECT (SELECT COUNT(*) FROM listing)  AS listings,
               (SELECT COUNT(*) FROM snapshot) AS snapshots,
               (SELECT COUNT(*) FROM occurrence) AS occurrences,
               (SELECT COUNT(*) FROM run)      AS runs,
               (SELECT COUNT(*) FROM shortlist) AS shortlisted,
               (SELECT MAX(captured_at) FROM snapshot) AS last_capture
    """).fetchone()
    return dict(row)
