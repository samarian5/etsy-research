# Measured behaviour (live Etsy, 2026-11-03)

Every number here came from a real GitHub Actions run, not an estimate. Re-measure
after Etsy changes markup; the runs are cheap and the artifacts are the evidence.

## Runs

| Run | Keyword | Depth | Listings | Pages | Wall clock | Challenges | Artifact |
|---|---|---|---|---|---|---|---|
| 41652434214 | `tshirt` | 2 | 110 | 2 | 15m50s | 1 (solved in ~4s) | 450 KB |
| 41655788838 | `tshirt` | 1 | 66 | 1 | 9m | 0 | 910 KB |
| 41657968928 | `tshirt` | 2 | 117 | 2 | 17m01s | 0 | 1.3 MB |
| 41661425603 | `tshirt` | 2 | 112 | 2 | 30m07s | 1 (solved, then a pause) | 1.2 MB |
| 41678060632 | `tshirt` | 2 | 0 | 0 | 13m | 1 (unsolvable, poisoned profile) | 478 B |
| 41680843773 | `tshirt` | 1 | 58 | 1 | 17m | 1 (solved) | 932 KB |

### Chunked chain, runs 41685115256 → 41686410969 → 41687596415

One dispatch, three self-chained runs, depth 3, `pages_per_run: 1`:

| Chunk | Pages | New listings | Carried ids | Wall clock | Note |
|---|---|---|---|---|---|
| 41685115256 | 1 | 56 | 0 | 8m00s | 1 challenge, solved in 70s |
| 41686410969 | 2 | 56 of 60 cards | 55 | 6m39s | 4 promoted repeats skipped |
| 41687596415 | 3 | 46 of 60 cards | 110 | 5m40s | 14 repeats skipped, then `chain_complete` |

The chain **dispatched itself twice** and stopped on its own when the queue emptied —
no fourth run was created. Total 158 unique listings in 22 minutes of wall clock
including three cold browser starts.

### Resource blocking, runs 41689377935 → 41690553389

`Network.setBlockedURLs` (22 patterns: images, fonts, media, tracker hosts) before the
first navigation, verified in the log as `blocked 22 resource patterns`:

| | Before | After |
|---|---|---|
| Seconds per listing (detail pass) | 7.1 – 8.4 | **5.2** |
| Listings per hour | ~450 | **~690** |
| Field coverage on the blocked run | — | 57/57 on title, price, favorites, thumbnail, description, category_path, payload |

Blocking images cannot hurt extraction because the thumbnail comes from JSON-LD
`contentURL`, not from a fetched image — the newest chunk proves it end to end.

Run 41661425603 is the first one where **every** extraction tier reported zero warnings.
It also shows the cost of a challenge: 15 min of wall clock for the same 2 pages, so a
challenged run is roughly twice as slow as a clean one and the budget must assume that.

## Incident: the 2-hour 2-page run (run 41667425519)

A depth-2 run dispatched from the dashboard ran **2h05m** without finishing and had to be
cancelled. Cause: `guard()` slept **900 s per blocked page** and the caller then moved on to the
next listing. Once the session was flagged, every subsequent listing paid that 15-minute pause
again — the run was not slow, it was geometrically doomed (110 listings × 15 min, capped only by
the job timeout).

Fix, now in the crawler:

- Bounded escalation per challenge: solve → 60 s → solve → 120 s → solve → 180 s, then give up.
  Worst case ≈ 6 minutes per page instead of unbounded.
- **Circuit breaker**: three consecutive blocked pages stop the run, write `status: "aborted"`,
  `stopped_reason: "blocked"` into `run.json`, keep every collected row, and exit non-zero so the
  poisoned profile is *not* saved to the cache.

Lesson for any retry loop against a bot wall: the pause must be bounded **and** the failure must
be able to end the run. A retry policy that only knows how to wait is a hang with extra steps.

- **Listing cards per search page: 60**, not the 48 assumed in the design session.
- **Page 2 repeats promoted cards**: 60 cards, 50 new → 10 overlaps. Dedupe by listing id
  is not optional, and "coverage stop" must count *new* listings, which it does.
- **Throughput ≈ 7 listings/minute** (~420/hour) end-to-end, including detail page
  loads at 1.5–3 s jitter. At that rate:
  - depth 20 (≈1,150 unique) ≈ **2h45m**
  - depth 40 (≈2,300) ≈ 5h30m — the practical ceiling inside the 6 h job cap
  - depth 50+ needs a split run or a shorter per-listing wait
