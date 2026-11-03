#!/usr/bin/env python3
"""
Etsy keyword crawler — SeleniumBase CDP mode, headed under Xvfb.

Design decisions live in docs/adr/:
  0001  engine: sb_cdp.Chrome, warm persistent profile, concurrency 1
  0003  output shape: one occurrence row per (keyword, listing) carrying its snapshot

Runs inside GitHub Actions (ubuntu-latest + Xvfb). Never run two of these at once.

Output (written under --out):
  results.jsonl          one JSON object per occurrence, snapshot embedded
  payloads/<id>.json.gz   embedded page data per listing, for re-parsing
  html/<page>.html.gz     first N result pages, only until selectors are proven
  run.json               run metadata + counters + warnings

Usage:
  python crawler/etsy_crawl.py --keyword "t-shirt" --depth 20 --run-id 12345 --out out/
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse, parse_qs

SEARCH_URL = "https://www.etsy.com/search?q={q}&explicit=1&page={page}"
LISTING_RE = re.compile(r"/listing/(\d+)")

# ────────────────────────────── small parsers ────────────────────────────────


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_money(text: str | None) -> float | None:
    """'$18.00' / 'US$18.00' / '18,00 €' -> float. Returns None if nothing numeric."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        # 1.234,56 -> 1234.56 ; 1,234.56 -> 1234.56
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        if head and len(tail) in (1, 2):
            cleaned = head.replace(".", "") + "." + tail      # 18,00 -> 18.00
        else:
            cleaned = cleaned.replace(",", "")                # 1,234 -> 1234
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_count(text: str | None) -> int | None:
    """'1,234' -> 1234, '1.2k' -> 1200, '3K' -> 3000, '1.2m' -> 1200000."""
    if not text:
        return None
    m = re.search(r"([\d][\d.,]*)\s*([kKmM])?", text)
    if not m:
        return None
    raw, suffix = m.group(1), (m.group(2) or "").lower()
    raw = raw.replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    if suffix == "k":
        val *= 1_000
    elif suffix == "m":
        val *= 1_000_000
    return int(round(val))


def first_number(patterns: list[str], text: str) -> int | None:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return parse_count(m.group(1))
    return None


# ──────────────────────────── block detection ───────────────────────────────


def looks_blocked(title: str, html: str) -> bool:
    """DataDome / interstitial detection. Conservative: only clear signals."""
    t = (title or "").lower()
    if "just a moment" in t or "access denied" in t or "are you a human" in t:
        return True
    if "datadome" in html[:200000].lower() and "captcha" in html[:200000].lower():
        return True
    # An Etsy search page always carries listing links; none at all is suspicious.
    if "etsy.com" in (title or "").lower() and "/listing/" not in html:
        return True
    return False


def human_delay(lo: float = 1.5, hi: float = 3.0) -> None:
    time.sleep(random.uniform(lo, hi))


# ──────────────────────────── search-page parsing ───────────────────────────

CARD_SELECTORS = [
    "div.v2-listing-card__info",       # verified in mdmintz/undetected-testing raw_cdp_etsy.py
    "div[data-listing-id]",
    "li.wt-list-unstyled div[data-palette-listing-image]",
]


