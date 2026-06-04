#!/bin/sh
# Josemar compatibility setup for the Hermes Agent Docker image.

set -eu

log() {
    echo "[josemar-hermes] $1"
}

HERMES_HOME="${HERMES_HOME:-/opt/data}"
WORKSPACE_DIR="${WORKSPACE_DIR:-${HERMES_HOME}/workspace}"
OBSIDIAN_VAULT_DIR="${OBSIDIAN_VAULT_DIR:-${HERMES_HOME}/obsidian}"
SOURCE_STATE_DIR="${JOSEMAR_SOURCE_STATE_DIR:-/opt/josemar/source-agent-state}"
CREDENTIALS_SOURCE_DIR="${JOSEMAR_CREDENTIALS_SOURCE_DIR:-/opt/josemar/credentials-source}"
CREDENTIALS_DIR="${JOSEMAR_CREDENTIALS_DIR:-${HERMES_HOME}/credentials}"
HERMES_UID_VALUE="${HERMES_UID:-${PUID:-10000}}"
HERMES_GID_VALUE="${HERMES_GID:-${PGID:-10000}}"

mkdir -p "$HERMES_HOME" "$WORKSPACE_DIR" "$OBSIDIAN_VAULT_DIR" "$CREDENTIALS_DIR"

if [ ! -f "${HERMES_HOME}/config.yaml" ] && [ -f /opt/josemar/hermes/config.yaml ]; then
    log "Seeding Hermes config.yaml"
    cp /opt/josemar/hermes/config.yaml "${HERMES_HOME}/config.yaml"
fi

seed_workspace_from_manifest() {
    if [ ! -f "${SOURCE_STATE_DIR}/.sync-manifest" ]; then
        return 0
    fi

    if [ -n "$(ls -A "$WORKSPACE_DIR" 2>/dev/null || true)" ]; then
        return 0
    fi

    log "Seeding workspace from mounted agent-state manifest"
    cp "${SOURCE_STATE_DIR}/.sync-manifest" "${WORKSPACE_DIR}/.sync-manifest"

    while IFS= read -r pattern; do
        case "$pattern" in
            \#*|"") continue ;;
        esac

        for src in ${SOURCE_STATE_DIR}/${pattern}; do
            [ -e "$src" ] || continue
            rel_path="${src#${SOURCE_STATE_DIR}/}"
            dest_path="${WORKSPACE_DIR}/${rel_path}"
            mkdir -p "$(dirname "$dest_path")"
            cp -R "$src" "$dest_path"
        done
    done < "${SOURCE_STATE_DIR}/.sync-manifest"
}

if [ -n "${WORKSPACE_STATE_REPO:-}" ]; then
    log "Running workspace git sync"
    WORKSPACE_DIR="$WORKSPACE_DIR" /usr/local/bin/workspace-sync.sh || \
        log "WARNING: workspace git sync failed; continuing with existing workspace"
else
    seed_workspace_from_manifest
fi

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

if [ "${HERMES_SYNC_JOSEMAR_SOUL:-true}" = "true" ] && [ -f "${WORKSPACE_DIR}/SOUL.md" ]; then
    log "Syncing Josemar SOUL.md into Hermes home"
    cp "${WORKSPACE_DIR}/SOUL.md" "${HERMES_HOME}/SOUL.md"
fi

mkdir -p "${HERMES_HOME}/memories"
for memory_file in MEMORY.md USER.md; do
    if [ -f "${WORKSPACE_DIR}/${memory_file}" ]; then
        cp "${WORKSPACE_DIR}/${memory_file}" "${HERMES_HOME}/memories/${memory_file}"
    fi
done

if [ -x "${WORKSPACE_DIR}/skills/gogcli-tables/bin/gogx" ]; then
    ln -sf "${WORKSPACE_DIR}/skills/gogcli-tables/bin/gogx" /usr/local/bin/gogx
fi

chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "$HERMES_HOME" 2>/dev/null || true

log "Josemar Hermes setup complete"
