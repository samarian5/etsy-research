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
| Parallel worker pool | ✅ migration 0009 — slots `w1..wN` share one Postgres queue; the dispatcher scales slots to the due backlog and stops at the daily budget (ADR 0010, supersedes single-flight) |
| Transport security | ✅ queue sealed in the dispatch input, keywords hashed in logs, results sealed per run (`ETSY_ARTIFACT_KEY`) |
| Self-healing | ✅ `scripts/coverage.py` audits pages from occurrence rows (discovered **and** detailed); `scripts/autopilot.py` syncs and re-queues every hole |
| Every second day | ✅ `cron: "10 4 */2 * *"` reads `queue.sealed` from the repo — no laptop needed; polished by `scripts/publish_queue.py`, refreshed by the dashboard (ADR 0009) |
| Movement tracking | ✅ `fav_delta` / `price_delta` between crawls, `Momentum Δ` sort, same columns in the CSV |
| Hang protection | ✅ per-page `navigate()` deadline (90 s) + stall watchdog (`STALL_MINUTES`, exit 3) + 90-minute job ceiling — a wedged CDP call now costs minutes, not hours (ADR 0008) |
| Signal extraction (0010) | ✅ 10 new listing columns from HTML that was already being downloaded — listing date, listing-level vs shop-level reviews, image count, stock, shipping origin, sponsored flag. Zero extra page loads |
| Honest flags (0011) | ✅ the whole-page regexes that invented 44 % star-sellers and 27 % bestsellers are gone; the bogus values were nulled, not guessed |
| Keyword discovery (0010) | ✅ every search page's related-search chips land in `keyword_candidates` — the market naming its own niches |
| Deployment path | ✅ Composio GitHub connection — no PAT anywhere; `scripts/deploy.py` pushes, `scripts/sync.py` pulls |

## What one page load buys you

Page loads are the scarce resource, so the parser takes everything the page
already prints. Nothing below costs an extra request.

| Field | Where it lives in the HTML | What it answers |
|---|---|---|
| `listing_date` | `div.wt-text-caption` → "Listed on Nov 3, 2026" | Is this niche fresh or saturated? (`share_listed_90d`) |
| `reviews` | JSON-LD `aggregateRating.reviewCount` | How much social proof does *this listing* have |
| `shop_reviews`, `shop_rating` | embedded `shop_reviews_count`, shop card | How big is the seller behind it |
| `image_count` | JSON-LD `image[]` | How hard the listing is to beat on presentation |
| `availability`, `quantity` | JSON-LD `offers` | In stock, and how deep the run is |
| `shipping_origin` | JSON-LD `shippingDetails` | Domestic vs overseas shipping promise |
| `price_original_usd`, `discount_pct` | price block, "Sale Price … (50 % off)" | Running a permanent discount, or a real price |
| `is_ad` | card label "Ad" | Sponsored slot — exclude before reading rank |
| `is_bestseller`, `is_star_seller` | `div#bestseller`, `p.wt-text-title-01` **read from the element** | Proven demand vs proven seller |
| `free_shipping` | the delivery block only | Offer strength, without the site-wide promo |
| related searches | `a[href*="search?q="]` chips | The next keywords to crawl, free |

Two traps this replaces, both measured on the saved corpus:

* **Whole-page regexes lie.** "Star Sellers have an outstanding track record…" is
  help text shipped on every listing page, so `\bstar seller\b` matched 44 % of the
  catalogue. The badge is a specific `<p>`; the explainer is inside `<clg-popover>`.
* **Some markup is in every page.** `div.clg-profile-avatar__badge-star-seller`
  ships inside a `<template>` even when the shop is not a star seller, so trusting
  that class reports 100 % of the catalogue. Only rendered label text counts.
* **Two review counts share one page.** 2,253 reviews on the listing and 15,362 on
  the shop are different facts; storing either one as "reviews" silently turns a
  big seller into a popular product.

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

## Hosted board + Supabase (current path)

**Live: <https://samarian5.github.io/etsy-research/>**

The SQLite dashboard above still runs on the laptop, but the system of record is now a
Postgres database on Supabase, and the queue lives there too — which is what makes the
board editable from any browser.

