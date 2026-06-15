#!/bin/sh
# Josemar compatibility setup for the Hermes Agent Docker image.

set -eu

log() {
    echo "[josemar-hermes] $1"
}

HERMES_HOME="${HERMES_HOME:-/opt/data}"
WORKSPACE_DIR="${WORKSPACE_DIR:-${HERMES_HOME}}"
OBSIDIAN_VAULT_DIR="${OBSIDIAN_VAULT_DIR:-${HERMES_HOME}/obsidian}"
SOURCE_STATE_DIR="${JOSEMAR_SOURCE_STATE_DIR:-/opt/josemar/source-agent-state}"
CREDENTIALS_SOURCE_DIR="${JOSEMAR_CREDENTIALS_SOURCE_DIR:-/opt/josemar/credentials-source}"
CREDENTIALS_DIR="${JOSEMAR_CREDENTIALS_DIR:-${HERMES_HOME}/credentials}"
HERMES_UID_VALUE="${HERMES_UID:-${PUID:-10000}}"
HERMES_GID_VALUE="${HERMES_GID:-${PGID:-10000}}"

mkdir -p "$HERMES_HOME" "$WORKSPACE_DIR" "$OBSIDIAN_VAULT_DIR" "$CREDENTIALS_DIR"

SOURCE_CONFIG="/opt/josemar/hermes/config.yaml"
RUNTIME_CONFIG="${HERMES_HOME}/config.yaml"

if [ -f "$SOURCE_CONFIG" ]; then
    if [ -f "$RUNTIME_CONFIG" ] && ! cmp -s "$SOURCE_CONFIG" "$RUNTIME_CONFIG" 2>/dev/null; then
        CONFIG_BACKUP="${RUNTIME_CONFIG}.runtime-backup.$(date +%Y%m%d%H%M%S).$$"
        log "Backing up existing Hermes config.yaml to ${CONFIG_BACKUP}"
        cp "$RUNTIME_CONFIG" "$CONFIG_BACKUP"
    fi

    log "Syncing Hermes config.yaml from repo template"
    CONFIG_TMP="${RUNTIME_CONFIG}.tmp.$$"
    cp "$SOURCE_CONFIG" "$CONFIG_TMP"
    mv "$CONFIG_TMP" "$RUNTIME_CONFIG"
fi

seed_workspace_from_manifest() {
    if [ ! -f "${SOURCE_STATE_DIR}/.sync-manifest" ]; then
        return 0
    fi

    log "Seeding missing state files from mounted agent-state manifest"
    if [ ! -f "${WORKSPACE_DIR}/.sync-manifest" ]; then
        cp "${SOURCE_STATE_DIR}/.sync-manifest" "${WORKSPACE_DIR}/.sync-manifest"
    fi

    while IFS= read -r pattern; do
        case "$pattern" in
            \#*|"") continue ;;
        esac

        for src in ${SOURCE_STATE_DIR}/${pattern}; do
            [ -e "$src" ] || continue
            rel_path="${src#${SOURCE_STATE_DIR}/}"
            dest_path="${WORKSPACE_DIR}/${rel_path}"
            if [ ! -e "$dest_path" ]; then
                mkdir -p "$(dirname "$dest_path")"
                cp -R "$src" "$dest_path"
            fi
        done
    done < "${SOURCE_STATE_DIR}/.sync-manifest"
}

install_workspace_sync_cron() {
    script_source="/opt/josemar/scripts/hermes-workspace-sync-cron.sh"
    script_dir="${HERMES_HOME}/.hermes/scripts"
    script_path="${script_dir}/hermes-workspace-sync-cron.sh"
    sync_interval="${WORKSPACE_SYNC_INTERVAL:-60}"

    if [ ! -x "$script_source" ]; then
        return 0
    fi

    mkdir -p "$script_dir"
    cp "$script_source" "$script_path"
    chmod 700 "$script_path"
    chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "${HERMES_HOME}/.hermes" "${HERMES_HOME}/cron" 2>/dev/null || true

    if [ -z "${WORKSPACE_STATE_REPO:-}" ]; then
        return 0
    fi

    case "$sync_interval" in
        ""|0|*[!0-9]*)
            log "Hermes workspace-sync cron disabled (WORKSPACE_SYNC_INTERVAL=${sync_interval:-unset})"
            return 0
            ;;
    esac

    if python3 - "${HERMES_HOME}/cron/jobs.json" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(1)

