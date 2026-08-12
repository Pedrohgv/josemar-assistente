#!/usr/bin/env bash
# Hermes script-only cron entrypoint for periodic gbrain vault refresh.

set -euo pipefail

# The lock and gbrain must never be touched as root: enforce the hermes
# runtime identity here instead of relying on base-image behavior. Fail
# closed — an unknown/garbage UID also refuses.
uid="$(/usr/bin/id -u 2>/dev/null)" || {
    echo "gbrain refresh cron: could not determine the effective UID; refusing to run" >&2
    exit 1
}
case "$uid" in
    ""|*[!0-9]*)
        echo "gbrain refresh cron: could not determine the effective UID; refusing to run" >&2
        exit 1
        ;;
esac
if [ "$uid" = "0" ]; then
    echo "gbrain refresh cron: refuses to run as root; run as the hermes runtime user" >&2
    exit 1
fi

log_file="${TMPDIR:-/tmp}/gbrain-refresh-cron.log"
# Immutable production paths: the lock runner and the shared lock are fixed
# constants — no environment can redirect them to a decoy runner or lock.
lock_runner="/opt/josemar/scripts/tasknotes_lock_run.py"
lock_path="/opt/data/.locks/tasknotes.lock"
trap 'rm -f "$log_file"' EXIT

# Hermes supplies HERMES_CRON_SCRIPT_TIMEOUT for the outer script deadline.
# Keep the runner's work timeout strictly below that deadline after TERM/KILL
# grace, post-KILL group drain, and a safety margin (same hierarchy as the
# embedding refresh cron), so the runner always finishes its cleanup before
# the outer deadline fires.
outer_timeout="${HERMES_CRON_SCRIPT_TIMEOUT:-300}"
requested_timeout="${GBRAIN_REFRESH_TIMEOUT:-240}"
kill_grace="${GBRAIN_REFRESH_KILL_GRACE:-5}"
group_drain="${GBRAIN_REFRESH_GROUP_DRAIN:-2}"
safety_margin="${GBRAIN_REFRESH_TIMEOUT_MARGIN:-10}"
export HERMES_CRON_SCRIPT_TIMEOUT

case "$outer_timeout:$requested_timeout:$kill_grace:$group_drain:$safety_margin" in
    *[!0-9:]*|*::*|"0"*)
        echo "invalid gbrain refresh timeout configuration" >&2
        exit 2
        ;;
esac

safe_timeout=$((outer_timeout - kill_grace - group_drain - safety_margin - 1))
if [ "$safe_timeout" -lt 1 ]; then
    echo "HERMES_CRON_SCRIPT_TIMEOUT is too short for gbrain refresh cleanup" >&2
    exit 2
fi
if [ "$requested_timeout" -ge "$safe_timeout" ]; then
    echo "capping GBRAIN_REFRESH_TIMEOUT to ${safe_timeout}s below Hermes outer timeout" >&2
    requested_timeout="$safe_timeout"
fi

# The lock runner starts with the fixed image interpreter in isolated mode:
# PYTHONPATH/sitecustomize from the cron environment cannot execute code in
# the runner before the flock (no interpreter override is honored).
set +e
"/opt/hermes/.venv/bin/python3" -I "$lock_runner" \
    --lock-path "$lock_path" \
    --nonblocking \
    --timeout "$requested_timeout" \
    --kill-grace "$kill_grace" \
    -- /usr/local/bin/josemar-gbrain refresh >"$log_file" 2>&1 &
runner_pid=$!
# Forward termination signals to the runner so it performs its bounded group
# cleanup instead of being orphaned mid-run when the outer deadline fires.
trap 'kill -TERM "$runner_pid" 2>/dev/null || true' TERM INT
wait "$runner_pid"
status=$?
trap - TERM INT
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
