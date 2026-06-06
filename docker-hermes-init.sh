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

if [ -n "${WORKSPACE_STATE_REPO:-}" ] && [ ! -d "${WORKSPACE_DIR}/.git" ]; then
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
        WORKSPACE_SYNC_INTERVAL='${WORKSPACE_SYNC_INTERVAL:-0}'
        export WORKSPACE_DIR WORKSPACE_STATE_REPO WORKSPACE_REPO_TOKEN
        export WORKSPACE_GIT_BRANCH WORKSPACE_GIT_USER_EMAIL WORKSPACE_GIT_USER_NAME
        export WORKSPACE_SYNC_ON_START WORKSPACE_SYNC_INTERVAL
        /usr/local/bin/workspace-sync.sh
    " || log "WARNING: workspace git sync failed; continuing"
elif [ ! -d "${WORKSPACE_DIR}/.git" ]; then
    seed_workspace_from_manifest
fi

mkdir -p "${WORKSPACE_DIR}/cron"

if [ ! -f "${WORKSPACE_DIR}/cron/jobs.json" ] && [ -f "${HERMES_HOME}/cron/jobs.json" ]; then
    log "Seeding workspace cron/jobs.json from Hermes home"
    cp "${HERMES_HOME}/cron/jobs.json" "${WORKSPACE_DIR}/cron/jobs.json"
fi

if [ ! -f "${WORKSPACE_DIR}/cron/jobs.json" ]; then
    log "Creating empty workspace cron/jobs.json"
    cat > "${WORKSPACE_DIR}/cron/jobs.json" <<'EOF'
{
  "jobs": [],
  "updated_at": null
}
EOF
fi

if [ -d "${HERMES_HOME}/cron" ] && [ ! -L "${HERMES_HOME}/cron" ]; then
    if [ -f "${HERMES_HOME}/cron/jobs.json" ] && [ ! -f "${WORKSPACE_DIR}/cron/jobs.json" ]; then
        log "Seeding workspace cron/jobs.json from legacy Hermes cron directory"
        cp "${HERMES_HOME}/cron/jobs.json" "${WORKSPACE_DIR}/cron/jobs.json"
    fi
    log "Replacing Hermes cron directory with workspace-backed symlink"
    rm -rf "${HERMES_HOME}/cron"
fi

if [ ! -L "${HERMES_HOME}/cron" ]; then
    ln -s "${WORKSPACE_DIR}/cron" "${HERMES_HOME}/cron"
fi

if [ -f "${WORKSPACE_DIR}/cron/jobs.json" ] && [ ! -f "${HERMES_HOME}/cron/jobs.json" ]; then
    cp "${WORKSPACE_DIR}/cron/jobs.json" "${HERMES_HOME}/cron/jobs.json"
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
    log "Linking SOUL.md from workspace into Hermes home"
    rm -f "${HERMES_HOME}/SOUL.md"
    ln -s "${WORKSPACE_DIR}/SOUL.md" "${HERMES_HOME}/SOUL.md"
fi

rm -rf "${HERMES_HOME}/memories"
mkdir -p "${HERMES_HOME}/memories"
for memory_file in MEMORY.md USER.md; do
    if [ -f "${WORKSPACE_DIR}/${memory_file}" ]; then
        log "Linking ${memory_file} from workspace into Hermes home"
        ln -s "${WORKSPACE_DIR}/${memory_file}" "${HERMES_HOME}/memories/${memory_file}"
    fi
done

if [ -x "${WORKSPACE_DIR}/skills/gogcli-tables/bin/gogx" ]; then
    ln -sf "${WORKSPACE_DIR}/skills/gogcli-tables/bin/gogx" /usr/local/bin/gogx
fi

chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "$HERMES_HOME" 2>/dev/null || true

log "Josemar Hermes setup complete"
