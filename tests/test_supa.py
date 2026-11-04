"""The crawl→Supabase boundary: what actually goes over the wire.

ingest() is the one place where a silent mistake costs data without raising: a
description dropped for being None, a boolean sent as the string "False", or a
listing_id sent as an integer would all post successfully and store nothing
useful. These tests pin the shape of the request, with the network stubbed out.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "crawler"))

from supa import Supa  # noqa: E402


class FakeSupa(Supa):
    def __init__(self):
        super().__init__(url="https://example.invalid", key="test-key")
        self.calls = []

    def _call(self, method, path, body=None, prefer=None, retries=4):
        self.calls.append((method, path, body))
        if path.endswith("ingest_batch"):
            return {"rows": len(body["p_rows"]), "snapshots": len(body["p_rows"])}
        if path.endswith("claim_slice"):
            return []
        return None


def row(**kw):
    base = {
        "listing_id": 1679237029, "url": "https://www.etsy.com/listing/1679237029/x",
        "title": "Ceramic Mug", "shop_name": "Shop", "shop_url": "https://etsy.com/shop/Shop",
        "price_usd": 24.5, "currency": "USD", "favorites": 12,
        "listing_review_count": 3, "listing_rating": 4.8,
        "thumbnail_url": "https://i.etsystatic.com/x.jpg", "category_path": "Home > Mugs",
        "description": "A mug.", "is_bestseller": False, "is_star_seller": True,
        "free_shipping": True, "captured_at": "2026-11-04T10:00:00+00:00",
        "page": 2, "position": 7,
    }
    base.update(kw)
    return base


class IngestShapeTests(unittest.TestCase):
    def test_batches_at_twenty_five(self):
        s = FakeSupa()
        s.ingest([row() for _ in range(60)], "run-1", "ceramic mug")
        self.assertEqual(len(s.calls), 3)
        self.assertEqual([len(c[2]["p_rows"]) for c in s.calls], [25, 25, 10])

    def test_every_batch_carries_the_run_id(self):
        s = FakeSupa()
        s.ingest([row()], "gha-41850990686", "ceramic mug")
        self.assertEqual(s.calls[0][2]["p_run_id"], "gha-41850990686")

    def test_listing_id_is_a_string(self):
        # Etsy ids exceed 2^31 but not 2^53; still, Postgres text columns want text
        # and a numeric id in JSON becomes an int on the wire.
        s = FakeSupa()
        s.ingest([row()], "r", "kw")
        self.assertEqual(s.calls[0][2]["p_rows"][0]["listing_id"], "1679237029")

    def test_booleans_are_json_booleans(self):
        s = FakeSupa()
        s.ingest([row(is_bestseller=1, free_shipping=0)], "r", "kw")
        sent = s.calls[0][2]["p_rows"][0]
        self.assertIs(sent["is_bestseller"], True)
        self.assertIs(sent["free_shipping"], False)

    def test_none_values_are_omitted_not_sent_as_null(self):
        # ingest_batch uses coalesce(excluded.x, l.x) on update: a literal null
        # would be harmless there, but an omitted key is what the SQL expects.
        s = FakeSupa()
        s.ingest([row(description=None, price_usd=None)], "r", "kw")
        sent = s.calls[0][2]["p_rows"][0]
        self.assertNotIn("description", sent)
        self.assertNotIn("price_usd", sent)

    def test_keyword_is_attached_to_every_row(self):
        s = FakeSupa()
        s.ingest([row(), row()], "r", "ceramic mug")
        self.assertTrue(all(r["keyword"] == "ceramic mug" for r in s.calls[0][2]["p_rows"]))

    def test_unknown_keys_are_not_forwarded(self):
        s = FakeSupa()
        s.ingest([row(raw_payload_path="payloads/x.json.gz", shop_rating=4.9)], "r", "kw")
        sent = s.calls[0][2]["p_rows"][0]
        self.assertNotIn("raw_payload_path", sent)
        self.assertNotIn("shop_rating", sent)


class QueueTests(unittest.TestCase):
    def test_empty_claim_is_none_not_a_crash(self):
        self.assertIsNone(FakeSupa().claim_slice())

    def test_ingest_of_nothing_makes_no_calls(self):
        s = FakeSupa()
        self.assertEqual(s.ingest([], "r", "kw"), {"rows": 0, "snapshots": 0})
        self.assertEqual(s.calls, [])


if __name__ == "__main__":
    unittest.main()
