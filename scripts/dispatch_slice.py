#!/usr/bin/env python3
"""Dispatch one supa-crawl slice through Composio — the scheduler that always works.

GitHub's own cron never fired for this repository (0 `schedule`-event runs across
its whole history, including a bi-daily cron that had been live for a day), so the
2-hour cadence needs a trigger that does not depend on GitHub's scheduler. This is
that trigger, meant for a systemd timer on an always-on machine:

    */7 1-23/2 * * *   →  dispatch  →  GitHub runner crawls  →  rows land in Supabase

It refuses to dispatch when a slice is already running or queued, so the single
flight guarantee holds even if the timer fires while a slow run is finishing.

  python scripts/dispatch_slice.py            # dispatch if idle
  python scripts/dispatch_slice.py --force    # dispatch regardless
  python scripts/dispatch_slice.py --status   # what would happen, change nothing
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

OWNER, REPO = "samarian5", "etsy-research"
WORKFLOW = "supa-crawl.yml"


def api(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    import tempfile, os
    cmd = ["composio", "proxy", f"https://api.github.com{path}", "--toolkit", "github"]
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
        if method != "GET" and payload is not None:
            os.unlink(tmp)
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"error": (p.stdout or p.stderr)[:300]}


def busy() -> tuple[bool, str]:
    runs = api(f"/repos/{OWNER}/{REPO}/actions/runs?per_page=10").get("workflow_runs", [])
    for r in runs:
        if r.get("name") != "supa-crawl":
            continue
        if r.get("status") in ("queued", "in_progress", "waiting", "pending"):
            return True, f"{r['id']} is {r['status']} (started {r['created_at']})"
    return False, "idle"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--pages", default="2")
    ap.add_argument("--fast", default="false")
    ap.add_argument("--profile", default="warm")
    args = ap.parse_args()

    is_busy, why = busy()
    if is_busy and not args.force:
        print(f"[dispatch] skipped — {why}")
        return 0
    print(f"[dispatch] {why}")

    if args.status:
        return 0

    out = api(f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/dispatches", "POST", {
        "ref": "main",
        "inputs": {"pages": args.pages, "fast": args.fast, "profile": args.profile},
    })
    if out.get("error") or out.get("message"):
        print(f"[dispatch] FAILED: {out}")
        return 1
    print(f"[dispatch] sent {WORKFLOW} (pages={args.pages}, fast={args.fast}, profile={args.profile})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
