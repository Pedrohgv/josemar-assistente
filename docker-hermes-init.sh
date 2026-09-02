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
#
# The vault-recovery staging volume is a BASE feature (the daily export cron
# is default-enabled), so its path is always allowlisted — unlike the
# Mnemosyne backup staging dir, which is phase-2 opt-in and gated on its
# exact expected path.
#
# Phase 2 opt-in: when the backup overlay marker env
# MNEMOSYNE_BACKUP_STAGING_DIR equals the exact expected path
# /opt/data/mnemosyne-backup/staging, append that path to the allowlist so
# the startup mkdir/chown/write-probe loop handles it before the Hermes
# runtime. Do NOT append arbitrary env paths, bind mounts, uploader state,
# or base-only paths. Base-only startup remains exactly HERMES_HOME + /shared
# + the vault-recovery staging volume.
HERMES_WRITABLE_VOLUMES="${HERMES_HOME} /shared /opt/data/vault-recovery/staging"
if [ "${MNEMOSYNE_BACKUP_STAGING_DIR:-}" = "/opt/data/mnemosyne-backup/staging" ]; then
    HERMES_WRITABLE_VOLUMES="$HERMES_WRITABLE_VOLUMES /opt/data/mnemosyne-backup/staging"
fi

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
# Python used to run the state helper. Overridable so the fail-closed
# migration flow is behaviorally testable; production always uses the
# pinned Hermes venv interpreter.
JOSEMAR_STATE_PYTHON="${JOSEMAR_STATE_PYTHON:-/opt/hermes/.venv/bin/python3}"

# Narrow detection of command-allowlist state: ONLY the dedicated
# allowlist sidecar trees are inspected (never a broad HERMES_HOME scan).
# Presence is file-existence based, so malformed state files that would
# need helper validation also count as present. Any present state
# requires the helper (migration/validation/apply), so helper absence
# with state present must fail closed rather than silently no-op.
command_allowlist_state_present() {
    local allowlist_base="${WORKSPACE_DIR}/hermes/command-allowlist"
    if [ -e "${allowlist_base}/default.json" ]; then
        return 0
    fi
    local allowlist_profiles="${allowlist_base}/profiles"
    if [ -d "$allowlist_profiles" ]; then
        local state_file
        for state_file in "$allowlist_profiles"/*.json; do
            if [ -e "$state_file" ]; then
                return 0
            fi
        done
    fi
    return 1
}

# Before the repo template overwrites the runtime config, extract existing
# default/named profile toggle keys and any non-empty runtime
# command_allowlist into absent sidecars only when those keys exist. This
# preserves a pre-feature deployment's toggles and allowlist across the
# upgrade. Does NOT create empty sidecars so production migration can
# preserve pre-feature state. Fail-closed: a command-allowlist migration
# failure returns nonzero BEFORE the template overwrite, because the
# template copy would otherwise erase the non-empty pre-feature runtime
# allowlist. (Toggle-key migration failures keep the historical
# warn-and-continue behavior inside the helper CLI.)
migrate_existing_toggles() {
    if [ ! -f "$JOSEMAR_SKILL_STATE" ]; then
        if command_allowlist_state_present; then
            log "ERROR: command-allowlist state present but josemar_skill_state helper missing; cannot migrate safely"
            return 1
        fi
        log "josemar_skill_state helper missing; skipping toggle migration"
        return 0
    fi

    log "Migrating existing skill toggles + command allowlist into sidecars (pre template overwrite)"
    # The one-time migration marker is monotonic. When present, legacy
    # runtime import has already been finalized: validate it and skip all
    # runtime allowlist import for every profile (stale runtime values must
    # never resurrect a deliberately deleted sidecar). When absent, run the
    # per-profile migration below. marker-present exits 2 on a malformed/
    # unreadable present marker (fatal). NOTE: the exit status must be
    # captured via "|| marker_rc=$?" — inside "if ! cmd; then", "$?" is
    # always 0 and the rc==2 fatal branch could never fire.
    marker_present=0
    marker_rc=0
    WORKSPACE_DIR="$WORKSPACE_DIR" "$JOSEMAR_STATE_PYTHON" "$JOSEMAR_SKILL_STATE" marker-present >/dev/null 2>&1 || marker_rc=$?
    if [ "$marker_rc" -eq 2 ]; then
        log "ERROR: migration marker is malformed/unreadable; refusing to overwrite runtime config (state history integrity cannot be confirmed)"
        return 1
    fi
    if [ "$marker_rc" -eq 0 ]; then
        marker_present=1
    fi
    # marker_rc == 1 (or any other nonzero) -> marker absent; proceed.

    if [ "$marker_present" -eq 1 ]; then
        log "Migration marker present; skipping legacy runtime command-allowlist import for all profiles"
        return 0
    fi

    if ! WORKSPACE_DIR="$WORKSPACE_DIR" "$JOSEMAR_STATE_PYTHON" "$JOSEMAR_SKILL_STATE" migrate \
            --hermes-home "$HERMES_HOME" --config-path "$RUNTIME_CONFIG"; then
        log "ERROR: default profile migration failed; refusing to overwrite ${RUNTIME_CONFIG} (pre-feature command-allowlist state would be lost)"
        return 1
    fi

    profiles_root="${HERMES_HOME}/profiles"
    if [ -d "$profiles_root" ]; then
        for profile_dir in "$profiles_root"/*/; do
            [ -d "$profile_dir" ] || continue
            profile_config="${profile_dir}config.yaml"
            [ -f "$profile_config" ] || continue
            if ! WORKSPACE_DIR="$WORKSPACE_DIR" "$JOSEMAR_STATE_PYTHON" "$JOSEMAR_SKILL_STATE" migrate \
                    --hermes-home "$profile_dir" --config-path "$profile_config"; then
                log "ERROR: named profile migration failed for ${profile_dir}; refusing to overwrite ${profile_config} (pre-feature command-allowlist state would be lost)"
                return 1
            fi
        done
    fi

    # Finalize the one-time migration-completion marker ONLY after every
    # default/named profile has been safely examined/migrated and BEFORE the
    # template overwrite. A marker write/validation failure is fatal: the
    # pre-feature runtime allowlist must never be erased by the template copy
    # without first finalizing the migration boundary.
    if ! WORKSPACE_DIR="$WORKSPACE_DIR" "$JOSEMAR_STATE_PYTHON" "$JOSEMAR_SKILL_STATE" finalize-migration-marker; then
        log "ERROR: migration marker finalization failed; refusing to overwrite runtime config (pre-feature command-allowlist state would be lost)"
        return 1
    fi
}

