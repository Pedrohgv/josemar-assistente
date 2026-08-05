#!/bin/sh
# mnemosyne-backup-restore.sh - Operator-only restore helper (Hermes-side).
#
# Hermes has NO rclone and NO rclone config, so this wrapper never invokes
# rclone. It consumes a verified recovery handoff produced by the short-lived
# rclone download step (scripts/mnemosyne-backup-recover.sh, which runs in an
# rclone image and writes RECOVERY_READY into the disposable recovery volume).
#
# Two separate commands so tests cannot accidentally target production:
#   verify-restore  - consume a RECOVERY_READY handoff: re-verify SHA/manifest,
#                     restore to /recovery/verified.db, run integrity
#                     verification, and publish VERIFIED_READY. NEVER touches
#                     the live DB.
#   install-restore - operator-only, writers stopped, explicit generation and
#                     confirmation: consume the same recovery handoff and
#                     atomically replace the live DB while retaining rollback.
#
# No automated production overwrite is performed by this wrapper.
#
# Env:
#   MNEMOSYNE_BACKUP_PYTHON - python interpreter (default /opt/hermes/.venv/bin/python3)
#   MNEMOSYNE_BACKUP_CORE   - core module path (default /opt/josemar/scripts/mnemosyne_backup_core.py)
#   MNEMOSYNE_DATA_DIR      - source data dir (for default live DB path)
#
# Exit codes: 0 success, 2 validation/known error, 3 unexpected error.

set -eu

PYTHON="${MNEMOSYNE_BACKUP_PYTHON:-/opt/hermes/.venv/bin/python3}"
CORE="${MNEMOSYNE_BACKUP_CORE:-/opt/josemar/scripts/mnemosyne_backup_core.py}"

log_info() { echo "[mnemosyne-backup-restore] $1"; }
log_error() { echo "[mnemosyne-backup-restore] ERROR: $1" >&2; }

usage() {
    cat <<EOF
Usage: $0 <command> [options]

Commands:
  verify-restore <recovery-dir> <dest-db>
      Consume a RECOVERY_READY handoff in <recovery-dir> (written by
      scripts/mnemosyne-backup-recover.sh), re-verify SHA-256/manifest,
      restore to a NEW disposable path <dest-db>, run integrity verification,
      and write VERIFIED_READY beside it. <dest-db> must be inside the
      recovery dir (the documented path is <recovery-dir>/verified.db).
      NEVER touches the live DB. Requires NO rclone and NO rclone config.

  install-restore <recovery-dir> <live-db> --generation <generation-id>
      --i-confirm-this-overwrites-production
      Operator-only: validate RECOVERY_READY, VERIFIED_READY, and the DB SHA
      for the selected generation, then atomically replace the live DB.
      Retains a rollback copy. Writers MUST be stopped first.
EOF
}

# Read a (possibly dotted) field from the machine-written manifest. The
# exporter writes sorted-keys JSON; e.g. "artifact.sha256".
manifest_field() {
    manifest="$1"
    key="$2"
    "$PYTHON" -c '
import json, sys
d = json.load(open(sys.argv[1]))
for part in sys.argv[2].split("."):
    if isinstance(d, dict) and part in d:
        d = d[part]
    else:
        d = ""
        break
print(d if isinstance(d, (str, int, float)) else "")
' "$manifest" "$key"
}

