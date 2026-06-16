#!/usr/bin/env bash
# Hermes script-only cron entrypoint for periodic workspace git sync.

set -euo pipefail

log_file="${TMPDIR:-/tmp}/workspace-sync-cron.log"
trap 'rm -f "$log_file"' EXIT

if WORKSPACE_SYNC_MODE=periodic WORKSPACE_SYNC_INTERVAL=0 /usr/local/bin/workspace-sync.sh >"$log_file" 2>&1; then
    exit 0
fi

status=$?
cat "$log_file"
exit "$status"