apply_sidecars_and_policy() {
    if [ ! -f "$JOSEMAR_SKILL_STATE" ]; then
        log "josemar_skill_state helper missing"
        # Fail-closed: if a models.yaml is present, init must fail nonzero
        # because the overlay cannot be validated/applied. Absence of
        # models.yaml is valid (rollback — no overlay to apply). Do NOT
        # silently boot the template configuration when a present
        # models.yaml cannot be validated/applied.
        if [ -f "${WORKSPACE_DIR}/hermes/models.yaml" ]; then
            log "ERROR: models.yaml present but helper unavailable; cannot validate/apply"
            return 1
        fi
        # Fail-closed: present command-allowlist sidecar state (including
        # malformed files) cannot be validated/reconciled without the
        # helper, so helper absence with state present is an error, not a
        # silent no-op.
        if command_allowlist_state_present; then
            log "ERROR: command-allowlist state present but helper unavailable; cannot validate/reconcile"
            return 1
        fi
        log "No models.yaml and no command-allowlist state present; skipping toggle apply/policy"
        return 0
    fi

    log "Applying skill toggle sidecars, models.yaml overlay, and enforcing policy"
    # Fail-closed: a models.yaml validation failure (or any apply error) must
    # NOT silently boot the template configuration. The helper validates the
    # full models.yaml before mutating config (last-known-good preserved on
    # failure). Propagate the nonzero exit so container startup surfaces the
    # error instead of silently continuing with template defaults.
    WORKSPACE_DIR="$WORKSPACE_DIR" \
    JOSEMAR_TEMPLATE_CONFIG="$SOURCE_CONFIG" \
    "$JOSEMAR_STATE_PYTHON" "$JOSEMAR_SKILL_STATE" apply-all
}

migrate_existing_toggles || exit 1

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

remove_gbrain_refresh_cron_job() {
    su -s /bin/sh -- hermes -c '
        HOME=$1
        HERMES_HOME=$1
        HERMES_CLI=$2
        export HOME HERMES_HOME HERMES_CLI
        shift 2
        exec "$HERMES_CLI" cron remove "$@"
    ' sh "$HERMES_HOME" "$HERMES_CLI" gbrain-refresh
}

