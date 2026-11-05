#!/usr/bin/env python3
"""Publish web/ to GitHub Pages via a gh-pages branch, through Composio.

GitHub Pages can only serve the repository root or /docs, and this repository's
/docs already holds the ADRs. So the site gets its own orphan branch containing
nothing but the four built files — no build step, no action, no gh-pages deploy
tooling: the tree is assembled with the Git database API and committed once.

  python scripts/deploy_pages.py            # publish
  python scripts/deploy_pages.py --status   # show Pages config + last build
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNER, REPO = "samarian5", "etsy-research"
SITE = ROOT / "web"
BRANCH = "gh-pages"


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(api(f"/repos/{OWNER}/{REPO}/pages"), indent=1)[:1200])
        return 0

    files = sorted(p for p in SITE.rglob("*") if p.is_file())
    if not files:
        print("nothing to publish: web/ is empty")
        return 1

    # Cache-bust the assets and stamp the build: GitHub Pages serves files with a
    # 10-minute cache, and an HTML page pointing at a stale app.js looks exactly
    # like "signed in but the board never appears".
    hashes = {}
    for f in files:
        if f.suffix in (".js", ".css"):
            hashes[f.name] = hashlib.md5(f.read_bytes()).hexdigest()[:8]

    entries = []
    for f in files:
        data = f.read_bytes()
        if f.name == "index.html":
            text = data.decode()
            for asset, h in hashes.items():
                text = re.sub(rf'(?:\./)?{re.escape(asset)}(\?v=[0-9a-z]+)?"', f'./{asset}?v={h}"', text)
            # any asset changing must change the build id, otherwise a CSS-only
            # fix stays cached behind the old ?v= URL
            stamp = hashlib.md5(
                "".join(f"{k}:{v}" for k, v in sorted(hashes.items())).encode()
            ).hexdigest()[:8]
            text = re.sub(r'(<meta name="build" content=")[^"]*(")', rf'\g<1>{stamp}\g<2>', text)
            data = text.encode()
            print(f"build  stamp {stamp}  assets {hashes}")
        blob = api(f"/repos/{OWNER}/{REPO}/git/blobs", "POST", {
            "content": base64.b64encode(data).decode(),
            "encoding": "base64",
        })
        if "sha" not in blob:
            print(f"FAIL blob {f.relative_to(SITE)}: {str(blob)[:200]}")
            return 1
        entries.append({"path": str(f.relative_to(SITE)), "mode": "100644",
                        "type": "blob", "sha": blob["sha"]})
        print(f"blob  {f.relative_to(SITE)}  {blob['sha'][:8]}")

    # The shell fetches this to decide which app.js to load. It is small, always
    # fetched fresh, and is the thing that makes a cached index.html harmless.
    ver_blob = api(f"/repos/{OWNER}/{REPO}/git/blobs", "POST", {
        "content": base64.b64encode(json.dumps({"build": stamp}).encode()).decode(),
        "encoding": "base64",
    })
    if "sha" not in ver_blob:
        print(f"FAIL blob version.json: {str(ver_blob)[:200]}")
        return 1
    entries.append({"path": "version.json", "mode": "100644", "type": "blob", "sha": ver_blob["sha"]})
    print(f"blob  version.json  {{'build': '{stamp}'}}")

    tree = api(f"/repos/{OWNER}/{REPO}/git/trees", "POST", {"tree": entries})
    if "sha" not in tree:
        print("FAIL tree:", str(tree)[:300])
        return 1

    commit = api(f"/repos/{OWNER}/{REPO}/git/commits", "POST", {
        "message": "pages: publish the Etsy research board",
        "tree": tree["sha"],
        "parents": [],
    })
    if "sha" not in commit:
        print("FAIL commit:", str(commit)[:300])
        return 1
    print("commit", commit["sha"][:8], "tree", tree["sha"][:8])

    existing = api(f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    if existing.get("object"):
        r = api(f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}", "PATCH",
                {"sha": commit["sha"], "force": True})
    else:
        r = api(f"/repos/{OWNER}/{REPO}/git/refs", "POST",
                {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    if "ref" not in r:
        print("FAIL ref:", str(r)[:300])
        return 1
    print("ref", r["ref"])

    site = api(f"/repos/{OWNER}/{REPO}/pages")
    if site.get("html_url"):
        out = api(f"/repos/{OWNER}/{REPO}/pages", "PUT",
                  {"source": {"branch": BRANCH, "path": "/"}})
    else:
        out = api(f"/repos/{OWNER}/{REPO}/pages", "POST",
                  {"source": {"branch": BRANCH, "path": "/"}})
    print("pages:", out.get("html_url") or out.get("message") or out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
