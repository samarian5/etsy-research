#!/usr/bin/env python3
"""Pre-flight: is anything due right now?

The board lets each keyword pick its own cadence (1–24 h), so the trigger has to run
hourly or a 1-hour keyword would never be honoured. But an hourly tick that boots
Xvfb, installs Chrome and starts SeleniumBase just to discover the queue is empty
wastes two minutes of runner on 12 of the 24 daily ticks.

So the workflow runs this first. It asks the database what is due, writes a single
`due=true|false` to $GITHUB_OUTPUT, and the browser job is skipped when nothing is.
Cost when idle: checkout + python + one HTTP request — about 15 seconds.

  python scripts/plan_due.py            # writes due=false/true for GitHub Actions
  python scripts/plan_due.py --human    # prints the queue instead, for a human
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crawler.supa import Supa  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--human", action="store_true", help="print a table instead of GITHUB_OUTPUT")
    args = ap.parse_args()

    supa = Supa()
    due = supa.peek_due()
    usage = supa.usage_today()
    used, budget = usage.get("pages_used", 0), usage.get("pages_budget", 24)
    over_budget = used >= budget

    if args.human:
        print(f"pages today: {used}/{budget}   slices: {usage.get('slices', 0)}"
              f" ({usage.get('slices_ok', 0)} ok, {usage.get('slices_blocked', 0)} blocked)")
        if over_budget:
            print("budget spent — nothing will be claimed until midnight UTC")
        if not due:
            print("nothing due right now")
        for d in due:
            print(f"  due {d['keyword']}: page {d['next_page']}/{d['depth']}, every {d['interval_hours']}h")
        return 0

    run = bool(due) and not over_budget
    # GitHub Actions reads these from $GITHUB_OUTPUT. The keyword is deliberately
    # not echoed: these logs are public and the pre-flight runs before redaction.
    print(f"due={'true' if run else 'false'}")
    print(f"reason={'over_budget' if over_budget else ('queue_empty' if not due else 'ready')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
