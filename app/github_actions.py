"""GitHub Actions transport via the Composio connection (no PAT, no token on disk).

Every call shells out to the `composio` CLI, which holds the OAuth connection to
GitHub. Nothing here reads or prints credentials.

Env:
  GH_REPO      "owner/name"                     (default samarian5/etsy-research)
  GH_WORKFLOW  workflow file name               (default scrape.yml)
  GH_REF       branch to dispatch from          (default main)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "crawler"))
import artifact_crypt as ac  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_REPO = "samarian5/etsy-research"


def config() -> dict:
    repo = os.environ.get("GH_REPO") or DEFAULT_REPO
    owner, _, name = repo.partition("/")
    return {
        "owner": owner,
        "repo": name,
        "repo_full": repo,
        "workflow": os.environ.get("GH_WORKFLOW") or "scrape.yml",
        "ref": os.environ.get("GH_REF") or "main",
        "cli": shutil.which("composio") or "",
    }


def configured() -> bool:
    c = config()
    return bool(c["cli"] and c["owner"] and c["repo"])


def _call(slug: str, payload: dict) -> dict:
    """Run a Composio tool and return its `data` (raises on failure)."""
    if not configured():
        raise RuntimeError("Composio CLI not found on PATH — cannot reach GitHub")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        tmp = fh.name
    try:
        proc = subprocess.run([config()["cli"], "execute", slug, "-d", f"@{tmp}"],
                              capture_output=True, text=True, timeout=180)
    finally:
        os.unlink(tmp)
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(f"composio returned non-JSON: {(proc.stdout or proc.stderr)[:300]}")
    if not out.get("successful"):
        raise RuntimeError(str(out.get("error") or out)[:400])
    # Big payloads get spilled to a file by the CLI instead of stdout.
    if out.get("storedInFile") and out.get("outputFilePath"):
        p = Path(out["outputFilePath"])
        if p.exists():
            spilled = json.loads(p.read_text())
            return spilled.get("data") if isinstance(spilled, dict) and "data" in spilled else spilled
    return out.get("data") or {}


def seal_queue(queue) -> str:
    """Serialise a queue and encrypt it for transport.

    The repository is public, so a workflow_dispatch input is a public field: the
    keyword list is sealed before it leaves this machine.
    """
    payload = queue if isinstance(queue, str) else json.dumps(queue)
    ac.load_env_file(str(ROOT / ".env"))
    return ac.seal(payload)


def dispatch_queue(queue: dict, fast: str = "false", profile: str = "warm") -> dict:
    """Dispatch a whole queue; the workflow runs its first chunk and chains the rest."""
    c = config()
    return _call("GITHUB_CREATE_A_WORKFLOW_DISPATCH_EVENT", {
        "owner": c["owner"], "repo": c["repo"],
        "workflow_id": c["workflow"], "ref": c["ref"],
        "inputs": json.dumps({"queue": seal_queue(queue), "fast": fast, "profile": profile}),
    })


def dispatch_raw(inputs: dict) -> dict:
    """Dispatch with the given inputs as-is (used to exercise the queue.sealed path)."""
    c = config()
    return _call("GITHUB_CREATE_A_WORKFLOW_DISPATCH_EVENT", {
        "owner": c["owner"], "repo": c["repo"],
        "workflow_id": c["workflow"], "ref": c["ref"],
        "inputs": json.dumps(inputs),
    })


def dispatch(keyword: str, depth: int, profile: str = "warm", pages_per_run: int = 3) -> dict:
    """Convenience wrapper: one keyword, chained to `depth` pages."""
    return dispatch_queue(
        {"items": [{"keyword": keyword, "depth": int(depth)}], "pages_per_run": pages_per_run},
        profile=profile,
    )


def workflow_runs(limit: int = 10) -> list[dict]:
    c = config()
    if not configured():
        return []
    d = _call("GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY",
              {"owner": c["owner"], "repo": c["repo"], "per_page": limit})
    runs = d.get("workflow_runs") or []
    return [r for r in runs if r.get("name") == "scrape" or r.get("path", "").endswith(c["workflow"])]


def artifacts() -> list[dict]:
    c = config()
    if not configured():
        return []
    d = _call("GITHUB_LIST_ARTIFACTS_FOR_A_REPOSITORY",
              {"owner": c["owner"], "repo": c["repo"], "per_page": 50})
    return d.get("artifacts") or []


def download_artifact(artifact_id: int, dest: Path) -> Path:
    c = config()
    d = _call("GITHUB_DOWNLOAD_AN_ARTIFACT",
              {"owner": c["owner"], "repo": c["repo"], "artifact_id": int(artifact_id)})
    url = ((d.get("artifact_file") or {}).get("s3url")
           or (d.get("artifact") or {}).get("s3url"))
    if not url:
        raise RuntimeError("no download URL in Composio response")
    with urllib.request.urlopen(url, timeout=180) as r:
        blob = r.read()
    dest.mkdir(parents=True, exist_ok=True)
    unpack_artifact(blob, dest)
    return dest


def unpack_artifact(blob: bytes, dest: Path) -> Path:
    """See crawler/artifact_crypt.unpack_artifact (shared with the runner's tests)."""
    return ac.unpack_artifact(blob, dest, env_file=str(ROOT / ".env"))

def sync(db_path: str) -> dict:
    """Download every artifact not yet imported, import it, report what happened."""
    import sqlite3
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
    from import_run import import_dir  # noqa: E402

    imported, skipped, ignored, errors = [], [], [], []
    con = sqlite3.connect(db_path)
    known = {r[0] for r in con.execute("SELECT run_id FROM import_log")}
    con.close()

    for art in artifacts():
        name = art.get("name") or ""
        if not name.startswith("etsy-run-"):
            continue
        run_id = name.split("etsy-run-", 1)[1]
        if run_id in known or art.get("expired"):
            skipped.append(run_id)
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                download_artifact(art["id"], Path(tmp))
                root = Path(tmp)
                hits = list(root.rglob("run.json"))
                if not hits:
                    # Cancelled runs upload partial rows but never write run.json.
                    # Record the run id so it is not re-downloaded, import nothing.
                    con = sqlite3.connect(db_path)
                    con.execute("INSERT OR REPLACE INTO import_log (run_id, artifact_id, rows)"
                                " VALUES (?,?,0)", (run_id, art["id"]))
                    con.commit()
                    con.close()
                    ignored.append(run_id)
                    continue
                imported.append(import_dir(db_path, str(hits[0].parent),
                                           run_id=run_id))
        except Exception as e:  # noqa: BLE001
            errors.append({"run_id": run_id, "error": f"{type(e).__name__}: {e}"})
    return {"imported": imported, "skipped": skipped, "ignored": ignored, "errors": errors}
