"""Chunked-queue arithmetic: the chain must resume exactly once per page."""
from __future__ import annotations

import pathlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "crawler"))
sys.path.insert(0, str(ROOT))

from run_chunk import advance  # noqa: E402  (imports etsy_crawl lazily)
from app.text import clamp_depth, parse_phrases  # noqa: E402


def ok(pages_done):
    return {"status": "succeeded", "stopped_reason": None, "pages_done": pages_done}


class NavigationTimeoutTests(unittest.TestCase):
    def test_timeout_retries_same_slice_with_a_cold_profile(self):
        """A chunk that stalled on navigation must retry the SAME pages, not skip them."""
        item = {"keyword": "tshirt", "depth": 20, "page": 4}
        nxt, reason = advance(item, [], 3, 6, 1,
                              {"stopped_reason": "timeout", "pages_done": 0}, "warm")
        self.assertEqual(nxt["items"][0]["page"], 4)
        self.assertEqual(nxt["profile"], "cold")
        self.assertEqual(nxt["items"][0]["attempts"], 1)
        self.assertIn("timeout", reason)

    def test_blocked_gets_a_second_cold_retry(self):
        """IP-level blocks are common; the second retry is what saves a pass."""
        item = {"keyword": "tshirt", "depth": 20, "page": 4, "attempts": 1}
        nxt, reason = advance(item, [], 3, 6, 1,
                              {"stopped_reason": "blocked", "pages_done": 0}, "cold")
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["items"][0]["attempts"], 2)
        self.assertEqual(nxt["profile"], "cold")

    def test_timeout_twice_gives_up(self):
        item = {"keyword": "tshirt", "depth": 20, "page": 4, "attempts": 3}
        nxt, reason = advance(item, [], 3, 6, 1,
                              {"stopped_reason": "timeout", "pages_done": 0}, "cold")
        self.assertIsNone(nxt)


class SessionBudgetTests(unittest.TestCase):
    """Measured 2026-11-04: ~150 page loads, then DataDome refuses every listing.
    A slice must fit inside what one browser session survives."""

    def test_a_bigger_slice_is_clamped_to_the_session_budget(self):
        import sys
        sys.path.insert(0, "crawler")
        import run_chunk
        self.assertEqual(run_chunk.session_pages(8, page_loads=130), 2)
        self.assertEqual(run_chunk.session_pages(5, page_loads=130), 2)
        self.assertEqual(run_chunk.session_pages(2, page_loads=130), 2)
        self.assertEqual(run_chunk.session_pages(1, page_loads=130), 1)

    def test_a_slow_mode_budget_still_allows_at_least_one_page(self):
        import sys
        sys.path.insert(0, "crawler")
        import run_chunk
        self.assertEqual(run_chunk.session_pages(5, page_loads=10), 1)