def extract_cards(soup, page_no: int) -> list[dict]:
    """Return [{listing_id, url, title, price_text, position}] for one results page."""
    cards: list[dict] = []
    seen: set[str] = set()

    # Strategy 1: anchor scan — most robust against class renames.
    for a in soup.find_all("a", href=True):
        m = LISTING_RE.search(a["href"])
        if not m:
            continue
        lid = m.group(1)
        if lid in seen:
            continue
        seen.add(lid)
        cards.append({
            "listing_id": lid,
            "url": f"https://www.etsy.com/listing/{lid}/",
            "title": " ".join((a.get("title") or a.get_text(" ", strip=True))[:300].split()),
            "price_text": None,
            "position": len(cards) + 1,
            "page": page_no,
        })

    # Strategy 2: enrich from the known card containers (price/title per card).
    enriched = 0
    for sel in CARD_SELECTORS:
        for node in soup.select(sel):
            lid = None
            parent = node.find_parent(lambda t: t.name and t.get("data-listing-id"))
            if parent and parent.get("data-listing-id"):
                lid = parent["data-listing-id"]
            if not lid:
                a = node.find("a", href=LISTING_RE)
                if a:
                    lid = LISTING_RE.search(a["href"]).group(1)
            if not lid:
                continue
            for card in cards:
                if card["listing_id"] != lid:
                    continue
                title_node = node.select_one("h3") or node.select_one("[data-listing-card-title]")
                price_node = node.select_one("span.currency-value") or node.select_one(
                    "div.n-listing-card__price")
                if title_node:
                    t = " ".join(title_node.get_text(" ", strip=True).split())
                    if t and len(t) > len(card["title"]) - 1:
                        card["title"] = t[:300]
                if price_node:
                    card["price_text"] = " ".join(price_node.get_text(" ", strip=True).split())[:80]
                enriched += 1
        if enriched:
            break
    return cards


# ─────────────────────────── detail-page parsing ────────────────────────────


def _find_json_ld(soup) -> dict | None:
    import json as _json
    best = None
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = _json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") in ("Product", "IndividualProduct"):
                return item
            if isinstance(item.get("@graph"), list):
                for g in item["@graph"]:
                    if isinstance(g, dict) and g.get("@type") in ("Product", "IndividualProduct"):
                        best = g
    return best


def _breadcrumb_path(soup) -> str | None:
    """Etsy ships a BreadcrumbList JSON-LD block; join the names into one path."""
    import json as _json
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        raw = tag.string or ""
        if "BreadcrumbList" not in raw:
            continue
        try:
            data = _json.loads(raw)
        except Exception:
            continue
        items = data.get("itemListElement") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        names = []
        for it in sorted(items, key=lambda x: x.get("position", 0) if isinstance(x, dict) else 0):
            if not isinstance(it, dict):
                continue
            item = it.get("item")
            name = it.get("name") or (item.get("name") if isinstance(item, dict) else item)
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        if names:
            return " > ".join(names)[:400]
    return None