install_gbrain_refresh_cron() {
    script_source="/opt/josemar/scripts/hermes-gbrain-refresh-cron.sh"
    script_dir="${HERMES_HOME}/scripts"
    script_path="${script_dir}/hermes-gbrain-refresh-cron.sh"
    refresh_interval="${GBRAIN_REFRESH_INTERVAL:-5}"
    jobs_file="${HERMES_HOME}/cron/jobs.json"

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
            remove_gbrain_refresh_cron_job
            return 0
            ;;
    esac

    # Reconcile by full expected state, not merely by name: the interval
    # schedule (kind=interval, minutes), script name, no_agent flag, and
    # workdir must all match. Hermes persists `every Nm` as
    # {"kind":"interval","minutes":N,"display":"every Nm"}.
    if python3 - "$jobs_file" "$refresh_interval" "$WORKSPACE_DIR" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f: data=json.load(f)
    minutes=int(sys.argv[2]); workdir=sys.argv[3]
    found=False
    for j in data.get("jobs",[]):
        if not isinstance(j,dict) or j.get("name") != "gbrain-refresh": continue
        s=j.get("schedule")
        actual = s.get("minutes") if isinstance(s,dict) else None
        found = (isinstance(s,dict) and s.get("kind") == "interval"
                 and isinstance(actual, int) and not isinstance(actual, bool)
                 and actual == minutes
                 and j.get("script") == "hermes-gbrain-refresh-cron.sh"
                 and j.get("no_agent") is True and j.get("workdir") == workdir)
        break
except Exception: found=False
sys.exit(0 if found else 1)
PY
    then
        log "Hermes gbrain-refresh cron job already exists"
        return 0
    fi

    # A same-name job with drift must be removed before recreation; otherwise
    # Hermes would retain the stale schedule/script/workdir.
    log "Reconciling Hermes gbrain-refresh cron job drift"
    remove_gbrain_refresh_cron_job

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

install_gbrain_embedding_refresh_cron() {
    # The schedule is evaluated by the Hermes scheduler in the container's
    # local timezone (TZ/HERMES_TIMEZONE, America/Sao_Paulo by default), NOT
    # UTC. `0 5 * * *` therefore means 05:00 local, and the string is passed
    # through verbatim to `hermes cron create` with no UTC conversion.
    script_source="/opt/josemar/scripts/hermes-gbrain-embedding-refresh-cron.sh"
    script_dir="${HERMES_HOME}/scripts"
    script_path="${script_dir}/hermes-gbrain-embedding-refresh-cron.sh"
    schedule="${GBRAIN_EMBED_REFRESH_SCHEDULE:-0 5 * * *}"
    jobs_file="${HERMES_HOME}/cron/jobs.json"

    # Disabled or malformed schedules remove the owned job (like the other
    # owned-job installers) instead of leaving a stale daily job behind.
    case "$schedule" in
        ""|0)
            log "Hermes gbrain-embedding-refresh cron disabled"
            remove_gbrain_embedding_refresh_cron_job
            return 0
            ;;
    esac

    [ -x "$script_source" ] || return 0
    # Hermes accepts a five-field cron expression here.  Reject malformed
    # values rather than passing shell-looking input to the CLI.
    if ! python3 - "$schedule" <<'PY'
import re, sys
s=sys.argv[1]
sys.exit(0 if len(s.split()) == 5 and all(re.fullmatch(r'[0-9*/?,\-]+', x) for x in s.split()) else 1)
PY
    then
        log "WARNING: invalid GBRAIN_EMBED_REFRESH_SCHEDULE; embedding refresh cron disabled"
        remove_gbrain_embedding_refresh_cron_job
        return 0
    fi

    mkdir -p "$script_dir"
    cp "$script_source" "$script_path"
    chmod 700 "$script_path"
    chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "${HERMES_HOME}/scripts" "${HERMES_HOME}/cron" 2>/dev/null || true

    # Reconcile by full expected state, not merely by name: the schedule
    # expression, script name, no_agent flag, and workdir must all match.
    # Hermes persists cron schedules as {"kind":"cron","expr":"0 5 * * *"}.
    if python3 - "$jobs_file" "$schedule" "$WORKSPACE_DIR" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f: data=json.load(f)
    wanted=sys.argv[2]
    workdir=sys.argv[3]
    found=False
    for j in data.get("jobs",[]):
        if not isinstance(j,dict) or j.get("name") != "gbrain-embedding-refresh": continue
        s=j.get("schedule")
        actual = s.get("expr") if isinstance(s,dict) else None
        found = (isinstance(s,dict) and s.get("kind") == "cron" and actual == wanted
                 and j.get("script") == "hermes-gbrain-embedding-refresh-cron.sh"
                 and j.get("no_agent") is True and j.get("workdir") == workdir)
        break
except Exception: found=False
sys.exit(0 if found else 1)
PY
    then
        log "Hermes gbrain-embedding-refresh cron job already exists"
        return 0
    fi

    # A same-name job with drift must be removed before recreation; otherwise
    # Hermes would retain the stale schedule/script/workdir.
    log "Reconciling Hermes gbrain-embedding-refresh cron job drift"
    remove_gbrain_embedding_refresh_cron_job

    log "Creating Hermes gbrain-embedding-refresh cron job"
    su -s /bin/sh -- "$HERMES_USER" -c '
        HOME=$1; HERMES_HOME=$1; WORKSPACE_DIR=$2; HERMES_CLI=$3
        export HOME HERMES_HOME WORKSPACE_DIR HERMES_CLI
        shift 3
        exec "$HERMES_CLI" cron create "$@"
    ' sh "$HERMES_HOME" "$WORKSPACE_DIR" "$HERMES_CLI" "$schedule" \
        --no-agent --script hermes-gbrain-embedding-refresh-cron.sh \
        --workdir "$WORKSPACE_DIR" --name gbrain-embedding-refresh \
        || log "WARNING: failed to create Hermes gbrain-embedding-refresh cron job"
}

# Remove the owned gbrain-embedding-refresh cron job when present. Safe to
# call when jobs.json is missing or malformed; failure is non-fatal.
remove_gbrain_embedding_refresh_cron_job() {
    [ -f "$jobs_file" ] || return 0
    if python3 - "$jobs_file" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f: data=json.load(f)
    found=any(isinstance(j,dict) and j.get("name")=="gbrain-embedding-refresh" for j in data.get("jobs",[]))
except Exception: found=False
sys.exit(0 if found else 1)
PY
    then
        su -s /bin/sh -- "$HERMES_USER" -c 'HOME="$1"; HERMES_HOME="$1"; export HOME HERMES_HOME; exec "$2" cron remove gbrain-embedding-refresh' sh "$HERMES_HOME" "$HERMES_CLI" || true
    fi
}

# Vault-recovery export cron (default-enabled). Installs a no-agent
# cron job that runs the vault-recovery export script on a daily schedule.
# The script sources /opt/josemar/scripts/hermes-vault-recovery-export-cron.sh
# and runs without an agent (no memory capture/LLM invocation). It creates
# ONLY local staged immutable generations on the vault-recovery staging
# volume; the encrypted remote uploader (default deployment lane) ships in
# its own container and mounts that staging volume read-only.
#
# Gating: VAULT_RECOVERY_EXPORT_ENABLED (default true) — false/0/empty
# removes the owned job with a clear log. The schedule is a five-field cron
# expression (VAULT_RECOVERY_EXPORT_SCHEDULE, default "0 4 * * *" = 04:00
# local container time); malformed schedules are rejected and remove the job
# rather than passing shell-looking input to the CLI. Failure is non-fatal to
# gateway startup. Idempotent by stable name; drift is reconciled safely.
install_vault_recovery_export_cron() {
    script_source="/opt/josemar/scripts/hermes-vault-recovery-export-cron.sh"
    script_dir="${HERMES_HOME}/scripts"
    script_path="${script_dir}/hermes-vault-recovery-export-cron.sh"
    schedule="${VAULT_RECOVERY_EXPORT_SCHEDULE:-0 4 * * *}"
    enabled="${VAULT_RECOVERY_EXPORT_ENABLED:-true}"
    jobs_file="${HERMES_HOME}/cron/jobs.json"

    case "$enabled" in
        true|1|yes)
            ;;
        *)
            log "Hermes vault-recovery-export cron disabled (VAULT_RECOVERY_EXPORT_ENABLED=${enabled})"
            remove_vault_recovery_export_cron_job
            return 0
            ;;
    esac

    # Hermes accepts a five-field cron expression here. Reject malformed
    # values rather than passing shell-looking input to the CLI.
    if ! python3 - "$schedule" <<'PY'
