"""Encryption round trip — the whole public-repo design rests on this working.

The repository is public so Actions minutes are free, which means the queue and
the results travel through a world-readable place. These tests pin the two
properties that make that safe: sealed data round-trips with the right key, and
is unreadable without it.
"""
from __future__ import annotations

import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

import artifact_crypt as ac  # noqa: E402

KEY = "a" * 64
WRONG = "b" * 64


class CryptTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get(ac.KEY_ENV)
        os.environ[ac.KEY_ENV] = KEY
        self._redact = os.environ.pop("REDACT_KEYWORDS", None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop(ac.KEY_ENV, None)
        else:
            os.environ[ac.KEY_ENV] = self._old
        if self._redact is not None:
            os.environ["REDACT_KEYWORDS"] = self._redact

    def test_queue_round_trips(self):
        queue = {"items": [{"keyword": "tshirt", "depth": 20, "page": 4}], "pages_per_run": 3}
        blob = ac.seal(json.dumps(queue))
        self.assertTrue(ac.is_sealed(blob))
        self.assertEqual(json.loads(ac.unseal(blob)), queue)

    def test_ciphertext_does_not_leak_the_keyword(self):
        blob = ac.seal(json.dumps({"keyword": "personalized-mug"}))
        self.assertNotIn("personalized-mug", blob)
        self.assertNotIn("keyword", blob)

    def test_wrong_key_is_rejected(self):
        blob = ac.seal('{"keyword":"tshirt"}')
        os.environ[ac.KEY_ENV] = WRONG
        with self.assertRaises(RuntimeError):
            ac.unseal(blob)

    def test_missing_key_raises_cleanly(self):
        os.environ.pop(ac.KEY_ENV, None)
        with self.assertRaises(ac.MissingKey):
            ac.seal("x")

    def test_plaintext_is_not_mistaken_for_sealed(self):
        self.assertFalse(ac.is_sealed('{"items":[]}'))
        self.assertFalse(ac.is_sealed(""))
        self.assertFalse(ac.is_sealed(None))

    def test_keyword_is_hashed_when_redaction_is_on(self):
        os.environ["REDACT_KEYWORDS"] = "1"
        tag = ac.kwtag("tshirt")
        self.assertTrue(tag.startswith("kw-"))
        self.assertNotIn("tshirt", tag)
        self.assertEqual(tag, ac.kwtag("tshirt"))      # stable within a chain

    def test_keyword_is_verbatim_when_private(self):
        os.environ.pop("REDACT_KEYWORDS", None)
        self.assertEqual(ac.kwtag("tshirt"), "tshirt")


class ArtifactUnpackTests(unittest.TestCase):
    """The download path must accept both shapes: sealed tarball and legacy zip."""

    def setUp(self):
        self._old = os.environ.get(ac.KEY_ENV)
        os.environ[ac.KEY_ENV] = KEY

    def tearDown(self):
        if self._old is None:
            os.environ.pop(ac.KEY_ENV, None)
        else:
            os.environ[ac.KEY_ENV] = self._old

    def _sealed_artifact(self) -> bytes:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "out").mkdir()
        (tmp / "out" / "results.jsonl").write_text('{"listing_id": 1}\n', encoding="utf-8")
        (tmp / "out" / "run.json").write_text('{"status": "succeeded"}', encoding="utf-8")
        tarball = tmp / "out.tar.gz"
        with tarfile.open(tarball, "w:gz") as tf:
            tf.add(tmp / "out", arcname="")            # matches the workflow step
        ac.seal_file(str(tarball), str(tmp / "out.tar.gz.enc"))
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(tmp / "out.tar.gz.enc", "out.tar.gz.enc")
        return buf.getvalue()

    def test_sealed_artifact_imports(self):
        from app import github_actions as gh
        dest = Path(tempfile.mkdtemp())
        gh.unpack_artifact(self._sealed_artifact(), dest)
        self.assertTrue((dest / "results.jsonl").exists())
        self.assertEqual(json.loads((dest / "run.json").read_text())["status"], "succeeded")
        self.assertEqual(list(dest.rglob("*.enc")), [])   # ciphertext cleaned up

    def test_sealed_artifact_with_wrong_key_fails_loudly(self):
        from app import github_actions as gh
        blob = self._sealed_artifact()
        os.environ[ac.KEY_ENV] = WRONG
        with self.assertRaises(RuntimeError):
            gh.unpack_artifact(blob, Path(tempfile.mkdtemp()))

    def test_legacy_plaintext_artifact_still_imports(self):
        from app import github_actions as gh
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("out/run.json", '{"status": "succeeded"}')
        dest = Path(tempfile.mkdtemp())
        gh.unpack_artifact(buf.getvalue(), dest)
        self.assertTrue((dest / "out" / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
