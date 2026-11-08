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
    python scripts/dispatch_slice.py --shards 8      # allow up to 8 parallel workers
    python scripts/dispatch_slice.py --shards 20     # ceiling: 20 workers in one run
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


def dispatch(slots: list[str], pages: str, fast: str, profile: str,
             filter_mode: str = "none") -> bool:
    """One run, N shards.

    A matrix job starts N separate GitHub runners — separate IPs, separate
    Chrome profiles, separate concurrency groups — from a single dispatch call.
    That is why extra repositories are not needed: the same N parallel workers
    come out of one repository, and the Postgres queue already keeps them from
    colliding.
    """
    out = api(f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/dispatches", "POST", {
        "ref": "main",
        "inputs": {
            "pages": pages, "fast": fast, "profile": profile,
            "shards": json.dumps(slots), "max_parallel": str(len(slots)),
            # 'best' makes discovery use Etsy's bestseller filter, which is how the
            # design method starts: collect proven sellers, not the whole market.
            "filter": filter_mode,
        },
    })
    if out.get("error") or out.get("message"):
        print(f"[dispatch] {len(slots)} shard(s) FAILED: {out}")
        return False
    for slot in slots:
        mark_dispatched(slot)
    print(f"[dispatch] sent {len(slots)} shard(s): {', '.join(slots)} "
          f"(pages={pages}, fast={fast}, profile={profile})")
    return True


def block_rate(limit: int = 12) -> float:
    """Share of the last N slices that were blocked, aborted or challenged.

    The pool is only allowed to grow while Etsy is answering cleanly. A dispatcher
    that keeps opening new sessions after a wave of challenges is how an IP range
    gets flagged, so this number caps the shard count instead of ignoring it.
    """
    try:
        from crawler.supa import Supa
        rows = Supa()._call("GET", f"/rest/v1/runs?select=status,challenges"
                                   f"&order=started_at.desc&limit={limit}") or []
    except Exception:                                    # noqa: BLE001
        return 0.0                                       # unknown: do not throttle on a guess
    if not rows:
        return 0.0
    # A single challenge is normal — every clean run solves one slider and
    # carries on (challenges=1 on a succeeded slice). Only a run that gave up, or
    # one that needed several solves, counts against the pool.
    bad = sum(1 for r in rows
              if r.get("status") in ("aborted", "failed", "blocked")
              or int(r.get("challenges") or 0) >= 3)
    return bad / len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", "--workers", dest="shards", type=int, default=3,
                    help="max parallel workers in one run (default 3, ceiling 20)")
    ap.add_argument("--pages", default="2")
    ap.add_argument("--fast", default="false")
    ap.add_argument("--profile", default="warm")
    ap.add_argument("--filter", dest="filter_mode", default="none", choices=["none", "best"],
                   help="best = only Etsy's bestseller-filtered results")
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
        return 0 if dispatch(["w1"], args.pages, args.fast, args.profile, filter_mode=args.filter_mode) else 1

    left = int(h.get("left") or 0)
    due = int(h.get("due_keywords") or 0)
    live = [w for w in (h.get("busy_workers") or []) if w]
    pages = int(args.pages)
    capacity = left // max(1, pages)                     # slices the budget still allows
    ceiling = max(1, min(20, int(args.shards)))
    slots = [f"w{i}" for i in range(1, ceiling + 1)]
    state = recently_dispatched()
    now = int(time.time())
    free = [s for s in slots
            if s not in live and now - int(state.get(s, 0)) > DISPATCH_COOLDOWN]

    # Ramp guard: halve the pool while Etsy is serving challenges.
    rate = block_rate()
    if rate > 0.25:
        capped = max(1, ceiling // 2)
        print(f"[dispatch] block rate {rate:.0%} over the last slices — capping shards "
              f"at {capped} (asked {ceiling})")
        free = free[:capped]

    want = min(len(free), due, capacity)

    print(f"[dispatch] budget {h.get('used')}/{h.get('budget')} "
          f"(left {left}, {capacity} slices) · due {due} keywords · "
          f"live {live or 'none'} · slots {slots} · blocked {rate:.0%}")

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
        print(f"[dispatch] would run {want} shard(s): {free[:want]}")
        return 0

    return 0 if dispatch(free[:want], args.pages, args.fast, args.profile, filter_mode=args.filter_mode) else 1


if __name__ == "__main__":
    sys.exit(main())