import re, sys
s=sys.argv[1]
sys.exit(0 if len(s.split()) == 5 and all(re.fullmatch(r'[0-9*/?,\-]+', x) for x in s.split()) else 1)
PY
    then
        log "WARNING: invalid VAULT_RECOVERY_EXPORT_SCHEDULE; vault-recovery-export cron disabled"
        remove_vault_recovery_export_cron_job
        return 0
    fi

    if [ ! -x "$script_source" ]; then
        log "Vault-recovery-export cron disabled (source script missing)"
        remove_vault_recovery_export_cron_job
        return 0
    fi

    mkdir -p "$script_dir"
    cp "$script_source" "$script_path"
    chmod 700 "$script_path"
    chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "${HERMES_HOME}/scripts" "${HERMES_HOME}/cron" 2>/dev/null || true

    # Reconcile by full expected state, not merely by name: the schedule
    # expression, script name, no_agent flag, and workdir must all match.
    # Hermes persists cron schedules as {"kind":"cron","expr":"0 4 * * *"}.
    if python3 - "$jobs_file" "$schedule" "$HERMES_HOME" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f: data=json.load(f)
    wanted=sys.argv[2]
    workdir=sys.argv[3]
    found=False
    for j in data.get("jobs",[]):
        if not isinstance(j,dict) or j.get("name") != "vault-recovery-export": continue
        s=j.get("schedule")
        actual = s.get("expr") if isinstance(s,dict) else None
        found = (isinstance(s,dict) and s.get("kind") == "cron" and actual == wanted
                 and j.get("script") == "hermes-vault-recovery-export-cron.sh"
                 and j.get("no_agent") is True and j.get("workdir") == workdir)
        break
except Exception: found=False
sys.exit(0 if found else 1)
PY
    then
        log "Hermes vault-recovery-export cron job already exists"
        return 0
    fi

    # A same-name job with drift must be removed before recreation; otherwise
    # Hermes would retain the stale schedule/script/workdir.
    log "Reconciling Hermes vault-recovery-export cron job drift"
    remove_vault_recovery_export_cron_job

    log "Creating Hermes vault-recovery-export cron job"
    su -s /bin/sh -- "$HERMES_USER" -c '
        HOME=$1; HERMES_HOME=$1; WORKSPACE_DIR=$2; HERMES_CLI=$3
        export HOME HERMES_HOME WORKSPACE_DIR HERMES_CLI
        shift 3
        exec "$HERMES_CLI" cron create "$@"
    ' sh "$HERMES_HOME" "$WORKSPACE_DIR" "$HERMES_CLI" "$schedule" \
        --no-agent --script hermes-vault-recovery-export-cron.sh \
        --workdir "$HERMES_HOME" --name vault-recovery-export \
        || log "WARNING: failed to create Hermes vault-recovery-export cron job"
}

# Remove the owned vault-recovery-export cron job when present. Safe to call
# when jobs.json is missing or malformed; failure is non-fatal.
remove_vault_recovery_export_cron_job() {
    [ -f "$jobs_file" ] || return 0
    if python3 - "$jobs_file" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f: data=json.load(f)
    found=any(isinstance(j,dict) and j.get("name")=="vault-recovery-export" for j in data.get("jobs",[]))
except Exception: found=False
sys.exit(0 if found else 1)
PY
    then
        su -s /bin/sh -- "$HERMES_USER" -c 'HOME="$1"; HERMES_HOME="$1"; export HOME HERMES_HOME; exec "$2" cron remove vault-recovery-export' sh "$HERMES_HOME" "$HERMES_CLI" || true
    fi
}