# Resolve existing symlinks and lexical components without creating anything.
# realpath() also resolves the existing portion of a path when the final target
# does not exist, which is required because verify-restore creates dest_db.
canonical_path() {
    "$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

validate_recovery_destination() {
    recovery_root="$1"
    destination="$2"
    "$PYTHON" -c '
import os, sys

root = os.path.realpath(sys.argv[1])
target = os.path.realpath(sys.argv[2])
try:
    inside = os.path.commonpath((root, target)) == root
except ValueError:
    inside = False
if not inside or target == root:
    raise SystemExit(1)
print(target)
' "$recovery_root" "$destination"
}

cmd_verify_restore() {
    recovery_dir="$1"
    dest_db="$2"
    if [ -z "$recovery_dir" ] || [ -z "$dest_db" ]; then
        log_error "verify-restore requires <recovery-dir> <dest-db>"
        exit 2
    fi
    if ! recovery_dir="$(canonical_path "$recovery_dir")"; then
        log_error "Could not canonicalize recovery dir: $recovery_dir"
        exit 2
    fi
    if [ ! -d "$recovery_dir" ]; then
        log_error "Recovery dir not found: $recovery_dir"
        exit 2
    fi
    if [ ! -f "$recovery_dir/RECOVERY_READY" ]; then
        log_error "No verified recovery handoff in $recovery_dir (missing RECOVERY_READY). Run scripts/mnemosyne-backup-recover.sh <slot> first."
        exit 2
    fi
    if ! dest_db="$(validate_recovery_destination "$recovery_dir" "$dest_db")"; then
        log_error "verified DB must be inside recovery dir to bind it to the handoff"
        exit 2
    fi
    if [ "$dest_db" = "$recovery_dir/verified.db" ] && [ -e "$recovery_dir/VERIFIED_READY" ]; then
        log_error "verified output already exists; run a fresh recovery download before verifying again"
        exit 2
    fi
    artifact="$recovery_dir/mnemosyne.db.gz"
    manifest="$recovery_dir/manifest.json"
    if [ ! -f "$artifact" ] || [ ! -f "$manifest" ]; then
        log_error "Recovery handoff incomplete: missing artifact or manifest in $recovery_dir"
        exit 2
    fi

    # The handoff sentinel carries "<generation_id>\n<sha256>\n"; re-verify it
    # against the manifest and the artifact (defense in depth).
    ready_gen="$(IFS= read -r ready_line < "$recovery_dir/RECOVERY_READY"; printf '%s' "$ready_line")"
    ready_sha="$(sed -n '2p' "$recovery_dir/RECOVERY_READY")"
    manifest_gen="$(manifest_field "$manifest" "generation_id")"
    if [ -z "$manifest_gen" ] || [ "$manifest_gen" != "$ready_gen" ]; then
        log_error "RECOVERY_READY generation mismatch: sentinel=$ready_gen manifest=$manifest_gen"
        exit 2
    fi
    manifest_sha="$(manifest_field "$manifest" "artifact.sha256")"
    if [ -z "$manifest_sha" ]; then
        log_error "Could not read artifact sha256 from manifest $manifest"
        exit 2
    fi
    if [ -n "$ready_sha" ] && [ "$ready_sha" != "$manifest_sha" ]; then
        log_error "RECOVERY_READY sha mismatch: sentinel=$ready_sha manifest=$manifest_sha"
        exit 2
    fi
    actual_sha="$(sha256sum "$artifact" | cut -d' ' -f1)"
    if [ "$actual_sha" != "$manifest_sha" ]; then
        log_error "SHA-256 mismatch: manifest=$manifest_sha artifact=$actual_sha"
        exit 2
    fi
    log_info "Handoff verified (generation $manifest_gen); restoring to NEW path $dest_db"
    "$PYTHON" "$CORE" verify-restore "$artifact" "$dest_db" --sha256 "$actual_sha"
    db_sha="$(sha256sum "$dest_db" | cut -d' ' -f1)"
    tmp_ready="$recovery_dir/.VERIFIED_READY.tmp"
    printf '%s\n%s\n%s\n' "$manifest_gen" "$actual_sha" "$db_sha" > "$tmp_ready"
    mv -f "$tmp_ready" "$recovery_dir/VERIFIED_READY"
    log_info "Verified output ready: generation $manifest_gen (db sha256 $db_sha)"
}

cmd_install_restore() {
    recovery_dir="$1"
    live_db="$2"
    shift 2 || true
    confirm=""
    generation=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --i-confirm-this-overwrites-production) confirm="$1"; shift ;;
            --generation)
                shift
                generation="${1:-}"
                shift
                ;;
            *) shift ;;
        esac
    done
    if [ -z "$recovery_dir" ] || [ -z "$live_db" ]; then
        log_error "install-restore requires <recovery-dir> <live-db> --generation <id> --i-confirm-this-overwrites-production"
        exit 2
    fi
    if [ -z "$confirm" ]; then
        log_error "install-restore requires --i-confirm-this-overwrites-production. Stop writers and confirm."
        exit 2
    fi
    if [ -z "$generation" ]; then
        log_error "install-restore requires --generation matching VERIFIED_READY"
        exit 2
    fi
    verified_db="$recovery_dir/verified.db"
    if [ ! -f "$recovery_dir/RECOVERY_READY" ] || [ ! -f "$recovery_dir/manifest.json" ] || [ ! -f "$recovery_dir/VERIFIED_READY" ] || [ ! -f "$verified_db" ]; then
        log_error "recovery handoff is incomplete; run download and verify in fresh short-lived containers"
        exit 2
    fi
    ready_gen="$(sed -n '1p' "$recovery_dir/RECOVERY_READY")"
    manifest_gen="$(manifest_field "$recovery_dir/manifest.json" generation_id)"
    verified_gen="$(sed -n '1p' "$recovery_dir/VERIFIED_READY")"
    if [ "$generation" != "$ready_gen" ] || [ "$generation" != "$manifest_gen" ] || [ "$generation" != "$verified_gen" ]; then
        log_error "generation mismatch: requested=$generation recovery=$ready_gen manifest=$manifest_gen verified=$verified_gen"
        exit 2
    fi
    expected_db_sha="$(sed -n '3p' "$recovery_dir/VERIFIED_READY")"
    actual_db_sha="$(sha256sum "$verified_db" | cut -d' ' -f1)"
    if [ -z "$expected_db_sha" ] || [ "$actual_db_sha" != "$expected_db_sha" ]; then
        log_error "verified DB SHA-256 mismatch; refusing install"
        exit 2
    fi
    exec "$PYTHON" "$CORE" install-restore "$verified_db" "$live_db" "$confirm"
}

main() {
    if [ "$#" -lt 1 ]; then
        usage
        exit 2
    fi
    cmd="$1"
    shift
    case "$cmd" in
        verify-restore) cmd_verify_restore "$@" ;;
        install-restore) cmd_install_restore "$@" ;;
        -h|--help|help) usage; exit 0 ;;
        *) log_error "Unknown command: $cmd"; usage; exit 2 ;;
    esac
}

main "$@"
