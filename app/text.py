"""Small pure helpers for keyword input — kept separate so they are unit-testable."""
from __future__ import annotations

MAX_DEPTH = 100


def clamp_depth(value, default: int = 20) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_DEPTH, n))


def parse_phrases(text: str) -> list[str]:
    """One phrase per line or comma separated → normalised, de-duplicated, ordered."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").replace(",", "\n").splitlines():
        term = " ".join(raw.strip().lower().split())
        if not term or term in seen:
            continue
        seen.add(term)
        out.append(term[:120])
    return out