# Mnemosyne backup export cron (Phase 2, opt-in). Installs a no-agent cron
# job that runs the backup export script on a minute-based schedule. The
# script sources /opt/josemar/scripts/mnemosyne-backup-export.sh and runs
# without an agent (no memory capture/LLM invocation).
#
# Install only when ALL hold:
#   1. MNEMOSYNE_PROVIDER=mnemosyne
#   2. MNEMOSYNE_BACKUP_STAGING_DIR equals the exact expected path
#      /opt/data/mnemosyne-backup/staging
#   3. MNEMOSYNE_BACKUP_EXPORT_INTERVAL is a positive integer (minutes)
# 0/unset/malformed disables with a clear log. Failure is non-fatal to
# gateway startup. Idempotent by stable name; drift is reconciled safely.
install_mnemosyne_backup_export_cron() {
    # The installed copy is chmod 700 and chowned to HERMES_UID_VALUE; these
    # are part of the owned-job contract, not merely cosmetic permissions.
    local script_source="${MNEMOSYNE_BACKUP_EXPORT_SCRIPT_SOURCE:-/opt/josemar/scripts/mnemosyne-backup-export.sh}"
    local script_dir="${HERMES_HOME}/scripts"
    local script_path="${script_dir}/mnemosyne-backup-export.sh"
    local interval="${MNEMOSYNE_BACKUP_EXPORT_INTERVAL:-0}"
    local jobs_file="${HERMES_HOME}/cron/jobs.json"

    # Return 0 when the named job exists and is correct, 2 when it drifts
    # (reconciliation required),
    # 1 when absent, and 3 when jobs.json cannot be trusted. Arguments must
    # precede the heredoc: otherwise the arguments become a separate command.
    mnemosyne_backup_job_state() {
        python3 - "$jobs_file" "$interval" "$HERMES_HOME" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    jobs = data["jobs"]
    if not isinstance(jobs, list):
        raise ValueError("jobs is not a list")
except Exception:
    sys.exit(3)
for job in jobs:
    if isinstance(job, dict) and job.get("name") == "mnemosyne-backup-export":
        schedule = job.get("schedule")
        # The pinned Hermes CLI persists interval schedules as an object, not
        # as the display string accepted by `cron create`:
        # {"kind": "interval", "minutes": 1, "display": "every 1m"}.
        # Compare the authoritative schema fields and deliberately reject
        # unknown/string schedule shapes rather than recreating the owned job.
        schedule_ok = (
            isinstance(schedule, dict)
            and schedule.get("kind") == "interval"
            and isinstance(schedule.get("minutes"), int)
            and not isinstance(schedule.get("minutes"), bool)
            and schedule.get("minutes") == int(sys.argv[2])
        )
        actual = (schedule_ok, job.get("script"), job.get("no_agent"),
                  job.get("workdir"))
        expected = (True, "mnemosyne-backup-export.sh", True, sys.argv[3])
        sys.exit(0 if actual == expected else 2)
sys.exit(1)
PY
    }

    remove_mnemosyne_backup_export_cron() {
        [ -f "$jobs_file" ] || return 0
        # Never guess on malformed state: this keeps unrelated jobs safe and
        # avoids creating a duplicate or exporting against unknown state.
        if python3 - "$jobs_file" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    jobs = data["jobs"]
    if not isinstance(jobs, list):
        raise ValueError()
except Exception:
    sys.exit(2)
sys.exit(0 if any(isinstance(j, dict) and j.get("name") == "mnemosyne-backup-export" for j in jobs) else 1)
PY
        then
            remove_owned=1
        else
            rc=$?
            remove_owned=0
            # The name is the ownership boundary. Even when the inventory is
            # malformed, ask Hermes to remove only this exact name; failure
            # is non-fatal and is surfaced below.
            [ "$rc" -eq 2 ] && log "WARNING: malformed Hermes jobs.json; attempting named backup-job removal"
        fi
        if [ "$remove_owned" -eq 1 ] || [ "$rc" -eq 2 ]; then
            # The installed CLI's removal subcommand is `cron remove` (it
            # accepts the stable job name as a positional id-or-name). The
            # separate `cron delete --name` form does not exist in the pinned
            # Hermes CLI, so the named removal would always fail and leave the
            # owned job behind.
            su -s /bin/sh -- "${HERMES_USER:-hermes}" -c '
                HOME=$1; HERMES_HOME=$1; HERMES_CLI=$2
                export HOME HERMES_HOME HERMES_CLI
                shift 2
                exec "$HERMES_CLI" cron remove "$1"
            ' sh "$HERMES_HOME" "$HERMES_CLI" mnemosyne-backup-export \
                || log "WARNING: failed to remove owned mnemosyne-backup-export cron job"
        elif [ "$rc" -eq 1 ]; then
            :
        fi
    }

    # Every false gate removes only this owned job. A successful create uses
    # exactly: every ${interval}m --no-agent --script mnemosyne-backup-export.sh
    # --workdir "$HERMES_HOME" --name mnemosyne-backup-export.
    if [ "${MNEMOSYNE_PROVIDER:-}" != mnemosyne ]; then
        remove_mnemosyne_backup_export_cron
        return 0
    fi
    if [ "${MNEMOSYNE_BACKUP_STAGING_DIR:-}" != "/opt/data/mnemosyne-backup/staging" ]; then
        log "Mnemosyne backup-export cron disabled (staging dir not exact expected path)"
        remove_mnemosyne_backup_export_cron
        return 0
    fi
    case "$interval" in
        ""|0|*[!0-9]*)
            log "Mnemosyne backup-export cron disabled (MNEMOSYNE_BACKUP_EXPORT_INTERVAL=${interval:-unset})"
            remove_mnemosyne_backup_export_cron
            return 0
            ;;
    esac
    if [ ! -x "$script_source" ]; then
        log "Mnemosyne backup-export cron disabled (source script missing)"
        remove_mnemosyne_backup_export_cron
        return 0
    fi

    mkdir -p "$script_dir"
    cp "$script_source" "$script_path"
    chmod 700 "$script_path"
    chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "${HERMES_HOME}/scripts" "${HERMES_HOME}/cron" 2>/dev/null || true

    if mnemosyne_backup_job_state; then
        log "Mnemosyne backup-export cron job already exists"
        return 0
    else
        rc=$?
    fi
    if [ "$rc" -eq 3 ]; then
        log "WARNING: malformed Hermes jobs.json; skipping backup-export cron reconciliation"
        return 0
    elif [ "$rc" -eq 2 ]; then
        log "WARNING: reconciling drifted mnemosyne-backup-export cron job"
        remove_mnemosyne_backup_export_cron
    fi

    log "Creating Mnemosyne backup-export cron job"
    su -s /bin/sh -- "${HERMES_USER:-hermes}" -c '
        HOME=$1
        HERMES_HOME=$1
        HERMES_CLI=$2
        export HOME HERMES_HOME HERMES_CLI
        shift 2
        exec "$HERMES_CLI" cron create "$@"
    ' sh \
        "$HERMES_HOME" \
        "$HERMES_CLI" \
        "every ${interval}m" \
        --no-agent \
        --script mnemosyne-backup-export.sh \
        --workdir "$HERMES_HOME" \
        --name mnemosyne-backup-export \
        || log "WARNING: failed to create Mnemosyne backup-export cron job"
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
# curator.enabled=false) while preserving unrelated config keys. The same
# apply-all step also layers the state-owned model authoring overlay
# (agent-state/hermes/models.yaml) onto the default runtime config AFTER
# the template copy + workspace sync, so the operator's git-backed model
# selection wins over template defaults. models.yaml is validated fully
# before mutating config (fail-closed); a malformed file leaves the
# config untouched (last-known-good preserved) and the helper returns
# nonzero so init does NOT silently boot the template configuration after
# invalid state. When models.yaml is absent/empty, state-owned keys are
# restored to the repo template defaults (rollback) using the template
# config passed via JOSEMAR_TEMPLATE_CONFIG. Application happens only
# through the shared advisory lock so dashboard writes and sync never
# race; no unrelated-field loss and no changes occur from validation
# failures.
apply_sidecars_and_policy

