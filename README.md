# Etsy Keyword Research Scraper

Bulk Etsy keyword scraper running on GitHub Actions (DataDome-proof via SeleniumBase CDP mode),
with a local SQLite store and a clean FastAPI/HTMX dashboard.

Built from the working methods of Michael Mintz's channel and repos
(`mdmintz/undetected-testing`, `seleniumbase/SeleniumBase/examples/cdp_mode/`), not from blog posts.
Research trail: [`docs/research/mintz-channel-findings.md`](docs/research/mintz-channel-findings.md).

## Status

| Layer | State |
|---|---|
| Design session | ✅ `CONTEXT.md` glossary + 9 ADRs in `docs/adr/` |
| Research corpus | ✅ 56/63 channel videos transcribed (Apify, $0.114), mined into `docs/research/mintz-channel-findings.md`; his Etsy scripts read from GitHub |
| SQLite schema | ✅ `db/schema.sql` — applies clean, WAL, FTS5 |
| Crawler | ✅ `crawler/etsy_crawl.py` — **validated against live Etsy** at depth 20: 1,493 listings, 100 % field coverage on title/price/favorites/description |
| Importer | ✅ `db/import_run.py` — verified idempotent (second import skipped) |
| Dashboard | ✅ `http://pc.tail2881d9.ts.net:8787` (Tailscale) — card grid, 5-up, filters with live counts, image cache, price/favorite movement, shortlist |
| GitHub Actions workflow | ✅ deployed to `samarian5/etsy-research` (**public** — free unlimited Actions minutes, ADR 0007); **depth-20 crawl complete: 20/20 pages, 1,289 listings** |
| Transport security | ✅ queue sealed in the dispatch input, keywords hashed in logs, results sealed per run (`ETSY_ARTIFACT_KEY`) |
| Self-healing | ✅ `scripts/coverage.py` audits pages from occurrence rows (discovered **and** detailed); `scripts/autopilot.py` syncs and re-queues every hole |
| Every second day | ✅ `cron: "10 4 */2 * *"` reads `queue.sealed` from the repo — no laptop needed; polished by `scripts/publish_queue.py`, refreshed by the dashboard (ADR 0009) |
| Movement tracking | ✅ `fav_delta` / `price_delta` between crawls, `Momentum Δ` sort, same columns in the CSV |
| Hang protection | ✅ per-page `navigate()` deadline (90 s) + stall watchdog (`STALL_MINUTES`, exit 3) + 90-minute job ceiling — a wedged CDP call now costs minutes, not hours (ADR 0008) |
| Deployment path | ✅ Composio GitHub connection — no PAT anywhere; `scripts/deploy.py` pushes, `scripts/sync.py` pulls |

## The dashboard

Two pages, no build step, no JS framework — Jinja + HTMX + one hand-written stylesheet.

**Products** (`/results`) is a card grid: image, keyword chip with rank page and crawl
count, two-line clamped title, price with strike-through original and % off, reviews and
favorites, then `Etsy ↗` and `View details`.

- Filter row 1: search (FTS5, debounced), research keyword, sort (favorites, reviews,
  **biggest movers**, best rank, price, recency), price band, status.
- Filter row 2: "seen on 2+ crawls" checkbox, min favorites, min reviews, export, reset.
- Every dropdown shows its own result count (`Gaining favorites (129)`), computed in one
  pass over the latest-snapshot CTE (~112 ms) and cached 30 s.
- Filtering is HTMX: the grid swaps in place, `history.replaceState` keeps the URL
  shareable, and the Export link is re-pointed at the new query — otherwise "Export CSV"
  would quietly download the unfiltered set.
- Badges over the image: `Bestseller`, `Sale` (with −% in the price row), `Star seller`;
  top-right shows `♥ +61` — favorites gained since the previous crawl.
- Thumbnails are served from `data/thumbs/` by `/thumb/{id}`: Etsy's CDN returns **WebP**
  (11 KB) when the Accept header allows it and JPEG (120 KB) otherwise, so the type is
  sniffed from magic bytes and cached on disk. A 404 hides the image instead of showing
  a broken icon.

**Keywords** (`/`) is the control room: KPI strip, tracked keywords with depth editing and
`Run now`, add-keywords form, run-everything, and the crawl history table.

**Detail drawer** slides in from the right (Esc closes, body scroll locked): hero image,
shop with ratings, price/favorites with deltas since the last crawl, crawl count, a
favorites-over-time sparkline, and the full description.

The whole grid was measured, not eyeballed: 5 columns of ~322 px at 1832 px wide
(matching the reference), 3 at 1024, 2 at 430, zero horizontal overflow, uniform card
heights, and muted text at 4.58:1 contrast against the card.

## Runs every second day, unattended

`cron: "10 4 */2 * *"` in the workflow starts a refresh every second day at 04:10 UTC.
The queue lives in the repository as `queue.sealed` (AES-256, same key as the
artifacts), so the schedule needs nothing from this machine:

