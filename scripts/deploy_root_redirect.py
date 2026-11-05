#!/usr/bin/env python3
"""Publish a root redirect at https://samarian5.github.io/.

Why this exists: a magic link redirects to
https://samarian5.github.io/etsy-research/#access_token=... . Some mail clients
drop the path and land on https://samarian5.github.io/#access_token=... instead,
where GitHub has no site and serves "There isn't a GitHub Pages site here" —
throwing away the session in the fragment.

A user site at the root fixes that permanently: whatever path survives, the page
forwards to /etsy-research/ with the query string and fragment intact, so the
board can still absorb the token.

  python scripts/deploy_root_redirect.py            # create/update + enable Pages
  python scripts/deploy_root_redirect.py --status   # show repo + Pages state
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile

OWNER = "samarian5"
REPO = "samarian5.github.io"
BRANCH = "main"

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Etsy research board</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         color: #2b2723; background: #faf6f0; margin: 0; display: grid;
         place-items: center; min-height: 100vh; padding: 24px; }
  .card { background: #fff; border: 1px solid #e8ddd0; border-radius: 14px;
          padding: 22px 24px; max-width: 420px; }
  a { color: #b4562f; }
  p { margin: 0 0 10px; }
  .muted { color: #6b645d; font-size: 13px; }
</style>
<div class="card">
  <p><b>Opening your Etsy research board…</b></p>
  <p class="muted">If nothing happens, <a id="go" href="/etsy-research/">tap here</a>.</p>
</div>
<script>
  // Keep the whole tail: query string and fragment (the sign-in session rides in
  // the fragment and is never sent to a server, so it must survive client-side).
  var dest = "/etsy-research/" + location.search + location.hash;
  document.getElementById("go").setAttribute("href", dest);
  location.replace(dest);
</script>
</html>
"""

NOT_FOUND = PAGE


def api(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    cmd = ["composio", "proxy", f"https://api.github.com{path}", "--toolkit", "github"]
    if method != "GET":
        cmd += ["-X", method]
        if payload is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(payload, fh)
                tmp = fh.name
            cmd += ["-H", "Content-Type: application/json", "-d", f"@{tmp}"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {"error": p.stderr[:300]}
    except json.JSONDecodeError:
        return {"error": (p.stdout or p.stderr)[:300]}


def ensure_repo() -> bool:
    info = api(f"/repos/{OWNER}/{REPO}")
    if info.get("name") == REPO:
        print(f"repo   {OWNER}/{REPO} exists")
        return True
    made = api("/user/repos", "POST", {
        "name": REPO, "private": False, "auto_init": True,
        "description": "Root redirect to the Etsy research board (sign-in links land here).",
        "has_issues": False, "has_wiki": False, "has_projects": False,
    })
    if made.get("name") != REPO:
        print(f"FAIL create repo: {str(made)[:300]}")
        return False
    print(f"repo   {OWNER}/{REPO} created")
    return True


def put(path: str, text: str, message: str) -> bool:
    cur = api(f"/repos/{OWNER}/{REPO}/contents/{path}?ref={BRANCH}")
    sha = cur.get("sha")
    body = {"message": message, "branch": BRANCH,
            "content": base64.b64encode(text.encode()).decode()}
    if sha:
        body["sha"] = sha
    out = api(f"/repos/{OWNER}/{REPO}/contents/{path}", "PUT", body)
    ok = "content" in out or "commit" in out
    print(f"{'OK  ' if ok else 'FAIL'} {path} {str(out)[:160] if not ok else ''}")
    return ok


def ensure_pages() -> bool:
    cur = api(f"/repos/{OWNER}/{REPO}/pages")
    if cur.get("html_url"):
        print(f"pages  already on {cur.get('html_url')} ({cur.get('status')})")
        return True
    made = api(f"/repos/{OWNER}/{REPO}/pages", "POST",
               {"source": {"branch": BRANCH, "path": "/"},
                "build_type": "legacy"})
    ok = bool(made.get("html_url") or made.get("status"))
    print(f"{'OK  ' if ok else 'FAIL'} pages {str(made)[:220]}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(api(f"/repos/{OWNER}/{REPO}"), indent=1)[:500])
        print(json.dumps(api(f"/repos/{OWNER}/{REPO}/pages"), indent=1)[:500])
        return 0

    if not ensure_repo():
        return 1
    if not put("index.html", PAGE, "Root redirect to /etsy-research/ preserving fragment"):
        return 1
    if not put("404.html", NOT_FOUND, "Same redirect for unknown paths"):
        return 1
    return 0 if ensure_pages() else 1


if __name__ == "__main__":
    raise SystemExit(main())
