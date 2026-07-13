#!/usr/bin/env bash
# Hermes script-only cron entrypoint for periodic gbrain vault refresh.

set -euo pipefail

log_file="${TMPDIR:-/tmp}/gbrain-refresh-cron.log"
trap 'rm -f "$log_file"' EXIT

set +e
/usr/local/bin/josemar-gbrain refresh >"$log_file" 2>&1
status=$?
set -e

if [ "$status" -eq 0 ]; then
    exit 0
fi

cat "$log_file"
exit "$status"
