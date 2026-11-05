# ADR 0010 — Parallel worker pool: the queue is the load balancer

- **Status:** Accepted
- **Date:** 2026-11-05
- **Supersedes:** the single-flight clause of ADR 0001 (one crawl at a time)

## Context

The crawler was single-flight: one concurrency group (`etsy-crawl`), one slice per
run, one keyword at a time. That was never a database constraint — `claim_slice()`
has used `for update skip locked` since 0001 — it was a scheduling choice made when
there was one keyword and one thing to prove: that a GitHub runner could survive
DataDome at all.

That choice now costs real throughput. The queue holds many keywords, each with its
own cadence, and a single slot can only advance one of them per run. The user asked
for "as fast as possible, as scalable as possible", and offered extra repositories.

The wrong answer is extra repositories. Parallelism comes from the **concurrency
group name**, not from the repo: two jobs in one repo with different group names run
at the same time, and two repos buy nothing that one repo cannot. Extra repos would
mean three sets of secrets, three workflow copies, three deploy paths to keep in
sync — three ways to drift, for zero capacity.

## Decision

**N worker slots (`w1..wN`) consume one Postgres queue, and a dispatcher decides how
many slots to start.**

1. **The queue is the coordinator.** `claim_slice()` already hands each caller a
   different keyword (`for update skip locked`) and pushes `due_at` forward inside
   the same transaction, so a keyword cannot be claimed twice within its interval.
   No new locking was needed — only a `worker` label on `runs` (migration 0009).
2. **One concurrency group per slot.** `group: etsy-crawl-${{ inputs.worker || 'w1' }}`.
   `w2` and `w3` run simultaneously; the same slot never overlaps itself.
3. **One Chrome profile per slot.** The cache key includes the slot. Two workers
   sharing one browser identity would present the same DataDome fingerprint from
   two egress IPs at once — the most conspicuous pattern available.
4. **The dispatcher is the balancer.** `queue_health()` returns budget left, keywords
   due, pages of demand and which slots are live; the dispatcher starts
   `min(free_slots, due_keywords, budget_left ÷ pages_per_run)` workers. Idle ticks
   start nothing. A per-slot cooldown (480 s) stops a slot being dispatched twice
   before its job has even registered.
5. **The budget stays global.** `max_pages_per_day` is summed over every run of the
   day regardless of worker, so adding slots cannot silently multiply the load on
   Etsy — the balancer fills slots *up to* the budget, never past it.
6. **Timer every 15 minutes**, not hourly: the queue decides what is due, so a
   check that costs one API call is cheaper than an idle hour of latency.

## Consequences

- Throughput = `max_pages_per_day` × ~44 listings/page, spread over up to N slots.
  Parallelism is bounded by (keywords due) and (budget left) — with two keywords
  there is no point starting three workers, and the balancer says so.
- A dead slot cannot wedge a keyword: `due_at` was already pushed at claim time.
- Runs carry `worker`, so the board and SQL can show which slot produced what.
- The extra repository (`samarian5/samarian5.github.io`) stays what it is: a root
  forwarder for the hosted board, not a crawler.
- Blocked-page behaviour is unchanged per slot (one solve + one retry, three strikes
  and the slice aborts). More slots means more independent chances of a block, which
  is why the budget — not the slot count — is the throttle that matters.

## Rejected alternatives

- **Multiple crawler repositories for load balancing.** Same capacity, three copies
  of the workflow and secrets, guaranteed to drift.
- **Several tabs in one browser.** One browser is one session is one DataDome
  budget; parallel tabs raise the request rate without raising the ceiling.
- **Proxy / IP rotation.** Evasion of an anti-bot control, and the fastest way to get
  the whole research path blocked. The budget and the 1.5–3 s jitter stay.
- **A queue service (Redis, SQS).** `claim_slice()` in Postgres is already the lock,
  the ledger and the cadence. One system, no new dependency.
