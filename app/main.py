"""Etsy keyword research dashboard — FastAPI + Jinja + HTMX, no build step."""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import queries
from . import github_actions as gh
from .text import clamp_depth, parse_phrases


def _load_env() -> None:  # ROOT is defined below; called after it exists
    """The dashboard needs ETSY_ARTIFACT_KEY to seal the scheduled queue."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


ROOT = Path(__file__).resolve().parents[1]
_load_env()
DB_PATH = os.environ.get("ETSY_DB", str(ROOT / "data" / "etsy.db"))
THUMB_DIR = Path(os.environ.get("ETSY_THUMBS", str(Path(DB_PATH).parent / "thumbs")))

app = FastAPI(title="Etsy Keyword Research")
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.filters["money"] = lambda v: "—" if v is None else f"${v:,.2f}"
templates.env.filters["compact"] = lambda v: "—" if v is None else (
    f"{v/1000:.1f}k".replace(".0k", "k") if v >= 1000 else str(int(v)))


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    schema = (ROOT / "db" / "schema.sql").read_text()
    con.executescript(schema)
    return con


def flash(request: Request) -> str | None:
    return request.query_params.get("msg")


# --------------------------------------------------------------- filter args --

FILTER_KEYS = ("keyword", "q", "sort", "min_favorites", "min_reviews", "min_price",
               "max_price", "band", "status", "on_sale", "bestseller", "shortlisted",
               "seen_twice")


def filter_params(keyword: str = "", q: str = "", sort: str = "favorites",
                  min_favorites: str = "", min_reviews: str = "", min_price: str = "",
                  max_price: str = "", band: str = "", status: str = "",
                  on_sale: str = "", bestseller: str = "", shortlisted: str = "",
                  seen_twice: str = "") -> dict:
    return {"keyword": keyword, "q": q, "sort": sort or "favorites",
            "min_favorites": min_favorites, "min_reviews": min_reviews,
            "min_price": min_price, "max_price": max_price, "band": band,
            "status": status, "on_sale": on_sale, "bestseller": bestseller,
            "shortlisted": shortlisted, "seen_twice": seen_twice}


def active_chips(params: dict) -> list[dict]:
    """Human labels for the filters currently applied, each with a URL that drops it."""
    labels, chips = [], []
    if params.get("keyword"):
        labels.append(("keyword", params["keyword"]))
    if params.get("q"):
        labels.append(("search", f'"{params["q"]}"'))
    if params.get("status"):
        labels.append(("status", params["status"].replace("_", " ")))
    if params.get("band"):
        labels.append(("price", params["band"].replace("to", "–").replace("under", "< $")
                       .replace("over", "> $")))
    if params.get("min_favorites"):
        labels.append(("min favs", params["min_favorites"]))
    if params.get("min_reviews"):
        labels.append(("min reviews", params["min_reviews"]))
    if params.get("seen_twice"):
        labels.append(("seen 2+ times", ""))
    if params.get("shortlisted"):
        labels.append(("shortlisted", ""))
    for key, label in labels:
        rest = {k: v for k, v in params.items() if v and k != _chip_key(key)}
        chips.append({"label": label, "key": key,
                      "url": "/results?" + _qs(rest)})
    return chips


def _chip_key(label: str) -> str:
    return {"keyword": "keyword", "search": "q", "status": "status", "price": "band",
            "min favs": "min_favorites", "min reviews": "min_reviews",
            "seen 2+ times": "seen_twice", "shortlisted": "shortlisted"}.get(label, label)


def _qs(params: dict) -> str:
    from urllib.parse import urlencode
    return urlencode({k: v for k, v in params.items() if v})


# ------------------------------------------------------------------- facets --

_FACET_TTL = 30.0
_facet_cache: dict = {"at": 0.0, "data": None}
_facet_lock = threading.Lock()


def facets_cached(con: sqlite3.Connection) -> dict:
    with _facet_lock:
        if _facet_cache["data"] is not None and time.time() - _facet_cache["at"] < _FACET_TTL:
            return _facet_cache["data"]
        data = queries.facets(con)
        _facet_cache["data"], _facet_cache["at"] = data, time.time()
        return data


# ------------------------------------------------------------------- thumbs --

_THUMB_SLOTS = threading.Semaphore(8)
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0.0.0 Safari/537.36")


def _fetch_thumb(url: str) -> tuple[bytes, str, str] | None:
    """Fetch a small thumbnail and identify it by magic bytes.

    Etsy's CDN answers with WebP (~11 KB) when the Accept header allows it and JPEG
    (~120 KB) otherwise — so the type is sniffed from the payload, never assumed.
    """
    candidates = [url.replace("il_fullxfull", "il_340x270"),
                  url.replace("il_570xN", "il_340x270"),
                  url]
    for cand in candidates:
        try:
            req = urllib.request.Request(cand, headers={
                "User-Agent": _UA, "Referer": "https://www.etsy.com/",
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=12) as r:
                blob = r.read(4_000_000)
            if blob[:3] == b"\xff\xd8\xff":
                return blob, "jpg", "image/jpeg"
            if blob[:8] == b"\x89PNG\r\n\x1a\n":
                return blob, "png", "image/png"
            if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
                return blob, "webp", "image/webp"
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
    return None


_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


@app.get("/thumb/{listing_id}")
def thumb(listing_id: str):
    """Serve thumbnails from a local cache — the grid must not hotlink Etsy's CDN."""
    if not listing_id.isdigit():
        raise HTTPException(status_code=404)
    for ext in ("webp", "jpg", "png"):
        path = THUMB_DIR / f"{listing_id}.{ext}"
        if path.exists() and path.stat().st_size > 512:
            return FileResponse(path, media_type=_MIME[ext],
                                headers={"Cache-Control": "public, max-age=604800"})
    con = db()
    row = con.execute("""SELECT thumbnail_url FROM snapshot
                         WHERE listing_id = ? AND thumbnail_url IS NOT NULL
                         ORDER BY captured_at DESC LIMIT 1""", (listing_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=404)
    with _THUMB_SLOTS:
        got = _fetch_thumb(row["thumbnail_url"])
    if not got:
        raise HTTPException(status_code=404)
    blob, ext, mime = got
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    path = THUMB_DIR / f"{listing_id}.{ext}"
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(blob)
    tmp.replace(path)
    return FileResponse(path, media_type=mime,
                        headers={"Cache-Control": "public, max-age=604800"})


# -------------------------------------------------------------------- pages --

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    con = db()
    kws = queries.keywords(con)
    rows = queries.runs(con)
    stats = queries.stats(con)
    con.close()
    return templates.TemplateResponse(request, "keywords.html", {
        "keywords": kws, "runs": rows, "stats": stats, "active": "keywords",
        "configured": gh.configured(), "msg": flash(request),
    })


def _sync_scheduled_queue() -> bool:
    """Re-seal `queue.sealed` so the every-second-day cron picks up keyword edits.

    The cron cannot read this SQLite file, so the committed queue is the contract.
    Best-effort: a network hiccup must not break adding a keyword.
    """
    if not gh.configured():
        return False
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "publish_queue", ROOT / "scripts" / "publish_queue.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        queue = mod.build_queue(DB_PATH, pages_per_run=5)
        if not queue["items"]:
            return False
        res = mod.publish(queue)
        return bool(res.get("committed"))
    except Exception:  # noqa: BLE001 - convenience only
        return False


@app.post("/keywords")
def add_keyword(phrase: str = Form(...), depth: int = Form(20)):
    """Accepts one phrase or a pasted list — one per line or comma separated."""
    con = db()
    added = 0
    for term in parse_phrases(phrase):
        cur = con.execute("INSERT OR IGNORE INTO keyword (phrase, depth) VALUES (?, ?)",
                          (term, clamp_depth(depth)))
        added += cur.rowcount if cur.rowcount > 0 else 0
    con.commit()
    con.close()
    if added:
        _sync_scheduled_queue()
    return RedirectResponse(f"/?msg={added}+keyword(s)+added", status_code=303)


@app.post("/run-queue")
def run_queue(pages_per_run: int = Form(3), profile: str = Form("warm")):
    """Dispatch every enabled keyword as one chained queue; the chain runs to completion."""
    con = db()
    items = [{"keyword": r["phrase"], "depth": int(r["depth"])}
             for r in con.execute("SELECT phrase, depth FROM keyword WHERE enabled = 1 ORDER BY id")]
    con.close()
    if not items:
        return RedirectResponse("/?msg=no+enabled+keywords", status_code=303)
    try:
        gh.dispatch_queue({"items": items, "pages_per_run": max(1, min(20, pages_per_run))},
                          profile=profile)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/?msg={str(e)[:120].replace(' ', '+')}", status_code=303)
    return RedirectResponse(f"/?msg=queue+dispatched+({len(items)}+keyword(s))", status_code=303)


@app.post("/keywords/{kw_id}/depth")
def set_depth(kw_id: int, depth: int = Form(...)):
    con = db()
    con.execute("UPDATE keyword SET depth = ? WHERE id = ?", (max(1, min(100, depth)), kw_id))
    con.commit()
    con.close()
    _sync_scheduled_queue()
    return RedirectResponse("/?msg=depth+updated", status_code=303)


@app.post("/keywords/{kw_id}/run")
def run_keyword(kw_id: int):
    con = db()
    row = con.execute("SELECT phrase, depth FROM keyword WHERE id = ?", (kw_id,)).fetchone()
    con.close()
    if not row:
        return RedirectResponse("/?msg=unknown+keyword", status_code=303)
    if not gh.configured():
        return RedirectResponse("/?msg=github+not+configured", status_code=303)
    try:
        gh.dispatch(row["phrase"], int(row["depth"]))
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/?msg={str(e)[:120].replace(' ', '+')}", status_code=303)
    return RedirectResponse("/?msg=run+dispatched", status_code=303)


@app.post("/sync")
def sync_now():
    try:
        res = gh.sync(DB_PATH)
        n = len(res["imported"])
        extra = f"+{len(res.get('ignored') or [])}+partial" if res.get("ignored") else ""
        return RedirectResponse(f"/?msg=imported+{n}+artifact(s){extra}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/?msg={str(e)[:120].replace(' ', '+')}", status_code=303)


def _results_context(request: Request, params: dict, limit: int = 120) -> dict:
    limit = max(1, min(1200, limit))
    con = db()
    rows = queries.results(con, params, limit=limit)
    total = queries.results_count(con, params)
    facets = facets_cached(con)
    con.close()
    return {
        "rows": rows, "total": total, "params": params, "active": "results",
        "facets": facets, "chips": active_chips(params), "qs": _qs(params),
        "shown": len(rows), "limit": limit, "msg": flash(request),
    }


@app.get("/results", response_class=HTMLResponse)
def results(request: Request, keyword: str = "", q: str = "", sort: str = "favorites",
            min_favorites: str = "", min_reviews: str = "", min_price: str = "",
            max_price: str = "", band: str = "", status: str = "", on_sale: str = "",
            bestseller: str = "", shortlisted: str = "", seen_twice: str = "",
            limit: int = 120):
    params = filter_params(keyword, q, sort, min_favorites, min_reviews, min_price,
                           max_price, band, status, on_sale, bestseller, shortlisted,
                           seen_twice)
    return templates.TemplateResponse(request, "results.html",
                                      _results_context(request, params, limit))


@app.get("/results/cards", response_class=HTMLResponse)
def result_cards(request: Request, keyword: str = "", q: str = "", sort: str = "favorites",
                 min_favorites: str = "", min_reviews: str = "", min_price: str = "",
                 max_price: str = "", band: str = "", status: str = "", on_sale: str = "",
                 bestseller: str = "", shortlisted: str = "", seen_twice: str = "",
                 limit: int = 120):
    """HTMX target: card grid + count line, so typing in the search box never reloads."""
    params = filter_params(keyword, q, sort, min_favorites, min_reviews, min_price,
                           max_price, band, status, on_sale, bestseller, shortlisted,
                           seen_twice)
    ctx = _results_context(request, params, limit)
    return templates.TemplateResponse(request, "_cards.html", {
        "rows": ctx["rows"], "total": ctx["total"], "params": params,
        "chips": ctx["chips"], "shown": ctx["shown"], "limit": ctx["limit"],
        "qs": ctx["qs"],
    })


@app.get("/listing/{listing_id}", response_class=HTMLResponse)
def listing(request: Request, listing_id: str):
    con = db()
    row = queries.listing_detail(con, listing_id)
    hist = queries.price_history(con, listing_id)
    con.close()
    if not row:
        return HTMLResponse("<p class='muted'>Listing not found.</p>", status_code=404)
    return templates.TemplateResponse(request, "_listing.html", {"l": row, "history": hist})


@app.post("/shortlist/{listing_id}/star", response_class=HTMLResponse)
def toggle_star(request: Request, listing_id: str):
    """HTMX target: returns just the button so the grid does not reload."""
    con = db()
    exists = con.execute("SELECT 1 FROM shortlist WHERE listing_id = ?", (listing_id,)).fetchone()
    if exists:
        con.execute("DELETE FROM shortlist WHERE listing_id = ?", (listing_id,))
    else:
        con.execute("INSERT OR IGNORE INTO shortlist (listing_id) VALUES (?)", (listing_id,))
    con.commit()
    con.close()
    return HTMLResponse(templates.get_template("_star.html").render(
        id=listing_id, on=not exists))


@app.post("/shortlist/{listing_id}")
def toggle_shortlist(request: Request, listing_id: str):
    con = db()
    exists = con.execute("SELECT 1 FROM shortlist WHERE listing_id = ?", (listing_id,)).fetchone()
    if exists:
        con.execute("DELETE FROM shortlist WHERE listing_id = ?", (listing_id,))
    else:
        con.execute("INSERT OR IGNORE INTO shortlist (listing_id) VALUES (?)", (listing_id,))
    con.commit()
    con.close()
    return RedirectResponse(request.headers.get("referer") or "/results", status_code=303)


@app.get("/export.csv")
def export_csv(keyword: str = "", q: str = "", sort: str = "favorites",
               min_favorites: str = "", min_reviews: str = "", min_price: str = "",
               max_price: str = "", band: str = "", status: str = "", on_sale: str = "",
               bestseller: str = "", shortlisted: str = "", seen_twice: str = ""):
    params = filter_params(keyword, q, sort, min_favorites, min_reviews, min_price,
                           max_price, band, status, on_sale, bestseller, shortlisted,
                           seen_twice)
    con = db()
    rows = queries.results(con, params, limit=100000)
    con.close()
    buf = io.StringIO()
    cols = list(rows[0].keys()) if rows else ["listing_id"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(dict(r))
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=etsy-results.csv"})


@app.get("/api/health")
def health():
    con = db()
    s = queries.stats(con)
    con.close()
    return {"ok": True, "db": DB_PATH, **s}


def run() -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8787, reload=False)


if __name__ == "__main__":  # pragma: no cover
    run()