```bash
python scripts/publish_queue.py              # re-seal from the keyword table, commit
python scripts/publish_queue.py --dispatch   # ...and run it now
python scripts/publish_queue.py --max-depth 3   # lighter recurring pass (top 3 pages)
python scripts/publish_queue.py --show       # what the cron will crawl next
```

The dashboard re-publishes that file whenever a keyword is added or its depth changes.
Because the same listings are re-observed, every result row now carries movement since
the previous crawl — `+11` favorites, `−0.50` price — with a `Momentum Δ` sort and the
same columns in the CSV. Decisions: `docs/adr/0009`.

## Cost model (why the repo is public)

GitHub-hosted runners are free and unlimited in public repositories; private ones get
2,000 minutes/month, and this workload needs ~1,700 per full 10-keyword pass. The repo
is therefore public and everything with research value is encrypted: the keyword list
travels sealed inside the `workflow_dispatch` input, logs show `kw-<hash>`, and each
chunk's `out/` is uploaded as a single AES-256-CBC tarball with 3-day retention.
Decisions: `docs/adr/0006` (multiple repos/accounts do not help) and `docs/adr/0007`.

## Architecture

```
GitHub Actions (ubuntu-latest, Xvfb, headed Chrome)
   workflow_dispatch ──► crawl.py  ──► results.jsonl + payloads/ (artifact)
                                             │
                            Composio GitHub connection (no PAT)
                                             ▼
Local box: sync ──► import into SQLite (idempotent, keyed by run_id)
                              │
                              ▼
                    FastAPI + Jinja + HTMX  ──►  browser at 127.0.0.1:8787
```

- **Discovery:** `/search?q=…&page=N` — measured **60 listings/page** (not 48), depth per
  keyword (default 20, max 100), early stop when a page adds no new listings.
- **Chunked queue (ADR 0005):** a crawl is not one job. The queue
  (`{"items":[{keyword,depth,page}],"pages_per_run":8}`) is the unit of work; each run
  crawls one chunk and **re-dispatches itself** with the remainder, so depth is limited
  only by how many chunks the chain runs, and a crash costs one chunk instead of a run.
  Carried listing ids (`--skip-ids`) stop the next chunk re-visiting what the last one
  already fetched.
- **Detail:** one fetch per unique listing per run, parsed for price, favorites, listing + shop
  reviews, description, badges, variations, plus a gzipped payload (JSON-LD + meta +
  description HTML) retained for re-parsing.
- **Model:** `listing` / `occurrence` / `snapshot` (+ raw payloads) — see ADR 0003.
- **Measured throughput:** ~7.5 listings/minute (56 listings in 7m25s, measured on chunk 1
  of run 41685115256), so ~480 listings/hour.

## Decisions of record

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-seleniumbase-cdp-headed-xvfb.md) | SeleniumBase CDP mode, headed under Xvfb, warm persistent profile |
| [0002](docs/adr/0002-github-actions-artifact-pipeline.md) | Runs on GitHub Actions; artifacts pulled home and imported idempotently |
| [0003](docs/adr/0003-listing-occurrence-snapshot-raw-payload.md) | Listing / Occurrence / Snapshot model, raw payloads retained |
| [0004](docs/adr/0004-robots-and-rate-stance.md) | Proceeds against the robots-disallowed search surface, personal use, polite rate |
| [0005](docs/adr/0005-chunked-queue-no-depth-ceiling.md) | Chunked queue + self-dispatch: depth has no job-timeout ceiling |

## Requirements

- Python 3.11+ on the runner (3.13 locally)
- `seleniumbase` (crawler), `beautifulsoup4`, `fastapi`, `uvicorn`, `jinja2` (UI)
- Chrome/Chromium + `xvfb` on the runner (the workflow installs Chrome for Testing if absent)
- The `composio` CLI with GitHub connected — it carries the credentials, so no PAT is stored

## Local use

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app            # dashboard on http://127.0.0.1:8787
```

### Running a crawl

Three equivalent entry points; all of them create a queue that chains itself to
completion on GitHub Actions (ADR 0005):

```bash
# 1. CLI — any number of keywords, any depth up to 100
python scripts/enqueue.py tshirt "personalized mug" --depth 20 --pages-per-run 8

# 2. CLI — everything already in the DB, at each keyword's own depth
python scripts/enqueue.py --from-db

# 3. Dashboard — paste a list into "Add keywords", then press "Run all keywords"

# Pull the results home and import them (idempotent, safe to run repeatedly)
python scripts/sync.py --db data/etsy.db
```

A chunk takes ~40 min for 8 pages; the workflow re-dispatches itself until the queue is
empty, so a depth-20 keyword finishes across 3 runs without supervision.

## Open questions

- The 6 channel videos with captions disabled upstream (notably *Undetectable Automation 2*) are
  not in the research corpus; local ASR is the only route if they become relevant.
- Listing "age" (first listed date) is not reliably exposed on the detail page — captured when
  present, never invented when absent.
- Review *text* and description *analysis* are deliberately out of ingest; both are on-demand
  passes over the shortlist.