def extract_detail(html: str, soup, url: str) -> dict:
    """Layered extraction: JSON-LD -> embedded state -> DOM text. Records which fired."""
    out: dict = {
        "title": None, "price_usd": None, "price_original_usd": None,
        "price_min_usd": None, "price_max_usd": None, "is_on_sale": None,
        "is_range": None, "currency": None, "favorites": None,
        "listing_review_count": None, "listing_rating": None,
        "shop_review_count": None, "shop_rating": None, "shop_name": None,
        "shop_url": None, "is_bestseller": None, "is_star_seller": None,
        "free_shipping": None, "variation_count": None, "images_count": None,
        "category_path": None, "description": None, "description_html": None,
        "thumbnail_url": None,
        "extraction": {"jsonld": False, "embedded": False, "dom": False, "warnings": []},
    }
    ex = out["extraction"]

    # 1) JSON-LD
    ld = _find_json_ld(soup)
    if ld:
        ex["jsonld"] = True
        out["title"] = (ld.get("name") or "").strip() or out["title"]
        offers = ld.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            out["price_usd"] = parse_money(str(offers.get("price"))) if offers.get("price") else None
            out["currency"] = offers.get("priceCurrency")
        agg = ld.get("aggregateRating") or {}
        if isinstance(agg, dict):
            out["listing_rating"] = float(agg.get("ratingValue")) if agg.get("ratingValue") else None
            out["listing_review_count"] = parse_count(str(agg.get("reviewCount"))) \
                if agg.get("reviewCount") else None
        desc = ld.get("description")
        if desc:
            out["description"] = " ".join(str(desc).split())[:20000]
        img = ld.get("image")
        if isinstance(img, list) and img:
            out["images_count"] = len(img)
            img = img[0]
        if isinstance(img, dict):                      # Etsy ships ImageObject{"contentURL": ...}
            img = img.get("contentURL") or img.get("url")
        if isinstance(img, str):
            out["thumbnail_url"] = img

    # 2) Embedded state (Etsy ships a big JSON blob; keys survive markup redesigns)
    embedded_patterns = {
        "favorites": [r'"num_favorers"\s*:\s*(\d+)', r'"favorers_count"\s*:\s*(\d+)'],
        "listing_review_count": [r'"review_count"\s*:\s*(\d+)'],
        "listing_rating": [r'"rating_average"\s*:\s*([\d.]+)'],
        # verified key on a live listing page: "shop_reviews_count": 15362
        "shop_review_count": [r'"shop_reviews?_count"\s*:\s*(\d+)'],
        "shop_rating": [r'"shop_rating_average"\s*:\s*([\d.]+)',
                        r'"shop_review_average"\s*:\s*([\d.]+)'],
        "variation_count": [r'"variations_count"\s*:\s*(\d+)'],
        "images_count": [r'"image_count"\s*:\s*(\d+)'],
    }
    for field, pats in embedded_patterns.items():
        for pat in pats:
            m = re.search(pat, html)
            if m:
                val = m.group(1)
                out[field] = float(val) if field.endswith("rating") else parse_count(val)
                ex["embedded"] = True
                break

    # 3) DOM fallbacks
    page_text = soup.get_text(" ", strip=True) if soup else ""
    if out["favorites"] is None:
        out["favorites"] = first_number([r"([\d.,]+[kKmM]?)\s+favorites?\b"], page_text)
        ex["dom"] = out["favorites"] is not None or ex["dom"]
    if out["listing_review_count"] is None:
        out["listing_review_count"] = first_number([r"([\d.,]+[kKmM]?)\s+reviews?\b"], page_text)
    if out["shop_name"] is None:
        shop = soup.select_one('a[href*="/shop/"]') if soup else None
        if shop:
            out["shop_name"] = " ".join(shop.get_text(" ", strip=True).split())[:120] or None
            href = shop.get("href") or ""
            out["shop_url"] = href.split("?")[0]
    if soup:
        # Description: the rendered block is the authority; JSON-LD is the fallback.
        node = (soup.select_one('[data-product-details-description-text-content]')
                or soup.select_one('[data-id="description-text"]')
                or soup.select_one("#listing-description"))
        if node:
            out["description_html"] = str(node)
            dom_desc = node.get_text("\n", strip=True)[:20000]
            if dom_desc:
                out["description"] = dom_desc
            ex["dom"] = True

        # Variations: one <select id="variation-selector-N"> per variation type.
        vsel = soup.select('select[id^="variation-selector"]')
        if vsel:
            out["variation_count"] = sum(len(s.find_all("option")) for s in vsel)
            ex["dom"] = True

        # Thumbnail fallback: OpenGraph image is always present on listing pages.
        if not out["thumbnail_url"]:
            og = soup.find("meta", attrs={"property": "og:image"})
            if og and og.get("content"):
                out["thumbnail_url"] = og["content"]

        # Category path from the BreadcrumbList JSON-LD block.
        if not out["category_path"]:
            out["category_path"] = _breadcrumb_path(soup)
    out["is_bestseller"] = bool(re.search(r"\bbestseller\b", page_text, re.I))
    out["is_star_seller"] = bool(re.search(r"\bstar seller\b", page_text, re.I))
    out["free_shipping"] = bool(re.search(r"\bfree shipping\b", page_text, re.I))

    # Favorites fallback: the OpenGraph description states it when the listing has
    # any ("... has 1,100 favorites from Etsy shoppers"). New listings omit the
    # clause entirely, which means zero — not "unknown".
    if out["favorites"] is None and soup:
        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            m = re.search(r"has\s+([\d.,]+[kKmM]?)\s+favorites", og["content"])
            if m:
                out["favorites"] = parse_count(m.group(1))
                ex["dom"] = True
            elif "is sold by" in og["content"]:
                out["favorites"] = 0
                ex["dom"] = True

    # Price range / sale inference from the visible price block
    price_block = soup.select_one('[data-buy-box-region="price"]') if soup else None
    if price_block:
        block_text = " ".join(price_block.get_text(" ", strip=True).split())
        prices = re.findall(r"(?:US\$|\$|€|£)\s?([\d.,]+)", block_text)
        if len(prices) >= 2:
            vals = [parse_money(p) for p in prices]
            vals = [v for v in vals if v is not None]
            if vals:
                out["price_min_usd"], out["price_max_usd"] = min(vals), max(vals)
                out["is_range"] = len(set(vals)) > 1
                if out["is_on_sale"] is None:
                    out["is_on_sale"] = bool(re.search(r"was price|original price|sale price",
                                                       block_text, re.I))
    if out["currency"] and out["currency"] != "USD":
        ex["warnings"].append(f"currency_not_usd:{out['currency']}")
    if out["favorites"] is None:
        ex["warnings"].append("favorites_missing")
    if out["price_usd"] is None:
        ex["warnings"].append("price_missing")
    return out