```
 every 2 h ──► GitHub Actions (Xvfb, headed Chrome)
                    │  claim_slice()      ← the queue lives in Postgres
                    ▼
                crawl 2 pages ──► ingest_batch()  (listings + observations, deltas
                    │                               computed server-side)
                    ▼
              Supabase Postgres  ──►  GitHub Pages SPA (anon key, RLS = SELECT only)
                    ▲                        │
                    └──── 4 token-gated RPCs ┘   add / pause / remove keyword, star
```

* `cron "17 */2 * * *"` — twelve slices a day, each one keyword-pair of pages. A slice
  takes 14–17 min, so runs never overlap; a failed slice is retried by the next tick
  because `next_page` only advances on a clean run. The `:17` matters: GitHub delays
  schedules at the top of the hour.
* **GitHub's own scheduler never fired for this repo**, so a systemd timer on the
  always-on laptop calls `scripts/dispatch_slice.py` at `:27` of every even hour to
  trigger the same workflow. The script refuses to dispatch while a slice is running
  or queued, so single-flight holds even with two triggers. If GitHub's cron starts
  working, the guard simply makes the second trigger a no-op.
* **Queue:** `/web` has an "Add to queue" box. A new keyword is picked up within two
  hours, no commit and no dispatch.
* **Writes need a token.** The browser holds only the anon key, which RLS restricts to
  `SELECT`. Adding a keyword calls `add_keyword(p_token, …)`, which checks a SHA-256
  hash stored in `settings`. The token is `SUPABASE_WRITE_TOKEN` in `.env`; the board
  asks for it once and keeps it in `localStorage`.
* **Schema:** `db/supabase/0001_init.sql` (tables, RPCs, RLS) and `0002_fav_gain.sql`
  (the stored momentum column PostgREST cannot express as a filter).
* **Migration:** `python scripts/migrate_sqlite_to_supa.py` replays local history
  chronologically, so `prev_favorites` / `prev_price` end up meaningful.
* **Publish the board:** `python scripts/deploy_pages.py` — assembles an orphan
  `gh-pages` branch through the Git database API (Pages only serves `/` or `/docs`,
  and `/docs` already holds the ADRs).

### Board design (`web/`)

The board is read at a glance, several times a day, to answer one question: *is this
keyword worth entering?* Everything on the page is ordered by that question, and
decoration comes last.

```
KPI strip      four numbers: listings, readings, keywords, search pages used today
Market         one row per keyword — price, demand at the top, how weak the field is
Listings       the products themselves, filtered and sorted, with a detail drawer
Crawler        the queue, intervals, depth, today's budget
Recent slices  what ran, and whether Etsy let it through
```

* **Market** is the decision table. It reads left to right: *listings in crawl* (sample
  size, not market size), *median price* (what buyers pay), *top 10% favourites* (the bar
  a new listing must clear), *weak sellers* (share with <50 reviews — high means the
  field is soft), *listed ≤ 90 days* (high means newcomers still rank), *next check*.
  Source: the `keyword_daily` view, one row per keyword per crawl day, latest row wins.
* **KPI strip** reads `stats` + `usage_today`; the budget bar is the same value the
  crawler enforces in `claim_slice()`, so the page cannot promise pages the queue will
  refuse.
* **Tokens only.** Every colour, space, radius and type size is a CSS variable in
  `:root`; the dark theme is a token override under `prefers-color-scheme`, not a second
  stylesheet. `--brand` is Etsy terracotta, `--up` is money-green.
* **Numbers are tabular** wherever they can change between crawls, so columns do not
  jitter when a row updates.
* **No CDN, no build step, no framework.** GitHub Pages serves four files; anything a
  design library would give (tokens, a grid, cards, a drawer, a segmented control) is
  written directly in `styles.css` + `app.js`. A missing font or blocked CDN cannot
  break the board.
* **Mobile:** under 640 px each table row becomes a labelled block (`data-label` on the
  cell) and the listing grid goes two-up; nothing scrolls sideways.
* **Honest states:** `null` renders as `—`, never as a zero or a guess. Rows crawled
  before a column existed stay dashed until that listing is re-crawled.


