#!/usr/bin/env bash
# Hermes no-agent cron entrypoint for the daily vault-recovery export
# (Phase 1: local staged immutable generations only; remote upload is phase 2).
set -euo pipefail

# The lock and gbrain must never be touched as root or by any non-Hermes
# identity: enforce the ACTUAL Hermes runtime uid here (configured
# HERMES_UID, else the system `hermes` user's uid, else the default 10000)
# instead of relying on base-image behavior. Fail closed — root, an
# unknown/garbage UID, and arbitrary non-Hermes uids all refuse.
uid="$(/usr/bin/id -u 2>/dev/null)" || {
    echo "vault-recovery-export cron: could not determine the effective UID; refusing to run" >&2
    exit 1
}
case "$uid" in
    ""|*[!0-9]*)
        echo "vault-recovery-export cron: could not determine the effective UID; refusing to run" >&2
        exit 1
        ;;
esac
expected_uid="${HERMES_UID:-}"
if [ -z "$expected_uid" ]; then
    expected_uid="$(/usr/bin/id -u hermes 2>/dev/null || echo 10000)"
fi
case "$expected_uid" in
    ""|*[!0-9]*)
        echo "vault-recovery-export cron: HERMES_UID is not a valid uid; refusing to run" >&2
        exit 1
        ;;
esac
if [ "$uid" = "0" ] || [ "$uid" != "$expected_uid" ]; then
    echo "vault-recovery-export cron: refuses to run as uid $uid; run as the hermes runtime user (uid $expected_uid)" >&2
    exit 1
fi

# Hermes supplies HERMES_CRON_SCRIPT_TIMEOUT for the outer script deadline.
# Keep the export's work timeout strictly below that deadline after TERM/KILL
# grace, post-KILL group drain, and a safety margin. The wrapper receives the
# constrained value, rather than independently guessing the outer timeout.
outer_timeout="${HERMES_CRON_SCRIPT_TIMEOUT:-300}"
requested_timeout="${VAULT_RECOVERY_EXPORT_TIMEOUT:-240}"
kill_grace="${VAULT_RECOVERY_KILL_GRACE:-5}"
group_drain="${VAULT_RECOVERY_GROUP_DRAIN:-2}"
safety_margin="${VAULT_RECOVERY_TIMEOUT_MARGIN:-10}"

case "$outer_timeout:$requested_timeout:$kill_grace:$group_drain:$safety_margin" in
    *[!0-9:]*|*::*|"0"*)
        echo "invalid vault-recovery-export timeout configuration" >&2
        exit 2
        ;;
esac

safe_timeout=$((outer_timeout - kill_grace - group_drain - safety_margin - 1))
if [ "$safe_timeout" -lt 1 ]; then
    echo "HERMES_CRON_SCRIPT_TIMEOUT is too short for vault-recovery export" >&2
    exit 2
fi
if [ "$requested_timeout" -ge "$safe_timeout" ]; then
    echo "capping VAULT_RECOVERY_EXPORT_TIMEOUT to ${safe_timeout}s below Hermes outer timeout" >&2
    requested_timeout="$safe_timeout"
fi

export HERMES_CRON_SCRIPT_TIMEOUT
export VAULT_RECOVERY_EXPORT_TIMEOUT="$requested_timeout"
export VAULT_RECOVERY_KILL_GRACE="$kill_grace"
export VAULT_RECOVERY_GROUP_DRAIN="$group_drain"
export VAULT_RECOVERY_TIMEOUT_MARGIN="$safety_margin"

# A busy shared tasknotes lock is an expected, graceful daily skip (another
# holder — TaskNotes MCP, a refresh cron, or a manual export — is active).
# Log it and succeed; the next daily run retries. Every other exit code
# (2 convergence/preflight failure, 124 timeout, ...) is passed through.
set +e
/opt/josemar/scripts/vault-recovery-export.sh
rc=$?
set -e
if [ "$rc" -eq 75 ]; then
    echo "vault-recovery-export: skipped (shared tasknotes lock busy; another holder is active)" >&2
    exit 0
fi
exit "$rc"
