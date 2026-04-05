#!/bin/sh
# obsidian-backup.sh - Daily Obsidian vault backup to Google Drive

set -eu

SOURCE_DIR="${OBSIDIAN_VAULT_DIR:-/data/obsidian}"
STATE_DIR="${OBSIDIAN_BACKUP_STATE_DIR:-/state}"
REMOTE_NAME="${OBSIDIAN_GDRIVE_REMOTE:-gdrive}"
REMOTE_PATH="${OBSIDIAN_GDRIVE_PATH:-Josemar/obsidian-backups}"
SLOTS="${OBSIDIAN_BACKUP_SLOTS:-5}"
RCLONE_CONFIG_FILE="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
LOCK_DIR=""

log_info() {
    echo "[obsidian-backup] $1"
}

log_error() {
    echo "[obsidian-backup] ERROR: $1" >&2
}

validate_slots() {
    case "$SLOTS" in
        ''|*[!0-9]*)
            log_error "Invalid OBSIDIAN_BACKUP_SLOTS: $SLOTS"
            exit 1
            ;;
        *) ;;
    esac

    if [ "$SLOTS" -le 0 ]; then
        log_error "OBSIDIAN_BACKUP_SLOTS must be greater than zero"
        exit 1
    fi
}

read_slot() {
    slot_file="$STATE_DIR/next-slot"
    slot="1"

    if [ -f "$slot_file" ]; then
        saved_slot=$(tr -dc '0-9' < "$slot_file")
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

release_lock() {
    if [ -n "$LOCK_DIR" ] && [ -d "$LOCK_DIR" ]; then
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
}

acquire_lock() {
    LOCK_DIR="$STATE_DIR/.backup.lock"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        log_error "Backup already running (lock: $LOCK_DIR)"
        exit 1
    fi
    trap release_lock EXIT INT TERM
}

main() {
    validate_slots

    if [ ! -f "$RCLONE_CONFIG_FILE" ]; then
        log_error "rclone config not found at $RCLONE_CONFIG_FILE"
        exit 1
    fi

    if [ ! -d "$SOURCE_DIR" ]; then
        log_error "Vault directory not found at $SOURCE_DIR"
        exit 1
    fi

    mkdir -p "$STATE_DIR"
    acquire_lock

    slot=$(read_slot)
    timestamp_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    timestamp_file=$(date -u +"%Y%m%dT%H%M%SZ")
    metadata_file="/tmp/obsidian-vault-${timestamp_file}.json"

    remote_path_clean="${REMOTE_PATH%/}"
    if [ -n "$remote_path_clean" ]; then
        remote_base="${REMOTE_NAME}:${remote_path_clean}"
    else
        remote_base="${REMOTE_NAME}:"
    fi

    slot_name="slot-${slot}"
    slot_target="${remote_base}/${slot_name}"
    metadata_name="slot-${slot}.json"

    log_info "Syncing vault to ${slot_target}"
    rclone sync "$SOURCE_DIR" "$slot_target" --create-empty-src-dirs

    cat > "$metadata_file" <<EOF
{
  "slot": ${slot},
  "created_at_utc": "${timestamp_utc}",
  "snapshot": "${slot_name}",
  "snapshot_path": "${slot_target}",
  "source": "${SOURCE_DIR}",
  "rotation_slots": ${SLOTS}
}
EOF

    log_info "Uploading ${metadata_name} to ${remote_base}"
    rclone copyto "$metadata_file" "${remote_base}/${metadata_name}"

    write_next_slot "$slot"

    rm -f "$metadata_file"
    log_info "Backup completed successfully"
}

main "$@"
