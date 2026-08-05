#!/bin/sh
# mnemosyne-backup-uploader.sh - Separate rclone uploader service.
#
# This service NEVER mounts hermes-data or /opt/data. Its staging mount is
# READ-ONLY. Its rclone config is READ-ONLY. Only its state volume is
# writable. It reuses the existing secret-managed obsidian-rclone-config
# volume because that volume can hold a separate mnemosyne-crypt remote.
#
# Contract (see docs/mnemosyne-operations.md):
#   - Require/configure MNEMOSYNE_BACKUP_RCLONE_REMOTE (must be rclone type
#     `crypt`; validated before upload, not just naming convention).
#   - Strictly validate the generation id read from the `latest` pointer
#     (no slash, no `..`, exact format YYYYmmddTHHMMSSffffffZ-<hex8>) and
#     verify the resolved generation path stays under the staging dir before
#     any rclone. Verify the manifest generation_id equals the pointer/dir.
#   - Verify manifest SHA before upload.
#   - Use rotating full-snapshot remote slots (default 5) with `rclone sync`
#     (full replacement) so stale files cannot remain in reused slots.
#   - Upload one immutable generation to slot-N and its manifest; advance slot
#     and append to the uploaded-generation ledger ONLY after BOTH the
#     generation sync AND the slot metadata upload succeed.
#   - If latest generation already uploaded, no-op.
#   - Do NOT delete READY/latest/artifacts from the read-only staging mount.
#   - Polls the staging mount for new generations (idempotent; does not depend
#     on deleting sentinels).
#   - A mkdir lock around run_once/upload prevents manual + daemon
#     invocations from rotating the same slot concurrently. Stale locks are
#     NOT auto-deleted; operator recovery is documented.
#
# Env:
#   MNEMOSYNE_BACKUP_STAGING_DIR  - read-only staging mount (default /staging)
#   MNEMOSYNE_BACKUP_STATE_DIR    - writable state volume (default /state)
#   MNEMOSYNE_BACKUP_RCLONE_REMOTE - REQUIRED crypt remote name (e.g. mnemosyne-crypt)
#   MNEMOSYNE_BACKUP_RCLONE_PATH  - remote base path (default Josemar/mnemosyne-backups)
#   MNEMOSYNE_BACKUP_SLOTS        - rotating full-snapshot slots (default 5)
#   MNEMOSYNE_BACKUP_POLL_INTERVAL - poll interval in SECONDS (default 300)
#   RCLONE_CONFIG                - rclone config path (default /config/rclone/rclone.conf)
#   MNEMOSYNE_BACKUP_RUN_ON_START - run once on start (default true)
#   MNEMOSYNE_BACKUP_ONCE        - one-shot mode (default false): perform ALL
#                                  startup validation, take the same upload
#                                  lock, run ONE upload attempt, clean the
#                                  lock/temp artifacts under the trap, then
#                                  EXIT with a meaningful status instead of
#                                  entering the poll loop. This is for tests,
#                                  manual cron-style invocations, and
#                                  operators; it is NOT the scheduled service
#                                  behavior (the default remains a
#                                  long-running daemon).
#
# Exit codes: 0 success/continue, 1 upload attempt failed (one-shot mode),
# 2 config/validation error, 3 unexpected error.

set -eu

STAGING_DIR="${MNEMOSYNE_BACKUP_STAGING_DIR:-/staging}"
STATE_DIR="${MNEMOSYNE_BACKUP_STATE_DIR:-/state}"
REMOTE_NAME="${MNEMOSYNE_BACKUP_RCLONE_REMOTE:-}"
REMOTE_PATH="${MNEMOSYNE_BACKUP_RCLONE_PATH:-Josemar/mnemosyne-backups}"
SLOTS="${MNEMOSYNE_BACKUP_SLOTS:-5}"
POLL_INTERVAL="${MNEMOSYNE_BACKUP_POLL_INTERVAL:-300}"
RUN_ON_START="${MNEMOSYNE_BACKUP_RUN_ON_START:-true}"
# One-shot mode: exactly one upload attempt, then exit (see header). This is
# the portable way to drive the uploader from tests/manual cron invocations
# without relying on signals to stop a backgrounded daemon.
ONCE="${MNEMOSYNE_BACKUP_ONCE:-false}"
RCLONE_CONFIG_FILE="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"

