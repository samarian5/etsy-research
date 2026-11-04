# ADR 0009 — The every-second-day refresh

- Status: accepted
- Date: 2026-11-04
- Builds on: ADR 0005 (chunked queue), 0007 (public repo, sealed transport), 0008 (bounded navigation)

## Context

The requirement is a scraper that can be run **every second day**, indefinitely,
without a human remembering to press anything, and without paying for it. Two
properties follow from that:

1. **The trigger has to live somewhere that is always on.** This laptop is not.
2. **The second observation has to be comparable to the first.** Re-crawling 1,200
   listings is only useful if the difference between the two snapshots is visible.

There is also a constraint the earlier design already imposed: the repository is
public (free unlimited standard-runner minutes, ADR 0007), so the keyword list cannot
sit in a committed file in plaintext.

## Decision

### The trigger

A `schedule:` trigger in `.github/workflows/scrape.yml`:

```yaml
schedule:
  - cron: "10 4 */2 * *"
```

04:10 UTC every second day — off-peak for both the US and EU, and far enough from
GitHub's own busy hour that a queued start does not sit behind other jobs.

### The queue, without a laptop

A cron run has **no dispatch inputs**, and `queue` was previously `required: true`.
It is now optional, and when it is empty the "Open the queue" step reads
`queue.sealed` from the checked-out repository:

```python
raw = os.environ.get("QUEUE_IN") or ""
if not raw.strip() and os.path.exists("queue.sealed"):
    raw = open("queue.sealed").read()
```

`scripts/publish_queue.py` writes that file from the keyword table — one item per
enabled keyword, page 1, the keyword's own depth — seals it with the same
`ETSY_ARTIFACT_KEY` used for artifacts, and commits it through the GitHub API. The
dashboard calls the same function whenever a keyword is added or its depth changes,
so the committed queue cannot drift from what the user actually wants crawled.

A dispatch input still wins over the file, so ad-hoc runs are unchanged.

### The chain

The scheduled run crawls one chunk and self-dispatches the next, exactly like a
manual one. That means a two-keyword, 22-page refresh is a handful of ~30-minute
jobs with 5-minute cooldowns between them, not one long job. It also means the
every-second-day schedule inherits every guard from ADR 0008 — 90-minute ceiling,
navigation deadline, stall watchdog — without new code.

### The reason to re-crawl: deltas

`snapshot` already stores one row per crawl per listing. The read model now carries
the previous observation:

```sql
LEAD(s.favorites) OVER (PARTITION BY s.listing_id ORDER BY s.captured_at DESC) AS fav_prev
```

`fav_delta` / `price_delta` appear inline in the results table (`+11`, `−0.50`), are
exported in the CSV, and there is a `Momentum Δ` sort. A listing seen once shows no
delta, which is honest: one observation cannot have a trend.

## Consequences

- **Cost stays zero.** A full-depth refresh of two keywords is ~2.5 h of standard
  runner time every two days; public repositories are free and unlimited.
- **Unattended failure is quiet.** A failed chunk still uploads its artifact
  (`if: always()`), still chains when it stopped on a block (ADR 0008), and the local
  `scripts/poll_run.sh` / `scripts/coverage.py --repair --dispatch` can recover holes
  on the next opportunity.
- **Where a hole can still appear:** if the machine is off for days, artifacts expire
  after 3 days before they are imported. The rows are not lost to the crawl, only to
  the import window — `coverage.py` will report the pages as missing and re-queue
  them, which costs a re-crawl rather than corrupting the dataset.
- **Etiquette:** the bi-daily pass visits each listing once per run; jitter between
  page loads, single-flight concurrency, and the 5-minute inter-chunk cooldown are
  unchanged. Lowering `--max-depth` lowers the recurring load proportionally.

## Alternatives rejected

- **A local cron/systemd timer.** Only fires when the laptop is awake and online; the
  goal is a schedule that survives a closed lid.
- **Committing a plaintext keyword list.** The research value of a niche keyword list
  is exactly what a public repository would leak.
- **Re-crawling into a new database each time.** Would lose the deltas, which are the
  point of running again.
- **Crawling every listing at full depth every two days for every keyword.** Allowed,
  but `--max-depth` exists because watching the top three pages is usually the useful
  signal, and it cuts the pass by 85 %.
