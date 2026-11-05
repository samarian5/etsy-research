"""PostgREST client for the Etsy store — stdlib only.

The crawl runs on a throwaway GitHub runner: every dependency is a chance for
the run to die before it produces rows. This module therefore speaks plain
HTTP with urllib and nothing else.

Auth model
    service key  → used here, on the runner only. Bypasses RLS, may write.
    anon key     → used by the browser. RLS grants SELECT, plus the four
                   token-gated RPCs; it cannot touch a table directly.

The two entry points that matter are claim_slice() (atomically take the next
slice out of the queue) and ingest() (write listings + snapshots in one call).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class SupaError(RuntimeError):
    pass


class Supa:
    def __init__(self, url: str | None = None, key: str | None = None, timeout: int = 60):
        self.url = (url or os.environ["SUPABASE_URL"]).rstrip("/")
        self.key = key or os.environ["SUPABASE_SERVICE_KEY"]
        self.timeout = timeout

    # -- transport ---------------------------------------------------------
    def _call(self, method: str, path: str, body=None, prefer: str | None = None,
              retries: int = 4) -> object:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer

        last = None
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(f"{self.url}{path}", data=data,
                                         method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:600]
                # 4xx other than 429 is a bug in the request, not a blip.
                if e.code < 500 and e.code != 429:
                    raise SupaError(f"{method} {path} -> {e.code}: {detail}") from None
                last = f"{e.code}: {detail}"
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(min(2 ** attempt, 10))
        raise SupaError(f"{method} {path} failed after {retries} tries: {last}")

    # -- queue -------------------------------------------------------------
    def peek_due(self) -> list[dict]:
        """What a tick would claim, without claiming it. Used by the pre-flight job."""
        return self._call("POST", "/rest/v1/rpc/peek_due", {}) or []

    def usage_today(self) -> dict:
        """Pages spent against today's budget, plus slice outcomes."""
        rows = self._call("GET", "/rest/v1/usage_today?select=*")
        return (rows or [{}])[0] if isinstance(rows, list) else (rows or {})

    def claim_slice(self, pages: int | None = None) -> dict | None:
        rows = self._call("POST", "/rest/v1/rpc/claim_slice",
                          {"p_pages": pages} if pages else {})
        return rows[0] if rows else None

    def finish_slice(self, keyword: str, next_page: int, status: str | None = None,
                     listings: int = 0) -> None:
        self._call("POST", "/rest/v1/rpc/finish_slice",
                   {"p_keyword": keyword, "p_next_page": next_page,
                    "p_status": status, "p_listings": listings})

    def add_keyword(self, keyword: str, depth: int = 20, pages_per_run: int = 2,
                    interval_hours: int = 2, token: str | None = None) -> object:
        token = token or os.environ["SUPABASE_WRITE_TOKEN"]
        return self._call("POST", "/rest/v1/rpc/add_keyword",
                          {"p_token": token, "p_keyword": keyword, "p_depth": depth,
                           "p_pages_per_run": pages_per_run,
                           "p_interval_hours": interval_hours})

    def update_keyword(self, keyword: str, interval_hours: int | None = None,
                       depth: int | None = None, pages_per_run: int | None = None,
                       token: str | None = None) -> object:
        """Change cadence/depth for one keyword. None means 'leave that field alone'."""
        token = token or os.environ["SUPABASE_WRITE_TOKEN"]
        return self._call("POST", "/rest/v1/rpc/update_keyword",
                          {"p_token": token, "p_keyword": keyword,
                           "p_interval_hours": interval_hours, "p_depth": depth,
                           "p_pages_per_run": pages_per_run})

    def set_interval_bulk(self, keywords: list[str], interval_hours: int,
                          token: str | None = None) -> int:
        token = token or os.environ["SUPABASE_WRITE_TOKEN"]
        return self._call("POST", "/rest/v1/rpc/set_interval_bulk",
                          {"p_token": token, "p_keywords": keywords,
                           "p_interval_hours": interval_hours}) or 0

    def set_page_budget(self, pages_per_day: int, token: str | None = None) -> int:
        token = token or os.environ["SUPABASE_WRITE_TOKEN"]
        return self._call("POST", "/rest/v1/rpc/set_page_budget",
                          {"p_token": token, "p_pages_per_day": pages_per_day}) or 0

    def prune(self, keep_days: int = 7) -> int:
        return self._call("POST", "/rest/v1/rpc/prune_snapshots",
                          {"p_keep_days": keep_days}) or 0

    # -- runs --------------------------------------------------------------
    def start_run(self, run_id: str, keyword: str, page_from: int, page_to: int,
                  egress_ip: str | None = None, gha_url: str | None = None) -> None:
        self._call("POST", "/rest/v1/runs", [{
            "run_id": run_id, "keyword": keyword, "page_from": page_from,
            "page_to": page_to, "status": "running",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "egress_ip": egress_ip, "gha_url": gha_url,
        }], prefer="resolution=merge-duplicates,return=minimal")

    def finish_run(self, run_id: str, status: str, listings: int = 0, rows: int = 0,
                   challenges: int = 0, stopped_reason: str | None = None,
                   note: str | None = None) -> None:
        self._call("PATCH", f"/rest/v1/runs?run_id=eq.{urllib.parse.quote(run_id)}", {
            "status": status, "listings": listings, "rows_written": rows,
            "challenges": challenges, "stopped_reason": stopped_reason, "note": note,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, prefer="return=minimal")

    # -- ingest ------------------------------------------------------------
    KEEP = ("listing_id", "url", "title", "shop_name", "shop_url", "price_usd",
            "currency", "favorites", "listing_review_count", "listing_rating",
            "thumbnail_url", "category_path", "description", "is_bestseller",
            "is_star_seller", "free_shipping", "captured_at", "page", "position")

    def ingest(self, rows: list[dict], run_id: str, keyword: str,
               batch: int = 25) -> dict:
        """Push occurrences. Batching matters: a description is ~5 KB, so 25 rows
        keeps each request around 130 KB — well under PostgREST's body limit and
        small enough that one network blip costs seconds, not the whole chunk."""
        total_rows = total_snaps = 0
        for i in range(0, len(rows), batch):
            chunk = []
            for r in rows[i:i + batch]:
                slim = {k: r.get(k) for k in self.KEEP if r.get(k) is not None}
                slim["listing_id"] = str(r["listing_id"])
                slim["keyword"] = keyword
                for flag in ("is_bestseller", "is_star_seller", "free_shipping"):
                    if slim.get(flag) is not None:
                        slim[flag] = bool(slim[flag])
                chunk.append(slim)
            out = self._call("POST", "/rest/v1/rpc/ingest_batch",
                             {"p_run_id": run_id, "p_rows": chunk})
            total_rows += int((out or {}).get("rows") or 0)
            total_snaps += int((out or {}).get("snapshots") or 0)
        return {"rows": total_rows, "snapshots": total_snaps}

