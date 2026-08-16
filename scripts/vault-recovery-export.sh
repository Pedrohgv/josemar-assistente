#!/bin/sh
# vault-recovery-export.sh - Thin wrapper for the Hermes-side vault-recovery
# exporter (Phase 1).
#
# Runs the Python core (scripts/vault_recovery_core.py) under the EXISTING
# shared TaskNotes/gbrain cooperative lock (/opt/data/.locks/tasknotes.lock)
# via the standard lock runner (scripts/tasknotes_lock_run.py), nonblocking
# and with a bounded runtime. The core directly invokes the private native
# gbrain binary for its doctor preflight; it never re-enters the public
# `gbrain` adapter or `josemar-gbrain` (no nested lock).
#
# This wrapper is what the Hermes no-agent export cron invokes (wired by
# docker-hermes-init.sh); it does NOT schedule itself.
#
# Env:
#   VAULT_RECOVERY_EXPORT_TIMEOUT  seconds before the whole export is killed
#                                  (default 240)
#   VAULT_RECOVERY_KILL_GRACE      TERM->KILL grace for the group (default 5)
#
# Exit codes: 0 success, 75 lock busy (skipped), 2 known export error
# (including convergence failure -> no generation published), 124 timeout,
# 3 unexpected error.
#
# Phase-1 boundary: creates ONLY local staged immutable generations on the
# staging volume. Remote upload/recovery/install is the default deployment
# lane (Phase 3); the retired plaintext obsidian-backup service is gone.

set -eu

PYTHON="${VAULT_RECOVERY_PYTHON:-/opt/hermes/.venv/bin/python3}"
CORE="${VAULT_RECOVERY_CORE:-/opt/josemar/scripts/vault_recovery_core.py}"
RUNNER="${VAULT_RECOVERY_LOCK_RUNNER:-/opt/josemar/scripts/tasknotes_lock_run.py}"
LOCK_PATH="${VAULT_RECOVERY_LOCK_PATH:-/opt/data/.locks/tasknotes.lock}"
TIMEOUT="${VAULT_RECOVERY_EXPORT_TIMEOUT:-240}"
KILL_GRACE="${VAULT_RECOVERY_KILL_GRACE:-5}"

for required in "$CORE" "$RUNNER"; do
    if [ ! -f "$required" ]; then
        echo "[vault-recovery-export] required file missing: $required" >&2
        exit 2
    fi
done

# The lock and gbrain must never be touched by a non-Hermes identity: enforce
# the ACTUAL Hermes runtime uid here (configured HERMES_UID, else the system
# `hermes` user's uid, else the default 10000), rejecting root and arbitrary
# non-Hermes uids before any work. The core re-validates the same identity at
# its own CLI boundary (defense in depth, not a substitute).
uid="$(/usr/bin/id -u 2>/dev/null)" || {
    echo "[vault-recovery-export] could not determine the effective UID; refusing to run" >&2
    exit 2
}
case "$uid" in
    ""|*[!0-9]*)
        echo "[vault-recovery-export] could not determine the effective UID; refusing to run" >&2
        exit 2
        ;;
esac
expected_uid="${HERMES_UID:-}"
if [ -z "$expected_uid" ]; then
    expected_uid="$(/usr/bin/id -u hermes 2>/dev/null || echo 10000)"
fi
case "$expected_uid" in
    ""|*[!0-9]*)
        echo "[vault-recovery-export] HERMES_UID is not a valid uid; refusing to run" >&2
        exit 2
        ;;
esac
if [ "$uid" = "0" ] || [ "$uid" != "$expected_uid" ]; then
    echo "[vault-recovery-export] refuses to run as uid $uid; run as the hermes runtime user (uid $expected_uid)" >&2
    exit 2
fi

# The lock runner holds the exclusive flock for the whole export and hands it
# to the core via TASKNOTES_LOCK_FD (validated by the core before any work).
exec "$PYTHON" -I "$RUNNER" \
    --lock-path "$LOCK_PATH" \
    --nonblocking \
    --timeout "$TIMEOUT" \
    --kill-grace "$KILL_GRACE" \
    -- "$PYTHON" -I "$CORE" export "$@"
