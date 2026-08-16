#!/bin/sh
# mnemosyne-backup-recover.sh - Operator recovery: download one immutable
# remote slot through crypt into a disposable recovery handoff volume.
#
# This is the short-lived, least-privilege DOWNLOAD step of the recovery
# lane. It runs in an rclone image (rclone + crypt config), NEVER in Hermes:
#   - NEVER mounts hermes-data or /opt/data.
#   - The published rclone crypt config (obsidian-rclone-config volume) is
#     READ-ONLY: rclone runs against an EPHEMERAL PRIVATE writable copy of
#     the config in a fresh temp dir (OAuth-refresh fix,
#     rclone-active-config.sh) — never inside the recovery handoff volume,
#     removed on exit; the seed itself is never modified.
#   - Only the disposable recovery handoff volume is writable.
#   - It downloads the selected immutable slot, verifies the manifest
#     generation_id and the artifact SHA-256 BEFORE writing the RECOVERY_READY
#     handoff sentinel.
#   - It NEVER restores anything and NEVER touches a live DB. The
#     Hermes-side `mnemosyne-backup-restore.sh verify-restore` step consumes
#     the handoff next.
#
# Usage:
#   mnemosyne-backup-recover.sh <slot>
#
# Env:
#   MNEMOSYNE_BACKUP_RCLONE_REMOTE - REQUIRED crypt remote name
#   MNEMOSYNE_BACKUP_RCLONE_PATH   - remote base path (default Josemar/mnemosyne-backups)
#   MNEMOSYNE_BACKUP_SLOTS         - rotating slots (default 5)
#   MNEMOSYNE_BACKUP_RECOVERY_DIR  - recovery handoff dir (default /recovery)
#   RCLONE_CONFIG                  - rclone config SEED path (default
#                                    /config/rclone/rclone.conf; the
#                                    published read-only config). The
#                                    recover step runs rclone against an
#                                    ephemeral private writable copy
#                                    (OAuth-refresh fix).
#
# Exit codes: 0 success, 2 validation/known error, 3 unexpected error.

set -eu

REMOTE_NAME="${MNEMOSYNE_BACKUP_RCLONE_REMOTE:-}"
REMOTE_PATH="${MNEMOSYNE_BACKUP_RCLONE_PATH:-Josemar/mnemosyne-backups}"
SLOTS="${MNEMOSYNE_BACKUP_SLOTS:-5}"
RECOVERY_DIR="${MNEMOSYNE_BACKUP_RECOVERY_DIR:-/recovery}"
RCLONE_CONFIG_FILE="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"

log_info() { echo "[mnemosyne-backup-recover] $1"; }
log_error() { echo "[mnemosyne-backup-recover] ERROR: $1" >&2; }

# Shared rclone OAuth-refresh runtime helper (see rclone-active-config.sh):
# rclone runs against a private writable ACTIVE copy of the config, never
# the read-only seed.
. "$(dirname "$0")/rclone-active-config.sh"

# OAuth-refresh fix (recover lane): the active copy lives in an EPHEMERAL
# PRIVATE temp dir created on first use and removed on exit — never inside
# the recovery handoff volume (which is handed off to Hermes-side
# verify/install steps without any rclone credentials).
_ACTIVE_CONFIG_DIR=""
prepare_active_config() {
    if [ -z "$_ACTIVE_CONFIG_DIR" ]; then
        _ACTIVE_CONFIG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mnemosyne-backup-rclone.XXXXXX")"
        trap 'rm -rf "$_ACTIVE_CONFIG_DIR"' EXIT
        trap 'rm -rf "$_ACTIVE_CONFIG_DIR"; exit 143' INT TERM
    fi
    rclone_active_config_ensure "$RCLONE_CONFIG_FILE" "$_ACTIVE_CONFIG_DIR/rclone.conf"
}

validate_slots() {
    case "$SLOTS" in
        ''|*[!0-9]*)
            log_error "Invalid MNEMOSYNE_BACKUP_SLOTS: $SLOTS"
            exit 2
            ;;
    esac
    if [ "$SLOTS" -le 0 ]; then
        log_error "MNEMOSYNE_BACKUP_SLOTS must be greater than zero"
        exit 2
    fi
}

validate_slot() {
    slot="$1"
    case "$slot" in
        ''|*[!0-9]*)
            log_error "Invalid slot: $slot (must be a positive integer)"
            exit 2
            ;;
    esac
    if [ "$slot" -lt 1 ] || [ "$slot" -gt "$SLOTS" ]; then
        log_error "Slot $slot out of range 1..$SLOTS"
        exit 2
    fi
}

