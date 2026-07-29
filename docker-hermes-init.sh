#!/command/with-contenv sh
# Josemar compatibility setup for the Hermes Agent Docker image.

set -eu

log() {
    echo "[${JOSEMAR_CONTAINER_PREFIX:-josemar}-hermes] $1"
}

HERMES_HOME="${HERMES_HOME:-/opt/data}"
WORKSPACE_DIR="${WORKSPACE_DIR:-${HERMES_HOME}}"
OBSIDIAN_VAULT_DIR="${OBSIDIAN_VAULT_DIR:-${HERMES_HOME}/obsidian}"
SOURCE_STATE_DIR="${JOSEMAR_SOURCE_STATE_DIR:-/opt/josemar/source-agent-state}"
CREDENTIALS_SOURCE_DIR="${JOSEMAR_CREDENTIALS_SOURCE_DIR:-/opt/josemar/credentials-source}"
CREDENTIALS_DIR="${JOSEMAR_CREDENTIALS_DIR:-${HERMES_HOME}/credentials}"
HERMES_UID_VALUE="${HERMES_UID:-${PUID:-10000}}"
HERMES_GID_VALUE="${HERMES_GID:-${PGID:-10000}}"
HERMES_CLI="${HERMES_CLI:-/opt/hermes/.venv/bin/hermes}"

mkdir -p "$HERMES_HOME" "$WORKSPACE_DIR" "$OBSIDIAN_VAULT_DIR" "$CREDENTIALS_DIR"

# Named volumes writable by the Hermes runtime user. Docker creates these
# as root:root 0755; chown them so uid HERMES_UID can read/write. This list
# is intentionally explicit — bind mounts, read-only volumes, and
# cross-service volumes (obsidian-vault, syncthing-config, etc.) are
# excluded to avoid host-side ownership changes and conflicts with
# other containers that manage their own perms.
HERMES_WRITABLE_VOLUMES="${HERMES_HOME} /shared"

for vol in $HERMES_WRITABLE_VOLUMES; do
    mkdir -p "$vol"
    if [ -w "$vol" ] && [ "$(id -u)" = "0" ]; then
        chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "$vol" 2>/dev/null || true
    fi
done

# Verify that the runtime user can actually write to each writable volume.
# Log a warning (not fatal) so permission issues surface at startup instead
# of failing mid-conversation.
# Resolve UID to username — runuser/su in this image reject numeric UIDs
# even when the user exists in /etc/passwd.
HERMES_USER=$(getent passwd "${HERMES_UID_VALUE}" | cut -d: -f1 || true)
if [ -z "$HERMES_USER" ]; then
    HERMES_USER="hermes"
fi

for vol in $HERMES_WRITABLE_VOLUMES; do
    perm_ok=0
    if command -v runuser >/dev/null 2>&1; then
        if runuser -u "${HERMES_USER}" -- touch "${vol}/.perm-test" 2>/dev/null; then
            perm_ok=1
        fi
    else
        if su -s /bin/sh "${HERMES_USER}" -c "touch \"${vol}/.perm-test\"" 2>/dev/null; then
            perm_ok=1
        fi
    fi
    if [ "$perm_ok" = "1" ]; then
        rm -f "${vol}/.perm-test" 2>/dev/null || true
    else
        log "WARNING: runtime user ${HERMES_USER} (uid ${HERMES_UID_VALUE}) cannot write to ${vol}"
    fi
done

SOURCE_CONFIG="/opt/josemar/hermes/config.yaml"
RUNTIME_CONFIG="${HERMES_HOME}/config.yaml"
JOSEMAR_SKILL_STATE="${JOSEMAR_SKILL_STATE:-/opt/hermes/hermes_cli/josemar_skill_state.py}"