### Supabase free tier — what actually constrains it

| Limit | Value | Consequence |
|---|---|---|
| Database | 500 MB | `prune_snapshots()` keeps a week of full history, then one row per day per listing |
| Project pause | 7 days idle | a run every 2 h keeps it awake |
| Egress | 5 GB/month | the board reads ~40 KB per page view; images come from Etsy's CDN, not storage |
| Writes | unlimited (row-based pricing) | two runs/day ≈ 3k rows is far below any threshold |

Measured ingest: **3,168 historical observations → 1,980 rows / 1,637 listings in ~2 min**
(127 batched calls of 25), and a full 2-page slice writes 60–370 rows in seconds.

First live slice (run `41850990686`, 2026-11-04): claimed `tshirt` pages 1–2, crawled for
**14 minutes**, wrote **107 rows / 107 observations** (+48 listings new to the store), hit
**1 challenge**, advanced the queue to page 3, and the board showed it without a redeploy.
A daily budget of 24 search pages is enforced inside `claim_slice()`, so the 2-hour cadence
cannot quietly turn into hammering — raise it in `settings` if you want more.

## Scale-out: shards, laps, and the publish-date backfill

The queue was always multi-consumer — `claim_slice()` takes its row with
`for update skip locked` — so capacity was never limited by the database or by the
number of repositories. It was limited by three things, all now fixed.

### 1. One keyword could only ever be crawled once per interval

`finish_slice()` used to push `due_at` a full `interval_hours` ahead. With `tshirt`
at depth 60 and a 2-hour interval that is a **60-hour lap**, no matter how many
workers were idle. `0013_slice_gap.sql` splits the two ideas:

| state after a slice | next due |
|---|---|
| pages left in this lap | `now() + slice_gap_seconds` (180 s) |
| lap finished (`next_page > depth`) | `now() + interval_hours` |

So `interval_hours` now means "how often this keyword is revisited", and the gap
means "how closely two slices of the same lap may follow". A full lap of both
target keywords takes about two hours at six shards instead of two and a half days.

### 2. Parallelism is a matrix, not twenty forks

`supa-crawl.yml` takes `shards` (a JSON array, default `["w1","w2","w3"]`) and
`max_parallel`. The `slice` job expands that list as `strategy.matrix.worker`, and
its `concurrency.group` is `etsy-crawl-${{ matrix.worker }}` — evaluated per matrix
instance, so a slot never overlaps itself while different slots run side by side.

**Why not twenty repositories?** Twenty forks would multiply the Chrome profile
caches, the secret surface and the log streams, and add twenty schedulers that can
drift out of sync — for exactly the same number of parallel crawlers, because the
concurrency-group name is what creates parallelism. The ceiling here is
`--shards 20` in `scripts/dispatch_slice.py`.

Two bugs surfaced while proving this, both fixed and both now covered:

* every shard shared `GITHUB_RUN_ID`, so the second one died on
  `runs.run_id` uniqueness — the run id is now `<run>-<slot>`;
* a shard killed between `claim_slice()` and `finish_slice()` left its keyword in
  `status='running'`, and the old `queue_health()` only counted `queued` when
  deciding whether anything was due, so the pool idled behind a dead claim
  (`0015_stale_claims.sql`: due = queued *or* running with a stale heartbeat, and
  `claim_slice()` now reaps stale claims before choosing).

### 3. Publish dates were the missing input, not the missing statistic

Every time-based number — entry score, age cohorts, fastest movers, launch curve —
is derived from `listings.listing_date`. That column only arrives with a **detail**
page load ("Listed on Nov 3, 2026"); search cards carry no date at all. The two
biggest keywords were first collected card-only, so they sat at **3.3% dated**.

Rather than re-crawl blindly, `0014_enrich_queue.sql` adds a claimable backfill
queue: `claim_enrich(n)` hands out the **most-favourited undated listings** first —
the ones the freshness question is actually about — and marks `detail_at` at claim
time so a dead runner releases its claims after a 12-hour cooldown. `scripts/supa_run.py`
claims them (`ENRICH_PER_RUN`, default 8) and passes them to the crawler, which
visits them *before* the slice's own listings through the same parser and payload
path. The date parser was also hardened: `Re-listed on`, non-breaking spaces,
`3 November 2026` and lowercase months all parse now, with cases in
`tests/test_signal.py`.

