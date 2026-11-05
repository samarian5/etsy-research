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

The board is private: out of the box it renders a sign-in card and nothing else.
The database enforces the same thing independently — the `anon` key can read no
table and call no RPC (see `db/supabase/0008_owner_auth.sql`), so hiding the UI is
convenience, not security.

**Email + password.** One owner account (`settings.owner.email`), no sign-up.

1. Open https://samarian5.github.io/etsy-research/
2. Email is prefilled; type the password and press **Sign in**.
3. Your name appears top-right. Click it for **Change password** / **Sign out**.
4. Signing out wipes the rendered board (cards, stats, queue) before hiding it,
   so the numbers are not left sitting in the DOM.

Mechanics: `POST /auth/v1/token?grant_type=password` with the public anon key
returns an access + refresh token; the session lives in `localStorage` under
`etsy_session` and renews itself every 60 minutes. Requests then carry
`Authorization: Bearer <access_token>`, which is what the RLS policies check.
The password never leaves the browser except as that one POST.

If you ever need to set or reset the password from a terminal:

```sh
# set (service key required — never put this in the browser)
curl -X PUT "$SUPABASE_URL/auth/v1/admin/users/$OWNER_UID" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" \
  -H "Content-Type: application/json" -d '{"password":"..."}'

# prove it works with the anon key only
curl -X POST "$SUPABASE_URL/auth/v1/token?grant_type=password" \
  -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
  -d '{"email":"pityailon@gmail.com","password":"..."}'
```

### Why not magic links

Tried, and they fail for reasons outside the app: the emails' long URLs get
truncated or re-wrapped by mail clients, a dropped path lands on a 404 that
discards the `#access_token` fragment, the free mailer is capped at roughly two
messages per hour, and **every new link invalidates the previous one**. A
password has none of those failure modes and needs no email at all after setup.

A collapsed "Have a code, or a link that didn't work?" box remains as a
break-glass path: it accepts the 8-digit `email_otp` or a pasted sign-in link.

### The root redirect (why https://samarian5.github.io/ exists)

Sign-in emails used to carry the session in the URL fragment. Mail clients
sometimes dropped the `/etsy-research/` path entirely, so the browser landed on
`https://samarian5.github.io/#access_token=...` — a site that did not exist,
where GitHub's 404 page threw the fragment away and the user saw
"There isn't a GitHub Pages site here."

`https://samarian5.github.io/` is now a real site (`samarian5/samarian5.github.io`)
whose `index.html` and `404.html` forward to `/etsy-research/` while preserving
`location.search` and `location.hash`. Any path that arrives at the root — or any
unknown path under it — still reaches the board with its fragment intact.

```sh
python scripts/deploy_root_redirect.py --status   # check what is published
python scripts/deploy_root_redirect.py            # publish + rebuild Pages
```

This site also keeps working if the app is ever moved to a different repo: the
address people type does not change.

### Build stamp

The footer of the sign-in card prints `build <hash>`. That hash is the content
hash of `app.js`, stamped by `scripts/deploy_pages.py` on every publish, and the
same hash is appended to the asset URL (`app.js?v=<hash>`) to defeat the ~10
minute Pages cache. If a sign-in *succeeds* but the board stays blank, compare
that hash with the latest deploy before debugging anything else.

`app.js` also never assumes an element exists — `<script>`s load at the end of
the body, and every listener is attached through a guarded helper — so an
out-of-step HTML/JS pair degrades to a missing control instead of a dead page.
Startup failures are caught and printed in the banner with the build hash.