# The migration/seed/apply steps above can create the dedicated
# ${WORKSPACE_DIR}/hermes/skill-toggles and
# ${WORKSPACE_DIR}/hermes/command-allowlist trees as root (e.g. when there
# is no WORKSPACE_STATE_REPO and the tree is seeded from the template, or
# when migration creates a sidecar before the final HERMES_HOME chown).
# The dashboard runtime user must be able to atomically replace the
# root-owned directory/file, so chown ONLY these dedicated state trees.
# This does NOT broaden the writable-volume policy or chown bind mounts,
# read-only mounts, or cross-service volumes.
repair_skill_toggle_ownership() {
    for toggle_tree in \
        "${WORKSPACE_DIR}/hermes/skill-toggles" \
        "${WORKSPACE_DIR}/hermes/command-allowlist"; do
        if [ -d "$toggle_tree" ] && [ "$(id -u)" = "0" ]; then
            chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "$toggle_tree" 2>/dev/null || true
        fi
    done
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
install_gbrain_embedding_refresh_cron
install_vault_recovery_export_cron || log "WARNING: vault-recovery-export cron setup failed; continuing"

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

# ---- Mnemosyne pilot activation/rollback (Phase 1, opt-in) ----
#
# Runtime config activation is GATED on MNEMOSYNE_PROVIDER=mnemosyne so the
# base image and base-only deploys are unaffected. This runs AFTER the source
# config template is copied to the runtime config and AFTER
# apply_sidecars_and_policy so the skill policy is enforced first. It runs as
# the Hermes user (not root) because the runtime config and data directory are
# owned by HERMES_UID.
#
# POLICY (Phase 1 pivot): upstream-native Mnemosyne, NOT curated-static
# coexistence. When active, the init sets:
#   memory.provider=mnemosyne
#   memory.memory_enabled=false        (disable static MEMORY.md/USER.md injection)
#   memory.user_profile_enabled=false (disable static USER.md injection)
#   memory.write_approval=true         (retained as archive protection; NOT touched)
#   memory.mnemosyne.default_scope=global
#   memory.mnemosyne.profile_isolation=false
#   memory.mnemosyne.auto_sleep=false  (validated key: provider reads "auto_sleep")
#   memory.mnemosyne.reflect_disabled_for_cron=true
#   memory.mnemosyne.reflect_max_calls_per_session=0
#   memory.mnemosyne.sync_roles=[user]
#   memory.mnemosyne.skip_contexts=[cron,flush,subagent,background,skill_loop]
#   memory.mnemosyne.sync_turn_user_limit=500
#   memory.mnemosyne.sync_turn_assistant_limit=800
#   (tools key is OMITTED so the provider exposes all upstream-native tools,
#    including mutating operations. This is upstream-native behavior.)
# MEMORY.md/USER.md remain at their versioned paths as archive/rollback
# material; they are NOT injected while the pilot is active. memory.write_approval
# stays true as archive protection for built-in archive writes but does not
# prevent passive capture or necessarily all Mnemosyne tools.
#
# PASSIVE-ONLY INGESTION: automatic ingestion is passive raw user-turn capture
# (sync_turn). Explicit upstream-native mutation/management tools (including
# mutating operations) remain available to the agent. No auto-sleep,
# reflection, or LLM consolidation runs in the pilot.
#
# ROLLBACK: when MNEMOSYNE_PROVIDER is absent/not mnemosyne on a subsequent
# base-only init, the rollback cleanup resets provider/static flags to base
# template values (provider blank, memory_enabled/user_profile_enabled true)
# and removes ONLY installer-owned plugin and override-skill artifacts, while
# preserving /opt/data/mnemosyne/data DB/WAL/SHM. It uses the upstream
# `mnemosyne-hermes cleanup` CLI (safe, never touches database) plus narrow
# managed-skill cleanup (validates the .sha256 sidecar content hash before
# deletion); no blanket rm -rf of generic skills.
#
# FAIL-CLOSED: a wrapper install or config-activation failure does NOT then
# activate memory.provider=mnemosyne. The helper calls the shared
# cleanup_mnemosyne_artifacts helper to remove installer-owned plugin/skill
# artifacts safely (preserving DB and user-modified/unverified skill content),
# then returns nonzero so the pilot does not silently half-activate.

# Shared artifact cleanup: removes installer-owned plugin (via upstream cleanup
# CLI) and the managed override skill (sha256-content-verified, narrow file
# removal + rmdir). NOT gated by MNEMOSYNE_PROVIDER so it can be called from
# both activation-failure paths (when MNEMOSYNE_PROVIDER=mnemosyne) and
# rollback (when it is absent). Does NOT early-return on absent plugin dir
# because a managed override skill may remain from a partial install. Never
# touches the DB at /opt/data/mnemosyne/data.
cleanup_mnemosyne_artifacts() {
    local plugin_dir="${HERMES_HOME}/plugins/mnemosyne"
    local override_skill_dir="${HERMES_HOME}/skills/memory/mnemosyne-memory-override"

    # Only log if there is something to clean.
    if [ ! -d "$plugin_dir" ] && [ ! -d "$override_skill_dir" ]; then
        return 0
    fi

    log "Cleaning Mnemosyne installer-owned artifacts (DB preserved)"

    # Use the upstream cleanup CLI (safe, never touches database) if the
    # plugin dir exists.
    if [ -d "$plugin_dir" ]; then
        su -s /bin/sh -- "$HERMES_USER" -c '
                HOME="'"$HERMES_HOME"'"
                HERMES_HOME="'"$HERMES_HOME"'"
                export HOME HERMES_HOME
                /opt/hermes/.venv/bin/mnemosyne-hermes \
                    --hermes-home "'"$HERMES_HOME"'" \
                    cleanup
            ' 2>/dev/null || log "WARNING: mnemosyne cleanup CLI failed; continuing with narrow removal"
    fi

    # Narrow managed-skill cleanup: remove ONLY the installer-owned SKILL.md
    # and its .sha256 sidecar, then rmdir the directory only if empty. The
    # .sha256 sidecar is a bare SHA256 hash of the SKILL.md content; verify
    # the hash matches before removing, so a user-modified skill is preserved.
    # No blanket rm -rf of the directory (which would delete user-added files).
    if [ -d "$override_skill_dir" ] && [ -f "$override_skill_dir/SKILL.md.sha256" ] && [ -f "$override_skill_dir/SKILL.md" ]; then
        expected_hash=$(cat "$override_skill_dir/SKILL.md.sha256" | tr -d ' \n')
        actual_hash=$(sha256sum "$override_skill_dir/SKILL.md" | cut -d' ' -f1)
        if [ "$expected_hash" = "$actual_hash" ]; then
            rm -f "$override_skill_dir/SKILL.md" "$override_skill_dir/SKILL.md.sha256"
            log "Removed installer-owned mnemosyne-memory-override SKILL.md + sidecar (sha256-verified)"
            # rmdir only if empty (preserves any user-added files).
            rmdir "$override_skill_dir" 2>/dev/null || true
            # Try to rmdir the parent memory/ dir if also empty.
            rmdir "${HERMES_HOME}/skills/memory" 2>/dev/null || true
        else
            log "WARNING: mnemosyne-memory-override SKILL.md hash mismatch (user-modified); preserving"
        fi
    fi
}

activate_mnemosyne() {
    case "${MNEMOSYNE_PROVIDER:-}" in
        mnemosyne) ;;
        *) return 0 ;;
    esac

    log "Activating Mnemosyne pilot (MNEMOSYNE_PROVIDER=mnemosyne)"

    # Dedicated data directory inside /opt/data (HERMES_HOME). No
    # writable-volume allowlist change: it lives under the already-writable
    # HERMES_HOME tree and is chowned with the final HERMES_HOME chown below.
    local mnemo_data_dir="${MNEMOSYNE_DATA_DIR:-/opt/data/mnemosyne/data}"
    mkdir -p "$mnemo_data_dir"

    # Run the verified console installer idempotently against the Hermes
    # venv/runtime home as the Hermes user. The verified supported CLI is the
    # console entry point `mnemosyne-hermes --hermes-home <home> install
    # --mode wrapper --force --python /opt/hermes/.venv/bin/python3`. The
    # Python module form does NOT work (the package lacks a __main__ entry
    # point), and --data-dir is NOT an installer flag. --force makes the
    # install idempotent across restarts (it replaces an existing plugin
    # directory); without it a second install fails because the plugin dir
    # already exists.
    if ! su -s /bin/sh -- "$HERMES_USER" -c '
            HOME="'"$HERMES_HOME"'"
            HERMES_HOME="'"$HERMES_HOME"'"
            MNEMOSYNE_DATA_DIR="'"$mnemo_data_dir"'"
            export HOME HERMES_HOME MNEMOSYNE_DATA_DIR
            /opt/hermes/.venv/bin/mnemosyne-hermes \
                --hermes-home "'"$HERMES_HOME"'" \
                install --mode wrapper --force \
                --python /opt/hermes/.venv/bin/python3
        '; then
        log "WARNING: mnemosyne wrapper installer failed; cleaning artifacts and leaving memory.provider blank"
        cleanup_mnemosyne_artifacts
        return 1
    fi

    # Set the full nested runtime config through the supported Hermes config
    # interface (hermes_cli.config load_config/save_config), NOT by writing
    # manual YAML. This preserves all unrelated config keys and uses the same
    # interface the skill-state helper uses. This only runs after a successful
    # wrapper install (fail-closed above).
    #
    # profile_isolation has NO env var mapping, so it MUST be set here in the
    # nested config. The env-mapped keys (default_scope, sync_roles, etc.) are
    # also set here so the runtime config is the single authoritative source
    # and survives env var changes. The `tools` key is intentionally OMITTED
    # so the provider exposes all upstream-native tools (including mutating
    # operations) — this is upstream-native behavior.
    if ! su -s /bin/sh -- "$HERMES_USER" -c '
            HOME="'"$HERMES_HOME"'"
            HERMES_HOME="'"$HERMES_HOME"'"
            export HOME HERMES_HOME
            /opt/hermes/.venv/bin/python3 - <<'"'"'PY'"'"'
