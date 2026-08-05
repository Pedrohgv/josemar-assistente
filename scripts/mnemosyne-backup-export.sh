#!/bin/sh
# mnemosyne-backup-export.sh - Thin wrapper for the Hermes-side exporter.
#
# Invokes the Python core (scripts/mnemosyne_backup_core.py) to create one
# immutable backup generation on the staging volume using the pinned DR seam
# mnemosyne.dr.recovery.create_backup (sqlite-vec-aware online backup).
#
# This wrapper is what the opt-in Hermes no-agent export cron invokes (the
# cron is wired by the activation/init code). It does NOT schedule itself.
#
# Env:
#   MNEMOSYNE_DATA_DIR            - source data dir (default /opt/data/mnemosyne/data)
#   MNEMOSYNE_BACKUP_STAGING_DIR  - staging volume root (default /opt/data/mnemosyne-backup/staging)
#   MNEMOSYNE_BACKUP_GENERATIONS_KEEP - local staging generations to retain (default 5)
#
# Exit codes: 0 success, 2 known backup error, 3 unexpected error.

set -eu

PYTHON="${MNEMOSYNE_BACKUP_PYTHON:-/opt/hermes/.venv/bin/python3}"
CORE="${MNEMOSYNE_BACKUP_CORE:-/opt/josemar/scripts/mnemosyne_backup_core.py}"

if [ ! -f "$CORE" ]; then
    echo "[mnemosyne-backup-export] core script not found at $CORE" >&2
    exit 2
fi

exec "$PYTHON" "$CORE" export "$@"