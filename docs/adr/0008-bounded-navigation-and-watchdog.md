# ADR 0008 — Bounded navigation, and a watchdog that cannot be blocked

- Status: accepted
- Date: 2026-11-04
- Supersedes: nothing; extends ADR 0005 (chunked queue) and ADR 0006/0007 (cost)

## Context

Run `41727178418` (the autopilot repair for pages 9–11) discovered 179 listings
and then produced **no output for 4 h 12 m**. It was cancelled by hand. Nothing in
the crawler was waiting on a timer, a queue, or a lock: it was inside the first
detail navigation.

Reading `vendor/sb_cdp.py` explains it. `Chrome.get()` bounds the navigation with
`asyncio.wait_for(...)`, but its error handling re-enters the same call **with no
timeout at all**:

```python
try:
    self.loop.run_until_complete(asyncio.wait_for(self.page.get(url, **kwargs), timeout))
except asyncio.TimeoutError:
    print("Timeout loading %s" % url)
except RuntimeError:
    self.loop.run_until_complete(self.page.get(url, **kwargs))   # ← unbounded retry
```

Cancelling a `page.get` task surfaces as a `RuntimeError` in several situations
(task destroyed, loop already running, target closed). The `except RuntimeError`
branch then re-runs the navigation with no deadline, so a load that will never
finish is retried forever, silently, at zero CPU cost. The earlier symptom — a
wall of `Timeout loading …` lines — is the same bug at a slower tempo.

Two consequences drive the design:

1. **A timeout cannot be trusted to return.** The call that times out may simply
   never come back, so the guard has to live *outside* the call.
2. **Killing a chunk is cheap now.** The repository is public, so minutes are
   free (ADR 0007). Losing twenty minutes of work costs nothing; losing five hours
   of the single-flight concurrency group costs a whole night and hides the bug.

## Decision

Two independent guards, because neither is sufficient alone.

### 1. `navigate()` — a deadline per page load

Every navigation in discovery and detail goes through `Crawl.navigate()`, which
runs `sb.goto(url)` in a **daemon thread** and waits at most `NAV_TIMEOUT`
(default 90 s). A thread that is still alive after that is abandoned, the page is
counted as a failure, and the crawl moves on. `nav_timeouts` accumulates, and
**three failed navigations stop the slice** (`stopped_reason: "timeout"`) rather
than burning the remaining budget on a suspect session.

Abandoning a thread leaks it, and the CDP loop it was using may stay wedged. That
is acceptable because the process is short-lived and the second guard exists for
exactly this case.

### 2. `start_watchdog()` — progress or death

A daemon thread wakes every 5 s and compares `last_progress` against
`STALL_MINUTES` (default 10). `touch()` is called on every discovery page, every
saved listing, and every completed navigation. When nothing has moved for the
limit, the watchdog dumps every thread's stack with `faulthandler` and calls
`os._exit(3)`.

`os._exit` rather than `sys.exit` on purpose: the point is to escape a wedged
event loop, and `sys.exit` only raises in the thread that calls it. Exit code 3
distinguishes "watchdog fired" from an ordinary traceback in the run log.

The artifact upload step is `if: always()`, so a watchdog exit still ships the
rows collected so far, and the dumped stacks name the exact call that was stuck.

### 3. A job ceiling that matches the work

`timeout-minutes` goes from 350 to **90**. A three-page chunk measures ~20 min;
anything past 90 is not slow, it is stuck. The 350-minute value was chosen to sit
under GitHub's 6-hour cap, which protected nothing and let one wedged run hold the
`etsy-crawl` concurrency group for most of a day.

### 4. `timeout` retries the same slice, cold

A chunk that stopped on navigation deadlines is advanced exactly like a `blocked`
chunk: the same page range is retried **once** with `profile: "cold"`, then
dropped. Resuming at the next page would silently skip listings that were never
read.

## Consequences

- A hung navigation costs at most 3 × 90 s = 4.5 min before the slice stops, and
  at most 10 min of total silence before the process exits with a stack dump.
- Up to three abandoned threads per run remain in the process. They cannot grow
  without bound because the third failure ends the slice.
- `stopped_reason` gains `"timeout"`, alongside `coverage`, `budget`, `time`,
  `blocked`, `challenges`.
- The leased Chrome profile is never saved on a `timeout` stop (the existing rule:
  never save a session that misbehaved).
- Tests: `tests/test_watchdog.py` reproduces both the hang (a `goto` that sleeps
  300 s is abandoned in 2 s) and the watchdog (silence kills the process with code
  3; steady `touch()` keeps it alive).

## Not done

The upstream bug is still there. `vendor/sb_cdp.py` is a vendored copy for
reading, and the crawler imports the installed `seleniumbase`; patching our copy
would change nothing on the runner. A proper fix is an upstream PR that makes the
`RuntimeError` branch bounded — worth doing, but it cannot be the thing our
reliability depends on.

## Addendum — what run 41760485370 taught us about the *other* two failure modes

The bounded-navigation fix worked: the repair chunk for pages 4–6 finished 150 of
188 listings before DataDome intervened, and it **never hung**. Two further bugs
surfaced in the same run.

### A chunk that stops on a block must still chain

`Cool down before chaining` and `Dispatch next chunk` were guarded by
`if: success()`. The crawl deliberately exits non-zero when it stops on a block
(`status: "aborted"`, rows kept), which meant the queue's own cold-profile retry —
the entire reason `advance()` returns `profile: "cold"` — could never fire. The
chain died silently at exactly the moment it was supposed to self-heal.

Both steps are now `if: always() && !cancelled()`. They are safe to run after a
failure because `out/next_queue.json` only exists when the chunk actually got far
enough to compute one; a dependency failure leaves no file and dispatches nothing.

### A dead solver must not be paid for three times

On a DataDome *block* page (title `etsy.com`, no captcha in the DOM) the solver has
nothing to solve, but the escalation policy still ran three attempts at 90 s
deadline each plus 60/120/180 s naps: **~7 minutes per blocked listing**, 21 minutes
to reach the three-strike circuit breaker, all of it spent on a session that was
already dead.

Now `solve()` counts consecutive failures. Two is enough evidence: after that the
crawler probes once with a 15 s wait and counts the page as blocked, so the
circuit breaker trips in about a minute and the cold-profile retry starts. A single
successful solve resets the counter, so a healthy session is never penalised.

Measured effect on the same workload: time-to-abort on a blocked session drops from
~21 min to ~3 min, and the chained retry is dispatched automatically.

### Outcome

| Step | Run | Result |
| --- | --- | --- |
| repair pages 4–6 | 41760485370 | 151 rows, aborted on block after 150/188 listings (**no hang**) |
| repair pages 7–8 | 41764505447 | 96 rows, success |
| second keyword `ceramic mug` (depth 2) | 41766308866 | 112 rows, success |

Coverage went from `15/20` to **`20/20 pages held, 1289/1289 listings detailed`**,
and a keyword added through the dashboard UI crawled and imported with no code
change — which is the property the whole design exists for.