import sys
try:
    from hermes_cli.config import load_config, save_config
except Exception as exc:
    print(f"[mnemosyne] could not import hermes_cli.config: {exc}", file=sys.stderr)
    sys.exit(1)
config = load_config()
if config is None:
    config = {}
memory = config.setdefault("memory", {})
# Upstream-native: disable static injection, activate provider.
memory["provider"] = "mnemosyne"
memory["memory_enabled"] = False
memory["user_profile_enabled"] = False
# write_approval is NOT touched here; it stays true from the template as
# archive protection for built-in archive writes. It does not prevent
# passive capture or necessarily all Mnemosyne tools.
# Nested Mnemosyne config (authoritative source for passive settings).
mnemo = memory.setdefault("mnemosyne", {})
mnemo["default_scope"] = "global"
mnemo["profile_isolation"] = False
# Validated key: the provider reads _read_config_key("auto_sleep"), not
# "auto_sleep_enabled". The env var MNEMOSYNE_AUTO_SLEEP_ENABLED=false
# remains a valid runtime mirror.
mnemo["auto_sleep"] = False
mnemo["reflect_disabled_for_cron"] = True
mnemo["reflect_max_calls_per_session"] = 0
mnemo["sync_roles"] = ["user"]
mnemo["skip_contexts"] = ["cron", "flush", "subagent", "background", "skill_loop"]
mnemo["sync_turn_user_limit"] = 500
mnemo["sync_turn_assistant_limit"] = 800
# tools key is intentionally OMITTED: upstream-default full tool exposure
# (including mutating operations). This is upstream-native behavior.
try:
    save_config(config)
