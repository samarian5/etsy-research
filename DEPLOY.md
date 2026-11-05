# etsy-research — crawler repo

This repository holds **only the crawl job**. It runs on GitHub Actions, writes a
chunk bundle, uploads it as an artifact, and re-dispatches itself for the next chunk.
Nothing here serves the UI and no scraped data is committed.

The dashboard, the SQLite database and the importer live on the operator's
machine and pull artifacts from here.

## Trigger a crawl

Preferred (builds the queue for you):

```bash
python scripts/enqueue.py tshirt --depth 20 --pages-per-run 8      # from the local repo
python scripts/enqueue.py --from-db                               # every enabled keyword
```

From the dashboard: paste keywords → **Run all keywords**.

By hand, the queue is the only input that matters:

```bash
composio execute GITHUB_CREATE_A_WORKFLOW_DISPATCH_EVENT -d '{
  "owner": "samarian5", "repo": "etsy-research",
  "workflow_id": "scrape.yml", "ref": "main",
  "inputs": "{\"queue\": \"{\\\"items\\\":[{\\\"keyword\\\":\\\"tshirt\\\",\\\"depth\\\":20}],\\\"pages_per_run\\\":8}\", \"fast\": \"true\", \"profile\": \"warm\"}"
}'
```

Inputs: `queue` (required JSON), `fast` (`true` = waits halved to ≥0.6 s), `profile`
(`warm` reuses the cached Chrome profile, `cold` starts clean after a block).

`depth` is a ceiling, not a promise: the crawler also stops when a page yields no new
listings (coverage stop) and when the per-chunk request budget runs out. Both are
reported as `stopped_reason` in `run.json` and handled by the chain (ADR 0005).

## What a run produces

| Artifact path | Contents |
|---|---|
| `out/run.json` | run metadata: keyword, depth, chunk page range, pages done, extraction tiers hit, warnings, `stopped_reason`, `slice_complete` |
| `out/results.jsonl` | one JSON object per occurrence (listing × page × position) with the parsed snapshot |
| `out/payloads/<id>.json.gz` | the embedded listing JSON as served, per listing — the re-parse source of truth |
| `out/html/page_N.html.gz` | raw search HTML for the first pages only (selector-drift forensics) |
| `out/next_queue.json` | the remainder of the queue, also used to re-dispatch |
| `out/chain_complete.json` | written instead of `next_queue.json` when the queue is empty |

## Rules baked into the workflow

- **Single-flight** — `concurrency: etsy-crawl`, never two crawls at once (a chain
  therefore never overlaps itself).
- **Warm profile cached** between runs, and **only saved when the run succeeds with
  zero challenges**, so one challenged run cannot poison every later run.
- **Headed Chrome under `xvfb-run`** — the DataDome slider needs a real pointer.
- **Heavy resources blocked at the CDP layer** (images, fonts, media, trackers) —
  ~30% faster detail passes, no effect on extraction (JSON-LD is server-rendered).
- **Budget and stop conditions enforced in code**, not by cancelling the job.
- **The chain stops on a hard failure** — no retry storms against a wall.
- Artifacts retained 14 days.

## Repository secret (required)

The repo is public so Actions minutes are free, so results travel sealed. Set the
key once, on the runner and locally:

```bash
# 1. local key (never printed, never committed)
openssl rand -hex 32 >> .env          # as ETSY_ARTIFACT_KEY=...
chmod 600 .env

# 2. same value as a repository secret
#    Settings -> Secrets and variables -> Actions -> New repository secret
#    name: ETSY_ARTIFACT_KEY
```

A run started without that secret fails immediately at `Open the queue` rather than
uploading anything readable.

## What is public now

Code, workflow, tests and one captured Etsy page used as a test fixture. Not the
database, not the dashboard, not the keywords (sealed in the dispatch input, hashed in
logs), not the results (sealed tarball per run, 3-day retention).

## Signing in to the hosted board

The board is a single-owner app. Nothing renders until a Supabase session exists,
and the API itself refuses anonymous reads (`anon` gets 401 on every table and RPC).

1. Open https://samarian5.github.io/etsy-research/
2. Enter the owner email and press **Send sign-in link**.
3. Click the link in the email. It verifies on Supabase, redirects to the board with
   the session in the URL fragment, and the app stores it (`etsy_session`), then
   strips the fragment from the address bar.
4. That browser stays signed in; the access token refreshes automatically every
   60 s window and on expiry.

**If the link does not work** (mail clients truncate long URLs, and GitHub Pages
serves a 404 while it rebuilds):

- `web/404.html` catches a deep/odd path and forwards to the board root *with the
  fragment intact*, so a mangled path still signs in.
- The gate has **"Link gave you a 404?"** — paste the whole link there and it is
  followed directly. It accepts HTML-escaped (`&amp;`) and line-wrapped copies.
- Landing with `#error=...` shows the reason instead of a blank gate.

**Email limits.** The free built-in mailer allows ~2 sign-in emails per hour for the
whole project. If that is exhausted the gate says so rather than failing silently.
For routine use this does not matter (one sign-in per device, weeks apart); to
remove the cap, configure custom SMTP in Supabase Auth settings.
