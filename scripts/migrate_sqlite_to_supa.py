#!/usr/bin/env python3
"""Copy the local SQLite store into Supabase, oldest observation first.

Order matters: ingest_batch copies the *previous* value into prev_*, so replaying
history chronologically leaves each listing's latest state correct and its deltas
meaningful. Run once; `ingest_batch` is idempotent per (etsy_id, run_id).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawler"))

from supa import Supa  # noqa: E402

DB = ROOT / "data" / "etsy.db"


def rows(con) -> list[dict]:
    con.row_factory = sqlite3.Row
    cur = con.execute("""
        select s.listing_id, l.url, s.title, l.shop_name, l.shop_url,
               s.price_usd, s.currency, s.favorites, s.listing_review_count,
               s.listing_rating, s.thumbnail_url, s.category_path, s.description,
               s.is_bestseller, s.is_star_seller, s.free_shipping, s.captured_at,
               k.phrase as keyword
          from snapshot s
          join listing l on l.listing_id = s.listing_id
          left join run r on r.run_id = s.run_id
          left join keyword k on k.id = r.keyword_id
         order by s.captured_at asc, s.listing_id asc
    """)
    out = []
    for r in cur:
        d = dict(r)
        d["is_bestseller"] = bool(d["is_bestseller"])
        d["is_star_seller"] = bool(d["is_star_seller"])
        d["free_shipping"] = bool(d["free_shipping"])
        out.append(d)
    return out


def main() -> int:
    if not DB.exists():
        print(f"no database at {DB}")
        return 1
    con = sqlite3.connect(DB)
    batch_rows = rows(con)
    print(f"read {len(batch_rows)} observations from {DB}")

    supa = Supa()
    total = {"rows": 0, "snapshots": 0}
    # A pseudo run per calendar day keeps (etsy_id, run_id) unique without
    # inventing a run row for every historical crawl.
    for i in range(0, len(batch_rows), 25):
        chunk = batch_rows[i:i + 25]
        day = (chunk[0].get("captured_at") or "2026-01-01")[:10]
        res = supa.ingest(chunk, f"sqlite-{day}", chunk[0].get("keyword") or "legacy")
        total["rows"] += res["rows"]
        total["snapshots"] += res["snapshots"]
        if (i // 25) % 10 == 0:
            print(f"  {i + len(chunk)}/{len(batch_rows)} …", flush=True)

    print(f"done: {total}")
    print("stats:", supa._call("GET", "/rest/v1/stats?select=*"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
