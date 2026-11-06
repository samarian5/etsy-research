#!/usr/bin/env python3
"""Parser unit tests. Uses synthetic HTML fixtures that mimic Etsy's structure —
they prove the parsing logic runs and degrades gracefully, NOT that Etsy's live
markup matches. Live selector validation happens on the first real run."""
import sys
from pathlib import Path
from unittest import TestCase, main

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "crawler"))
from etsy_crawl import (  # noqa: E402
    parse_money, parse_count, looks_blocked, extract_cards, extract_detail, first_number,
)

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None


FIXTURE = """<!doctype html><html><head><title>Funny T-Shirt | Etsy</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Vintage Sun Tee",
 "image":"https://i.etsystatic.com/123/r/il/abc/1.jpg",
 "description":"Soft cotton tee.\\nUnisex fit.",
 "offers":{"@type":"Offer","price":"26.50","priceCurrency":"USD"},
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.8","reviewCount":"1,204"}}
</script></head><body>
<a href="/shop/SunnyThreads">SunnyThreads</a>
<div data-buy-box-region="price"><span>$26.50</span><span>Was $34.00</span></div>
<div data-product-details-description-text-content>
  <p>Soft cotton tee.</p><p>Unisex fit.</p>
</div>
<div>1,847 favorites</div>
<div>3,902 reviews</div>
<span>Bestseller</span><span>Free shipping</span>
</body></html>"""

BLOCK_FIXTURE = """<html><head><title>etsy.com</title></head><body>no products here</body></html>"""

SEARCH_FIXTURE = """<html><body>
<li><div class="v2-listing-card__info" data-listing-id="111">
  <a href="/listing/111/vintage-sun-tee"><h3>Vintage Sun Tee</h3></a>
  <div class="n-listing-card__price"><span class="currency-value">$26.50</span></div>
</div></li>
<li><div class="v2-listing-card__info" data-listing-id="222">
  <a href="/listing/222/retro-mountain-tee"><h3>Retro Mountain Tee</h3></a>
  <div class="n-listing-card__price"><span class="currency-value">$31.00</span></div>
</div></li>
</body></html>"""


class TestParsers(TestCase):
    def test_money(self):
        self.assertEqual(parse_money("$26.50"), 26.5)
        self.assertEqual(parse_money("US$1,234.00"), 1234.0)
        self.assertEqual(parse_money("18,00 €"), 18.0)
        self.assertIsNone(parse_money("free shipping"))
        self.assertIsNone(parse_money(None))

    def test_count(self):
        self.assertEqual(parse_count("1,847"), 1847)
        self.assertEqual(parse_count("1.2k"), 1200)
        self.assertEqual(parse_count("3K"), 3000)
        self.assertIsNone(parse_count("no digits"))

    def test_first_number(self):
        self.assertEqual(first_number([r"([\d,]+)\s+favorites"], "1,847 favorites"), 1847)

    def test_block_detection(self):
        self.assertTrue(looks_blocked("etsy.com", BLOCK_FIXTURE))
        self.assertFalse(looks_blocked("Funny T-Shirt | Etsy", FIXTURE))

    def test_detail_jsonld(self):
        if BeautifulSoup is None:
            self.skipTest("bs4 missing")
        soup = BeautifulSoup(FIXTURE, "html.parser")
        d = extract_detail(FIXTURE, soup, "https://www.etsy.com/listing/111/")
        self.assertTrue(d["extraction"]["jsonld"])
        self.assertEqual(d["title"], "Vintage Sun Tee")
        self.assertEqual(d["price_usd"], 26.5)
        self.assertEqual(d["currency"], "USD")
        self.assertEqual(d["listing_review_count"], 1204)
        self.assertAlmostEqual(d["listing_rating"], 4.8)
        self.assertEqual(d["favorites"], 1847)
        self.assertEqual(d["shop_name"], "SunnyThreads")
        # A word in the page text is not a badge: since 0010 both flags come from
        # their element (the fixture mentions "Bestseller" in prose only).
        self.assertFalse(d["is_bestseller"])
        self.assertTrue(d["free_shipping"])
        self.assertIn("Soft cotton tee", d["description"])

    def test_cards(self):
        if BeautifulSoup is None:
            self.skipTest("bs4 missing")
        soup = BeautifulSoup(SEARCH_FIXTURE, "html.parser")
        cards = extract_cards(soup, 1)
        ids = [c["listing_id"] for c in cards]
        self.assertEqual(ids, ["111", "222"])
        self.assertEqual(cards[0]["title"], "Vintage Sun Tee")
        self.assertEqual(cards[0]["position"], 1)
        self.assertEqual(cards[1]["page"], 1)


if __name__ == "__main__":
    main()
