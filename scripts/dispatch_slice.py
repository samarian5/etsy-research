#!/usr/bin/env python3
"""Fan-out scheduler / load balancer for the supa-crawl worker pool.

The architecture has three parts:

    Postgres queue   claim_slice() with `for update skip locked` — the queue is
                     the coordinator; two workers never get the same keyword.
    worker slots     w1..wN, each an independent GitHub Actions job with its own
                     concurrency group, its own Chrome profile and its own
                     egress IP. That is what makes them parallel at all.
    this script      the load balancer. Reads queue_health() (budget left, how
                     many keywords are due, which slots are live) and dispatches
                     exactly as many slots as there is work for — no more, no
                     less. An idle tick dispatches nothing.

Why not "run everything at once": the budget is finite and Etsy's DataDome
refuses a browser after roughly 150 page loads in one session. Workers multiply
throughput only up to `max_pages_per_day`. Raise the budget to go faster; the
dispatcher will fill the slots to match.

    python scripts/dispatch_slice.py                 # scale out as the queue justifies
    python scripts/dispatch_slice.py --workers 4     # allow up to 4 slots
    python scripts/dispatch_slice.py --status        # show the decision, dispatch nothing
    python scripts/dispatch_slice.py --force --workers 1   # ignore due/budget (debug)

Exit code is 0 when the decision was made, even if nothing was dispatched.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

OWNER, REPO = "samarian5", "etsy-research"
WORKFLOW = "supa-crawl.yml"
STATE = os.path.join(tempfile.gettempdir(), "etsy-dispatch-state.json")
DISPATCH_COOLDOWN = 480          # seconds; a slot dispatched again sooner is skipped


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
            os.unlink(tmp)
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"error": (p.stdout or p.stderr)[:300]}


def health() -> dict:
    """The queue's own view of the world. Never guess from the clock."""
    from crawler.supa import Supa
    return Supa().queue_health()


def recently_dispatched() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:                                    # noqa: BLE001
        return {}


def mark_dispatched(slot: str) -> None:
    state = recently_dispatched()
    state[slot] = int(time.time())
    with open(STATE, "w") as fh:
        json.dump(state, fh)


def dispatch(slot: str, pages: str, fast: str, profile: str) -> bool:
    out = api(f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/dispatches", "POST", {
        "ref": "main",
        "inputs": {"pages": pages, "fast": fast, "profile": profile, "worker": slot},
    })
    if out.get("error") or out.get("message"):
        print(f"[dispatch] {slot} FAILED: {out}")
        return False
    mark_dispatched(slot)
    print(f"[dispatch] {slot} sent (pages={pages}, fast={fast}, profile={profile})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3, help="max parallel slots (default 3)")
    ap.add_argument("--pages", default="2")
    ap.add_argument("--fast", default="false")
    ap.add_argument("--profile", default="warm")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    try:
        h = health()
    except Exception as e:                               # noqa: BLE001
        # A database hiccup must not stop the crawl: dispatch one slot and let
        # the runner make the same decision with the same data.
        print(f"[dispatch] queue check failed ({type(e).__name__}) — dispatching w1 anyway")
        if args.status:
            return 0
        return 0 if dispatch("w1", args.pages, args.fast, args.profile) else 1

    left = int(h.get("left") or 0)
    due = int(h.get("due_keywords") or 0)
    live = [w for w in (h.get("busy_workers") or []) if w]
    pages = int(args.pages)
    capacity = left // max(1, pages)                     # slices the budget still allows
    slots = [f"w{i}" for i in range(1, max(1, args.workers) + 1)]
    state = recently_dispatched()
    now = int(time.time())
    free = [s for s in slots
            if s not in live and now - int(state.get(s, 0)) > DISPATCH_COOLDOWN]

    want = min(len(free), due, capacity)

    print(f"[dispatch] budget {h.get('used')}/{h.get('budget')} "
          f"(left {left}, {capacity} slices) · due {due} keywords · "
          f"live {live or 'none'} · slots {slots}")

    if args.force:
        want = 1
        free = ["w1"] if "w1" not in live else free[:1]
        print("[dispatch] --force: ignoring budget and due check")

    if want <= 0:
        if due == 0:
            print("[dispatch] skipped — nothing due")
        elif capacity <= 0:
            print("[dispatch] skipped — daily page budget spent")
        elif not free:
            print("[dispatch] skipped — every slot is busy or cooling down")
        return 0

    if args.status:
        print(f"[dispatch] would dispatch {want} slot(s): {free[:want]}")
        return 0

    ok = 0
    for slot in free[:want]:
        if dispatch(slot, args.pages, args.fast, args.profile):
            ok += 1
    print(f"[dispatch] {ok}/{want} dispatched")
    return 0 if ok == want else 1


if __name__ == "__main__":
    sys.exit(main())
