#!/usr/bin/env python3
"""Report how much of each keyword's search range is actually held.

Two different questions, and they need different answers:

* **Discovered** — did we read search page N and see the cards on it?
* **Detailed** — of the listings seen on page N, how many did we actually visit?

Counting only the first is how a crawler lies to itself: a chunk can record
"3 pages done" and still have visited none of the listings on them (that happened
on 2026-11-04, and the earlier manifest-based version of this script called the
keyword complete while pages 4-8 had never been fetched). Occurrences answer the
first question, snapshots answer the second, and both come from rows rather than
from run metadata.

  python scripts/coverage.py                     # report
  python scripts/coverage.py --keyword tshirt
  python scripts/coverage.py --repair            # print a queue for the first gap
  python scripts/coverage.py --repair --dispatch # dispatch it now
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# A page counts as held when this share of its listings has been visited.
DETAIL_TARGET = 0.90


def audit(db: str, keyword: str | None = None, depth: int | None = None) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out: dict[str, dict] = {}

    for kw in con.execute("select id, phrase, depth from keyword").fetchall():
        if keyword and kw["phrase"] != keyword:
            continue
        target = int(depth or kw["depth"] or 20)
        pages: dict[int, dict] = {}
        rows = con.execute(
            """SELECT o.page,
                      COUNT(DISTINCT o.listing_id) AS discovered,
                      COUNT(DISTINCT s.listing_id) AS detailed
                 FROM occurrence o
                 LEFT JOIN snapshot s ON s.listing_id = o.listing_id
                WHERE o.keyword_id = ?
                GROUP BY o.page ORDER BY o.page""", (kw["id"],)).fetchall()
        for r in rows:
            if r["page"] is None:
                continue
            pages[int(r["page"])] = {"discovered": r["discovered"], "detailed": r["detailed"]}

        held, thin, missing = [], [], []
        for page in range(1, target + 1):
            info = pages.get(page)
            if not info or not info["discovered"]:
                missing.append(page)
            elif info["detailed"] / info["discovered"] >= DETAIL_TARGET:
                held.append(page)
            else:
                thin.append(page)
        out[kw["phrase"]] = {
            "depth": target, "held": held, "thin": thin, "missing": missing, "pages": pages,
            "listings": con.execute(
                """SELECT COUNT(DISTINCT o.listing_id) FROM occurrence o
                    WHERE o.keyword_id = ?""", (kw["id"],)).fetchone()[0],
            "detailed_listings": con.execute(
                """SELECT COUNT(DISTINCT o.listing_id) FROM occurrence o
                     JOIN snapshot s ON s.listing_id = o.listing_id
                    WHERE o.keyword_id = ?""", (kw["id"],)).fetchone()[0],
        }
    con.close()
    return out


def _ranges(pages: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for p in sorted(pages):
        if out and p == out[-1][1] + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def repair_queue(report: dict, pages_per_run: int = 3) -> dict:
    """One item: the first keyword with a gap, restarting at its first bad page."""
    for phrase, e in report.items():
        gaps = sorted(set(e["missing"]) | set(e["thin"]))
        if not gaps:
            continue
        return {"items": [{"keyword": phrase, "depth": e["depth"], "page": gaps[0]}],
                "pages_per_run": pages_per_run}
    return {"items": [], "pages_per_run": pages_per_run}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("ETSY_DB", str(ROOT / "data" / "etsy.db")))
    ap.add_argument("--keyword")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--dispatch", action="store_true")
    ap.add_argument("--pages-per-run", type=int, default=3)
    args = ap.parse_args()

    report = audit(args.db, args.keyword, args.depth)
    for phrase, e in report.items():
        gaps = sorted(set(e["missing"]) | set(e["thin"]))
        pct = 100.0 * len(e["held"]) / max(1, e["depth"])
        print(f"{phrase}: {len(e['held'])}/{e['depth']} pages held ({pct:.0f}%) | "
              f"{e['detailed_listings']}/{e['listings']} listings detailed")
        if e["missing"]:
            print("   never fetched: " + ", ".join(
                f"{a}" if a == b else f"{a}-{b}" for a, b in _ranges(e["missing"])))
        if e["thin"]:
            print("   thin (<90% visited): " + ", ".join(
                f"p{p} {e['pages'][p]['detailed']}/{e['pages'][p]['discovered']}"
                for p in e["thin"][:8]))
        if not gaps:
            print("   complete")

    if not args.repair:
        return 0
    queue = repair_queue(report, args.pages_per_run)
    if not queue["items"]:
        print("\nnothing to repair")
        return 0
    print("\nrepair queue: " + json.dumps(queue))
    if args.dispatch:
        from app import github_actions as gh
        gh.dispatch_queue(queue)
        print("dispatched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
