#!/bin/sh
# vault-recovery-restore.sh - Operator-only verify/install helper (Phase 2,
# Hermes-side).
#
# Hermes has NO rclone and NO rclone config, so this wrapper never invokes
# rclone. It consumes a verified recovery handoff produced by the short-lived
# rclone download step (scripts/vault-recovery-recover.sh, which runs in an
# rclone image and writes RECOVERY_READY into the disposable recovery volume).
#
# Two commands (plus crash-recovery rollback), so tests cannot accidentally
# target production:
#   verify-recovery <recovery-dir>
#       Consume a RECOVERY_READY handoff: re-validate the full bundle
#       (manifest, entries digests, exact tree re-scan) and run the PINNED
#       doctor against a DISPOSABLE copy of the restored .gbrain. Writes
#       VERIFIED_READY (any stale one is removed and fsynced up front, so
#       the sentinel only reflects the most recent verification). NEVER
#       opens or mutates the live state.
#   install-recovery <recovery-dir> --live-vault <dir> --live-gbrain <dir>
#       [--generation <id>] --i-confirm-this-overwrites-production
#       Operator-only: consume RECOVERY_READY + VERIFIED_READY, re-validate,
#       and run the journaled two-tree rollback transaction (.gbrain gets a
#       whole-tree atomic rename swap; the vault swap is journaled per
#       top-level entry because its backup root must live inside the vault
#       tree - same filesystem - so rename(2) cannot move the whole tree).
#       Writers MUST be stopped first. The optional --generation is passed
#       to the core, which binds it UNDER the shared lock: a handoff that
#       carries a different generation (e.g. replaced by a concurrent
#       lock-less recover download) refuses the install after the lock.
#       The wrapper's own generation pre-check below is only a fast-fail
#       convenience; the core check is authoritative.
#   rollback <generation-id> [--journal-root <dir>]
#       Reverse a journaled install from its journal (crash recovery).
#
# Env:
#   VAULT_RECOVERY_PYTHON      - python interpreter (default /opt/hermes/.venv/bin/python3)
#   VAULT_RECOVERY_RESTORE_CORE - core module path (default /opt/josemar/scripts/vault_recovery_restore_core.py)
#   VAULT_RECOVERY_RECOVERY_DIR - recovery handoff dir (default /recovery)
#   VAULT_RECOVERY_JOURNAL_ROOT - install journal root (default /opt/data/vault-recovery/install-journal)
#
# Exit codes: 0 success, 2 validation/known error, 3 unexpected error.

set -eu

PYTHON="${VAULT_RECOVERY_PYTHON:-/opt/hermes/.venv/bin/python3}"
CORE="${VAULT_RECOVERY_RESTORE_CORE:-/opt/josemar/scripts/vault_recovery_restore_core.py}"
DEFAULT_RECOVERY_DIR="${VAULT_RECOVERY_RECOVERY_DIR:-/recovery}"
DEFAULT_JOURNAL_ROOT="${VAULT_RECOVERY_JOURNAL_ROOT:-/opt/data/vault-recovery/install-journal}"

log_info() { echo "[vault-recovery-restore] $1"; }
log_error() { echo "[vault-recovery-restore] ERROR: $1" >&2; }

usage() {
    cat <<EOF
Usage: $0 <command> [options]

Commands:
  verify-recovery [recovery-dir]
      Validate a RECOVERY_READY handoff and run the pinned doctor on a
      DISPOSABLE copy of the restored .gbrain; write VERIFIED_READY.
      Never touches live state.

  install-recovery <recovery-dir> --live-vault <dir> --live-gbrain <dir>
      [--generation <generation-id>] --i-confirm-this-overwrites-production
      Install a RECOVERY_READY + VERIFIED_READY bundle over the live vault
      and .gbrain with a journaled two-tree rollback transaction (.gbrain:
      atomic rename swap; vault: journaled per-entry swap). Writers must be
      stopped first.

  rollback <generation-id> [--journal-root <dir>]
      Reverse a journaled install from its journal.
EOF
}

# Read the first line of a sentinel file.
first_line() {
    sed -n '1p' "$1" | tr -d '\r\n'
}

