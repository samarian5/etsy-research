"""Stall protection — the fix for a four-hour silent hang.

On 2026-11-04 run 41727178418 discovered 179 listings and then produced no output
for 4h12m inside SeleniumBase's CDP `get()`, whose `except RuntimeError:` branch
retries the navigation with no timeout at all. Two defences are tested here:

* ``navigate()`` gives every page load a deadline and reports failure instead of
  waiting forever;
* ``start_watchdog()`` kills the process when nothing has progressed, so the
  artifact step (``if: always()``) still runs and the log says where it stopped.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRAWLER = ROOT / "crawler"


def _run(code: str, env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    script = textwrap.dedent(code)
    path = Path(tempfile.mkdtemp()) / "probe.py"
    path.write_text(script)
    return subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, **(env or {})})


class NavigateTests(unittest.TestCase):
    def test_hung_goto_is_abandoned_and_reported_as_failure(self):
        """A goto that never returns must not hold the chunk."""
        r = _run(f"""
            import sys, types, time
            sys.path.insert(0, r"{CRAWLER}")
            import etsy_crawl

            class Args:
                keyword = "tshirt"; depth = 1; pages_from = 1; pages_to = 1
                max_requests = 100; save_detail_html = 0; fast = True
                run_id = "test"; started_at = "now"; out = "out"
                user_data_dir = "p"; browser_executable_path = None; proxy = None
                skip_ids = ""

            class SB:
                def goto(self, url):
                    time.sleep(300)          # the hang under test

            crawl = etsy_crawl.Crawl(Args())
            ok = crawl.navigate(SB(), "https://www.etsy.com/", "home")
            print("RESULT", ok, crawl.nav_timeouts)
        """, env={"NAV_TIMEOUT": "2"})
        self.assertIn("exceeded 2s", r.stdout)
        self.assertIn("RESULT False 1", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_navigation_error_is_counted_not_raised(self):
        r = _run(f"""
            import sys
            sys.path.insert(0, r"{CRAWLER}")
            import etsy_crawl

            class Args:
                keyword = "tshirt"; depth = 1; pages_from = 1; pages_to = 1
                max_requests = 100; save_detail_html = 0; fast = True
                run_id = "test"; started_at = "now"; out = "out"
                user_data_dir = "p"; browser_executable_path = None; proxy = None
                skip_ids = ""

            class SB:
                def goto(self, url):
                    raise RuntimeError("net::ERR_TIMED_OUT")

            crawl = etsy_crawl.Crawl(Args())
            print("RESULT", crawl.navigate(SB(), "https://x", "page"), crawl.nav_timeouts)
        """, env={"NAV_TIMEOUT": "5"})
        self.assertIn("RESULT False 1", r.stdout)


class WatchdogTests(unittest.TestCase):
    def test_watchdog_exits_the_process_when_nothing_progresses(self):
        r = _run(f"""
            import sys, time
            sys.path.insert(0, r"{CRAWLER}")
            import etsy_crawl

            class Args:
                keyword = "tshirt"; depth = 1; pages_from = 1; pages_to = 1
                max_requests = 100; save_detail_html = 0; fast = True
                run_id = "test"; started_at = "now"; out = "out"
                user_data_dir = "p"; browser_executable_path = None; proxy = None
                skip_ids = ""

            crawl = etsy_crawl.Crawl(Args())
            crawl.start_watchdog()
            time.sleep(30)                   # pretend to be stuck in a CDP call
            print("SHOULD NOT REACH HERE")
        """, env={"STALL_MINUTES": "0.03"})   # ~2 s
        self.assertIn("WATCHDOG", r.stdout)
        self.assertNotIn("SHOULD NOT REACH HERE", r.stdout)
        self.assertEqual(r.returncode, 3)      # distinct code: watchdog, not a crash

    def test_progress_resets_the_watchdog(self):
        r = _run(f"""
            import sys, time
            sys.path.insert(0, r"{CRAWLER}")
            import etsy_crawl

            class Args:
                keyword = "tshirt"; depth = 1; pages_from = 1; pages_to = 1
                max_requests = 100; save_detail_html = 0; fast = True
                run_id = "test"; started_at = "now"; out = "out"
                user_data_dir = "p"; browser_executable_path = None; proxy = None
                skip_ids = ""

            crawl = etsy_crawl.Crawl(Args())
            crawl.start_watchdog()
            for i in range(6):
                time.sleep(1)
                crawl.touch(f"listing {{i}}")
            print("SURVIVED", crawl.progress_note)
        """, env={"STALL_MINUTES": "0.05"})    # 3 s of silence would otherwise kill it
        self.assertIn("SURVIVED listing 5", r.stdout)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