`enrich_health()` reports the coverage (`dated_pct`) so the backfill is measurable
rather than hopeful.

### The statistics that answer "what is working right now"

| view | question it answers |
|---|---|
| `keyword_entry` | Is this market open? One 0–100 entry score from weak-seller share, new listings in the top 30, shop HHI and winner demand. |
| `age_cohorts` | Do new listings reach the top? Top-20 share and median favourites/day by age band (0–7, 8–30, 31–90, 90+ days). |
| `new_winners` | Which listings under 90 days old are moving? Ranked by favourites per day since publication. |
| `launch_curve` | Does age matter here? Median favourites/day by the week a listing was published. |
| `phrase_stats` | What do winning titles say? Words and two-word phrases ranked by the median favourites of the listings using them (n ≥ 3). |
| `shop_concentration` | Who owns the results? HHI and top-shop share per keyword. |
| `listing_stats` | Per-listing age, observed favourites/day, and whether the age came from Etsy or is missing. |

All seven are `security_invoker`, with `anon` revoked and `SELECT` granted to
`authenticated` only — Supabase grants `anon` everything on new public objects, and
a view without `security_invoker` runs as its owner, which would have published the
whole board to anyone holding the public anon key.

**Favourites are the demand proxy.** Etsy publishes no sold counts, so every view
says so in its header. `fav_per_day` is floored at one day so an eight-hour-old
listing does not look like a rocket.

## Design briefs — from "which market" to "what do I design next"

The market tables answer *where* the demand is. This layer answers *what to make*:
it files every shirt into a buyer-identity niche and puts the winning designs on
screen so the next decision is visual.

```
titles ──► design_niches (29 regexes) ──► listing_niche_mv (matview)
                                              │
                    niche_stats ◄─────────────┤
                         │                    └──► niche_examples (6 winners + thumbnail)
                    design_briefs  ← demand vs saturation, shrunk for small samples
```

**Opportunity score.** `0.55 × demand + 0.25 × freshness + 0.20 × openness`, then
multiplied by `min(1, listings / 30)`:

* **demand** — percentile of the niche's p90 favourites inside that keyword, i.e. what
  a top-10 % listing there actually has;
* **openness** — inverse percentile of listing count (fewer listings, more room);
* **freshness** — share published in the last 90 days, and only counted when ≥30 % of
  the niche has a publish date; otherwise it is neutral. Without that rule the still-
  running backfill made every niche look like a gold rush;
* the small-sample shrink stops a 9-listing niche with one viral shirt from
  outranking a 70-listing niche with a real median.

**Designs, not just numbers.** `niche_examples` returns the six most-favourited shirts
per niche with `image_url`, which the board renders as a 570 px thumbnail grid
(Etsy serves a 2,000 px original by default; the CDN accepts a resize segment).
Read the *idea and the phrasing*, never the artwork itself.

**What they are printed on.** `production_stats` reads the blank and the method out of
descriptions — Comfort Colors, Gildan, Bella+Canvas, DTF, DTG, screen print,
embroidery — with median favourites per method. On the current sample Comfort Colors
appears on 41 % of shirts and screen-printed shirts carry the highest median
favourites, which is the cost-and-margin half of the decision.

**Why a matview.** Matching 2,600 titles against 29 regexes took longer than
PostgREST's statement timeout, and `?limit=6` is applied *after* the view — the panel
rendered empty with a 57014. `listing_niche_mv` is refreshed once per crawl
(`refresh_design_views()`, called at the end of `scripts/supa_run.py`) with
`REFRESH ... CONCURRENTLY`, so readers keep the previous snapshot.

**Two honesty guards on age.** `0016_age_guard.sql` rejects a publish date later than
`first_seen` or earlier than 2015, and refuses to rank a listing whose "age" is 0–2
days but which already holds 1,000+ favourites with one reading — that is a re-list
inheriting favourites, not demand measured over a day. 59 rows were set aside on the
first run.

## Runs every second day, unattended (legacy artifact path)

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