require_remote() {
    # Prepare the ephemeral private writable config BEFORE validating the
    # remote, so the validation reads the ACTIVE config.
    prepare_active_config
    if [ -z "$REMOTE_NAME" ]; then
        log_error "MNEMOSYNE_BACKUP_RCLONE_REMOTE is required (must be rclone type 'crypt')"
        exit 2
    fi
    if [ ! -f "$RCLONE_CONFIG_FILE" ]; then
        log_error "rclone config not found at $RCLONE_CONFIG_FILE"
        exit 2
    fi
    # Validate the remote is rclone type 'crypt' (not just naming convention).
    remote_type="$(rclone config show "$REMOTE_NAME:" --config "$RCLONE_CONFIG_FILE" 2>/dev/null \
        | awk -F'=' '/^type[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}')"
    if [ "$remote_type" != "crypt" ]; then
        log_error "Remote '$REMOTE_NAME' is not rclone type 'crypt' (got: '${remote_type:-missing}'). Recovery refuses to download from a non-crypt remote."
        exit 2
    fi
    log_info "Remote '$REMOTE_NAME' validated as type 'crypt'"
}

remote_base() {
    remote_path_clean="${REMOTE_PATH%/}"
    if [ -n "$remote_path_clean" ]; then
        echo "${REMOTE_NAME}:${remote_path_clean}"
    else
        echo "${REMOTE_NAME}:"
    fi
}

# Strictly validate a generation id: exactly YYYYmmddTHHMMSSffffffZ-<hex8>.
# Same contract as the uploader so a tampered manifest cannot point at a
# path that would escape the recovery handoff.
is_valid_generation_id() {
    gen_id="$1"
    if [ "${#gen_id}" -ne 31 ]; then return 1; fi
    case "$gen_id" in
        */*|*..*) return 1 ;;
    esac
    case "$gen_id" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f])
            return 0 ;;
        *)
            return 1 ;;
    esac
}

# Clear the disposable handoff volume so a stale partial download from a
# previous recovery attempt can never be mistaken for a fresh one.
clear_recovery_dir() {
    if [ -d "$RECOVERY_DIR" ]; then
        for entry in "$RECOVERY_DIR"/* "$RECOVERY_DIR"/.[!.]*; do
            [ -e "$entry" ] || continue
            rm -rf "$entry"
        done
    fi
}

main() {
    slot="${1:-}"
    validate_slots
    validate_slot "$slot"
    require_remote

    mkdir -p "$RECOVERY_DIR"
    clear_recovery_dir

    base="$(remote_base)"
    log_info "Downloading slot-$slot from ${base}/slot-${slot} to $RECOVERY_DIR"
    if ! rclone copy "${base}/slot-${slot}" "$RECOVERY_DIR" \
        --config "$RCLONE_CONFIG_FILE" \
        --include "mnemosyne.db.gz" --include "manifest.json"; then
        log_error "rclone download of slot-$slot failed"
        exit 2
    fi

    artifact="$RECOVERY_DIR/mnemosyne.db.gz"
    manifest="$RECOVERY_DIR/manifest.json"
    if [ ! -f "$artifact" ] || [ ! -f "$manifest" ]; then
        log_error "Download incomplete: missing artifact or manifest in $RECOVERY_DIR"
        exit 2
    fi

    # Validate the manifest generation_id strictly before handing off.
    manifest_gen="$(grep -o '"generation_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$manifest" \
        | head -n1 | sed 's/.*"generation_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')"
    if [ -z "$manifest_gen" ]; then
        log_error "Could not read generation_id from manifest $manifest"
        exit 2
    fi
    if ! is_valid_generation_id "$manifest_gen"; then
        log_error "Manifest generation_id invalid (rejected): $manifest_gen"
        exit 2
    fi

    # Verify the artifact SHA-256 against the manifest before handoff.
    expected_sha="$(grep -o '"sha256"[[:space:]]*:[[:space:]]*"[0-9a-f]\{64\}' "$manifest" \
        | head -n1 | sed 's/.*"\([0-9a-f]\{64\}\)$/\1/')"
    if [ -z "$expected_sha" ]; then
        log_error "Could not read artifact sha256 from manifest $manifest"
        exit 2
    fi
    actual_sha="$(sha256sum "$artifact" | cut -d' ' -f1)"
    if [ "$actual_sha" != "$expected_sha" ]; then
        log_error "SHA-256 mismatch for $artifact: expected $expected_sha got $actual_sha"
        exit 2
    fi

    # Write the handoff sentinel ONLY after all verification passed.
    printf '%s\n%s\n' "$manifest_gen" "$actual_sha" > "$RECOVERY_DIR/RECOVERY_READY"
    log_info "Recovery handoff ready: generation $manifest_gen (sha256 $actual_sha)"
    exit 0
}

main "$@"
