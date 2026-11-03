# etsy-research

A small, polite, **resumable** crawler for Etsy search results, used for one person's
niche and product research. It runs on GitHub Actions, saves what it finds as a
sealed artifact, and is imported into a local SQLite database with a FastAPI
dashboard.

This repository contains **only the crawl job** — no database, no dashboard, no
results. Your data never lives here.

## Why the repository is public

Public repositories get **unlimited free GitHub Actions minutes** on standard
runners; private ones are capped at 2,000/month. This workload needs ~1,600 minutes
per full research pass, so a public repository is the difference between "fits, once"
and "runs whenever I want". The trade-off — every log line and artifact is readable
by anyone — is handled by keeping the *data* encrypted:

| Thing | Where it lives | Who can read it |
|---|---|---|
| Crawl code, workflow, tests | this repo | anyone |
| Keywords being researched | sealed in the dispatch input; logs show `kw-<hash>` | nobody without the key |
| Results, payloads, `run.json` | one AES-256-CBC tarball per run | nobody without the key |

`ETSY_ARTIFACT_KEY` is a repository secret used only to seal and open those blobs;
the plaintext key lives on the researcher's machine. Ciphertext is `openssl enc
-aes-256-cbc -pbkdf2`, base64-armoured, so it decrypts anywhere with `openssl`.

## How a crawl is structured

GitHub kills a job at 6 hours, and a logged-in browser session survives roughly
120–150 Etsy listing pages before anti-bot checks start dominating. So a crawl is
**not** one long job — it is a durable queue:

```json
{"items": [{"keyword": "tshirt", "depth": 20, "page": 4}], "pages_per_run": 3}
```

Each run crawls one page-slice, uploads its artifact, waits 5 minutes, and
re-dispatches itself with the remainder. Depth is limited only by chain length; a
crash costs one chunk. Listing ids fetched by the previous chunk travel with the
queue (`--skip-ids`) so nothing is visited twice.

`run.json` records `stopped_reason` — `coverage`, `budget`, `time`, `blocked`,
`challenges` or `null` — and `blocked` retries the slice once on a cold browser
profile before giving up.

## Politeness

This is personal research, not a scraper farm:

- 1.5–3 s of jittered delay between page loads, a 300 s cooldown between chunks,
  and a hard per-run request budget;
- one browser at a time, never parallel tabs (`concurrency: group: etsy-crawl`);
- images, fonts, media and trackers are blocked at the CDP layer, so each listing
  costs the site ~150 KB instead of ~1 MB;
- bounded challenge handling: 90 s per solver attempt, 6 challenges per run, then
  the run stops cleanly and keeps the rows it already has.

Nothing here republishes, resells or redistributes Etsy content. If you are the
site owner and want this stopped, open an issue and it stops.

## Layout

```
crawler/etsy_crawl.py      one keyword, one page slice (SeleniumBase CDP + Xvfb)
crawler/run_chunk.py       queue -> one chunk -> next queue
crawler/artifact_crypt.py  openssl-based sealing of queue and results
.github/workflows/scrape.yml  the chained job
tests/                     parsers, real captured markup, queue arithmetic, crypto
```

Tests run on every job before the crawl starts: `python -m unittest discover -s tests`.
