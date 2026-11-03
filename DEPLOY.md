# etsy-research — crawler repo

This repository holds **only the crawl job**. It runs on GitHub Actions, writes a
run bundle, and uploads it as an artifact. Nothing here serves the UI and no
scraped data is committed.

The dashboard, the SQLite database and the importer live on the operator's
machine and pull artifacts from here.

## Trigger a run

From the local UI (`Run now`), or by hand:

```bash
composio execute GITHUB_CREATE_A_WORKFLOW_DISPATCH_EVENT -d '{
  "owner": "samarian5", "repo": "etsy-research",
  "workflow_id": "scrape.yml", "ref": "main",
  "inputs": {"keyword": "t-shirt", "depth": "2"}
}'
```

`depth` is a ceiling, not a promise: the crawler also stops when a page yields
no new listings (coverage stop) and when the request budget runs out.

## What a run produces

| Artifact path | Contents |
|---|---|
| `out/run.json` | run metadata: keyword, depth, pages done, extraction tiers hit, warnings, stop reason |
| `out/results.jsonl` | one JSON object per occurrence (listing × page × position) with the parsed snapshot |
| `out/payloads/<id>.json.gz` | the embedded listing JSON as served, per listing — the re-parse source of truth |
| `out/html/page_N.html.gz` | raw search HTML for the first pages only (selector-drift forensics) |

## Rules baked into the workflow

- **Single-flight** — `concurrency: etsy-crawl`, never two crawls at once.
- **Warm profile cached** between runs, and **only saved when the run succeeds**,
  so one challenged/aborted run cannot poison every later run.
- **Headed Chrome under `xvfb-run`** — the DataDome slider needs a real pointer.
- **Budget and stop conditions enforced in code**, not by cancelling the job.
- Artifacts retained 14 days.