- **DataDome fires on the first search page of a cold-ish session** and
  `solve_captcha()` clears it in about 4 seconds without a proxy. With the cached
  warm profile the second and third runs had **zero** challenges.
- **Etsy's JSON-LD `image` is a list of `ImageObject` dicts**, not strings — the reason
  thumbnails were null before the fix.
- **`shop_reviews_count` is the JSON key**, singular `shop_review_count` never matches.
- **Variations are `select#variation-selector-N`**; option counts summed across
  selectors match the visible variant count (49 + 23 = 72 on the sample listing).
- **Favorites are not on every listing page.** Established listings render
  "2,141 favorites" in text; brand-new ones omit the line entirely and only say
  "is sold by", which is read as 0 rather than unknown.
- **Shop rating is not on the listing page at all** — only shop review count. Fetching
  it would mean a shop-page visit per seller, which the design deliberately refuses.
- Listing pages are **~1 MB of HTML**; the saved payload (JSON-LD + meta +
  description HTML, gzipped) is **~14 KB**, which is why payload-per-listing is cheap
  and full-HTML-per-listing is not.

## Field coverage, run 41661425603 (112 rows, after both parser fixes)

| Field | Present | Note |
|---|---|---|
| title, price, favorites, images_count, thumbnail, description, description_html, category_path, payload | 112/112 | |
| listing_review_count, listing_rating, shop_review_count | 103/112 | the 9 gaps are listings whose shop/listing has no reviews at all |
| variation_count | 94/112 | null = single-variant listing, not a parse failure |

Warnings emitted: **none**. That is the bar for calling extraction "validated".

## Cost

Weekly depth-20 run ≈ 2h45m on `ubuntu-latest` ≈ 660 min/month against the 2,000
free private-repo minutes. Artifact ~8 MB/run at depth 20.

## Telling a hang from a slow chunk (read this before cancelling anything)

Three runs were cancelled by mistake because elapsed time was judged from the local
shell clock, which on this box ran **65 minutes fast** against real UTC. Always check:

```bash
curl -sI https://api.github.com | grep -i '^date:'   # real time
```

Then read the crawl step's own log, which prints a progress line every 10 listings:

```
[crawl] 130/166 listings done, budget left 102
```

- **Healthy**: that counter advances every ~55 s (≈5.4 s/listing). Let it run.
- **Hung in the solver**: the log stops right after `solve_captcha() attempted` and no
  progress line follows for many minutes. This was real on run 41692014955 (the
  DataDome slider was blocked from rendering by our own image blocking). Fixed: media
  blocking is lifted while a challenge is on screen, the solver runs under a 90 s
  deadline, a 6-challenge budget ends the chunk, and the chain retries that chunk once
  with a cold profile.
- **Hung in discovery**: no `page N: … cards` line. Bounded by `goto` timeouts.

## Chunk sizing (measured)

A session reliably serves ~120–150 listing pages before DataDome starts challenging
every request. Beyond that a chunk burns its time on challenges rather than data.

| Chunk size | Listings | Observed |
|---|---|---|
| 1 page | ~56 | clean, 5.2–8 s/listing |
| 3 pages | ~166 | clean, 5.4 s/listing, 1 challenge cleared in 64 s |
| 8 pages | ~388 | clean at 124 listings, then flagged: 1 listing per several minutes |

So the chain runs **3 pages per chunk** with a **300 s cooldown** between chunks.
Depth 20 = 7 chunks ≈ 2h50m of wall clock, still far below the 350-minute job cap.

## Cost: private vs public repository (measured, 2026-11-03)

Standard GitHub-hosted runners are free in **public** repositories and metered in
private ones (2,000 included minutes per account per month, then $0.006/min for
Linux 2-core). This workload runs at ~7 listings per runner-minute.

| | private repo | public repo |
|---|---|---|
| Depth-20 chain, 1 keyword | ~24 min/chunk × 7 chunks = 168 min | same minutes, $0 |
| 10 keywords × depth 20, weekly | ~6,750 min/mo → ~$29/mo over the cap | $0 |
| Runner-minutes used today (16 runs) | 320 min = 16 % of the monthly cap | not billed |

The switch is only safe because nothing readable leaves the runner: the queue is
sealed in the dispatch input, keywords are logged as `kw-<hash>`, and results are a
single AES-sealed tarball per run with 3-day retention (ADR 0007).