for job in data.get("jobs", []):
    if job.get("name") == "workspace-sync":
        sys.exit(0)

sys.exit(1)
PY
    then
        log "Hermes workspace-sync cron job already exists"
        return 0
    fi

    log "Creating Hermes workspace-sync cron job"
    su -s /bin/sh hermes -c '
        HOME=$1
        HERMES_HOME=$1
        WORKSPACE_DIR=$2
        export HOME HERMES_HOME WORKSPACE_DIR
        shift 2
        exec hermes cron create "$@"
    ' sh \
        "$HERMES_HOME" \
        "$WORKSPACE_DIR" \
        "every ${sync_interval}m" \
        --no-agent \
        --script hermes-workspace-sync-cron.sh \
        --workdir "$WORKSPACE_DIR" \
        --name workspace-sync \
        || log "WARNING: failed to create Hermes workspace-sync cron job"
}

if [ -n "${WORKSPACE_STATE_REPO:-}" ]; then
    log "Running workspace git sync as hermes user"
    chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "$WORKSPACE_DIR" 2>/dev/null || true
    su -s /bin/sh hermes -c "
        WORKSPACE_DIR='${WORKSPACE_DIR}'
        WORKSPACE_STATE_REPO='${WORKSPACE_STATE_REPO}'
        WORKSPACE_REPO_TOKEN='${WORKSPACE_REPO_TOKEN}'
        WORKSPACE_GIT_BRANCH='${WORKSPACE_GIT_BRANCH:-main}'
        WORKSPACE_GIT_USER_EMAIL='${WORKSPACE_GIT_USER_EMAIL:-agent@josemar.local}'
        WORKSPACE_GIT_USER_NAME='${WORKSPACE_GIT_USER_NAME:-Josemar Agent}'
        WORKSPACE_SYNC_ON_START='${WORKSPACE_SYNC_ON_START:-true}'
        export WORKSPACE_DIR WORKSPACE_STATE_REPO WORKSPACE_REPO_TOKEN
        export WORKSPACE_GIT_BRANCH WORKSPACE_GIT_USER_EMAIL WORKSPACE_GIT_USER_NAME
        export WORKSPACE_SYNC_ON_START
        /usr/local/bin/workspace-sync.sh
    " || log "WARNING: workspace git sync failed; continuing"
elif [ ! -d "${WORKSPACE_DIR}/.git" ]; then
    seed_workspace_from_manifest
fi

mkdir -p "${HERMES_HOME}/cron"

if [ ! -f "${HERMES_HOME}/cron/jobs.json" ]; then
    log "Creating empty Hermes cron/jobs.json"
    cat > "${HERMES_HOME}/cron/jobs.json" <<'EOF'
{
  "jobs": [],
  "updated_at": null
}
EOF
fi

install_workspace_sync_cron

if [ -d "$CREDENTIALS_SOURCE_DIR" ]; then
    log "Copying mounted credentials into Hermes data volume"
    rm -rf "${CREDENTIALS_DIR:?}/"*
    for service_dir in "$CREDENTIALS_SOURCE_DIR"/*/; do
        [ -d "$service_dir" ] || continue
        service_name=$(basename "$service_dir")
        mkdir -p "${CREDENTIALS_DIR}/${service_name}"
        cp -R "${service_dir}"* "${CREDENTIALS_DIR}/${service_name}/" 2>/dev/null || true
    done
    chmod -R go-rwx "$CREDENTIALS_DIR" 2>/dev/null || true
fi

mkdir -p "${HERMES_HOME}/memories"

if [ -x "${WORKSPACE_DIR}/skills/gogcli-tables/bin/gogx" ]; then
    ln -sf "${WORKSPACE_DIR}/skills/gogcli-tables/bin/gogx" /usr/local/bin/gogx
fi

chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "$HERMES_HOME" 2>/dev/null || true

log "Josemar Hermes setup complete"
