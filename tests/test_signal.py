"""Tests for the second-generation extractors (migration 0010).

Every assertion here is pinned to a real Etsy page saved under tests/fixtures —
the bugs these tests guard against were all caused by trusting the page *text*
instead of the element that actually carries the signal.
"""

import gzip
import pathlib
import re
import sys
import unittest

from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.etsy_crawl import (  # noqa: E402
    extract_cards,
    extract_detail,
    extract_related,
    scoped_badges,
)

FX = ROOT / "tests" / "fixtures"


def load(name: str) -> str:
    with gzip.open(FX / name, "rt", encoding="utf-8", errors="ignore") as fh:
        return fh.read()


@unittest.skipUnless((FX / "detail_with_badges.html.gz").exists(), "fixture missing")
class DetailSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load("detail_with_badges.html.gz")
        cls.soup = BeautifulSoup(cls.html, "html.parser")
        cls.detail = extract_detail(cls.html, cls.soup, "https://www.etsy.com/listing/1679237029/")

    def test_bestseller_comes_from_the_badge_element(self):
        self.assertTrue(self.detail["is_bestseller"])
        self.assertIsNotNone(self.soup.select_one("div#bestseller clg-icon[name='bestseller']"))

    def test_star_seller_badge_is_detected(self):
        self.assertTrue(self.detail["is_star_seller"])

    def test_star_seller_tooltip_alone_is_not_a_badge(self):
        """The explainer text ships on listing pages that have no star seller.

        This is the exact bug that flagged 44% of the catalogue: the words
        "Star Sellers have an outstanding track record…" are page furniture.
        """
        fake = BeautifulSoup(
            '<html><body><div class="clg-popover"><p class="wt-text-title-01">Star Seller</p>'
            "<p>Star Sellers have an outstanding track record…</p></clg-popover></body></html>",
            "html.parser",
        )
        self.assertFalse(scoped_badges(fake)["is_star_seller"])

    def test_listing_date_is_parsed(self):
        self.assertEqual(self.detail["listing_date"], "2026-11-03")

    def test_listing_and_shop_review_counts_are_different_numbers(self):
        # JSON-LD aggregateRating = this listing's reviews ("2.3k reviews" in the shop card);
        # the embedded state carries the shop's lifetime count. Conflating them would
        # turn "a seller with 15k reviews" into "a listing with 15k reviews".
        self.assertEqual(self.detail["listing_review_count"], 2253)
        self.assertEqual(self.detail["shop_review_count"], 15362)

    def test_shop_reviews_dom_fallback_handles_the_k_suffix(self):
        st = BeautifulSoup("<html><body><p>Bob's shop (2.3k reviews)</p></body></html>", "html.parser")
        detail = extract_detail("<html></html>", st, "https://www.etsy.com/listing/1/")
        self.assertEqual(detail["shop_review_count"], 2300)

    def test_jsonld_extras(self):
        self.assertEqual(self.detail["images_count"], 10)
        self.assertEqual(self.detail["availability"], "InStock")
        self.assertEqual(self.detail["shipping_origin"], "TX/US")
        self.assertEqual(self.detail["quantity"], 973)

    def test_free_shipping_is_not_the_sitewide_promo(self):
        fake = BeautifulSoup(
            "<html><body><p>Free shipping on orders over $35</p></body></html>", "html.parser"
        )
        detail = extract_detail("<html></html>", fake, "https://www.etsy.com/listing/1/")
        self.assertFalse(detail["free_shipping"])


@unittest.skipUnless((FX / "detail_no_star.html.gz").exists(), "fixture missing")
class NegativeBadgeTests(unittest.TestCase):
    def test_a_page_without_the_badge_reports_false(self):
        html = load("detail_no_star.html.gz")
        soup = BeautifulSoup(html, "html.parser")
        detail = extract_detail(html, soup, "https://www.etsy.com/listing/4343268015/")
        self.assertFalse(detail["is_star_seller"])
        self.assertTrue(detail["is_bestseller"])          # this listing does have it

    def test_avatar_template_markup_is_not_a_badge(self):
        """Every listing page ships the star-seller avatar badge inside a <template>.

        Trusting that class reports 100% star sellers. The page below has the
        markup and is not a star seller; only rendered label text counts.
        """
        html = load("detail_no_star.html.gz")
        soup = BeautifulSoup(html, "html.parser")
        self.assertIsNotNone(soup.select_one('[class*="badge-star-seller"]'))
        self.assertFalse(extract_detail(html, soup, "https://www.etsy.com/listing/4343268015/")["is_star_seller"])


@unittest.skipUnless((FX / "search_page.html.gz").exists(), "fixture missing")
class SearchPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load("search_page.html.gz")
        cls.soup = BeautifulSoup(cls.html, "html.parser")

    def test_related_searches_are_harvested(self):
        rel = extract_related(self.soup, "tshirt")
        self.assertGreaterEqual(len(rel), 8)
        self.assertIn("tshirt png", [r.lower() for r in rel])

    def test_pagination_is_not_a_keyword(self):
        rel = extract_related(self.soup, "tshirt")
        for junk in ("1", "2", "Next", "Previous"):
            self.assertNotIn(junk, rel)

    def test_cards_flag_sponsored_slots(self):
        cards = extract_cards(self.soup, 1)
        self.assertGreater(len(cards), 20)
        flagged = [c for c in cards if c.get("is_ad") is True]
        self.assertTrue(flagged, "at least one Ad card should be present")
        self.assertTrue(all(c.get("is_ad") is not None for c in cards))


if __name__ == "__main__":
    unittest.main()