class WorkflowShapeTests(unittest.TestCase):
    """A workflow GitHub cannot parse fails as "a run with zero jobs", 5 s apart —
    which is how eight dead runs appeared on 2026-11-04. Parse it before pushing."""

    def setUp(self):
        import yaml
        with open(".github/workflows/scrape.yml") as fh:
            self.doc = yaml.safe_load(fh)
        with open(".github/workflows/supa-crawl.yml") as fh:
            self.supa = yaml.safe_load(fh)

    def test_parses_and_has_the_expected_triggers(self):
        triggers = self.doc[True]
        self.assertIn("workflow_dispatch", triggers)
        # The schedule moved to supa-crawl.yml when the queue left the repository.
        # Two crawlers on one cron would fight for the same site and the same
        # concurrency group, so scrape.yml must stay dispatch-only.
        self.assertNotIn("schedule", triggers)

    def test_the_supabase_workflow_owns_the_cadence(self):
        triggers = self.supa[True]
        cron = triggers["schedule"][0]["cron"]
        # Assert the cadence, not the exact minute. The trigger is hourly because a
        # keyword may be set to "every hour"; the queue's due_at decides what actually
        # runs, so an hourly tick does not mean hourly crawling of everything.
        minute, hours = cron.split()[0], cron.split()[1]
        self.assertEqual(hours, "*", f"not an hourly tick: {cron}")
        self.assertTrue(minute.isdigit(), f"schedule must pin one minute: {cron}")
        self.assertNotEqual(minute, "0", "avoid :00 — GitHub congests the top of the hour")
        self.assertIn("workflow_dispatch", triggers)

    def test_an_idle_tick_costs_no_browser(self):
        """The pre-flight job must exist and gate the crawl, or an hourly trigger
        would boot Chrome 24 times a day to discover there is nothing to do."""
        jobs = self.supa["jobs"]
        self.assertIn("plan", jobs)
        self.assertEqual(jobs["slice"]["needs"], "plan")
        self.assertIn("needs.plan.outputs.due", jobs["slice"]["if"])
        plan_steps = " ".join(s.get("run", "") + s.get("uses", "") for s in jobs["plan"]["steps"])
        self.assertIn("plan_due.py", plan_steps)
        # the plan job must not install a browser
        self.assertNotIn("chrome", plan_steps.lower())
        self.assertNotIn("xvfb", plan_steps.lower())

    def test_each_slot_has_its_own_concurrency_group(self):
        """Parallelism comes from distinct group names, not from extra repos.

        One group per worker slot: w2 and w3 crawl at the same time, while the
        same slot can never overlap itself. A single hard-coded group would
        silently serialise the whole pool, which is exactly the bug this test
        exists to catch.
        """
        group = self.supa["concurrency"]["group"]
        self.assertIn("inputs.worker", group)
        self.assertTrue(group.startswith("etsy-crawl-"), group)
        self.assertFalse(self.supa["concurrency"]["cancel-in-progress"])
        # the legacy SQLite crawler keeps its own, non-colliding group
        self.assertEqual(self.doc["concurrency"]["group"], "etsy-crawl-legacy")
        self.assertFalse(self.doc["concurrency"]["cancel-in-progress"])
        # and the slot must actually reach the crawler
        self.assertIn("inputs.worker", self.supa["jobs"]["slice"]["env"]["WORKER"])

    def test_the_supabase_slice_writes_to_the_configured_project(self):
        env = self.supa["jobs"]["slice"]["env"]
        self.assertEqual(env["SUPABASE_URL"], "${{ secrets.SUPABASE_URL }}")
        self.assertEqual(env["SUPABASE_SERVICE_KEY"], "${{ secrets.SUPABASE_SERVICE_KEY }}")
        run = [s for s in self.supa["jobs"]["slice"]["steps"] if s.get("id") == "slice"][0]
        self.assertIn("scripts/supa_run.py", run["run"])

    def test_every_step_has_a_name_or_a_uses(self):
        for doc in (self.doc, self.supa):
            for job, body in doc["jobs"].items():
                for i, step in enumerate(body["steps"], 1):
                    self.assertTrue(step.get("name") or step.get("uses"),
                                    f"{job} step {i} has neither name nor uses")

    def test_chaining_steps_survive_a_failed_chunk(self):
        names = {s.get("name"): s for s in self.doc["jobs"]["crawl"]["steps"]}
        for step in ("Cool down before chaining", "Dispatch next chunk"):
            self.assertIn("always()", names[step]["if"], step)

    def test_queue_input_is_optional_so_cron_can_run(self):
        self.assertFalse(self.doc[True]["workflow_dispatch"]["inputs"]["queue"]["required"])


class PublishQueueTests(unittest.TestCase):
    """The every-second-day queue is built from the keyword table, not hand-written."""

    def _db(self):
        import sqlite3, tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "q.db")
        con = sqlite3.connect(path)
        con.execute("create table keyword (id integer primary key, phrase text, depth int, enabled int)")
        con.executemany("insert into keyword (phrase, depth, enabled) values (?,?,?)",
                        [("tshirt", 20, 1), ("ceramic mug", 2, 1), ("retired phrase", 5, 0)])
        con.commit(); con.close()
        return path

    def test_enabled_keywords_become_items_starting_at_page_one(self):
        import sys
        sys.path.insert(0, "scripts")
        import publish_queue
        q = publish_queue.build_queue(self._db(), pages_per_run=5)
        self.assertEqual([i["keyword"] for i in q["items"]], ["tshirt", "ceramic mug"])
        self.assertEqual([i["depth"] for i in q["items"]], [20, 2])
        self.assertTrue(all(i["page"] == 1 for i in q["items"]))
        self.assertEqual(q["pages_per_run"], 5)

    def test_max_depth_caps_a_recurring_pass(self):
        import sys
        sys.path.insert(0, "scripts")
        import publish_queue
        q = publish_queue.build_queue(self._db(), max_depth=3)
        self.assertEqual([i["depth"] for i in q["items"]], [3, 2])


class SolverEscalationTests(unittest.TestCase):
    """A solver that cannot clear DataDome must stop being retried."""

    def test_two_dead_solves_shorten_the_escalation(self):
        import sys
        sys.path.insert(0, "crawler")
        import etsy_crawl

        class Args:
            keyword = "tshirt"; depth = 1; pages_from = 1; pages_to = 1
            max_requests = 100; save_detail_html = 0; save_html_pages = 0; fast = True
            run_id = "t"; started_at = "now"; out = "out"
            user_data_dir = "p"; browser_executable_path = None; proxy = None
            skip_ids = ""

        crawl = etsy_crawl.Crawl(Args())

        class SB:
            bufsize = 0
            def solve_captcha(self):
                raise RuntimeError("no slider")
            def refresh(self):
                pass

        self.assertFalse(crawl.solve(SB(), "page"))
        self.assertEqual(crawl.solve_failures, 1)
        self.assertFalse(crawl.solve(SB(), "page"))
        self.assertEqual(crawl.solve_failures, 2)
        # a good solve resets the counter, so a healthy session is not penalised
        class Good(SB):
            def solve_captcha(self):
                pass
        crawl.delay = lambda *a, **k: None
        self.assertTrue(crawl.solve(Good(), "page"))
        self.assertEqual(crawl.solve_failures, 0)