## Page coverage is a first-class number

`scripts/coverage.py` answers "which search pages do I already have?" from
`run.meta_json` (pages_from / pages_to / pages_done), and `scripts/autopilot.py`
syncs finished artifacts, finds the holes and re-queues them, giving up after two
attempts on the same page rather than looping. Without this, deleting one artifact
silently leaves a permanent gap in the niche data.

## A hang has a root cause, and it is in the driver

Run `41727178418` sat silent for **4 h 12 m** after discovering 179 listings. The
stack dump we never got would have shown `asyncio` inside `seleniumbase`'s
`sb_cdp.Chrome.get()`, because that method retries the navigation **with no
timeout** when the cancelled task raises `RuntimeError`:

```python
except RuntimeError:
    self.loop.run_until_complete(self.page.get(url, **kwargs))   # no deadline
```

So "slow" and "hung" were never distinguishable from the log: the same code path
can return in 5 s or never. Two numbers now separate them.

| Signal | Means | Where |
| --- | --- | --- |
| `navigation exceeded 90s` | one page load was abandoned | `Crawl.navigate()` |
| `WATCHDOG: no progress for 10 min` | the whole process is wedged | `Crawl.start_watchdog()` |
| exit code 3 | the watchdog fired (not a crash) | `os._exit(3)` |
| `stopped_reason: "timeout"` | 3 failed navigations ended the slice | `run.json` |

Cost of the guard: a stalled chunk now dies in ≤10 min instead of 5 h, and the
artifact still uploads because the upload step is `if: always()`. Measured on the
runner: `tests/test_watchdog.py` proves a 300 s `goto` is abandoned after 2 s and
that steady progress keeps the watchdog quiet.

## Page coverage, measured from rows

Coverage used to be derived from `run.meta_json`, and an early backfill claimed
`pages_from=1` for runs that started later. The report said **20/20 pages complete**
while pages 4–8 had never been fetched. Occurrence rows settled it:

```
tshirt: 15/20 pages held (75%) | 1095/1095 listings detailed
   never fetched: 4-8
```

`scripts/coverage.py` now asks two questions per page — *was it discovered* (any
`occurrence` row) and *was it detailed* (join to `snapshot`, ≥90 % visited). A page
is only held when both are true. Every listing that was discovered is also
detailed, which is the useful invariant: this crawler never half-keeps a page.

## Final state, depth 20 (2026-11-04)

| Metric | Value |
| --- | --- |
| Pages held for `tshirt` | **20 / 20** (100 %) |
| Listings for `tshirt` | **1,289** unique, all with a detail snapshot |
| Snapshots | 2,086 |
| Second keyword `ceramic mug` | 2 / 2 pages, 112 listings — added via the dashboard, no code change |
| Runs recorded | 22 (16 succeeded) |
| Price range seen | $0.15 – $600.00, mean $28.61 |
| Favorites | mean 714.7, max 47,147 |
| Field coverage | title 100 %, description 99.8 % (2,082 / 2,086) |
| Actions cost | **$0.00** — public repository, standard runners |

Time-to-complete was spread across chunks by design: every chunk is ~20 min, and a
blocked chunk now costs ~3 min before the cold retry instead of ~21 min.

## The three failure modes this crawl actually had

1. **A wedged CDP call** (silent for 4 h 12 m) — fixed by `navigate()` deadlines and
   the stall watchdog (ADR 0008).
2. **A chain that could not heal itself** — `if: success()` on the dispatch step
   stopped the queue from retrying a blocked chunk (ADR 0008 addendum).
3. **A solver that cannot solve** — three 90 s attempts plus 60/120/180 s naps on a
   block page with no captcha (ADR 0008 addendum).

Each one produced the same symptom from the outside — a run that never finishes or
a page that never appears — which is why page coverage is now computed from rows
(`occurrence` ⋈ `snapshot`) and not from what a run claims it did.

## Every second day, for free, without this laptop

The refresh is a `schedule:` trigger in the workflow — `cron: "10 4 */2 * *"` — so
GitHub runs it whether the machine is on, asleep, or in another country. A scheduled
run has no dispatch inputs, so the queue has to live in the repository:

```
scripts/publish_queue.py      # builds from the keyword table, seals, commits queue.sealed
   ↓ (committed, AES-256-CBC, same ETSY_ARTIFACT_KEY as the artifacts)
cron 10 4 */2 * *  →  "Open the queue" step  →  QUEUE_IN empty ⇒ read queue.sealed
   ↓
first chunk  →  5-min cooldown  →  self-dispatch  →  … until the queue is empty
```