cmd_verify_recovery() {
    recovery_dir="${1:-$DEFAULT_RECOVERY_DIR}"
    if [ ! -d "$recovery_dir" ]; then
        log_error "Recovery dir not found: $recovery_dir"
        exit 2
    fi
    if [ ! -f "$recovery_dir/RECOVERY_READY" ]; then
        log_error "No verified recovery handoff in $recovery_dir (missing RECOVERY_READY). Run scripts/vault-recovery-recover.sh download <gen-id> first."
        exit 2
    fi
    exec "$PYTHON" -I "$CORE" verify "$recovery_dir"
}

cmd_install_recovery() {
    recovery_dir="${1:-}"
    # Consume only the positional <recovery-dir>; the remaining flags
    # (--live-vault/--live-gbrain/--generation/--i-confirm-...) are parsed in
    # the loop below. (`shift 2>/dev/null || true` swallows the error when
    # there is nothing to shift; `set -eu` would otherwise abort the friendly
    # usage error below.)
    shift 2>/dev/null || true
    live_vault=""
    live_gbrain=""
    generation=""
    confirm=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --live-vault)
                shift
                live_vault="${1:-}"
                shift
                ;;
            --live-gbrain)
                shift
                live_gbrain="${1:-}"
                shift
                ;;
            --generation)
                shift
                generation="${1:-}"
                shift
                ;;
            --i-confirm-this-overwrites-production) confirm="$1"; shift ;;
            *) shift ;;
        esac
    done
    if [ -z "$recovery_dir" ] || [ -z "$live_vault" ] || [ -z "$live_gbrain" ]; then
        log_error "install-recovery requires <recovery-dir> --live-vault <dir> --live-gbrain <dir> --i-confirm-this-overwrites-production"
        exit 2
    fi
    if [ -z "$confirm" ]; then
        log_error "install-recovery requires --i-confirm-this-overwrites-production. Stop writers and confirm."
        exit 2
    fi
    if [ ! -f "$recovery_dir/RECOVERY_READY" ] || [ ! -f "$recovery_dir/VERIFIED_READY" ]; then
        log_error "recovery handoff is incomplete; run download and verify-recovery first"
        exit 2
    fi
    ready_gen="$(first_line "$recovery_dir/RECOVERY_READY")"
    verified_gen="$(first_line "$recovery_dir/VERIFIED_READY")"
    if [ -z "$ready_gen" ] || [ "$ready_gen" != "$verified_gen" ]; then
        log_error "RECOVERY_READY/VERIFIED_READY generation mismatch: $ready_gen vs $verified_gen"
        exit 2
    fi
    if [ -n "$generation" ] && [ "$generation" != "$ready_gen" ]; then
        log_error "generation mismatch: requested=$generation handoff=$ready_gen"
        exit 2
    fi
    exec "$PYTHON" -I "$CORE" install "$recovery_dir" \
        --live-vault "$live_vault" \
        --live-gbrain "$live_gbrain" \
        --journal-root "$DEFAULT_JOURNAL_ROOT" \
        ${generation:+--generation "$generation"} \
        --i-confirm-this-overwrites-production
}

cmd_rollback() {
    generation="${1:-}"
    journal_root="$DEFAULT_JOURNAL_ROOT"
    # Consume only the positional <generation-id>; --journal-root is parsed in
    # the loop below.
    shift 2>/dev/null || true
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --journal-root)
                shift
                journal_root="${1:-$DEFAULT_JOURNAL_ROOT}"
                shift
                ;;
            *) shift ;;
        esac
    done
    if [ -z "$generation" ]; then
        log_error "rollback requires <generation-id>"
        exit 2
    fi
    exec "$PYTHON" -I "$CORE" rollback "$generation" --journal-root "$journal_root"
}

main() {
    if [ "$#" -lt 1 ]; then
        usage
        exit 2
    fi
    cmd="$1"
    shift
    case "$cmd" in
        verify-recovery) cmd_verify_recovery "$@" ;;
        install-recovery) cmd_install_recovery "$@" ;;
        rollback) cmd_rollback "$@" ;;
        -h|--help|help) usage; exit 0 ;;
        *) log_error "Unknown command: $cmd"; usage; exit 2 ;;
    esac
}

main "$@"
