"""Seal and unseal small blobs with the shared artifact key.

Named ``artifact_crypt`` rather than ``crypt``: on Python 3.12 the runner has a
stdlib module called ``crypt``, and shadowing it breaks unrelated imports.

Why this exists: the crawl runs in a **public** GitHub repository, where Actions
minutes are free but every log line and every artifact is readable by anyone.
The queue (which carries the keywords being researched) and the results are the
only things worth protecting, so both are encrypted with AES-256-CBC + PBKDF2
before they leave the runner, and decrypted locally by ``scripts/sync.py``.

Deliberately dependency-free: ``openssl`` ships with the runner and with every
Linux/macOS box, whereas adding ``cryptography``/``pynacl`` to the crawler
requirements would slow every job down for one function.

Ciphertext is base64 text led by ``U2FsdGVkX1`` (base64 of openssl's
``Salted__`` header), so callers can tell sealed data from plaintext and stay
backwards compatible with chunks produced before encryption existed.
"""
from __future__ import annotations

import os
import subprocess

MARKER = "U2FsdGVkX1"
KEY_ENV = "ETSY_ARTIFACT_KEY"


class MissingKey(RuntimeError):
    """No artifact key available in the environment."""


def key() -> str:
    value = (os.environ.get(KEY_ENV) or "").strip()
    if not value:
        raise MissingKey(
            f"{KEY_ENV} is not set. On the runner it comes from the repository "
            f"secret of the same name; locally it lives in .env (chmod 600)."
        )
    return value


def is_sealed(text: str | None) -> bool:
    return bool(text) and text.strip().startswith(MARKER)


def seal(text: str) -> str:
    """Encrypt text to single-line base64."""
    return _seal_bytes(text.encode()).decode()


def _seal_bytes(data: bytes) -> bytes:
    env = dict(os.environ)
    env[KEY_ENV] = key()
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-salt",
         "-a", "-A", "-pass", f"env:{KEY_ENV}"],
        input=data, capture_output=True, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"seal failed: {proc.stderr.decode()[:300]}")
    return proc.stdout.strip()


def unseal(blob: str) -> str:
    """Decrypt single-line base64 back to text."""
    env = dict(os.environ)
    env[KEY_ENV] = key()
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-md", "sha256",
         "-a", "-A", "-pass", f"env:{KEY_ENV}"],
        input=blob.strip().encode(), capture_output=True, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"unseal failed (wrong key?): {proc.stderr.decode()[:300]}")
    return proc.stdout.decode()


def seal_file(src: str, dst: str) -> None:
    env = dict(os.environ)
    env[KEY_ENV] = key()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-md", "sha256",
             "-salt", "-pass", f"env:{KEY_ENV}"],
            stdin=fin, stdout=fout, env=env,
        )
    if proc.returncode != 0:
        raise RuntimeError("seal_file failed")


def unseal_file(src: str, dst: str) -> None:
    env = dict(os.environ)
    env[KEY_ENV] = key()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-md", "sha256",
             "-pass", f"env:{KEY_ENV}"],
            stdin=fin, stdout=fout, env=env,
        )
    if proc.returncode != 0:
        raise RuntimeError("unseal_file failed (wrong key?)")


def load_env_file(path: str = ".env") -> None:
    """Load KEY=value pairs from .env without clobbering the real environment."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip())


def kwtag(keyword: str) -> str:
    """Keyword for a log line: real when private, hashed when the repo is public.

    A public repository publishes its workflow logs, and the keyword list *is* the
    research. ``REDACT_KEYWORDS=1`` (set by the workflow) turns "tshirt" into
    "kw-1f2a…", which is still enough to follow a chunk through the logs.
    """
    if os.environ.get("REDACT_KEYWORDS") == "1":
        import hashlib
        return "kw-" + hashlib.sha256(keyword.encode()).hexdigest()[:8]
    return keyword
