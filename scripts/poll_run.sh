#!/usr/bin/env bash
# Poll a GitHub run until it leaves in_progress, printing real UTC each time.
# Local clock runs ~65 min fast on this host, so never trust `date`.
RUN_ID="${1:?usage: poll_run.sh RUN_ID [interval_s]}"
INTERVAL="${2:-120}"
cd "$(dirname "$0")/.." || exit 1

status_of() {
  timeout 90 composio execute GITHUB_GET_A_WORKFLOW_RUN \
    -d "{\"owner\":\"samarian5\",\"repo\":\"etsy-research\",\"run_id\":$RUN_ID}" 2>/dev/null \
  | .venv/bin/python -c '
import json,sys
raw=sys.stdin.read()
try:
    d=json.loads(raw)
except Exception:
    print("parse-error"); raise SystemExit
p=d.get("outputFilePath")
if p:
    d=json.load(open(p))
data=d.get("data") or d
run=data.get("workflow_run") if isinstance(data.get("workflow_run"),dict) else data
print(run.get("status"), run.get("conclusion") or "-")
'
}

while true; do
  now=$(curl -sI https://api.github.com | grep -i '^date:' | cut -d' ' -f2-)
  s=$(status_of)
  echo "$(date +%H:%M:%S) utc=$now run=$RUN_ID $s"
  case "$s" in
    in_progress*|queued*) sleep "$INTERVAL" ;;
    *) echo "finished: $s"; break ;;
  esac
done