class AdvanceTests(unittest.TestCase):
    def test_mid_depth_slice_resumes_at_next_page(self):
        nxt, reason = advance({"keyword": "tshirt", "depth": 20}, [], 8, 8, 0, ok(8))
        self.assertEqual(nxt["items"], [{"keyword": "tshirt", "depth": 20, "page": 9}])
        self.assertEqual(nxt["pages_per_run"], 8)
        self.assertIn("page 9", reason)

    def test_final_slice_moves_to_next_keyword(self):
        rest = [{"keyword": "mug", "depth": 5}]
        nxt, _ = advance({"keyword": "tshirt", "depth": 20, "page": 17}, rest, 8, 20, 0, ok(20))
        self.assertEqual(nxt["items"], rest)

    def test_queue_exhausted_returns_none(self):
        nxt, reason = advance({"keyword": "mug", "depth": 4}, [], 8, 4, 0, ok(4))
        self.assertIsNone(nxt)
        self.assertIn("complete at depth 4", reason)

    def test_blocked_chunk_retries_once_with_a_cold_profile(self):
        nxt, reason = advance({"keyword": "tshirt", "depth": 20}, [], 8, 8, 1,
                              {"status": "aborted", "stopped_reason": "blocked", "pages_done": 3})
        self.assertEqual(nxt["profile"], "cold")
        self.assertEqual(nxt["items"][0]["attempts"], 1)
        self.assertNotIn("page", nxt["items"][0])       # same chunk, page 1 implicitly
        self.assertIn("cold", reason)

    def test_second_block_stops_the_chain(self):
        """Three cold retries, then it stops rather than hammering a blocked keyword."""
        nxt, reason = advance({"keyword": "tshirt", "depth": 20, "attempts": 3}, [], 8, 8, 1,
                              {"status": "aborted", "stopped_reason": "blocked", "pages_done": 0})
        self.assertIsNone(nxt)
        self.assertIn("blocked", reason)

    def test_coverage_stop_finishes_keyword_and_continues(self):
        rest = [{"keyword": "mug", "depth": 5}]
        nxt, reason = advance({"keyword": "tshirt", "depth": 20}, rest, 8, 8, 1,
                              {"status": "aborted", "stopped_reason": "coverage", "pages_done": 2})
        self.assertEqual(nxt["items"], rest)
        self.assertIn("keyword finished", reason)

    def test_budget_stop_resumes_from_first_uncrawled_page(self):
        nxt, reason = advance({"keyword": "tshirt", "depth": 20, "page": 9}, [], 8, 16, 1,
                              {"status": "aborted", "stopped_reason": "budget", "pages_done": 12})
        self.assertEqual(nxt["items"][0]["page"], 13)
        self.assertIn("page 13", reason)

    def test_budget_stop_on_last_page_finishes_keyword(self):
        rest = [{"keyword": "mug", "depth": 5}]
        nxt, _ = advance({"keyword": "tshirt", "depth": 20, "page": 17}, rest, 8, 20, 1,
                         {"status": "aborted", "stopped_reason": "budget", "pages_done": 20})
        self.assertEqual(nxt["items"], rest)


class InputTests(unittest.TestCase):
    def test_parse_phrases_dedupes_and_normalises(self):
        got = parse_phrases("  T-Shirt \n\npersonalized   Mug, t-shirt, wedding gift\n")
        self.assertEqual(got, ["t-shirt", "personalized mug", "wedding gift"])

    def test_parse_phrases_empty(self):
        self.assertEqual(parse_phrases("  ,, \n"), [])

    def test_depth_is_clamped(self):
        self.assertEqual(clamp_depth("0"), 1)
        self.assertEqual(clamp_depth("400"), 100)
        self.assertEqual(clamp_depth("abc"), 20)


if __name__ == "__main__":
    unittest.main()


class PlanJobTests(unittest.TestCase):
    """The pre-flight runs on every hourly tick, and its output goes to public logs."""

    def test_plan_never_echoes_a_keyword(self):
        src = (pathlib.Path(__file__).resolve().parent.parent / "scripts" / "plan_due.py").read_text()
        # REDACT_KEYWORDS only covers the crawl step; the plan job runs before it,
        # so the pre-flight must simply never print a keyword.
        self.assertNotIn("keyword=", src)
        self.assertNotIn("{due[0]['keyword']}", src)

    def test_dispatch_stays_silent_when_nothing_is_due(self):
        src = (pathlib.Path(__file__).resolve().parent.parent / "scripts" / "dispatch_slice.py").read_text()
        # An idle tick must not create a GitHub run at all: it reads the queue's
        # own numbers and dispatches zero slots when nothing is due or the budget
        # is spent.
        self.assertIn("queue_health", src)
        self.assertIn("nothing due", src)
