#!/usr/bin/env python3
"""Publish one file to the repository in seconds.

`scripts/deploy.py` mirrors ~80 files through the Git database API, which takes
about fifteen minutes — fine for a release, far too slow when what you changed
is one workflow and you want to test it now. This helper does the same job for a
single path with the Contents API.

    python scripts/push_file.py .github/workflows/supa-crawl.yml
    python scripts/push_file.py web/app.js --message "fix: drawer focus"
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNER, REPO = "samarian5", "etsy-research"
BRANCH = "main"


def api(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    cmd = ["composio", "proxy", f"https://api.github.com{path}", "--toolkit", "github"]
    tmp = None
    if method != "GET":
        cmd += ["-X", method]
        if payload is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(payload, fh)
                tmp = fh.name
            cmd += ["-H", "Content-Type: application/json", "-d", f"@{tmp}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"error": (p.stdout or p.stderr)[:400]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="repo-relative paths")
    ap.add_argument("--message", default=None)
    args = ap.parse_args()

    for rel in args.files:
        src = ROOT / rel
        if not src.exists():
            print(f"missing locally: {rel}")
            return 1
        content = base64.b64encode(src.read_bytes()).decode()
        cur = api(f"/repos/{OWNER}/{REPO}/contents/{rel}?ref={BRANCH}")
        sha = cur.get("sha")
        payload = {
            "message": args.message or f"chore: sync {rel}",
            "content": content,
            "branch": BRANCH,
        }
        if sha:
            payload["sha"] = sha
        out = api(f"/repos/{OWNER}/{REPO}/contents/{rel}", "PUT", payload)
        if out.get("error") or out.get("message"):
            print(f"FAILED {rel}: {out}")
            return 1
        commit = ((out.get("commit") or {}).get("sha") or "")[:7]
        print(f"pushed {rel} -> {commit} ({len(src.read_bytes())} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
