#!/usr/bin/env python3
"""Push the crawler bundle to GitHub via Composio (no PAT needed).

  python scripts/deploy.py            # upload/update the file list
  python scripts/deploy.py --check    # show which files differ from the remote
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNER, REPO = "samarian5", "etsy-research"
BRANCH = "main"

FILES = [
    ".github/workflows/scrape.yml",
    ".github/workflows/supa-crawl.yml",
    "crawler/etsy_crawl.py",
    "crawler/run_chunk.py",
    "crawler/artifact_crypt.py",
    "crawler/supa.py",
    "scripts/supa_run.py",
    "scripts/migrate_sqlite_to_supa.py",
    "scripts/dispatch_slice.py",
    "scripts/plan_due.py",
    "scripts/deploy_root_redirect.py",
    "scripts/deploy_pages.py",
    "db/supabase/0001_init.sql",
    "db/supabase/0002_fav_gain.sql",
    "db/supabase/0003_daily_budget.sql",
    "db/supabase/0004_clean_status.sql",
    "db/supabase/0005_schedule.sql",
    "db/supabase/0006_fix_update_keyword.sql",
    "db/supabase/0007_verify_token.sql",
    "db/supabase/0008_owner_auth.sql",
    "db/supabase/0009_workers.sql",
    "db/supabase/0010_signal.sql",
    "db/supabase/0011_repair_badges.sql",
    "db/supabase/0012_time_signals.sql",
    "db/supabase/0013_slice_gap.sql",
    "db/supabase/0014_enrich_queue.sql",
    "db/supabase/0015_stale_claims.sql",
    "db/supabase/0016_age_guard.sql",
    "db/supabase/0017_design_briefs.sql",
    "db/supabase/0018_design_matview.sql",
    "db/supabase/0019_design_library.sql",
    "db/supabase/0020_ingest_gallery.sql",
    "db/supabase/0021_case_briefs.sql",
    "db/supabase/0022_claim_dna_scope.sql",
    "db/supabase/0023_case_niches_widen.sql",
    "db/supabase/0024_signals_matview.sql",
    "docs/adr/0010-parallel-worker-pool.md",
    "README.md",
    "web/index.html",
    "web/app.js",
    "web/styles.css",
    "web/config.js",
    "app/__init__.py",
    "app/text.py",
    "app/queries.py",
    "app/main.py",
    "app/github_actions.py",
    "app/templates/base.html",
    "app/templates/results.html",
    "app/templates/_cards.html",
    "app/templates/_star.html",
    "app/templates/_listing.html",
    "app/templates/keywords.html",
    "app/static/app.css",
    "tests/test_parsers.py",
    "tests/test_real_listing.py",
    "tests/test_queue.py",
    "tests/test_signal.py",
    "tests/fixtures/detail_with_badges.html.gz",
    "tests/fixtures/detail_no_star.html.gz",
    "tests/fixtures/search_page.html.gz",
    "tests/test_artifact_crypt.py",
    "tests/test_watchdog.py",
    "scripts/publish_queue.py",
    "tests/fixtures/real/listing_1679237029.html.gz",
    "requirements.txt",
    "requirements-crawler.txt",
    "DEPLOY.md",
]

# Ship every test and fixture. The list above used to be the only way a test
# reached main, and a stale tests/test_supa.py sat up there asserting the old
# ingest contract — CI failed on code that was already correct and the crawl
# refused to start. A directory walk cannot go stale.
FILES += sorted(
    str(f.relative_to(ROOT))
    for f in (ROOT / "tests").rglob("*")
    if f.is_file() and "__pycache__" not in f.parts
)
FILES = list(dict.fromkeys(FILES))


def validate_workflow(path: "Path") -> None:
    """Refuse to push a workflow GitHub cannot parse.

    A heredoc body left at column 0 breaks the YAML silently: GitHub then creates a
    run per dispatch that fails in 5 seconds with zero jobs, and the chain dispatches
    another. Measured 2026-11-04: eight dead runs in thirty seconds before it was
    spotted. Parsing locally takes a millisecond.
    """
    import yaml
    text = path.read_text()
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or "jobs" not in doc:
        raise SystemExit(f"refusing to deploy {path}: no jobs key")
    for job, body in (doc["jobs"] or {}).items():
        if not isinstance(body, dict) or "steps" not in body:
            raise SystemExit(f"refusing to deploy {path}: job {job} has no steps")
        for step in body["steps"]:
            if not isinstance(step, dict):
                raise SystemExit(f"refusing to deploy {path}: malformed step in {job}")
    print(f"validated {path}: {len(doc['jobs'])} job(s), "
          f"{sum(len(j['steps']) for j in doc['jobs'].values())} steps")


def call(slug: str, payload: dict) -> dict:
    """composio caps a single argv entry at 128 KB, so pass the JSON via @file."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        tmp = fh.name
    try:
        p = subprocess.run(
            ["composio", "execute", slug, "-d", f"@{tmp}"],
            capture_output=True, text=True, timeout=180,
        )
    finally:
        os.unlink(tmp)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"successful": False, "error": (p.stdout or p.stderr)[:400]}


def remote_sha(path: str) -> str | None:
    r = call("GITHUB_GET_REPOSITORY_CONTENT",
             {"owner": OWNER, "repo": REPO, "path": path, "ref": BRANCH})
    data = r.get("data") or {}
    if isinstance(data, dict):
        return data.get("sha")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    failures = 0
    for rel in FILES:
        src = ROOT / rel
        if rel.endswith((".yml", ".yaml")):
            validate_workflow(src)
        if not src.exists():
            print(f"SKIP {rel} (missing locally)")
            continue
        sha = remote_sha(rel)
        payload = {
            "owner": OWNER, "repo": REPO, "path": rel, "branch": BRANCH,
            "message": f"chore: sync {rel}",
            "content": base64.b64encode(src.read_bytes()).decode(),
        }
        if sha:
            payload["sha"] = sha
        if args.check:
            print(f"{'update' if sha else 'create':6} {rel}")
            continue
        r = call("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", payload)
        ok = r.get("successful")
        commit = ((r.get("data") or {}).get("commit") or {}).get("sha", "")
        print(f"{'OK  ' if ok else 'FAIL'} {rel} {commit[:8]}"
              + ("" if ok else f"  -> {str(r.get('error'))[:160]}"))
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