# Lock dir for run_once/upload mutual exclusion (manual + daemon).
UPLOAD_LOCK_DIR="$STATE_DIR/.upload.lock"
# Temp slot metadata file (cleaned on exit/failure).
_SLOT_META=""

log_info() { echo "[mnemosyne-backup-uploader] $1"; }
log_error() { echo "[mnemosyne-backup-uploader] ERROR: $1" >&2; }

cleanup() {
    # Clean temp slot metadata on exit/failure.
    if [ -n "$_SLOT_META" ] && [ -f "$_SLOT_META" ]; then
        rm -f "$_SLOT_META"
        _SLOT_META=""
    fi
    # Release the upload lock if we hold it. We do NOT remove a lock we did
    # not create (no unsafe automatic stale-lock deletion).
    if [ -n "${_LOCK_HELD:-}" ] && [ "$_LOCK_HELD" = "1" ]; then
        rmdir "$UPLOAD_LOCK_DIR" 2>/dev/null || true
        _LOCK_HELD="0"
    fi
}
# EXIT trap runs on every exit path (including the signal traps below).
trap cleanup EXIT
# INT/TERM must CLEAN UP AND EXIT. dash defers a trapped signal until the
# current foreground command (e.g. `sleep`) returns, so a trap that only
# cleans up would let the poll loop continue forever; exiting explicitly is
# what actually stops the daemon.
trap 'cleanup; exit 143' INT TERM
_LOCK_HELD="0"

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

validate_poll_interval() {
    case "$POLL_INTERVAL" in
        ''|*[!0-9]*)
            log_error "Invalid MNEMOSYNE_BACKUP_POLL_INTERVAL: $POLL_INTERVAL (must be a positive integer of seconds)"
            exit 2
            ;;
    esac
    if [ "$POLL_INTERVAL" -le 0 ]; then
        log_error "MNEMOSYNE_BACKUP_POLL_INTERVAL must be greater than zero"
        exit 2
    fi
}

require_remote() {
    if [ -z "$REMOTE_NAME" ]; then
        log_error "MNEMOSYNE_BACKUP_RCLONE_REMOTE is required (must be rclone type 'crypt')"
        exit 2
    fi
    if [ ! -f "$RCLONE_CONFIG_FILE" ]; then
        log_error "rclone config not found at $RCLONE_CONFIG_FILE"
        exit 2
    fi
    # Validate the remote is rclone type 'crypt' (not just naming convention).
    # rclone config show prints the remote's config; we check type = crypt.
    remote_type="$(rclone config show "$REMOTE_NAME:" --config "$RCLONE_CONFIG_FILE" 2>/dev/null \
        | awk -F'=' '/^type[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}')"
    if [ "$remote_type" != "crypt" ]; then
        log_error "Remote '$REMOTE_NAME' is not rclone type 'crypt' (got: '${remote_type:-missing}'). Configure a crypt remote in the obsidian-rclone-config volume."
        exit 2
    fi
    log_info "Remote '$REMOTE_NAME' validated as type 'crypt'"
}

