#!/usr/bin/env python3
"""Run exactly one chunk of a crawl queue, then say what should run next.

The queue is the durable unit of work, handed run-to-run as a workflow input:

    {"items": [{"keyword": "tshirt", "depth": 40, "page": 1},
               {"keyword": "mug",    "depth": 20}],
     "pages_per_run": 8}

Each invocation takes `items[0]`, crawls `page` .. `page + pages_per_run - 1`
(never past its depth), and writes `out/next_queue.json` with the remainder, so
the next workflow runs the next chunk. Nothing is carried but the queue itself,
so any interruption costs at most one chunk, and a keyword's depth is limited
only by how many chunks the chain runs — not by the 6-hour job cap.
"""
from __future__ import annotations

import json
import os

import artifact_crypt as ac
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def advance(item: dict, rest: list[dict], pages_per_run: int, page_to: int,
            rc: int, meta: dict, profile: str = "warm") -> tuple[dict | None, str]:
    """Decide the next queue from what just happened. Pure — unit tested.

    Returns (next_queue | None, reason).
    """
    depth = int(item.get("depth") or 20)
    stopped = meta.get("stopped_reason")
    done = int(meta.get("pages_done") or 0)

    # A failed run never advances: the same chunk is retried from where it began.
    if rc != 0:
        if stopped == "coverage":
            # Deeper pages yield nothing new — this keyword is finished, go on.
            nxt = ({"items": rest, "pages_per_run": pages_per_run, "profile": profile}
                   if rest else None)
            return nxt, "coverage-stop: keyword finished"
        if stopped == "budget" or stopped == "time":
            # Only the chunk's own budget ran out: resume at the first page that was
            # never crawled. Nothing was lost.
            resume = max(int(item.get("page") or 1), done + 1)
            if resume <= depth:
                return ({"items": [{**item, "page": resume}, *rest],
                         "pages_per_run": pages_per_run, "profile": profile},
                        f"budget-stop: resuming at page {resume}")
            return ({"items": rest, "pages_per_run": pages_per_run, "profile": profile}
                    if rest else None,
                    "budget-stop at final page: keyword finished")
        if stopped == "blocked":
            # A blocked chunk means a suspect session, not a dead end: retry the same
            # page range ONCE with a fresh profile, then stop rather than hammer.
            if int(item.get("attempts") or 0) < 1:
                retry = {**item, "attempts": int(item.get("attempts") or 0) + 1}
                return ({"items": [retry, *rest], "pages_per_run": pages_per_run,
                         "profile": "cold"},
                        "blocked: retrying this chunk once with a cold profile")
        return None, f"failed rc={rc} stopped_reason={stopped}"

    if page_to < depth:
        return ({"items": [{**item, "page": page_to + 1}, *rest],
                 "pages_per_run": pages_per_run, "profile": profile},
                f"slice done: resuming at page {page_to + 1}")
    return ({"items": rest, "pages_per_run": pages_per_run, "profile": profile}
            if rest else None,
            f"keyword '{item.get('keyword')}' complete at depth {depth}")


def main() -> int:
    raw = os.environ.get("QUEUE") or ""
    if not raw:
        print("[chunk] QUEUE is empty — nothing to do")
        return 0
    queue = json.loads(raw)
    items = queue.get("items") or []
    if not items:
        print("[chunk] queue exhausted — chain complete")
        return 0

    item = dict(items[0])
    rest = list(items[1:])
    pages_per_run = max(1, int(queue.get("pages_per_run") or 8))
    depth = max(1, int(item.get("depth") or 20))
    page_from = max(1, min(int(item.get("page") or 1), depth))
    page_to = min(page_from + pages_per_run - 1, depth)

    print(f"[chunk] {ac.kwtag(item['keyword'])}: pages {page_from}-{page_to} of {depth} "
          f"({len(rest)} item(s) queued behind it)", flush=True)

    out_dir = Path(os.environ.get("OUT_DIR") or "out")
    carried = [str(x) for x in (item.get("seen") or [])][-300:]
    argv = [
        "--keyword", item["keyword"],
        "--depth", str(depth),
        "--pages-from", str(page_from),
        "--pages-to", str(page_to),
        "--run-id", os.environ.get("GITHUB_RUN_ID") or str(int(time.time())),
        "--out", str(out_dir),
        "--user-data-dir", os.environ.get("PROFILE_DIR") or str(Path.home() / ".etsy-profile"),
    ]
    if carried:
        argv += ["--skip-ids", ",".join(carried)]
    if os.environ.get("BROWSER_BIN"):
        argv += ["--browser-executable-path", os.environ["BROWSER_BIN"]]
    if (os.environ.get("FAST") or "true").lower() == "true":
        argv.append("--fast")

    import etsy_crawl  # imported late so unit tests can import this file without seleniumbase

    sys.argv = ["etsy_crawl.py", *argv]
    rc = etsy_crawl.main()

    meta_path = out_dir / "run.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    # Carry this chunk's ids forward so the next chunk never re-visits them.
    fresh = []
    rp = out_dir / "results.jsonl"
    if rp.exists():
        for line in rp.read_text(encoding="utf-8").splitlines():
            try:
                lid = str(json.loads(line).get("listing_id") or "")
            except Exception:  # noqa: BLE001
                continue
            if lid:
                fresh.append(lid)
    item["seen"] = (carried + fresh)[-300:]

    nxt, reason = advance(item, rest, pages_per_run, page_to, rc, meta,
                          profile=(os.environ.get("PROFILE_MODE") or "warm"))
    print(f"[chunk] {reason}", flush=True)

    if nxt and nxt.get("items"):
        blob = json.dumps(nxt)
        if len(blob) > 30000:  # sealed+base64 grows ~35%, and inputs cap near 64 KB
            for it in nxt["items"]:
                if it.get("seen"):
                    it["seen"] = it["seen"][-80:]
            blob = json.dumps(nxt)
            print(f"[chunk] trimmed carried ids to fit input limit ({len(blob)} bytes)")
        (out_dir / "next_queue.json").write_text(blob, encoding="utf-8")
        print("[chunk] next: %d item(s), next page %s, %d bytes"
              % (len(nxt["items"]), nxt["items"][0].get("page") if nxt["items"] else None, len(blob)),
              flush=True)
    else:
        (out_dir / "chain_complete.json").write_text(json.dumps({
            "last_run": meta.get("run_id"), "keyword": item.get("keyword"),
            "reason": reason, "depth_reached": meta.get("pages_done"),
        }), encoding="utf-8")
        print("[chunk] chain complete", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