Verified end to end on 2026-11-04: a dispatch with an **empty** queue input logged
`queue opened from queue.sealed (scheduled run): 2 item(s), sealed, pages/run 5`, and
the same code path passes locally when run exactly as the workflow runs it.

Decisions that matter:

| Choice | Why |
| --- | --- |
| Queue in the repo, sealed | A cron cannot read the local SQLite file; a plaintext keyword list would be public |
| `queue.sealed` refreshed by the dashboard | Adding a keyword re-seals and commits it, so the cron is never stale |
| `pages_per_run: 5` | One chunk stays under ~35 min, well inside the 90-min ceiling and the stall watchdog |
| Depth = each keyword's own depth | `--max-depth N` caps a recurring pass when full-depth monitoring is unnecessary |
| 04:10 UTC | Off-peak for both US and EU; the politeness budget is per listing, not per hour |

## Movement is the product of a bi-daily crawl

Re-crawling the same listings is only worth anything if the second observation can be
compared with the first. `snapshot` has always kept a row per crawl; the dashboard now
uses the *previous* row:

```sql
LEAD(s.favorites) OVER (PARTITION BY s.listing_id ORDER BY s.captured_at DESC) AS fav_prev
```

`fav_delta` and `price_delta` are shown inline (`+11`, `−0.50`) and exported in the CSV,
and there is a `Momentum Δ` sort. On the data already in the store — listings seen twice
— the top mover had gained 11 favorites between crawls, which is the signal a single
snapshot cannot give.

## An invalid workflow looks like eight mysterious failures

While wiring the bi-daily cooldown, a heredoc body ended up at column 0 inside a
`run: |` block. YAML stops the block scalar there, so the file no longer parsed.
GitHub's response is quiet and confusing: **every push to the branch creates a run
named `.github/workflows/scrape.yml` with `event: push`, zero jobs, and conclusion
`failure`, five seconds apart.** Eight of them appeared in thirty seconds and looked
exactly like a dispatch storm from the chain.

Nothing was looping — a run with no jobs cannot dispatch anything. The lesson is
mechanical, so it is now enforced: `scripts/deploy.py` parses any `.yml` with PyYAML
and refuses to push a workflow without `jobs`/`steps`, and `tests/test_queue.py`
asserts the same shape (triggers, named steps, `always()` on the chaining steps,
optional queue input). A broken workflow can no longer reach the branch.

Related: the cooldown is now computed by a `python -c` one-liner into
`$GITHUB_OUTPUT` (300 s, or 900 s when the next chunk is a retry after a block) — a
single line cannot break a block scalar.

## What actually trips DataDome (measured, 2026-11-04 04:00–05:35 UTC)

Four runs in ninety minutes, same code, same keyword, same target pages:

| Run | Mode | Egress IP | Outcome |
| --- | --- | --- | --- |
| 41771853096 | fast, 5 pages | `172.171.13.58` | 130 listings, then every listing blocked |
| 41775021297 | fast, retry | — | blocked again |
| 41778108727 | fast, retry | — | blocked again |
| 41779000674 | **slow, 1 page** | `20.64.107.49` | 1 challenge on the search page, **cleared after 45 s**, 60/60 listings, zero detail blocks |

Two conclusions, one of them uncomfortable:

1. **The egress IP is the dominant variable.** The same slice that died at 130 listings
   on one Azure address finished cleanly on another. Nothing in our code changed.
2. **The escalation ladder is worth exactly one round.** The 45 s settle cleared the
   search-page challenge. The old 60/120/180 s ladder spent 4.5 minutes per page and
   never cleared a block that had already started. Blocks that survive the first
   round are session/IP-bound, and the only thing that fixes them is a different
   runner.

Cost of the difference: fast is 5.4 s/listing, slow is **7.5 s/listing**. For a
2-page chunk that is ~11 minutes instead of ~8. Given that a block throws away the
rest of the chunk, slow is the better default for scheduled runs — and a scheduled
run has nobody watching to retry it.

Chunk sizing follows from the same numbers: ~150 page loads or ~14 minutes is what a
session survives before DataDome starts refusing. `session_pages()` clamps any
requested `pages_per_run` to `MAX_PAGE_LOADS_PER_SESSION // 55`, so a chunk cannot be
written that asks for more than a session can deliver.
