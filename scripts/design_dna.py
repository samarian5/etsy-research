#!/usr/bin/env python3
"""design_dna.py — read the artwork, not the marketing copy.

The crawler stores what Etsy *says* about a listing. This worker reads what the
listing *shows*: OCR of the shirt's artwork gives the phrase, pixel statistics give
the composition (text-only vs text+graphic), the background treatment (the striped
cut-out look from the design-research method), the ink coverage and the palette.

Everything is deterministic and every uncertain value is left NULL — a wrong phrase
is worse than a missing one, because the phrase is what the pattern library groups on.

Runs locally against Supabase over PostgREST (~1 s per image; images come straight
from Etsy's CDN, which is not challenge-protected, so no browser is needed).

  python scripts/design_dna.py --limit 40
  python scripts/design_dna.py --limit 5 --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.supa import Supa  # noqa: E402

MAX_IMAGES = 3          # tries per listing; the cover is often a mockup
OCR_MIN_CONF = 0.55     # below this the "phrase" is noise
MAX_TEXT = 320


# --------------------------------------------------------------------- pixels ---
def load(url: str) -> Image.Image | None:
    """Fetch an Etsy CDN image, downsized to 570 px (the CDN honours the resize)."""
    small = re.sub(r"/il_[^/.]+\.", "/il_570xN.", url)
    try:
        r = requests.get(small, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or len(r.content) < 4000:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((640, 640))
        return img
    except Exception:  # noqa: BLE001 — a dead CDN url must not stop the batch
        return None


def hexc(t) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in t[:3])


def pixels(img: Image.Image) -> dict:
    """Composition statistics. No ML: colour buckets, row means, edge density."""
    import numpy as np

    small = img.convert("RGB").resize((96, 96))
    arr = np.asarray(small, dtype=np.int16)
    flat = arr.reshape(-1, 3)

    # Coarse 32-level buckets are robust to JPEG noise and anti-aliasing, which is
    # what defeated an exact-quantise approach: a flat cream background fragmented
    # into six near-identical colours and read as a "photo".
    q = (flat // 32) * 32
    vals, counts = np.unique(q, axis=0, return_counts=True)
    order = np.argsort(-counts)
    bg = vals[order[0]].astype(np.int16)
    dist = np.abs(flat - bg).sum(axis=1)
    bg_share = float((dist < 48).mean())
    share = counts / counts.sum()
    n_major = int((share > 0.055).sum())            # how many colours actually matter
    others = [hexc(vals[i]) for i in order[1:4] if share[i] > 0.02]

    # Row brightness separates a striped background from a flat one: stripes give a
    # high-frequency sawtooth, a gradient a single long drift, a photo neither.
    lum = arr.mean(axis=2).mean(axis=1)
    dev = lum - lum.mean()
    crossings = int((np.diff(np.sign(dev)) != 0).sum())
    amp = float(np.abs(dev).mean())
    drift = float(abs(lum[:12].mean() - lum[-12:].mean()))

    if crossings >= 14 and amp > 3.5 and bg_share < 0.75:
        bg_kind = "striped"
    elif drift > 55 and bg_share < 0.62:
        bg_kind = "gradient"
    elif n_major >= 5 and bg_share < 0.42:
        bg_kind = "photo"
    else:
        bg_kind = "solid"

    gx = np.abs(np.diff(arr, axis=1)).sum(axis=2)[1:, :]   # (95,95) after alignment
    gy = np.abs(np.diff(arr, axis=0)).sum(axis=2)[:, 1:]
    edges = float(((gx > 40) | (gy > 40)).mean())
    return {"bg_share": round(bg_share, 3), "colours": others, "bg": bg_kind,
            "ink": round(1 - bg_share, 3), "edges": round(edges, 3), "bg_hex": hexc(bg)}


# ------------------------------------------------------------------------ ocr ---
_OCR = None


JUNK = re.compile(
    r"(ships? from|unlimited proof|free shipping|personaliz|customi[sz]|size chart|add to cart"
    r"|etsy|handmade|% off|\bsale\b|reviews?\b|fast shipping|made in|order now|click|swipe)", re.I)


def ocr(img: Image.Image) -> tuple[str, float]:
    """Return the *headline* of the artwork, not everything OCR can find.

    Mockup images carry shop badges, "unlimited proofs!" ribbons and size charts,
    so a naive join produces marketing noise instead of the phrase printed on the
    shirt. Type size is the discriminator: on a real shirt design the headline is
    the largest text on the image, which is exactly what the OCR box height measures.
    """
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    out, _ = _OCR(buf.getvalue())
    if not out:
        return "", 0.0

    lines = []
    for box, text, conf in out:
        text = (text or "").strip()
        conf = float(conf)
        if not text or conf < OCR_MIN_CONF or JUNK.search(text):
            continue
        ys = [pt[1] for pt in box]
        lines.append({"t": text, "c": conf, "h": max(ys) - min(ys)})
    if not lines:
        return "", 0.0

    tallest = max(l["h"] for l in lines)
    head = [l for l in lines if l["h"] >= 0.62 * tallest]           # headline + subline
    head.sort(key=lambda l: -l["h"])
    text = " / ".join(l["t"] for l in head[:3])[:MAX_TEXT]
    return text, round(sum(l["c"] for l in head[:3]) / min(3, len(head)), 3)


def norm_phrase(text: str) -> str | None:
    if not text:
        return None
    t = text.lower()
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def layout_of(text: str, st: dict) -> str:
    words = len(text.split())
    if not text:
        return "graphic_only" if st["edges"] > 0.05 else "unreadable"
    if st["edges"] > 0.16 or st["bg"] == "photo":
        return "text_graphic"
    return "text_only" if words else "graphic_only"


# ---------------------------------------------------------------------- main ---
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--cool", type=int, default=30, help="minutes before a claim is released")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    supa = Supa()
    rows = supa._call("POST", "/rest/v1/rpc/claim_dna", {"p_limit": args.limit, "p_cool_minutes": args.cool})
    print(f"[dna] claimed {len(rows)} listings", flush=True)

    done = text_hits = 0
    for row in rows:
        urls = []
        if row.get("gallery"):
            urls = [u for u in row["gallery"] if isinstance(u, str)]
        if row.get("image_url"):
            urls.append(row["image_url"])
        if not urls:
            continue

        best = None
        for url in urls[:MAX_IMAGES]:
            img = load(url)
            if img is None:
                continue
            st = pixels(img)
            txt, conf = ocr(img)
            # A flat artwork with legible text beats a lifestyle photo every time;
            # a photo with no text is the last resort.
            score = st["bg_share"] + (0.8 * conf if txt else 0) - (0.3 if st["bg"] == "photo" else 0)
            if best is None or score > best[0]:
                best = (score, url, st, txt, conf)
            if txt and st["bg_share"] > 0.42:
                break

        if best is None:
            print(f"[dna] {row['etsy_id']} no image could be read", flush=True)
            continue

        _, url, st, txt, conf = best
        lay = layout_of(txt, st)
        phrase = norm_phrase(txt)
        payload = {
            "design_image_url": re.sub(r"/il_[^/.]+\.", "/il_570xN.", url),
            "design_text": txt or None,
            "design_text_conf": conf or None,
            "design_layout": lay,
            "design_bg": st["bg"],
            "design_colours": st["colours"] or None,
            "design_ink": st["ink"],
            "phrase_norm": phrase,
            "design_dna_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        done += 1
        text_hits += 1 if txt else 0
        print(f"[dna] {row['etsy_id']} {lay:13s} {st['bg']:8s} conf={conf:.2f} "
              f"\"{(txt or '')[:58]}\"", flush=True)
        if not args.dry_run:
            supa._call("PATCH", f"/rest/v1/listings?etsy_id=eq.{row['etsy_id']}", payload,
                       prefer="return=minimal")

    print(f"[dna] read {done}/{len(rows)} · {text_hits} with legible text", flush=True)
    if not args.dry_run:
        print("[dna] health:", json.dumps(supa._call("POST", "/rest/v1/rpc/dna_health", {})), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
