#!/usr/bin/env bash
# Hermes script-only cron entrypoint for periodic workspace git sync.
#
# Runs workspace sync and applies merged skill-toggle sidecars + policy
# under ONE advisory lock (the same lock the dashboard uses for toggle
# writes) so dashboard writes and sync+apply never race. The lock is
# acquired by the helper BEFORE the sync command runs and held across
# git sync + remote merge + sidecar apply.
#
# Sync exit status is preserved exactly. If apply fails after a
# successful sync, the failure is logged but does not change the exit
# status (sync succeeded and that fact is reported faithfully); callers
# that need apply failures to fail the run can inspect the apply
# statuses in the log.

set -euo pipefail

log_file="${TMPDIR:-/tmp}/workspace-sync-cron.log"
trap 'rm -f "$log_file"' EXIT

JOSEMAR_SKILL_STATE="${JOSEMAR_SKILL_STATE:-/opt/hermes/hermes_cli/josemar_skill_state.py}"

if [ ! -f "$JOSEMAR_SKILL_STATE" ]; then
    # Helper missing: fall back to sync-only so periodic git sync still runs.
    set +e
    WORKSPACE_SYNC_MODE=periodic WORKSPACE_SYNC_INTERVAL=0 /usr/local/bin/workspace-sync periodic >"$log_file" 2>&1
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
        cat "$log_file"
    fi
    exit "$status"
fi

# Delegate sync+apply to the helper so one advisory lock covers both.
# The helper runs the sync command with the inherited environment and
# applies sidecars/policy only after a successful sync.
set +e
WORKSPACE_SYNC_MODE=periodic WORKSPACE_SYNC_INTERVAL=0 \
    /opt/hermes/.venv/bin/python3 "$JOSEMAR_SKILL_STATE" sync-and-apply -- \
    /usr/local/bin/workspace-sync periodic >"$log_file" 2>&1
status=$?
set -e

if [ "$status" -ne 0 ]; then
    cat "$log_file"
    exit "$status"
fi

# Apply errors intentionally do not change a successful sync exit status,
# but they must remain visible after this wrapper deletes its temporary log.
if grep -q 'sync-and-apply: .*:error:' "$log_file"; then
    cat "$log_file" >&2
fi
exit 0
