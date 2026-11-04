"""Chunked-queue arithmetic: the chain must resume exactly once per page."""
from __future__ import annotations

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

    def test_timeout_twice_gives_up(self):
        item = {"keyword": "tshirt", "depth": 20, "page": 4, "attempts": 1}
        nxt, reason = advance(item, [], 3, 6, 1,
                              {"stopped_reason": "timeout", "pages_done": 0}, "cold")
        self.assertIsNone(nxt)


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
        nxt, reason = advance({"keyword": "tshirt", "depth": 20, "attempts": 1}, [], 8, 8, 1,
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
