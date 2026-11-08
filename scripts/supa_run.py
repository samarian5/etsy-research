#!/usr/bin/env python3
"""Run one slice of the Etsy queue, sourced from and written to Supabase.

Replaces the sealed-queue + artifact chain with a database round trip:

    claim_slice()  →  crawl 2 pages  →  ingest_batch()  →  finish_slice()

Every two hours a fresh runner asks the database "what needs doing?", does that
one thing, writes the rows, and exits. Nothing is carried between runs except
the queue's own `next_page`, so a dead runner costs one slice, not a pass.

Exit codes: 0 slice done (or queue empty), 1 crawl failed — the slice is left
un-advanced so the next run retries it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawler"))

from supa import Supa, SupaError  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def egress_ip() -> str | None:
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=15) as r:
            return r.read().decode().strip()
    except Exception:
        return None


def keyword_label(keyword: str) -> str:
    """A public repo publishes its logs; the keyword list is the research."""
    if os.environ.get("REDACT_KEYWORDS") == "1":
        from artifact_crypt import kwtag
        return kwtag(keyword or "")
    return keyword or ""


def crawl(slice_: dict, run_id: str, enrich: list[dict] | None = None) -> tuple[int, dict, list[dict]]:
    kw = slice_["keyword"]
    pf, pt = int(slice_["page_from"]), int(slice_["page_to"])
    depth = int(slice_["depth"])

    cmd = [sys.executable, str(ROOT / "crawler" / "etsy_crawl.py"),
           "--keyword", kw, "--depth", str(depth),
           "--pages-from", str(pf), "--pages-to", str(pt),
           "--out", str(ROOT / "out"), "--run-id", run_id]
    if (os.environ.get("FAST") or "false").lower() in ("1", "true"):
        cmd.append("--fast")
    browser = os.environ.get("BROWSER_BIN")
    if browser and Path(browser).exists():
        cmd += ["--browser-executable-path", browser]
    if enrich:
        # Handed to the crawler as a file, not a flag: the string form of 60 URLs
        # would blow past the command-line limit eventually.
        path = ROOT / "out" / "enrich.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(enrich))
        cmd += ["--enrich-file", str(path)]

    log(f"[slice] {keyword_label(kw)} pages {pf}-{pt} of {depth} (run {run_id})")
    rc = subprocess.call(cmd, cwd=str(ROOT))

    meta_path = ROOT / "out" / "run.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    rows: list[dict] = []
    results = ROOT / "out" / "results.jsonl"
    if results.exists():
        with results.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        log("[slice] skipping a truncated results line")
    return rc, meta, rows


def consecutive_failures(supa: Supa, keyword: str) -> int:
    """How many of the most recent slices failed, counting back from now.

    Counting any failure in a window would park a keyword because of a block two
    days ago; only an unbroken streak means the site is refusing this keyword.
    """
    try:
        rows = supa._call(
            "GET",
            "/rest/v1/runs?select=status&keyword=eq."
            f"{_quote(keyword)}&order=started_at.desc&limit=6")
    except SupaError:
        return 0
    streak = 0
    for r in rows or []:
        if (r or {}).get("status") in ("aborted", "failed"):
            streak += 1
        else:
            break
    return streak


def _quote(v: str) -> str:
    import urllib.parse
    return urllib.parse.quote(v)


def main() -> int:
    supa = Supa()
    pages = int(os.environ["PAGES_PER_RUN"]) if os.environ.get("PAGES_PER_RUN") else None

    slice_ = supa.claim_slice(pages)
    if not slice_:
        log("[queue] nothing to do — every keyword is paused, done, or the queue is empty")
        return 0

    kw = slice_["keyword"]
    pf, pt = int(slice_["page_from"]), int(slice_["page_to"])
    worker = os.environ.get("WORKER") or "w1"
    # Every shard in a matrix run shares GITHUB_RUN_ID, so the raw value collides
    # on runs.run_id and the second shard dies on insert. The slot is what makes
    # a run unique.
    run_id = (os.environ.get("GITHUB_RUN_ID") or f"local-{int(time.time())}") + f"-{worker}"
    gha_url = None
    if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY"):
        gha_url = (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}"
                   f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")

    ip = egress_ip()
    log(f"[net] egress ip {ip}")
    log(f"[worker] slot {worker} (run {run_id})")
    supa.start_run(run_id, kw, pf, pt, egress_ip=ip, gha_url=gha_url, worker=worker)

    enrich: list[dict] = []
    enrich_n = int(os.environ.get("ENRICH_PER_RUN") or "0")
    if enrich_n:
        try:
            enrich = supa.claim_enrich(enrich_n)
            log(f"[enrich] claimed {len(enrich)} listing(s) with no publish date yet")
        except Exception as e:                              # noqa: BLE001
            log(f"[enrich] claim failed: {type(e).__name__}: {e}")

    try:
        rc, meta, rows = crawl(slice_, run_id, enrich)
    except Exception as e:  # a crash before rows exist still has to close the run
        log(f"[fatal] {type(e).__name__}: {e}")
        supa.finish_run(run_id, "failed", note=f"{type(e).__name__}: {e}"[:400])
        supa.finish_slice(kw, pf)
        return 1

    # Related-search chips Etsy printed alongside the results: free keyword research,
    # collected on a page we already downloaded.
    related = meta.get("related") or []
    if related:
        try:
            n = supa.note_candidates(related)
            log(f"[keywords] stored {n} related-search suggestions")
        except Exception as e:                              # noqa: BLE001
            log(f"[keywords] candidate store failed: {type(e).__name__}: {e}")

    status = meta.get("status") or ("failed" if rc else "succeeded")
    stopped = meta.get("stopped_reason")
    blocked = status != "succeeded"

    written = {"rows": 0, "snapshots": 0}
    if rows:
        try:
            written = supa.ingest(rows, run_id, kw)
        except SupaError as e:
            log(f"[fatal] ingest failed: {e}")
            supa.finish_run(run_id, "failed", listings=len(rows),
                            note=f"ingest failed: {str(e)[:300]}")
            supa.finish_slice(kw, pf)
            return 1

    # Advance only on a clean slice: a blocked page must be retried, not skipped,
    # or the pass silently develops holes.
    next_page = pf if blocked else pt + 1
    kw_status = None
    if blocked:
        # This run is not written yet, so the streak counts it plus the ones before.
        streak = 1 + consecutive_failures(supa, kw)
        if streak >= 3:
            kw_status = "blocked"
            log(f"[queue] {streak} failed slices in a row — parking this keyword "
                f"until it is resumed by hand")

    supa.finish_slice(kw, next_page, status=kw_status, listings=len(rows))
    supa.finish_run(run_id, status, listings=len(rows), rows=written["rows"],
                    challenges=int(meta.get("challenges") or 0),
                    stopped_reason=stopped,
                    note=meta.get("error"))

    log(f"[done] {keyword_label(kw)} pages {pf}-{pt}: status={status} "
        f"rows={len(rows)} written={written['rows']} snapshots={written['snapshots']} "
        f"challenges={meta.get('challenges')} next_page={next_page}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### {keyword_label(kw)} — pages {pf}–{pt}\n\n"
                     f"| status | rows | snapshots | challenges | next page |\n"
                     f"|---|---|---|---|---|\n"
                     f"| {status} | {len(rows)} | {written['snapshots']} | "
                     f"{meta.get('challenges')} | {next_page} |\n")

    if os.environ.get("PRUNE") == "1":
        try:
            log(f"[maint] pruned {supa.prune(7)} old snapshot(s)")
        except SupaError as e:
            log(f"[maint] prune skipped: {e}")

    return 0 if not blocked else 1


if __name__ == "__main__":
    sys.exit(main())
