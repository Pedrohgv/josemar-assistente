#!/bin/sh
# obsidian-backup-daemon.sh - Schedules one backup per day at OBSIDIAN_BACKUP_TIME

set -eu

BACKUP_SCRIPT="${OBSIDIAN_BACKUP_SCRIPT:-/scripts/obsidian-backup.sh}"
BACKUP_TIME="${OBSIDIAN_BACKUP_TIME:-03:15}"
RUN_ON_START="${OBSIDIAN_BACKUP_RUN_ON_START:-false}"

log_info() {
    echo "[obsidian-backup-daemon] $1"
}

log_error() {
    echo "[obsidian-backup-daemon] ERROR: $1" >&2
}

to_int() {
    value="$1"
    value="${value#0}"
    if [ -z "$value" ]; then
        value=0
    fi
    echo "$value"
}

validate_time() {
    case "$BACKUP_TIME" in
        [0-2][0-9]:[0-5][0-9]) ;;
        *)
            log_error "Invalid OBSIDIAN_BACKUP_TIME format: $BACKUP_TIME (expected HH:MM)"
            exit 1
            ;;
    esac

    target_hour=$(to_int "${BACKUP_TIME%:*}")
    target_minute=$(to_int "${BACKUP_TIME#*:}")

    if [ "$target_hour" -gt 23 ]; then
        log_error "Invalid OBSIDIAN_BACKUP_TIME hour: $target_hour"
        exit 1
    fi
}

seconds_until_next_run() {
    now_hour=$(to_int "$(date +%H)")
    now_minute=$(to_int "$(date +%M)")
    now_second=$(to_int "$(date +%S)")

    now_total=$((now_hour * 3600 + now_minute * 60 + now_second))
    target_total=$((target_hour * 3600 + target_minute * 60))

    if [ "$target_total" -le "$now_total" ]; then
        echo $((86400 - now_total + target_total))
    else
        echo $((target_total - now_total))
    fi
}

run_backup() {
    if /bin/sh "$BACKUP_SCRIPT"; then
        log_info "Backup job finished"
    else
        log_error "Backup job failed"
    fi
}

main() {
    if [ ! -f "$BACKUP_SCRIPT" ]; then
        log_error "Backup script not found at $BACKUP_SCRIPT"
        exit 1
    fi

    validate_time
    log_info "Scheduler started (daily at ${BACKUP_TIME})"

    if [ "$RUN_ON_START" = "true" ]; then
        log_info "Running immediate startup backup"
        run_backup
    fi

    while true; do
        sleep_seconds=$(seconds_until_next_run)
        log_info "Next backup in ${sleep_seconds}s"
        sleep "$sleep_seconds"
        run_backup
    done
}

main "$@"
