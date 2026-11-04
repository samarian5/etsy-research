#!/usr/bin/env python3
"""Keep the every-second-day refresh queue in sync with the keyword table.

The scheduled run (`.github/workflows/scrape.yml`, cron `10 4 */2 * *`) cannot read
this laptop's SQLite file, so the queue travels in the repository — **sealed**, with
the same `ETSY_ARTIFACT_KEY` as the artifacts. That file is the only thing the cron
needs; the keyword list never appears in the public tree.

  python scripts/publish_queue.py                 # re-seal from the DB, commit it
  python scripts/publish_queue.py --dry-run       # show what would be published
  python scripts/publish_queue.py --dispatch      # ...and start it now
  python scripts/publish_queue.py --max-depth 5   # lighter recurring pass
  python scripts/publish_queue.py --show          # decrypt and print the committed file

Run it whenever the keyword table changes; the UI calls `--dispatch` for you.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

import artifact_crypt as ac  # noqa: E402

QUEUE_FILE = "queue.sealed"
OWNER, REPO, BRANCH = "samarian5", "etsy-research", "main"


def build_queue(db: str, max_depth: int = 0, pages_per_run: int = 5,
                keyword: str | None = None) -> dict:
    """One item per enabled keyword, each starting at page 1.

    `max_depth` caps what a recurring pass reads — useful when the full-depth crawl
    is done and only the top of the results needs watching. 0 means "use the depth
    stored on the keyword", which is what the first pass uses.
    """
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    items = []
    for row in con.execute("select phrase, depth from keyword where enabled = 1 order by id"):
        if keyword and row["phrase"] != keyword:
            continue
        depth = int(row["depth"] or 20)
        if max_depth:
            depth = min(depth, max_depth)
        items.append({"keyword": row["phrase"], "depth": depth, "page": 1})
    con.close()
    return {"items": items, "pages_per_run": pages_per_run}


def _call(slug: str, payload: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        tmp = fh.name
    try:
        p = subprocess.run(["composio", "execute", slug, "-d", f"@{tmp}"],
                           capture_output=True, text=True, timeout=180)
    finally:
        os.unlink(tmp)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"successful": False, "error": (p.stdout or p.stderr)[:300]}
    if out.get("storedInFile"):
        out = json.load(open(out["outputFilePath"]))
    return out


def _remote_sha(path: str) -> str | None:
    r = _call("GITHUB_GET_REPOSITORY_CONTENT",
              {"owner": OWNER, "repo": REPO, "path": path, "ref": BRANCH})
    data = r.get("data") or {}
    if isinstance(data, dict) and data.get("sha"):
        return data["sha"]
    return None


def publish(queue: dict, dry_run: bool = False) -> dict:
    """Seal the queue and commit it. Returns the sealed text (never logged)."""
    sealed = ac.seal(json.dumps(queue))
    if dry_run:
        return {"sealed_bytes": len(sealed), "committed": False}
    payload = {"owner": OWNER, "repo": REPO, "path": QUEUE_FILE, "branch": BRANCH,
               "message": "chore: refresh the every-second-day crawl queue",
               "content": base64.b64encode(sealed.encode()).decode()}
    sha = _remote_sha(QUEUE_FILE)
    if sha:
        payload["sha"] = sha
    r = _call("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", payload)
    return {"sealed_bytes": len(sealed), "committed": bool(r.get("successful")),
            "error": r.get("error")}


def dispatch() -> None:
    """Start a run with NO queue input, so it exercises the scheduled path."""
    from app import github_actions as gh
    gh.dispatch_raw({"inputs": {"queue": "", "fast": "true", "profile": "warm"}})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("ETSY_DB", str(ROOT / "data" / "etsy.db")))
    ap.add_argument("--max-depth", type=int, default=0)
    ap.add_argument("--pages-per-run", type=int, default=5)
    ap.add_argument("--keyword")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dispatch", action="store_true")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if args.show:
        if not Path(QUEUE_FILE).exists():
            print("no local queue.sealed")
            return 1
        print(ac.unseal(Path(QUEUE_FILE).read_text()))
        return 0

    queue = build_queue(args.db, args.max_depth, args.pages_per_run, args.keyword)
    if not queue["items"]:
        print("no enabled keywords — nothing to publish")
        return 1
    print(f"queue: {len(queue['items'])} item(s), {queue['pages_per_run']} pages/run")
    for item in queue["items"]:
        print(f"   {item['keyword']!r} → page {item['page']}, depth {item['depth']}")

    res = publish(queue, args.dry_run)
    print(f"sealed {res['sealed_bytes']} bytes; committed={res.get('committed')}"
          + (f" error={res.get('error')}" if res.get("error") else ""))
    if not args.dry_run and not res.get("committed"):
        return 1
    if args.dispatch:
        dispatch()
        print("dispatched a run that reads queue.sealed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