def extract_payload(html: str) -> str | None:
    """Grab the largest inline JSON blob for re-parsing later (bounded to 400 KB)."""
    best = None
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S):
        blob = m.group(1)
        if best is None or len(blob) > len(best):
            best = blob
    if best is None:
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', html, re.S)
        if m:
            best = m.group(1)
    return best[:400_000] if best else None


def build_payload(html: str, soup, detail: dict, url: str) -> dict:
    """Everything needed to re-derive a row without re-crawling (ADR 0003).

    Etsy no longer ships a fat <script type="application/json"> block on listing
    pages, so the payload is assembled from the three tiers we actually use:
    JSON-LD, OpenGraph/meta, and the description HTML block.
    """
    import json as _json

    meta = {}
    if soup:
        for tag in soup.find_all("meta"):
            k = tag.get("property") or tag.get("name")
            v = tag.get("content")
            if k and v:
                meta[k] = v
    jsonld = None
    if soup:
        for tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                jsonld = _json.loads(tag.string or "")
                break
            except Exception:
                continue
    return {
        "url": url,
        "html_length": len(html),
        "jsonld": jsonld,
        "meta": meta,
        "blob": extract_payload(html),
        "description_html": detail.get("description_html"),
    }



# ──────────────────────────────── the crawl ─────────────────────────────────