except Exception as exc:
    print(f"[mnemosyne] failed to save config: {exc}", file=sys.stderr)
    sys.exit(1)
PY
        '; then
        log "WARNING: mnemosyne runtime config activation failed; cleaning artifacts and leaving memory.provider blank"
        cleanup_mnemosyne_artifacts
        return 1
    fi
}

# Rollback cleanup: when MNEMOSYNE_PROVIDER is absent/not mnemosyne, reset
# provider/static flags to base template values and remove installer-owned
# plugin/skill artifacts while preserving the DB. The base template copy
# above already restored memory_enabled/user_profile_enabled to true and
# cleared provider, so this only needs to remove stale plugin/skill
# behavioral artifacts and ensure the nested config does not linger.
rollback_mnemosyne() {
    case "${MNEMOSYNE_PROVIDER:-}" in
        mnemosyne) return 0 ;;
    esac

    # Delegate artifact removal to the shared cleanup helper. It does NOT
    # early-return on absent plugin dir (a managed skill may remain).
    cleanup_mnemosyne_artifacts

    # The runtime config was already reset by the base template copy above
    # (provider blank, memory_enabled/user_profile_enabled true). Ensure the
    # nested mnemosyne block does not linger as a stale provider config.
    su -s /bin/sh -- "$HERMES_USER" -c '
            HOME="'"$HERMES_HOME"'"
            HERMES_HOME="'"$HERMES_HOME"'"
            export HOME HERMES_HOME
            /opt/hermes/.venv/bin/python3 - <<'"'"'PY'"'"'
import sys
try:
    from hermes_cli.config import load_config, save_config
except Exception:
    sys.exit(0)
config = load_config()
if config is None:
    sys.exit(0)
memory = config.get("memory")
if not isinstance(memory, dict):
    sys.exit(0)
changed = False
# Reset provider/static flags to base template values.
if memory.get("provider"):
    memory["provider"] = ""
    changed = True
if memory.get("memory_enabled") is False:
    memory["memory_enabled"] = True
    changed = True
if memory.get("user_profile_enabled") is False:
    memory["user_profile_enabled"] = True
    changed = True
# Remove stale nested mnemosyne block so no provider config lingers.
if "mnemosyne" in memory:
    del memory["mnemosyne"]
    changed = True
if changed:
    try:
        save_config(config)
    except Exception as exc:
        print(f"[mnemosyne] failed to save rollback config: {exc}", file=sys.stderr)
PY
    ' 2>/dev/null || log "WARNING: mnemosyne rollback config reset failed; base template already restored flags"

    # The DB at /opt/data/mnemosyne/data is intentionally preserved for
    # future re-activation. Do NOT remove it.
    log "Mnemosyne rollback complete (DB preserved at ${MNEMOSYNE_DATA_DIR:-/opt/data/mnemosyne/data})"
}

if [ "${MNEMOSYNE_PROVIDER:-}" = "mnemosyne" ]; then
    # Do not reconcile the export job until both wrapper installation and
    # runtime provider configuration have succeeded.
    if activate_mnemosyne; then
        install_mnemosyne_backup_export_cron || log "WARNING: Mnemosyne backup-export cron setup failed; continuing"
    else
        log "WARNING: Mnemosyne pilot activation did not complete; removing owned backup-export cron"
        MNEMOSYNE_PROVIDER= install_mnemosyne_backup_export_cron || log "WARNING: Mnemosyne backup-export cron cleanup failed; continuing"
    fi
else
    rollback_mnemosyne
    # All provider-off gates must remove only the owned export job.
    install_mnemosyne_backup_export_cron || log "WARNING: Mnemosyne backup-export cron cleanup failed; continuing"
fi

if [ -x "${WORKSPACE_DIR}/skills/gogcli-tables/bin/gogx" ]; then
    ln -sf "${WORKSPACE_DIR}/skills/gogcli-tables/bin/gogx" /usr/local/bin/gogx
fi

chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "$HERMES_HOME" 2>/dev/null || true

log "Josemar Hermes setup complete"
