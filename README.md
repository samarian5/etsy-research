# Etsy Keyword Research Scraper

Bulk Etsy keyword scraper running on GitHub Actions (DataDome-proof via SeleniumBase CDP mode),
with a local SQLite store and a clean FastAPI/HTMX dashboard.

Built from the working methods of Michael Mintz's channel and repos
(`mdmintz/undetected-testing`, `seleniumbase/SeleniumBase/examples/cdp_mode/`), not from blog posts.
Research trail: [`docs/research/mintz-channel-findings.md`](docs/research/mintz-channel-findings.md).

## Status

| Layer | State |
|---|---|
| Design session | ✅ `CONTEXT.md` glossary + 4 ADRs in `docs/adr/` |
| Research corpus | ✅ 56/63 channel videos transcribed (Apify, $0.114), mined into `docs/research/mintz-channel-findings.md`; his Etsy scripts read from GitHub |
| SQLite schema | ✅ `db/schema.sql` — applies clean, WAL, FTS5 |
| Crawler | ✅ `crawler/etsy_crawl.py` — **validated against live Etsy**: 4 runs, 405 listing rows, zero extraction warnings on the last one |
| Importer | ✅ `db/import_run.py` — verified idempotent (second import skipped) |
| Dashboard | ✅ running at `http://127.0.0.1:8787` on real data: **1,401 listings / 2,198 snapshots / 23 runs**, 2 keywords |
| GitHub Actions workflow | ✅ deployed to `samarian5/etsy-research` (**public** — free unlimited Actions minutes, ADR 0007); **depth-20 crawl complete: 20/20 pages, 1,289 listings** |
| Transport security | ✅ queue sealed in the dispatch input, keywords hashed in logs, results sealed per run (`ETSY_ARTIFACT_KEY`) |
| Self-healing | ✅ `scripts/coverage.py` audits pages from occurrence rows (discovered **and** detailed); `scripts/autopilot.py` syncs and re-queues every hole |
| Hang protection | ✅ per-page `navigate()` deadline (90 s) + stall watchdog (`STALL_MINUTES`, exit 3) + 90-minute job ceiling — a wedged CDP call now costs minutes, not hours (ADR 0008) |
| Deployment path | ✅ Composio GitHub connection — no PAT anywhere; `scripts/deploy.py` pushes, `scripts/sync.py` pulls |

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