class Crawl:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out)
        (self.out / "payloads").mkdir(parents=True, exist_ok=True)
        (self.out / "html").mkdir(parents=True, exist_ok=True)
        self.results_fh = open(self.out / "results.jsonl", "a", encoding="utf-8")
        self.budget_left = args.max_requests
        self.challenges = 0
        self.pages_done = 0
        self.occurrences = 0
        self.seen: dict[str, dict] = {}   # listing_id -> card (first sighting wins)

    # -- browser ------------------------------------------------------------
    def launch(self):
        from seleniumbase import sb_cdp
        kwargs = dict(
            user_data_dir=self.args.user_data_dir,
            incognito=False,              # ADR 0001: warm profile, not cold
            headless=False,               # slider needs a real pointer
            ad_block=True,
            lang="en-US",
            tzone="America/New_York",     # US locale -> Etsy serves USD
        )
        # CI containers: Chromium's sandbox needs unprivileged user namespaces,
        # which GitHub's kernel config and AppArmor deny.
        if os.environ.get("CI"):
            kwargs["sandbox"] = False
        if self.args.browser_executable_path:
            kwargs["browser_executable_path"] = self.args.browser_executable_path
        if self.args.proxy:
            kwargs["proxy"] = self.args.proxy
        print(f"[crawl] launching CDP chrome, profile={self.args.user_data_dir}", flush=True)
        return sb_cdp.Chrome(**kwargs)

    def guard(self, sb, page_label: str) -> bool:
        """Return True if we may continue on this page."""
        title = ""
        try:
            title = sb.get_page_title() or ""
        except Exception:
            pass
        html = ""
        try:
            html = sb.get_page_source() or ""
        except Exception:
            pass
        if looks_blocked(title, html):
            self.challenges += 1
            print(f"[crawl] challenge detected on {page_label} (title={title!r})", flush=True)
            self.solve(sb, page_label)
            try:
                html = sb.get_page_source() or ""
                title = sb.get_page_title() or ""
            except Exception:
                pass
            if looks_blocked(title, html):
                print("[crawl] still blocked after solve; pausing 15 minutes", flush=True)
                time.sleep(900)
                try:
                    sb.refresh()
                    html = sb.get_page_source() or ""
                    title = sb.get_page_title() or ""
                except Exception:
                    return False
                return not looks_blocked(title, html)
        return True

    def solve(self, sb, label: str):
        try:
            sb.solve_captcha()
            print(f"[crawl] solve_captcha() attempted on {label}", flush=True)
        except Exception as e:
            print(f"[crawl] solve_captcha failed: {e}", flush=True)
        human_delay(2, 3)

    # -- discovery ----------------------------------------------------------
    def discover(self, sb):
        kw = self.args.keyword
        for page_no in range(1, self.args.depth + 1):
            if self.budget_left <= 0:
                print("[crawl] request budget exhausted during discovery", flush=True)
                break
            url = SEARCH_URL.format(q=quote_plus(kw), page=page_no)
            self.budget_left -= 1
            print(f"[crawl] search page {page_no}/{self.args.depth}: {url}", flush=True)
            try:
                sb.goto(url)
            except Exception as e:
                print(f"[crawl] goto failed: {e}", flush=True)
                break
            human_delay(1.0, 1.8)
            if not self.guard(sb, f"search p{page_no}"):
                print(f"[crawl] blocked, stopping discovery at page {page_no}", flush=True)
                break
            # Lazy-loaded cards: scroll to force render (verified approach in raw_cdp_etsy.py)
            try:
                for _ in range(15):
                    sb.scroll_down(15)
                    sb.sleep(0.15)
                sb.click_if_visible('button[aria-label="Ok"]')
            except Exception:
                pass
            soup = sb.get_beautiful_soup() if hasattr(sb, "get_beautiful_soup") else None
            if soup is None:
                print("[crawl] no soup; aborting", flush=True)
                break
            if page_no <= self.args.save_html_pages:
                with gzip.open(self.out / "html" / f"page_{page_no}.html.gz", "wt",
                               encoding="utf-8") as fh:
                    fh.write(str(soup))
            cards = extract_cards(soup, page_no)
            new = [c for c in cards if c["listing_id"] not in self.seen]
            for c in new:
                self.seen[c["listing_id"]] = c
            self.pages_done += 1
            print(f"[crawl] page {page_no}: {len(cards)} cards, {len(new)} new "
                  f"(total {len(self.seen)})", flush=True)
            if not new:
                print("[crawl] coverage stop: no new listings on this page", flush=True)
                break
            human_delay()

    # -- detail -------------------------------------------------------------
    def details(self, sb):
        items = list(self.seen.values())
        print(f"[crawl] visiting {len(items)} unique listings", flush=True)
        for i, card in enumerate(items, 1):
            if self.budget_left <= 0:
                print("[crawl] request budget exhausted during detail pass", flush=True)
                break
            lid = card["listing_id"]
            self.budget_left -= 1
            try:
                sb.goto(card["url"])
            except Exception as e:
                print(f"[crawl] detail goto failed {lid}: {e}", flush=True)
                continue
            human_delay(0.8, 1.5)
            if not self.guard(sb, f"listing {lid}"):
                print(f"[crawl] blocked on listing {lid}", flush=True)
                continue
            soup = sb.get_beautiful_soup()
            html = sb.get_page_source()
            detail = extract_detail(html, soup, card["url"])
            if i <= self.args.save_detail_html:
                with gzip.open(self.out / "html" / f"detail_{lid}.html.gz", "wt",
                               encoding="utf-8") as fh:
                    fh.write(html)
            payload_path = f"payloads/{lid}.json.gz"
            with gzip.open(self.out / payload_path, "wt", encoding="utf-8") as fh:
                json.dump(build_payload(html, soup, detail, card["url"]), fh,
                          ensure_ascii=False, default=str)

            row = {
                "run_id": self.args.run_id,
                "keyword": self.args.keyword,
                "page": card["page"],
                "position": card["position"],
                "listing_id": lid,
                "url": card["url"],
                "captured_at": now(),
                "title": detail["title"] or card["title"],
                "price_usd": detail["price_usd"],
                "price_original_usd": detail["price_original_usd"],
                "price_min_usd": detail["price_min_usd"],
                "price_max_usd": detail["price_max_usd"],
                "is_on_sale": detail["is_on_sale"],
                "is_range": detail["is_range"],
                "currency": detail["currency"] or "USD",
                "favorites": detail["favorites"],
                "listing_review_count": detail["listing_review_count"],
                "listing_rating": detail["listing_rating"],
                "shop_review_count": detail["shop_review_count"],
                "shop_rating": detail["shop_rating"],
                "shop_name": detail["shop_name"],
                "shop_url": detail["shop_url"],
                "is_bestseller": detail["is_bestseller"],
                "is_star_seller": detail["is_star_seller"],
                "free_shipping": detail["free_shipping"],
                "variation_count": detail["variation_count"],
                "images_count": detail["images_count"],
                "category_path": detail["category_path"],
                "thumbnail_url": detail["thumbnail_url"],
                "description": detail["description"],
                "description_html": detail["description_html"],
                "description_chars": len(detail["description"] or ""),
                "raw_payload_path": payload_path,
                "extraction": detail["extraction"],
            }
            self.results_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.results_fh.flush()
            self.occurrences += 1
            if i % 10 == 0:
                print(f"[crawl] {i}/{len(items)} listings done, budget left {self.budget_left}",
                      flush=True)
            human_delay()

    # -- entry --------------------------------------------------------------
    def run(self):
        sb = None
        status, error = "succeeded", None
        try:
            sb = self.launch()
            sb.goto("https://www.etsy.com/")
            human_delay(1.5, 2.5)
            self.guard(sb, "home")
            self.discover(sb)
            self.details(sb)
        except Exception as e:
            status = "failed"
            error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            try:
                if sb:
                    sb.quit()
            except Exception:
                pass
            self.results_fh.close()
            meta = {
                "run_id": self.args.run_id,
                "keyword": self.args.keyword,
                "depth": self.args.depth,
                "status": status,
                "error": error,
                "started_at": self.args.started_at,
                "finished_at": now(),
                "pages_done": self.pages_done,
                "occurrences": self.occurrences,
                "unique_listings": len(self.seen),
                "challenges": self.challenges,
                "requests_used": self.args.max_requests - self.budget_left,
                "budget": self.args.max_requests,
            }
            (self.out / "run.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            print("[crawl] run.json:", json.dumps(meta), flush=True)
        return 0 if status == "succeeded" else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Crawl one Etsy keyword.")
    p.add_argument("--keyword", required=True)
    p.add_argument("--depth", type=int, default=20, choices=range(1, 101))
    p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID") or str(int(time.time())))
    p.add_argument("--out", default="out")
    p.add_argument("--max-requests", type=int, default=0,
                   help="hard request budget; default = depth*3 + 200")
    p.add_argument("--user-data-dir", default=str(Path.home() / ".etsy-profile"))
    p.add_argument("--browser-executable-path", default=None)
    p.add_argument("--proxy", default=os.environ.get("CRAWL_PROXY") or None)
    p.add_argument("--save-html-pages", type=int, default=2,
                   help="keep HTML of the first N result pages for selector debugging")
    p.add_argument("--save-detail-html", type=int, default=3,
                   help="keep full HTML of the first N listing pages for selector debugging")
    p.add_argument("--started-at", default=now())
    args = p.parse_args()
    if not args.max_requests:
        args.max_requests = args.depth * 3 + 200
    return Crawl(args).run()


if __name__ == "__main__":
    sys.exit(main())
