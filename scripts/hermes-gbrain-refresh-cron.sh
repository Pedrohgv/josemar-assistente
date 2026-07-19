#!/usr/bin/env bash
# Hermes script-only cron entrypoint for periodic gbrain vault refresh.

set -euo pipefail

log_file="${TMPDIR:-/tmp}/gbrain-refresh-cron.log"
lock_runner="${TASKNOTES_LOCK_RUNNER:-/opt/josemar/scripts/tasknotes_lock_run.py}"
lock_path="${TASKNOTES_LOCK_PATH:-/opt/data/.locks/tasknotes.lock}"
refresh_timeout="${GBRAIN_REFRESH_TIMEOUT:-240}"
trap 'rm -f "$log_file"' EXIT

set +e
"$lock_runner" \
    --lock-path "$lock_path" \
    --nonblocking \
    --timeout "$refresh_timeout" \
    -- /usr/local/bin/josemar-gbrain refresh >"$log_file" 2>&1
status=$?
set -e

if [ "$status" -eq 0 ]; then
    exit 0
fi

if [ "$status" -eq 75 ]; then
    echo "gbrain refresh skipped: TaskNotes lock is busy"
    exit 0
fi

cat "$log_file"
exit "$status"