# Before the repo template overwrites the runtime config, extract existing
# default/named profile toggle keys into absent sidecars only when the keys
# exist. This preserves a pre-feature deployment's toggles across the upgrade.
# Does NOT create an empty default.json so production migration can preserve
# pre-feature toggles. Malformed sidecars surface clearly and never modify
# config.
migrate_existing_toggles() {
    if [ ! -f "$JOSEMAR_SKILL_STATE" ]; then
        log "josemar_skill_state helper missing; skipping toggle migration"
        return 0
    fi

    log "Migrating existing skill toggles into sidecars (pre template overwrite)"
    WORKSPACE_DIR="$WORKSPACE_DIR" /opt/hermes/.venv/bin/python3 "$JOSEMAR_SKILL_STATE" migrate \
        --hermes-home "$HERMES_HOME" --config-path "$RUNTIME_CONFIG" \
        || log "WARNING: default profile toggle migration failed; continuing"

    profiles_root="${HERMES_HOME}/profiles"
    if [ -d "$profiles_root" ]; then
        for profile_dir in "$profiles_root"/*/; do
            [ -d "$profile_dir" ] || continue
            profile_config="${profile_dir}config.yaml"
            [ -f "$profile_config" ] || continue
            WORKSPACE_DIR="$WORKSPACE_DIR" /opt/hermes/.venv/bin/python3 "$JOSEMAR_SKILL_STATE" migrate \
                --hermes-home "$profile_dir" --config-path "$profile_config" \
                || log "WARNING: named profile toggle migration failed for ${profile_dir}; continuing"
        done
    fi
}

apply_sidecars_and_policy() {
    if [ ! -f "$JOSEMAR_SKILL_STATE" ]; then
        log "josemar_skill_state helper missing; skipping toggle apply/policy"
        return 0
    fi

    log "Applying skill toggle sidecars and enforcing policy"
    WORKSPACE_DIR="$WORKSPACE_DIR" /opt/hermes/.venv/bin/python3 "$JOSEMAR_SKILL_STATE" apply-all \
        || log "WARNING: skill toggle apply/policy failed; continuing"
}

migrate_existing_toggles

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
    script_dir="${HERMES_HOME}/scripts"
    script_path="${script_dir}/hermes-workspace-sync-cron.sh"
    sync_interval="${WORKSPACE_SYNC_INTERVAL:-60}"

    if [ ! -x "$script_source" ]; then
        return 0
    fi

    mkdir -p "$script_dir"
    cp "$script_source" "$script_path"
    chmod 700 "$script_path"
    chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "${HERMES_HOME}/scripts" "${HERMES_HOME}/cron" 2>/dev/null || true

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
    su -s /bin/sh -- hermes -c '
        HOME=$1
        HERMES_HOME=$1
        WORKSPACE_DIR=$2
        HERMES_CLI=$3
        export HOME HERMES_HOME WORKSPACE_DIR HERMES_CLI
        shift 3
        exec "$HERMES_CLI" cron create "$@"
    ' sh \
        "$HERMES_HOME" \
        "$WORKSPACE_DIR" \
        "$HERMES_CLI" \
        "every ${sync_interval}m" \
        --no-agent \
        --script hermes-workspace-sync-cron.sh \
        --workdir "$WORKSPACE_DIR" \
        --name workspace-sync \
        || log "WARNING: failed to create Hermes workspace-sync cron job"
}

install_gbrain_refresh_cron() {
    script_source="/opt/josemar/scripts/hermes-gbrain-refresh-cron.sh"
    script_dir="${HERMES_HOME}/scripts"
    script_path="${script_dir}/hermes-gbrain-refresh-cron.sh"
    refresh_interval="${GBRAIN_REFRESH_INTERVAL:-5}"

    if [ ! -x "$script_source" ]; then
        return 0
    fi

    mkdir -p "$script_dir"
    cp "$script_source" "$script_path"
    chmod 700 "$script_path"
    chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "${HERMES_HOME}/scripts" "${HERMES_HOME}/cron" 2>/dev/null || true

    case "$refresh_interval" in
        ""|0|*[!0-9]*)
            log "Hermes gbrain-refresh cron disabled (GBRAIN_REFRESH_INTERVAL=${refresh_interval:-unset})"
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
    if job.get("name") == "gbrain-refresh":
        sys.exit(0)

sys.exit(1)
PY
    then
        log "Hermes gbrain-refresh cron job already exists"
        return 0
    fi

    log "Creating Hermes gbrain-refresh cron job"
    su -s /bin/sh -- hermes -c '
        HOME=$1
        HERMES_HOME=$1
        WORKSPACE_DIR=$2
        HERMES_CLI=$3
        export HOME HERMES_HOME WORKSPACE_DIR HERMES_CLI
        shift 3
        exec "$HERMES_CLI" cron create "$@"
    ' sh \
        "$HERMES_HOME" \
        "$WORKSPACE_DIR" \
        "$HERMES_CLI" \
        "every ${refresh_interval}m" \
        --no-agent \
        --script hermes-gbrain-refresh-cron.sh \
        --workdir "$WORKSPACE_DIR" \
        --name gbrain-refresh \
        || log "WARNING: failed to create Hermes gbrain-refresh cron job"
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
        /usr/local/bin/workspace-sync startup
    " || log "WARNING: workspace git sync failed; continuing"
elif [ ! -d "${WORKSPACE_DIR}/.git" ]; then
    seed_workspace_from_manifest
fi

# After the template overwrite and workspace clone/sync/seed, apply the
# canonical sidecars back to default/named configs and enforce the Josemar
# skill policy (creation_nudge_interval=0, write_approval=true,
# curator.enabled=false) while preserving unrelated config keys.
apply_sidecars_and_policy

# The migration/seed/apply steps above can create the dedicated
# ${WORKSPACE_DIR}/hermes/skill-toggles tree as root (e.g. when there is
# no WORKSPACE_STATE_REPO and the tree is seeded from the template, or
# when migration creates a sidecar before the final HERMES_HOME chown).
# The dashboard runtime user must be able to atomically replace the
# root-owned directory/file, so chown ONLY this dedicated toggle tree.
# This does NOT broaden the writable-volume policy or chown bind mounts,
# read-only mounts, or cross-service volumes.
repair_skill_toggle_ownership() {
    toggle_tree="${WORKSPACE_DIR}/hermes/skill-toggles"
    if [ -d "$toggle_tree" ] && [ "$(id -u)" = "0" ]; then
        chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "$toggle_tree" 2>/dev/null || true
    fi
}

repair_skill_toggle_ownership

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
install_gbrain_refresh_cron

# Bridge provider API keys from the container env into gbrain's config file.
# s6-overlay stores container env vars in /run/s6/container_environment/, but
# the Hermes gateway process (spawned via s6) may not inherit them — so gbrain
# subprocesses spawned by Hermes can't find provider keys in process.env.
# This step copies any *_API_KEY from the container env into gbrain's config
# file (/opt/data/.gbrain/config.json) so the generic *_api_key → *_API_KEY
# mapping in buildGatewayConfig (gbrain patch) can reach the gateway.
# Provider-agnostic: works for DeepSeek, Google, Groq, Together, etc.
bridge_gbrain_api_keys() {
    local gbrain_config="${HERMES_HOME}/.gbrain/config.json"
    [ -f "$gbrain_config" ] || return 0

    local env_dir="/run/s6/container_environment"
    [ -d "$env_dir" ] || return 0

    python3 - "$gbrain_config" "$env_dir" <<'PY'
import json, os, sys

config_path, env_dir = sys.argv[1], sys.argv[2]

try:
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
except Exception:
    sys.exit(0)

changed = False
for entry in os.listdir(env_dir):
    if not entry.endswith("_API_KEY"):
        continue
    config_key = entry.lower()
    try:
        with open(os.path.join(env_dir, entry), "r") as f:
            value = f.read().strip()
    except Exception:
        continue
    if not value:
        continue
    if config.get(config_key) != value:
        config[config_key] = value
        changed = True

if changed:
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
PY
}
bridge_gbrain_api_keys

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
