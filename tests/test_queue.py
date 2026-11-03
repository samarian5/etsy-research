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

    def test_failure_stops_the_chain_without_advancing(self):
        nxt, reason = advance({"keyword": "tshirt", "depth": 20}, [], 8, 8, 1,
                              {"status": "aborted", "stopped_reason": "blocked", "pages_done": 3})
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
