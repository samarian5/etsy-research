#!/usr/bin/env python3
"""Regression test against a REAL Etsy listing page captured by run 41655788838.

This is the antidote to the synthetic fixtures: those proved the parser ran, this
proves it reads the live markup. When Etsy changes shape, this test is what goes
red — then replace the fixture with a fresh capture (`--save-detail-html 1`).
"""
import gzip
import sys
from pathlib import Path
from unittest import TestCase, main

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "crawler"))

from bs4 import BeautifulSoup  # noqa: E402
from etsy_crawl import extract_detail  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "real" / "listing_1679237029.html.gz"
URL = "https://www.etsy.com/listing/1679237029/"


class RealListingTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE.exists():
            raise unittest_skip()
        html = gzip.open(FIXTURE, "rt", encoding="utf-8", errors="ignore").read()
        cls.detail = extract_detail(html, BeautifulSoup(html, "html.parser"), URL)

    def test_title(self):
        self.assertTrue(self.detail["title"].startswith("Custom Logo Shirts"))

    def test_price_range(self):
        self.assertEqual(self.detail["price_usd"], 4.44)
        self.assertEqual(self.detail["price_min_usd"], 4.44)
        self.assertEqual(self.detail["price_max_usd"], 14.79)
        self.assertTrue(self.detail["is_range"])

    def test_favorites_are_listing_level(self):
        # 2141 is this listing's count, straight off the page text.
        self.assertEqual(self.detail["favorites"], 2141)

    def test_reviews_and_rating(self):
        self.assertEqual(self.detail["listing_review_count"], 2253)
        self.assertEqual(self.detail["listing_rating"], 4.9)

    def test_shop_level(self):
        self.assertEqual(self.detail["shop_name"], "TeaShirtsUS")
        self.assertEqual(self.detail["shop_review_count"], 15362)

    def test_images_and_thumbnail(self):
        # JSON-LD image is a list of ImageObject dicts, not strings.
        self.assertEqual(self.detail["images_count"], 10)
        self.assertIn("i.etsystatic.com", self.detail["thumbnail_url"])

    def test_variation_count(self):
        # select#variation-selector-0 (49 options) + -1 (23 options)
        self.assertEqual(self.detail["variation_count"], 72)

    def test_description_and_html(self):
        self.assertGreater(len(self.detail["description"]), 3000)
        self.assertIn("<", self.detail["description_html"])

    def test_tiers(self):
        ex = self.detail["extraction"]
        self.assertTrue(ex["jsonld"])
        self.assertEqual(ex["warnings"], [])


def unittest_skip():
    import unittest
    raise unittest.SkipTest("real-capture fixture not present")


if __name__ == "__main__":
    main()
