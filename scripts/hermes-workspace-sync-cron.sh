#!/usr/bin/env bash
# Hermes script-only cron entrypoint for periodic workspace git sync.

set -euo pipefail

log_file="${TMPDIR:-/tmp}/workspace-sync-cron.log"
trap 'rm -f "$log_file"' EXIT

set +e
WORKSPACE_SYNC_MODE=periodic WORKSPACE_SYNC_INTERVAL=0 /usr/local/bin/workspace-sync.sh >"$log_file" 2>&1
status=$?
set -e

if [ "$status" -eq 0 ]; then
    exit 0
fi

cat "$log_file"
exit "$status"