# Strictly validate a generation id: exactly YYYYmmddTHHMMSSffffffZ-<hex8>.
# Rejects slashes, '..', and any other traversal/malformed input. This guards
# against path traversal via a malicious `latest` pointer.
is_valid_generation_id() {
    gen_id="$1"
    # Length must be exactly 31.
    if [ "${#gen_id}" -ne 31 ]; then return 1; fi
    # Must not contain a slash or '..' (defense in depth).
    case "$gen_id" in
        */*|*..*) return 1 ;;
    esac
    # Pattern: 8 digits, T, 6 digits, 6 digits, Z, -, 8 hex.
    # POSIX sh has no [[ =~ ]]; use a case glob approximation plus a grep
    # check for the hex suffix.
    case "$gen_id" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f])
            return 0 ;;
        *)
            return 1 ;;
    esac
}

# Verify the resolved generation path stays under the staging dir (no
# traversal). Uses realpath if available; falls back to a lexical prefix
# check.
path_under_staging() {
    target="$1"
    if command -v realpath >/dev/null 2>&1; then
        real_staging="$(realpath "$STAGING_DIR" 2>/dev/null || echo "$STAGING_DIR")"
        real_target="$(realpath "$target" 2>/dev/null || echo "$target")"
        case "$real_target" in
            "$real_staging"|"$real_staging"/*) return 0 ;;
            *) return 1 ;;
        esac
    else
        case "$target" in
            "$STAGING_DIR"|"$STAGING_DIR"/*) return 0 ;;
            *) return 1 ;;
        esac
    fi
}

read_slot() {
    slot_file="$STATE_DIR/next-slot"
    slot="1"
    if [ -f "$slot_file" ]; then
        saved_slot="$(tr -dc '0-9' < "$slot_file")"
        if [ -n "$saved_slot" ] && [ "$saved_slot" -ge 1 ] && [ "$saved_slot" -le "$SLOTS" ]; then
            slot="$saved_slot"
        fi
    fi
    echo "$slot"
}

write_next_slot() {
    current_slot="$1"
    next_slot=$((current_slot + 1))
    if [ "$next_slot" -gt "$SLOTS" ]; then
        next_slot=1
    fi
    tmp_slot_file="$STATE_DIR/.next-slot.tmp.$$"
    printf '%s\n' "$next_slot" > "$tmp_slot_file"
    mv "$tmp_slot_file" "$STATE_DIR/next-slot"
}

read_last_uploaded() {
    f="$STATE_DIR/last-uploaded-generation"
    if [ -f "$f" ]; then
        cat "$f"
    else
        echo ""
    fi
}

write_last_uploaded() {
    gen="$1"
    tmp="$STATE_DIR/.last-uploaded.tmp.$$"
    printf '%s\n' "$gen" > "$tmp"
    mv "$tmp" "$STATE_DIR/last-uploaded-generation"
}

# Append a generation id to the uploaded-generation ledger (JSONL, one id
# per line). This is the bounded/parseable acknowledgement ledger the
# exporter reads read-only to learn which generations are safely remote
# before pruning anything locally. Append-only so it is safe under
# concurrent readers.
append_uploaded_ledger() {
    gen="$1"
    ledger="$STATE_DIR/uploaded-generations.jsonl"
    tmp="$STATE_DIR/.uploaded-ledger.tmp.$$"
    # Append atomically: copy current, append, rename.
    if [ -f "$ledger" ]; then
        cp "$ledger" "$tmp"
    fi
    printf '%s\n' "$gen" >> "$tmp"
    mv "$tmp" "$ledger"
}

remote_base() {
    remote_path_clean="${REMOTE_PATH%/}"
    if [ -n "$remote_path_clean" ]; then
        echo "${REMOTE_NAME}:${remote_path_clean}"
    else
        echo "${REMOTE_NAME}:"
    fi
}

# Verify the artifact SHA-256 against the manifest before upload.
# Shell-only JSON extraction (the uploader runs in the rclone image which has
# no Python). The manifest is machine-written by the exporter with a stable
# sorted-keys format: the artifact.sha256 field appears as
#   "sha256": "<64 hex chars>"
# Extract the first such value after the "artifact" block.
verify_manifest_sha() {
    gen_dir="$1"
    artifact="$gen_dir/mnemosyne.db.gz"
    manifest="$gen_dir/manifest.json"
    if [ ! -f "$artifact" ] || [ ! -f "$manifest" ]; then
        log_error "Missing artifact or manifest in $gen_dir"
        return 1
    fi
    # Extract the sha256 value from the manifest using grep + sed. The
    # exporter writes sorted-keys JSON so "sha256" appears inside the artifact
    # object. Match the first 64-hex-char value after a "sha256" key.
    expected_sha="$(grep -o '"sha256"[[:space:]]*:[[:space:]]*"[0-9a-f]\{64\}' "$manifest" \
        | head -n1 | sed 's/.*"\([0-9a-f]\{64\}\)$/\1/')"
    if [ -z "$expected_sha" ]; then
        log_error "Could not read artifact sha256 from manifest $manifest"
        return 1
    fi
    actual_sha="$(sha256sum "$artifact" | cut -d' ' -f1)"
    if [ "$actual_sha" != "$expected_sha" ]; then
        log_error "SHA-256 mismatch for $artifact: expected $expected_sha got $actual_sha"
        return 1
    fi
    return 0
}

# Verify the manifest's generation_id equals the expected dir name. This
# prevents uploading a manifest that points at a different generation than
# its containing dir (defense against a swapped/tampered manifest).
verify_manifest_generation_id() {
    gen_dir="$1"
    expected_gen="$2"
    manifest="$gen_dir/manifest.json"
    if [ ! -f "$manifest" ]; then
        log_error "Missing manifest in $gen_dir"
        return 1
    fi
    manifest_gen="$(grep -o '"generation_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$manifest" \
        | head -n1 | sed 's/.*"generation_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')"
    if [ -z "$manifest_gen" ]; then
        log_error "Could not read generation_id from manifest $manifest"
        return 1
    fi
    if [ "$manifest_gen" != "$expected_gen" ]; then
        log_error "Manifest generation_id mismatch: manifest=$manifest_gen dir=$expected_gen"
        return 1
    fi
    return 0
}

upload_one() {
    gen_id="$1"
    # Strictly validate the generation id before any path construction.
    if ! is_valid_generation_id "$gen_id"; then
        log_error "Invalid generation id (rejected to prevent path traversal): $gen_id"
        return 1
    fi
    gen_dir="$STAGING_DIR/$gen_id"
    # Verify the resolved path stays under the staging dir.
    if ! path_under_staging "$gen_dir"; then
        log_error "Resolved generation path escapes staging dir: $gen_dir"
        return 1
    fi
    if [ ! -d "$gen_dir" ]; then
        log_error "Generation dir not found: $gen_dir"
        return 1
    fi
    if [ ! -f "$gen_dir/READY" ]; then
        log_info "Generation $gen_id not READY, skipping"
        return 1
    fi
    ready_gen="$(IFS= read -r ready_line < "$gen_dir/READY"; printf '%s' "$ready_line")"
    if [ "$ready_gen" != "$gen_id" ]; then
        log_error "READY generation_id mismatch: READY=$ready_gen dir=$gen_id"
        return 1
    fi
    if ! verify_manifest_generation_id "$gen_dir" "$gen_id"; then
        return 1
    fi
    if ! verify_manifest_sha "$gen_dir"; then
        return 1
    fi

    slot="$(read_slot)"
    base="$(remote_base)"
    slot_name="slot-${slot}"
    slot_target="${base}/${slot_name}"

    log_info "Uploading generation $gen_id to ${slot_target}"
    # Upload the immutable generation to slot-N using `rclone sync` (full
    # replacement) so stale files from a previous generation cannot remain in
    # the reused slot. Copy the whole generation dir (it contains only
    # mnemosyne.db.gz, manifest.json, READY). Do not use --include with
    # plaintext names because the crypt remote encrypts file names and the
    # filter would not match through the crypt layer.
    rclone sync "$gen_dir" "$slot_target" --create-empty-src-dirs \
        --config "$RCLONE_CONFIG_FILE"

    # Upload a slot manifest pointing at this generation.
    _SLOT_META="$(mktemp)"
    cat > "$_SLOT_META" <<EOF
{
  "slot": ${slot},
  "generation_id": "${gen_id}",
  "created_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "snapshot": "${slot_name}",
  "snapshot_path": "${slot_target}",
  "rotation_slots": ${SLOTS}
}
EOF
    rclone copyto "$_SLOT_META" "${base}/slot-${slot}.json" --config "$RCLONE_CONFIG_FILE"
    rm -f "$_SLOT_META"
    _SLOT_META=""

    # Advance slot and record the upload in the ledger ONLY after BOTH the
    # generation sync AND the slot metadata upload succeed.
    write_next_slot "$slot"
    write_last_uploaded "$gen_id"
    append_uploaded_ledger "$gen_id"
    log_info "Upload complete: generation $gen_id -> slot $slot"
    return 0
}

run_once() {
    latest_file="$STAGING_DIR/latest"
    if [ ! -f "$latest_file" ]; then
        log_info "No latest pointer yet; nothing to upload"
        return 0
    fi
    latest_gen="$(tr -d '[:space:]' < "$latest_file")"
    if [ -z "$latest_gen" ]; then
        log_info "Empty latest pointer; nothing to upload"
        return 0
    fi
    # Strictly validate the latest pointer before any use.
    if ! is_valid_generation_id "$latest_gen"; then
        log_error "Invalid latest pointer (rejected to prevent path traversal): $latest_gen"
        return 1
    fi
    last_uploaded="$(read_last_uploaded)"
    if [ "$latest_gen" = "$last_uploaded" ]; then
        log_info "Latest generation $latest_gen already uploaded; no-op"
        return 0
    fi
    upload_one "$latest_gen" || {
        log_error "Upload of generation $latest_gen failed; state NOT advanced"
        return 1
    }
    return 0
}

# Acquire the upload lock (mkdir-based, flock-equivalent). Does NOT auto-delete
# a stale lock; operator recovery is documented.
acquire_upload_lock() {
    if [ -d "$UPLOAD_LOCK_DIR" ]; then
        log_error "Upload already in progress (lock: $UPLOAD_LOCK_DIR). If no upload is running, remove the lock dir manually."
        return 1
    fi
    if ! mkdir "$UPLOAD_LOCK_DIR" 2>/dev/null; then
        log_error "Could not acquire upload lock: $UPLOAD_LOCK_DIR"
        return 1
    fi
    _LOCK_HELD="1"
    return 0
}

# Startup validation shared by daemon and one-shot modes. Exits 2 on any
# config/validation error (the trap cleans temp artifacts).
startup_checks() {
    validate_slots
    validate_poll_interval
    require_remote
    mkdir -p "$STATE_DIR"

    if [ ! -d "$STAGING_DIR" ]; then
        log_error "Staging dir not found at $STAGING_DIR"
        exit 2
    fi
}

main() {
    startup_checks

    if [ "$ONCE" = "true" ]; then
        # One-shot mode: validate everything (done above), take the SAME
        # upload lock, run ONE upload attempt, then exit with a meaningful
        # status instead of polling. This is NOT scheduled-service behavior;
        # the default daemon path below is unchanged.
        log_info "Uploader one-shot mode (staging=$STAGING_DIR state=$STATE_DIR remote=$REMOTE_NAME slots=$SLOTS)"
        if ! acquire_upload_lock; then
            # Lock held by a concurrent daemon/manual invocation. Fail closed
            # (one-shot callers must observe the lock was not taken). The trap
            # releases nothing because we never held the lock.
            exit 1
        fi
        run_once
        rc=$?
        # Cleanup runs under the trap (releases our lock, removes temp files).
        # `exit $rc` preserves the attempt status through the EXIT trap.
        exit $rc
    fi

    log_info "Uploader started (staging=$STAGING_DIR state=$STATE_DIR remote=$REMOTE_NAME slots=$SLOTS poll=${POLL_INTERVAL}s)"

    if [ "$RUN_ON_START" = "true" ]; then
        if acquire_upload_lock; then
            run_once || true
            cleanup
        fi
    fi

    while true; do
        sleep "$POLL_INTERVAL"
        if acquire_upload_lock; then
            run_once || true
            cleanup
        fi
    done
}

main "$@"
