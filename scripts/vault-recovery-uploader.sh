#!/bin/sh
# vault-recovery-uploader.sh - Encrypted rclone uploader for the
# vault-recovery lane (Phase 2).
#
# A SEPARATE pinned rclone service. It NEVER mounts hermes-data or /opt/data:
#   - the staging mount is READ-ONLY (Phase-1 immutable generations),
#   - the published rclone crypt config (the secret-managed
#     obsidian-rclone-config volume) is READ-ONLY: rclone runs against a
#     private writable ACTIVE copy seeded into the state volume
#     (OAuth-refresh fix, rclone-active-config.sh). The active copy is
#     preserved while the seed is unchanged (a refreshed OAuth token
#     survives restarts) and atomically reseeded only when the seed
#     changes; the seed itself is never modified.
#   - ONLY its own state volume is writable.
#
# Contract (see docs/vault-recovery-operations.md -> Phase 2):
#   - Requires VAULT_RECOVERY_RCLONE_REMOTE to be an rclone remote of type
#     `crypt` with a NON-EMPTY underlying remote and a NON-EMPTY password
#     (validated before any transfer, not just naming convention).
#   - Reads Phase-1 READY immutable generations from the staging mount and
#     performs FULL local validation before upload: strict generation id,
#     READY sentinel content, manifest generation_id/schema, per-tree entries
#     index digest (manifest `entries_digest` == sha256 of the index file),
#     and the full manifest/hashes/tree: every entry's path/type/mode/size/
#     sha256 is verified against the local tree with find/stat/sha256sum
#     (no symlinks, no special files, no extra and no missing entries).
#   - Uploads the immutable generation to the remote UNCOMMITTED namespace
#     (Josemar/vault-recovery/uncommitted/<gen>). No destructive slot
#     overwrite: every generation always lands under its own unique name.
#   - Verifies the REMOTE DECRYPTED content (downloads through the crypt
#     remote and re-runs the same full validation) BEFORE commit.
#   - Commits the payload into the COMMITTED namespace and verifies the
#     COMMITTED decrypted content; ONLY THEN publishes the READY sentinel
#     (moved alone in a final step, READY-last). A partial or interrupted
#     commit never leaves a READY-carrying partial generation in the
#     committed namespace, and the READY marker is never published before
#     the committed payload was verified.
#   - NEVER mutates a published generation: on a retry where the committed
#     namespace already holds a VALID READY marker bound to the manifest
#     (READY content == generation id == manifest generation_id), the
#     committed payload is re-downloaded and fully re-validated, then
#     acknowledged (or failed) — no upload, no commit move, no overwrite.
#     Only committed remote generations are listable/recoverable.
#   - Distinguishes a CONFIRMED missing/invalid READY marker (rclone
#     "file/directory not found", exit 3/4, or content/binding mismatch)
#     from an INDETERMINATE remote read FAILURE (rclone transport, auth,
#     backend, or uncategorised error: any other non-zero exit). An
#     indeterminate marker state is NEVER treated as markerless: the
#     upload aborts BEFORE any remote mutation and retention skips the
#     entire prune (fail closed).
#   - Writes the local acknowledgement (ledger + last-uploaded-generation)
#     ONLY after verification + commit both succeeded. Every ledger entry is
#     BOUND to the remote identity it was recorded against AND to the
#     VERIFIED remote payload digests
#     (generation-id TAB remote-name TAB remote-path TAB manifest-sha256
#     TAB ready-sha256). An acknowledgement is honored (backlog skip and
#     both retention passes) ONLY while the CURRENT remote still holds a
#     committed payload whose manifest/READY digests match the recorded
#     ones: the remote identity rotation case, a remote WIPE, or a remote
#     REPOINT to a different payload all invalidate the ack — the staged
#     generation is re-uploaded (or, for a READY-visible committed payload,
#     re-validated against the authoritative staged/acked content and
#     re-uploaded when it differs) and re-acknowledged under the current
#     identity, and a stale ack NEVER authorizes a local delete. An
#     INDETERMINATE remote read failure (rclone transport/auth/backend
#     error) never treats the ack as confirmed: the run aborts before any
#     remote mutation and retention skips the entire prune (fail closed).
#   - Retains the newest VAULT_RECOVERY_RETENTION (default 14) committed
#     generations that carry a VALID READY marker; prunes older ones ONLY
#     after a clean MACHINE inventory listing of the committed namespace
#     (rclone `lsjson --dirs-only`, parsed with the strict shared
#     vault-recovery-lsjson.awk parser — never the human-readable `lsd`
#     columns) validates AND the generation's acknowledgement is CONFIRMED
#     against the current remote payload (digest-bound ledger entry
#     matching the remote manifest/READY digests right now; safety over
#     convenience). Incomplete committed dirs (interrupted commits: no
#     READY, invalid marker, or unbound manifest) are NEVER counted toward
#     retention, NEVER evict valid generations, and NEVER pruned — they are
#     preserved for the next idempotent commit.
#   - LOCAL staged retention (ack-based): after a remote-acknowledged
#     committed upload, the local staging generations beyond the newest
#     VAULT_RECOVERY_LOCAL_RETENTION (default 14) FULL generations are
#     pruned — ONLY generations whose acknowledgement is CONFIRMED against
#     the CURRENT remote payload at prune time (the remote still holds the
#     committed payload matching the ledger-bound digests) are ever removed
#     locally, and only through the dedicated writable PRUNE_DIR mount (the
#     read-only staging mount is never written). A stale ack (remote
#     wiped/repointed: confirmed mismatch) never deletes locally; an
#     indeterminate remote read failure skips the ENTIRE local prune. Every
#     candidate is FULLY validated first (strict id, READY binding, manifest
#     generation_id/schema, entries-index digests, exact tree/hashes of both
#     trees): invalid states (invalid names, missing/unbound READY, manifest
#     mismatch, tampered entries/tree) SKIP the ENTIRE local prune with a
#     visible error — any doubt -> no prune, valid old state is never
#     removed. `latest` and non-generation artifacts are never touched.
#   - Failures and partial inbound objects are NEVER considered snapshots:
#     a failed upload leaves at most an uncommitted/ partial directory that
#     is neither listable nor recoverable.
#   - Backlog reconciliation: every run processes the FULL staged backlog,
#     not just the `latest` pointer. Each staged generation that is NOT
#     acknowledged in the local ledger is uploaded and acknowledged,
#     OLDEST first (generation ids are lexically sortable UTC timestamps,
#     so plain sort == chronological order). The run succeeds only when
#     the whole backlog was committed + acknowledged; any failure (a
#     staged directory whose name is not a strict generation id — e.g. a
#     crashed export's leftover — a validation failure, or a remote
#     error) ABORTS the run BEFORE any further upload with a visible
#     error, so an uploader that was down for several days catches up
#     incrementally across runs (acked generations are skipped, the next
#     run resumes at the same oldest unacknowledged generation). `latest`
#     and non-generation artifacts are never uploaded; a dangling `latest`
#     pointer (its generation neither staged nor acked) fails closed.
#   - Polls the staging mount for new generations (idempotent; does not
#     depend on deleting sentinels).
#   - A KERNEL-RELEASED exclusive advisory lock (flock(1), non-blocking)
#     around run_once/upload prevents manual + daemon invocations from
#     uploading/pruning concurrently. The kernel releases the lock
#     automatically when the owning process dies — even on SIGKILL (e.g.
#     `docker kill` during a deploy) — so a dead container can NEVER leave
#     a stale lease behind that blocks future starts. The lock FILE
#     (`.upload.flock` in the state volume) is never deleted: deleting a
#     lock file while another process holds it would break mutual
#     exclusion; the kernel lock is what matters, not the file's presence.
#     The runtime MUST provide flock(1); the uploader fails closed (exit 2)
#     when it is unavailable. The legacy mkdir lease (`.upload.lock`
#     directory) is gone; the deploy's stale-lock migration step removes
#     only that old empty directory, never the flock file.
#
# Env:
#   VAULT_RECOVERY_UPLOADER_STAGING_DIR - read-only staging mount (default /staging)
#   VAULT_RECOVERY_PRUNE_DIR            - writable staging-prune mount used ONLY
#                                         for ack-based local retention
#                                         (default: the staging dir itself)
#   VAULT_RECOVERY_UPLOADER_STATE_DIR   - writable state volume (default /state)
#   VAULT_RECOVERY_RCLONE_REMOTE        - REQUIRED crypt remote name
#   VAULT_RECOVERY_RCLONE_PATH          - remote base path (default Josemar/vault-recovery)
#   VAULT_RECOVERY_RETENTION            - committed generations to retain (default 14)
#   VAULT_RECOVERY_LOCAL_RETENTION      - local staged generations to retain
#                                         after ack (default 14)
#   VAULT_RECOVERY_POLL_INTERVAL        - poll interval in SECONDS (default 300)
#   VAULT_RECOVERY_RUN_ON_START         - run once on start (default true)
#   VAULT_RECOVERY_ONCE                 - one-shot mode (default false): full
#                                         startup validation, same upload lock,
#                                         ONE backlog reconciliation pass (every
#                                         staged unacknowledged generation is
#                                         uploaded and acknowledged, oldest
#                                         first), then EXIT with a meaningful
#                                         status (for tests and manual
#                                         cron-style invocations).
#   RCLONE_CONFIG                       - rclone config SEED path (default
#                                         /config/rclone/rclone.conf; the
#                                         published read-only config). The
#                                         uploader seeds a private writable
#                                         active copy at
#                                         $STATE_DIR/rclone.active.conf and
#                                         runs rclone against it
#                                         (OAuth-refresh fix).
#
# Exit codes: 0 success/continue, 1 upload attempt failed (one-shot mode),
# 2 config/validation error, 3 unexpected error.

set -eu

STAGING_DIR="${VAULT_RECOVERY_UPLOADER_STAGING_DIR:-/staging}"
PRUNE_DIR="${VAULT_RECOVERY_PRUNE_DIR:-$STAGING_DIR}"
STATE_DIR="${VAULT_RECOVERY_UPLOADER_STATE_DIR:-/state}"
REMOTE_NAME="${VAULT_RECOVERY_RCLONE_REMOTE:-}"
REMOTE_PATH="${VAULT_RECOVERY_RCLONE_PATH:-Josemar/vault-recovery}"
# The remote identity used for ack binding: the normalized remote path (no
# trailing slash). Every acknowledgement in the local ledger is bound to
# `REMOTE_NAME + REMOTE_PATH_CLEAN`, so an ack recorded against one remote
# (name/path) can never authorize retention against a different one.
REMOTE_PATH_CLEAN="${REMOTE_PATH%/}"
RETENTION="${VAULT_RECOVERY_RETENTION:-14}"
LOCAL_RETENTION="${VAULT_RECOVERY_LOCAL_RETENTION:-14}"
POLL_INTERVAL="${VAULT_RECOVERY_POLL_INTERVAL:-300}"
RUN_ON_START="${VAULT_RECOVERY_RUN_ON_START:-true}"
ONCE="${VAULT_RECOVERY_ONCE:-false}"
RCLONE_CONFIG_FILE="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
UNCOMMITTED_NS="uncommitted"
COMMITTED_NS="committed"
INSTALL_DIR_NAME=".vault-recovery-install"
# Strict JSON well-formedness validator (POSIX awk; the pinned rclone image
# has no python3/jq). Manifests must be REAL JSON before any field is
# extracted — a malformed document with grep-visible fields is rejected.
JSON_VALIDATOR="$(dirname "$0")/vault-recovery-json.awk"
# Strict FULL-SCHEMA validator: mirrors the authoritative Python
# validate_manifest_schema (unknown keys anywhere, types, digests, doctor
# metadata) and prints the extracted values the shell steps consume.
MANIFEST_SCHEMA_AWK="$(dirname "$0")/vault-recovery-manifest-schema.awk"
# Strict rclone `lsjson` machine-inventory parser (never human lsd columns).
LSJSON_PARSER="$(dirname "$0")/vault-recovery-lsjson.awk"

# The upload lock: a KERNEL-RELEASED exclusive advisory flock(1) on a
# regular file in the state volume. The file itself persists across runs
# (never deleted — deleting a lock file while another process holds it
# would break mutual exclusion), but the KERNEL releases the exclusive lock
# automatically when the owning process dies, even on SIGKILL: a container
# killed mid-upload (e.g. `docker kill` during a deploy) can never leave a
# stale lease behind. Named `.upload.flock` (NOT the legacy mkdir
# `.upload.lock` DIRECTORY) so it never collides with the deploy's
# legacy-lock migration step, which removes only the old empty directory.
UPLOAD_LOCK_FILE="$STATE_DIR/.upload.flock"
# Dedicated file descriptor carrying the kernel lock for the process
# lifetime (closed on release/exit; the kernel then drops the lock).
UPLOAD_LOCK_FD=9
VERIFY_TMP="$STATE_DIR/.verify-tmp"
LEDGER_FILE="$STATE_DIR/uploaded-generations.jsonl"
LAST_UPLOADED_FILE="$STATE_DIR/last-uploaded-generation"

log_info() { echo "[vault-recovery-uploader] $1"; }
log_error() { echo "[vault-recovery-uploader] ERROR: $1" >&2; }

# Shared rclone OAuth-refresh runtime helper: rclone runs against a private
# writable ACTIVE copy of the config (see rclone-active-config.sh); the
# published seed stays read-only.
. "$(dirname "$0")/rclone-active-config.sh"

_cleanup() {
    # Release the upload lock (close the lock fd 9, see UPLOAD_LOCK_FD) if
    # we hold it. The kernel would release it on process exit anyway;
    # closing here lets the daemon drop the lock between poll iterations
    # exactly like the old mkdir lease. The lock FILE is never removed.
    #
    # NEVER attach `2>/dev/null` (or any stderr redirection) to this `exec`:
    # redirections on special builtins PERSIST after the command (same rule
    # as acquire_upload_lock), so it would permanently redirect the whole
    # daemon shell's stderr to /dev/null and silently swallow every later
    # log_error — e.g. the contention rejection of the next poll — hiding
    # daemon errors from the operator. A redirection failure on the `exec`
    # special builtin is FATAL in POSIX sh (dash exits 2 with its own
    # visible message), and closing the held fd cannot fail, so no
    # redirection is needed here.
    if [ -n "${_LOCK_HELD:-}" ] && [ "$_LOCK_HELD" = "1" ]; then
        exec 9>&-
        _LOCK_HELD="0"
    fi
    rm -rf "$VERIFY_TMP" 2>/dev/null || true
}
trap _cleanup EXIT
trap '_cleanup; exit 143' INT TERM
_LOCK_HELD="0"

# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

validate_retention() {
    case "$RETENTION" in
        ''|*[!0-9]*)
            log_error "Invalid VAULT_RECOVERY_RETENTION: $RETENTION"
            exit 2
            ;;
    esac
    if [ "$RETENTION" -lt 1 ]; then
        log_error "VAULT_RECOVERY_RETENTION must be at least 1"
        exit 2
    fi
}

validate_local_retention() {
    case "$LOCAL_RETENTION" in
        ''|*[!0-9]*)
            log_error "Invalid VAULT_RECOVERY_LOCAL_RETENTION: $LOCAL_RETENTION"
            exit 2
            ;;
    esac
    if [ "$LOCAL_RETENTION" -lt 1 ]; then
        log_error "VAULT_RECOVERY_LOCAL_RETENTION must be at least 1"
        exit 2
    fi
}

validate_poll_interval() {
    case "$POLL_INTERVAL" in
        ''|*[!0-9]*)
            log_error "Invalid VAULT_RECOVERY_POLL_INTERVAL: $POLL_INTERVAL (must be a positive integer of seconds)"
            exit 2
            ;;
    esac
    if [ "$POLL_INTERVAL" -le 0 ]; then
        log_error "VAULT_RECOVERY_POLL_INTERVAL must be greater than zero"
        exit 2
    fi
}

# The remote MUST be rclone type `crypt` with a non-empty underlying remote
# and a non-empty password. Validated from the real config, not naming
# convention (same spirit as the Mnemosyne uploader, plus the Phase-2
# underlying/password requirements). The metadata-encryption standard is
# enforced too: `filename_encryption` must be `standard` (reject
# `off`/`obfuscate`) and `directory_name_encryption` must not be `false`
# (absent means rclone's default `true`) — otherwise plaintext file and
# directory names would leak in the ciphertext metadata and the ciphertext
# non-leak proof would be void.
require_remote() {
    if [ -z "$REMOTE_NAME" ]; then
        log_error "VAULT_RECOVERY_RCLONE_REMOTE is required (must be rclone type 'crypt' with a non-empty underlying remote and password)"
        exit 2
    fi
    if [ ! -f "$RCLONE_CONFIG_FILE" ]; then
        log_error "rclone config not found at $RCLONE_CONFIG_FILE"
        exit 2
    fi
    cfg="$(rclone config show "$REMOTE_NAME:" --config "$RCLONE_CONFIG_FILE" 2>/dev/null || true)"
    if [ -z "$cfg" ]; then
        log_error "could not read config for remote '$REMOTE_NAME'"
        exit 2
    fi
    remote_type="$(printf '%s\n' "$cfg" | awk -F'=' '/^type[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}')"
    underlying="$(printf '%s\n' "$cfg" | awk -F'=' '/^remote[[:space:]]*=/{gsub(/^[[:space:]]*/,"",$2); gsub(/[[:space:]]*$/,"",$2); print $2; exit}')"
    password="$(printf '%s\n' "$cfg" | awk -F'=' '/^password[[:space:]]*=/{gsub(/^[[:space:]]*/,"",$2); gsub(/[[:space:]]*$/,"",$2); print $2; exit}')"
    filename_encryption="$(printf '%s\n' "$cfg" | awk -F'=' '/^filename_encryption[[:space:]]*=/{gsub(/^[[:space:]]*/,"",$2); gsub(/[[:space:]]*$/,"",$2); print $2; exit}')"
    directory_name_encryption="$(printf '%s\n' "$cfg" | awk -F'=' '/^directory_name_encryption[[:space:]]*=/{gsub(/^[[:space:]]*/,"",$2); gsub(/[[:space:]]*$/,"",$2); print $2; exit}')"
    if [ "$remote_type" != "crypt" ]; then
        log_error "Remote '$REMOTE_NAME' is not rclone type 'crypt' (got: '${remote_type:-missing}'). Configure a crypt remote in the obsidian-rclone-config volume."
        exit 2
    fi
    if [ -z "$underlying" ]; then
        log_error "Remote '$REMOTE_NAME' is crypt but has an EMPTY underlying remote; refusing to upload unencrypted-boundary data"
        exit 2
    fi
    if [ -z "$password" ]; then
        log_error "Remote '$REMOTE_NAME' is crypt but has an EMPTY password; refusing to upload"
        exit 2
    fi
    # Metadata-encryption standard: `standard` filename encryption (never
    # `off`/`obfuscate`) and directory-name encryption enabled. An absent
    # filename_encryption means rclone's default `standard`; an absent
    # directory_name_encryption means rclone's default `true`.
    if [ -n "$filename_encryption" ] && [ "$filename_encryption" != "standard" ]; then
        log_error "Remote '$REMOTE_NAME' filename_encryption is '${filename_encryption}' (must be 'standard'): plaintext file names would leak in the ciphertext metadata; refusing to upload"
        exit 2
    fi
    if [ "$directory_name_encryption" = "false" ]; then
        log_error "Remote '$REMOTE_NAME' directory_name_encryption is 'false' (must be 'true'): plaintext directory names would leak in the ciphertext metadata; refusing to upload"
        exit 2
    fi
    log_info "Remote '$REMOTE_NAME' validated as type 'crypt' (underlying + password set, standard filename + directory-name encryption)"
}

# ---------------------------------------------------------------------------
# Strict generation id validation (no slash, no '..', exact 31-char shape)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Manifest field extraction (STRICT: values come ONLY from the shared
# full-schema validator's output, never from raw greps of the document)
# ---------------------------------------------------------------------------

# schema_field <schema_out> <key> [tree] - extract one value from the strict
# manifest schema validator's printed output (tab-separated lines:
# `schema_version\t1`, `generation_id\t<id>`,
# `entries_digest\t<tree>\t<sha256>`).
schema_field() {
    out="$1"
    key="$2"
    tree="${3:-}"
    if [ -n "$tree" ]; then
        printf '%s\n' "$out" | awk -F '\t' -v k="$key" -v t="$tree" \
            '$1 == k && $2 == t { print $3; exit }'
    else
        printf '%s\n' "$out" | awk -F '\t' -v k="$key" \
            '$1 == k { print $2; exit }'
    fi
}

# Run the strict full-schema validator over a manifest FILE. Fails (nonzero)
# on malformed JSON, unknown keys anywhere, wrong types, or invalid doctor
# metadata — mirroring the Python restore core exactly. The awk diagnostic
# is surfaced in the log so the rejection reason is visible.
manifest_schema_validate() {
    manifest="$1"
    out="$(awk -f "$MANIFEST_SCHEMA_AWK" "$manifest" 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_error "$(printf '%s\n' "$out" | head -n1)"
        return 1
    fi
    printf '%s\n' "$out"
    return 0
}

# ---------------------------------------------------------------------------
# Remote READY-marker validation (READY bound to the manifest)
# ---------------------------------------------------------------------------
#
# A committed generation is "READY-visible" ONLY when its remote READY
# sentinel exists, its content equals the generation id, and the remote
# manifest's generation_id binds to the same id. This is the single
# predicate that decides whether a committed payload may be treated as a
# published snapshot: listing exposes only such generations, retention
# counts/prunes only such generations, and the uploader never mutates a
# payload once this marker is visible.
#
# Tri-state result — callers MUST distinguish, never treat as a boolean:
#   0 - READY valid and manifest-bound (confirmed published snapshot).
#   1 - READY CONFIRMED missing/invalid: rclone reported file/directory
#       not found (exit 3/4), the marker content does not match the
#       generation id, or the manifest does not bind to it. The generation
#       is NOT a published snapshot.
#   2 - INDETERMINATE: the rclone cat itself FAILED (transport, auth,
#       backend, or uncategorised error: any other non-zero exit). The
#       marker state is UNKNOWN and must never be treated as markerless.
#       Callers must fail closed: the uploader aborts BEFORE any remote
#       mutation and retention skips the entire prune.
#
# Real rclone exits 3 (directory not found) / 4 (file not found) when the
# object is confirmed absent; every other non-zero exit code means the
# read itself failed. The rclone stderr is captured so the failure reason
# is visible in the logs instead of being silenced.

remote_ready_valid() {
    gen_id="$1"
    gen_remote="$2"
    ready="$(rclone cat "$gen_remote/READY" --config "$RCLONE_CONFIG_FILE" 2>&1)" && rc=0 || rc=$?
    if [ "$rc" -ne 0 ]; then
        case "$rc" in
            3|4) return 1 ;;
            *)
                log_error "remote READY read FAILED for $gen_remote (rclone exit $rc): $(printf '%s' "$ready" | head -n1)"
                log_error "marker state INDETERMINATE, NOT markerless; refusing to proceed"
                return 2
                ;;
        esac
    fi
    # Bounded first-line read, same sentinel contract as validate_generation.
    ready_line="$(printf '%s' "$ready" | head -c 4096 | tr -d '\r\n' | head -n1)"
    if [ "$ready_line" != "$gen_id" ]; then
        return 1
    fi
    manifest="$(rclone cat "$gen_remote/manifest.json" --config "$RCLONE_CONFIG_FILE" 2>&1)" && mrc=0 || mrc=$?
    if [ "$mrc" -ne 0 ]; then
        case "$mrc" in
            3|4) return 1 ;;
            *)
                log_error "remote manifest read FAILED for $gen_remote (rclone exit $mrc): $(printf '%s' "$manifest" | head -n1)"
                log_error "marker state INDETERMINATE, NOT markerless; refusing to proceed"
                return 2
                ;;
        esac
    fi
    # Strict JSON well-formedness BEFORE any field extraction: a manifest
    # that is not real JSON is a CONFIRMED invalid marker (the read itself
    # succeeded), never a valid published snapshot. The rejection reason is
    # surfaced so the operator sees WHY the marker was refused.
    if ! json_diag="$(printf '%s\n' "$manifest" | awk -f "$JSON_VALIDATOR" 2>&1)"; then
        log_error "remote manifest of $gen_id is not well-formed JSON: $(printf '%s\n' "$json_diag" | head -n1)"
        return 1
    fi
    # Strict FULL schema (unknown keys anywhere + doctor metadata, mirroring
    # the Python restore core): a schema-violating remote manifest is also a
    # CONFIRMED invalid marker — it could never be restored. The awk
    # diagnostic is surfaced (same convention as manifest_schema_validate)
    # so the schema rejection reason is visible.
    if ! schema_out="$(printf '%s\n' "$manifest" | awk -f "$MANIFEST_SCHEMA_AWK" 2>&1)"; then
        log_error "remote manifest of $gen_id violates the strict manifest schema: $(printf '%s\n' "$schema_out" | head -n1)"
        return 1
    fi
    manifest_gen="$(schema_field "$schema_out" "generation_id")"
    [ "$manifest_gen" = "$gen_id" ]
}

# ---------------------------------------------------------------------------
# FULL local tree validation against the manifest-bound entries index
# ---------------------------------------------------------------------------
#
# Every entry of the entries index (path/type/mode/size/sha256) is verified
# against the tree on disk, and every disk entry must be in the index (no
# extras, no missing, no symlinks, no special files). All disk facts are
# gathered in batched find/stat/sha256sum passes (no per-file shells).
#
# $3 is the mode-check flag: "modes" (default) compares modes exactly, which
# is correct for the immutable local staging tree. The REMOTE verification
# passes "loose": rclone crypt cannot round-trip POSIX modes, so the
# downloaded decrypted tree is validated content-exactly (path/type/size/
# sha256/dirs) with modes ignored; the install re-applies the exact recorded
# modes from the entries index.

validate_tree() {
    tree_root="$1"
    entries_file="$2"
    mode_check="${3:-modes}"
    if [ "$mode_check" != "modes" ]; then
        mode_check="loose"
    fi
    if [ ! -d "$tree_root" ]; then
        log_error "tree root not found: $tree_root"
        return 1
    fi
    if [ ! -f "$entries_file" ]; then
        log_error "entries index not found: $entries_file"
        return 1
    fi

    # The explicit no-symlink/no-special property is checked BEFORE the
    # entry-count check so a symlinked component is reported as such (and a
    # count mismatch caused by one still fails closed either way).
    if [ -n "$(find "$tree_root" -type l)" ]; then
        log_error "symlink found in $tree_root; refusing (no-follow contract)"
        return 1
    fi
    if [ -n "$(find "$tree_root" \( -type p -o -type s -o -type b -o -type c \) )" ]; then
        log_error "special file (fifo/socket/device) found in $tree_root; refusing"
        return 1
    fi

    total="$(wc -l < "$entries_file")"
    disk_total="$(find "$tree_root" | wc -l)"
    if [ "$disk_total" -ne "$total" ]; then
        log_error "tree entry count mismatch for $tree_root: disk=$disk_total entries=$total"
        return 1
    fi

    mkdir -p "$VERIFY_TMP"
    tmp_hashes="$VERIFY_TMP/hashes.$$"
    tmp_stats="$VERIFY_TMP/stats.$$"
    tmp_dirstats="$VERIFY_TMP/dirstats.$$"
    rm -f "$tmp_hashes" "$tmp_stats" "$tmp_dirstats"
    : > "$tmp_hashes"
    : > "$tmp_stats"
    : > "$tmp_dirstats"

    # Files: "sha256\tpath" and "mode\tsize\tpath" (relative, "./" stripped).
    if find "$tree_root" -type f -print -quit | grep -q .; then
        ( cd "$tree_root" && find . -type f -print0 | sort -z | xargs -0 sha256sum \
            | sed -E 's#^([0-9a-f]{64})  \./#\1\t#' ) > "$tmp_hashes"
        ( cd "$tree_root" && find . -type f -print0 | xargs -0 stat -c '%a %s %n' \
            | sed -E 's#^([0-7]+) ([0-9]+) \./#\1\t\2\t#' ) > "$tmp_stats"
    fi
    # Directories: "mode\tpath" (root included as "."). The root line from
    # `stat -c '%a %n' .` has no "./" prefix, so a second substitution turns
    # "775 ." into "775\t." — without it the dir-validation awk would see a
    # single tab-less field and fail on the root.
    ( cd "$tree_root" && find . -type d -print0 | xargs -0 stat -c '%a %n' \
        | sed -E 's#^([0-7]+) \./#\1\t#; s#^([0-7]+) \.#\1\t.#' ) > "$tmp_dirstats"

    file_count_entries="$(grep -c '^file	' "$entries_file" || true)"
    dir_count_entries="$(grep -c '^dir	' "$entries_file" || true)"
    file_count_disk="$(wc -l < "$tmp_hashes")"
    dir_count_disk="$(wc -l < "$tmp_dirstats")"
    if [ "$file_count_entries" -ne "$file_count_disk" ] || [ "$dir_count_entries" -ne "$dir_count_disk" ]; then
        log_error "entry type count mismatch for $tree_root: files entries=$file_count_entries disk=$file_count_disk; dirs entries=$dir_count_entries disk=$dir_count_disk"
        return 1
    fi

    awk_diag="$(awk -F '\t' -v MODE_CHECK="$mode_check" '
        NR == FNR { e_mode[$5] = $2; e_size[$5] = $3; e_sha[$5] = $4; next }
        FNR == 1 && NR > 1 { phase++ }
        phase == 1 { # disk hashes: $1=sha $2=path
            if (!($2 in e_sha)) { print "extra file on disk: " $2; bad = 1; next }
            if (e_sha[$2] != $1) { print "sha256 mismatch for " $2; bad = 1 }
            next
        }
        phase == 2 { # disk file stats: $1=mode $2=size $3=path
            if (!($3 in e_sha)) { print "extra file stat: " $3; bad = 1; next }
            if (MODE_CHECK == "modes" && e_mode[$3] != $1) { print "mode mismatch for " $3; bad = 1 }
            if (e_size[$3] != $2) { print "size mismatch for " $3; bad = 1 }
        }
        END { exit (bad ? 1 : 0) }
    ' "$entries_file" "$tmp_hashes" "$tmp_stats")" || true
    if [ -n "$awk_diag" ]; then
        log_error "file validation failed for $tree_root:"
        log_error "$awk_diag"
        return 1
    fi

    awk_diag="$(awk -F '\t' -v MODE_CHECK="$mode_check" '
        NR == FNR { e_type[$5] = $1; e_mode[$5] = $2; next }
        FNR == 1 && NR > 1 { phase++ }
        phase == 1 { # disk dir stats: $1=mode $2=path
            if (!($2 in e_type)) { print "extra directory on disk: " $2; bad = 1; next }
            if (e_type[$2] != "dir") { print "type mismatch (expected dir): " $2; bad = 1 }
            if (MODE_CHECK == "modes" && e_mode[$2] != $1) { print "mode mismatch for dir " $2; bad = 1 }
        }
        END { exit (bad ? 1 : 0) }
    ' "$entries_file" "$tmp_dirstats")" || true
    if [ -n "$awk_diag" ]; then
        log_error "directory validation failed for $tree_root:"
        log_error "$awk_diag"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Generation-level validation (READY + manifest + both trees)
# ---------------------------------------------------------------------------

validate_generation() {
    gen_dir="$1"
    gen_id="$2"
    mode_check="${3:-modes}"
    require_ready="${4:-1}"
    if [ "$require_ready" = "1" ]; then
        if [ ! -f "$gen_dir/READY" ]; then
            log_error "generation $gen_id not READY (missing READY sentinel)"
            return 1
        fi
        ready="$(head -c 4096 "$gen_dir/READY" | tr -d '\r\n' | head -n1)"
        if [ "$ready" != "$gen_id" ]; then
            log_error "READY generation mismatch: READY=$ready dir=$gen_id"
            return 1
        fi
    fi
    manifest="$gen_dir/manifest.json"
    if [ ! -f "$manifest" ]; then
        log_error "manifest missing in $gen_dir"
        return 1
    fi
    # Strict JSON well-formedness (council fix): the manifest must be REAL
    # JSON before any field is extracted. A malformed document whose
    # required fields are grep-visible is rejected here — it could never be
    # restored by the Python core, so it must never become READY or be
    # acknowledged.
    if ! awk -f "$JSON_VALIDATOR" "$manifest" 2>/dev/null; then
        log_error "manifest.json of $gen_id is not well-formed JSON; refusing upload"
        return 1
    fi
    # Strict FULL-SCHEMA validation (council fix): the manifest must match
    # the Python-authoritative schema EXACTLY — unknown keys anywhere,
    # wrong types, invalid digests, or broken doctor metadata reject the
    # generation the same way the restore core would. Only the validator's
    # output is used for field extraction (no raw greps).
    if ! schema_out="$(manifest_schema_validate "$manifest")"; then
        log_error "manifest.json of $gen_id does not match the strict manifest schema; refusing upload"
        return 1
    fi
    manifest_gen="$(schema_field "$schema_out" "generation_id")"
    if [ "$manifest_gen" != "$gen_id" ]; then
        log_error "manifest generation_id mismatch: manifest=$manifest_gen dir=$gen_id"
        return 1
    fi
    schema="$(schema_field "$schema_out" "schema_version")"
    if [ "$schema" != "1" ]; then
        log_error "manifest schema_version is '${schema:-missing}' (expected 1); refusing upload"
        return 1
    fi
    for tree in vault .gbrain; do
        entries_file="$gen_dir/$tree.entries.txt"
        if [ ! -f "$entries_file" ]; then
            log_error "generation $gen_id has no entries index for tree '$tree' ($entries_file); only phase-2-exporter generations are uploadable"
            return 1
        fi
        expected_digest="$(schema_field "$schema_out" "entries_digest" "$tree")"
        if [ -z "$expected_digest" ]; then
            log_error "manifest has no entries_digest for tree '$tree'; refusing upload"
            return 1
        fi
        # Strict format check on the bound digest: exactly 64 lowercase hex
        # chars (the sha256 of the entries index the validator will rehash).
        if ! printf '%s' "$expected_digest" | grep -Eq '^[0-9a-f]{64}$'; then
            log_error "manifest entries_digest for tree '$tree' is not a 64-hex sha256: '$expected_digest'; refusing upload"
            return 1
        fi
        actual_digest="$(sha256sum "$entries_file" | cut -d' ' -f1)"
        if [ "$actual_digest" != "$expected_digest" ]; then
            log_error "entries index digest mismatch for tree '$tree': manifest=$expected_digest file=$actual_digest"
            return 1
        fi
        if ! validate_tree "$gen_dir/$tree" "$entries_file" "$mode_check"; then
            return 1
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# State (ledger / last-uploaded)
# ---------------------------------------------------------------------------
#
# ACK BINDING (council fix, digest-strengthened): every ledger entry binds
# the acknowledgement to the remote identity it was recorded against AND to
# the VERIFIED remote payload:
#
#     <generation-id> TAB <remote-name> TAB <remote-path>
#         TAB <manifest-sha256> TAB <ready-sha256>
#
# The digests are the sha256 of the manifest.json and READY bytes VERIFIED
# against the remote committed payload at ack time (the manifest as
# downloaded from the verified committed payload; the READY as the published
# sentinel bytes). `ledger_has` matches any entry bound to the CURRENT
# remote identity (legacy pre-digest 3-field entries count only for the
# existence question — dangling-latest); `ledger_digests` returns the last
# recorded digest pair for the current remote. An acknowledgement is HONORED
# only while the current remote still holds a committed payload matching
# those digests (remote_ack_confirmed): a different remote identity, a
# remote wipe, or a repoint to different bytes all invalidate the ack — the
# generation is re-uploaded and re-acknowledged under the current identity,
# and a stale ack never authorizes a local delete.

write_last_uploaded() {
    tmp="$STATE_DIR/.last-uploaded.tmp.$$"
    printf '%s\n' "$1" > "$tmp"
    mv "$tmp" "$LAST_UPLOADED_FILE"
}

append_uploaded_ledger() {
    gen="$1"
    manifest_sha="$2"
    ready_sha="$3"
    tmp="$STATE_DIR/.uploaded-ledger.tmp.$$"
    if [ -f "$LEDGER_FILE" ]; then
        cp "$LEDGER_FILE" "$tmp"
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$gen" "$REMOTE_NAME" "$REMOTE_PATH_CLEAN" "$manifest_sha" "$ready_sha" >> "$tmp"
    mv "$tmp" "$LEDGER_FILE"
}

# ledger_has <gen>: any acknowledgement bound to the CURRENT remote identity
# (digest-bound 5-field entries and legacy pre-digest 3-field entries). Used
# for the existence question (dangling-latest); the honoring question is
# decided by remote_ack_confirmed below.
ledger_has() {
    gen="$1"
    [ -f "$LEDGER_FILE" ] || return 1
    grep -Eq "^${gen}	${REMOTE_NAME}	${REMOTE_PATH_CLEAN}($|	)" "$LEDGER_FILE"
}

# ledger_digests <gen>: the LAST recorded "manifest_sha TAB ready_sha" pair
# for the current remote identity (empty for legacy pre-digest entries).
ledger_digests() {
    gen="$1"
    [ -f "$LEDGER_FILE" ] || return 1
    grep -E "^${gen}	${REMOTE_NAME}	${REMOTE_PATH_CLEAN}	[0-9a-f]{64}	[0-9a-f]{64}$" "$LEDGER_FILE" \
        | tail -n1 | cut -f4,5
}

remote_base() {
    if [ -n "$REMOTE_PATH_CLEAN" ]; then
        printf '%s:%s' "$REMOTE_NAME" "$REMOTE_PATH_CLEAN"
    else
        printf '%s:' "$REMOTE_NAME"
    fi
}

# ---------------------------------------------------------------------------
# Remote payload digests (the verified remote manifest/READY bytes)
# ---------------------------------------------------------------------------
#
# remote_payload_digests <gen_id> <gen_remote> reads the remote READY
# sentinel and manifest.json through the crypt remote, validates the marker
# binding (READY content == generation id, manifest strict full schema,
# manifest generation_id == generation id) and prints
# "manifest_sha256 TAB ready_sha256" of the EXACT remote bytes.
#
# Tri-state rc (callers MUST distinguish, never treat as a boolean):
#   0 - success: printed digests are the verified remote payload digests.
#   1 - CONFIRMED absent/invalid: rclone exit 3/4 (file/directory not
#       found), marker content/binding mismatch, or a schema-violating
#       manifest. The payload is NOT what the ack claims.
#   2 - INDETERMINATE: the rclone read itself FAILED (transport, auth,
#       backend, or uncategorised error: any other non-zero exit). The
#       payload state is UNKNOWN and must never be treated as a confirmed
#       mismatch: callers fail closed.

remote_payload_digests() {
    gen_id="$1"
    gen_remote="$2"
    pd_tmp="$VERIFY_TMP/pd.$$"
    rm -rf "$pd_tmp"; mkdir -p "$pd_tmp"
    ready_file="$pd_tmp/READY"
    manifest_file="$pd_tmp/manifest.json"
    # NOTE: the explicit `&& rc=0 || rc=$?` capture is required — `$?` right
    # after `if ! cmd; then` is the NEGATED status (always 0), which would
    # turn every rclone failure into an "indeterminate" read. The `||`
    # branch preserves the REAL exit code (3/4 = confirmed absent, anything
    # else = failed read). The explicit `--config` pins the ACTIVE config
    # (OAuth-refresh fix) exactly like every other rclone call in this
    # script.
    rclone cat "$gen_remote/READY" --config "$RCLONE_CONFIG_FILE" > "$ready_file" 2>&1 && rc=0 || rc=$?
    if [ "$rc" -ne 0 ]; then
        case "$rc" in
            3|4) rm -rf "$pd_tmp"; return 1 ;;
            *)
                log_error "remote READY read FAILED for $gen_remote (rclone exit $rc): $(head -n1 "$ready_file")"
                log_error "marker state INDETERMINATE, NOT markerless; refusing to proceed"
                rm -rf "$pd_tmp"
                return 2
                ;;
        esac
    fi
    # Bounded first-line read, same sentinel contract as validate_generation.
    ready_line="$(head -c 4096 "$ready_file" | tr -d '\r\n' | head -n1)"
    if [ "$ready_line" != "$gen_id" ]; then
        rm -rf "$pd_tmp"
        return 1
    fi
    rclone cat "$gen_remote/manifest.json" --config "$RCLONE_CONFIG_FILE" > "$manifest_file" 2>&1 && mrc=0 || mrc=$?
    if [ "$mrc" -ne 0 ]; then
        case "$mrc" in
            3|4) rm -rf "$pd_tmp"; return 1 ;;
            *)
                log_error "remote manifest read FAILED for $gen_remote (rclone exit $mrc): $(head -n1 "$manifest_file")"
                log_error "marker state INDETERMINATE, NOT markerless; refusing to proceed"
                rm -rf "$pd_tmp"
                return 2
                ;;
        esac
    fi
    # Strict well-formedness AND full schema (unknown keys + doctor
    # metadata): a manifest that could not be restored is CONFIRMED invalid.
    # The awk diagnostic is surfaced (same convention as
    # manifest_schema_validate) so the rejection reason is visible.
    if ! schema_out="$(awk -f "$MANIFEST_SCHEMA_AWK" "$manifest_file" 2>&1)"; then
        log_error "remote manifest of $gen_id violates the strict manifest schema: $(printf '%s\n' "$schema_out" | head -n1)"
        rm -rf "$pd_tmp"
        return 1
    fi
    manifest_gen="$(schema_field "$schema_out" "generation_id")"
    if [ "$manifest_gen" != "$gen_id" ]; then
        rm -rf "$pd_tmp"
        return 1
    fi
    printf '%s\t%s\n' \
        "$(sha256sum "$manifest_file" | cut -d' ' -f1)" \
        "$(sha256sum "$ready_file" | cut -d' ' -f1)"
    rm -rf "$pd_tmp"
    return 0
}

# remote_ack_confirmed <gen_id> <gen_remote>: tri-state ack confirmation.
#   0 - the remote committed payload MATCHES the ledger-bound digests for
#       the CURRENT remote identity (the acknowledgement is honored).
#   1 - CONFIRMED not matching: no digest-bound ledger entry for the
#       current remote, marker absent/invalid, or digest mismatch (remote
#       wiped or repointed).
#   2 - INDETERMINATE remote read failure (never treated as a confirmed
#       mismatch — callers fail closed).
remote_ack_confirmed() {
    gen_id="$1"
    gen_remote="$2"
    digests="$(remote_payload_digests "$gen_id" "$gen_remote")" || { rc=$?; return "$rc"; }
    recorded="$(ledger_digests "$gen_id")"
    [ -n "$recorded" ] || return 1
    [ "$digests" = "$recorded" ]
}

# ---------------------------------------------------------------------------
# Upload one immutable generation: validate -> upload to uncommitted ->
# verify remote decrypted content -> commit payload -> verify committed
# payload -> publish READY -> ack. A committed generation whose READY marker
# is already visible (retry after a crash between READY publication and the
# local ack) is never mutated: validate it and acknowledge or fail.
# ---------------------------------------------------------------------------

upload_one() {
    gen_id="$1"
    if ! is_valid_generation_id "$gen_id"; then
        log_error "Invalid generation id (rejected to prevent path traversal): $gen_id"
        return 1
    fi
    staged_dir="$STAGING_DIR/$gen_id"
    if ! path_under_staging "$staged_dir"; then
        log_error "Resolved generation path escapes staging dir: $staged_dir"
        return 1
    fi
    if [ ! -d "$staged_dir" ]; then
        log_error "Generation dir not found: $staged_dir"
        return 1
    fi

    # FULL local validation before any transfer.
    if ! validate_generation "$staged_dir" "$gen_id"; then
        log_error "local validation failed for $gen_id; NOT uploading"
        return 1
    fi

    base="$(remote_base)"
    uncommitted="${base}/${UNCOMMITTED_NS}/${gen_id}"
    committed="${base}/${COMMITTED_NS}/${gen_id}"

    # Retry-after-READY path: when the committed namespace already holds a
    # VALID READY marker bound to the manifest, the payload is a published
    # snapshot and must NEVER be mutated (no upload, no commit move, no
    # overwrite). Re-download the committed payload and fully re-validate
    # it; acknowledge on success, fail without touching it otherwise.
    # An INDETERMINATE remote read failure (rclone transport/auth/backend
    # error, remote_ready_valid rc 2) is NEVER treated as markerless: the
    # marker state is unknown, so the upload ABORTS here, BEFORE any remote
    # mutation.
    if remote_ready_valid "$gen_id" "$committed"; then
        log_info "Generation $gen_id already committed with a valid READY marker; validating the committed payload (no overwrite)"
        rm -rf "$VERIFY_TMP"; mkdir -p "$VERIFY_TMP"
        if ! rclone copy "$committed" "$VERIFY_TMP" --create-empty-src-dirs \
            --config "$RCLONE_CONFIG_FILE"; then
            log_error "retry validation download of committed $gen_id failed; NOT acknowledged"
            return 1
        fi
        if ! validate_generation "$VERIFY_TMP" "$gen_id" "loose"; then
            log_error "committed generation $gen_id FAILED validation despite a valid READY marker; refusing to overwrite the published payload; NOT acknowledged (operator intervention required)"
            return 1
        fi
        # Digest binding: acknowledge WITHOUT mutation only when the remote
        # payload still matches the acknowledged/staged content. A ledger
        # record for the CURRENT remote pins the exact manifest/READY
        # digests of the verified payload; without one (crash recovery,
        # remote rotation, or a legacy pre-digest ack) the LOCAL staged
        # manifest is the authoritative content. A remote that was WIPED or
        # REPOINTED to a different payload invalidates the ack: the staged
        # content is re-uploaded and replaces the remote payload (never
        # accepted silently, never a stale-ack local delete).
        remote_m="$(sha256sum "$VERIFY_TMP/manifest.json" | cut -d' ' -f1)"
        remote_r="$(sha256sum "$VERIFY_TMP/READY" | cut -d' ' -f1)"
        repoint=0
        if rec="$(ledger_digests "$gen_id")" && [ -n "$rec" ]; then
            set -- $rec
            if [ "$remote_m" != "$1" ] || [ "$remote_r" != "$2" ]; then
                repoint=1
            fi
        else
            staged_m="$(sha256sum "$staged_dir/manifest.json" | cut -d' ' -f1)"
            if [ "$remote_m" != "$staged_m" ]; then
                repoint=1
            fi
        fi
        if [ "$repoint" -eq 0 ]; then
            append_uploaded_ledger "$gen_id" "$remote_m" "$remote_r"
            write_last_uploaded "$gen_id"
            log_info "Committed generation $gen_id re-validated and acknowledged (digest-bound)"
            prune_local_generations
            prune_old_generations "$base"
            return 0
        fi
        log_error "committed generation $gen_id no longer matches the acknowledged/staged payload (remote wipe or repoint); re-uploading the staged content"
        # Fall through to the normal upload flow: the staged content replaces
        # the repointed/wiped remote payload and is re-acknowledged under the
        # current identity.
    else
        ready_rc=$?
        if [ "$ready_rc" -eq 2 ]; then
            log_error "remote READY/manifest check FAILED for committed $gen_id; marker state UNKNOWN — aborting BEFORE any remote mutation (NOT treated as markerless)"
            return 1
        fi
        # ready_rc == 1: READY CONFIRMED missing/invalid -> the committed
        # payload is not a published snapshot; fall through to the normal
        # upload flow.
    fi

    log_info "Uploading generation $gen_id to $uncommitted"
    if ! rclone copy "$staged_dir" "$uncommitted" --create-empty-src-dirs \
        --config "$RCLONE_CONFIG_FILE"; then
        log_error "upload of $gen_id to the uncommitted namespace failed; nothing committed"
        return 1
    fi

    # Verify the REMOTE DECRYPTED content BEFORE commit: download through the
    # crypt remote and re-run the full validation against the downloaded
    # manifest/entries index. A tampered or partial remote object can never
    # be committed. `--create-empty-src-dirs` is REQUIRED here too: the
    # staged trees carry empty directories (e.g. the PGLite pg_* layout)
    # that are part of the entries-index count; without the flag rclone
    # drops them and the count check fails.
    rm -rf "$VERIFY_TMP"; mkdir -p "$VERIFY_TMP"
    if ! rclone copy "$uncommitted" "$VERIFY_TMP" --create-empty-src-dirs \
        --config "$RCLONE_CONFIG_FILE"; then
        log_error "remote verification download of $gen_id failed; NOT committing"
        return 1
    fi
    if ! validate_generation "$VERIFY_TMP" "$gen_id" "loose"; then
        log_error "remote decrypted content validation failed for $gen_id; NOT committing"
        return 1
    fi

    # Commit the payload: move the verified generation into the committed
    # namespace. rclone move is per-file with an unspecified per-file order,
    # so the READY sentinel is NEVER moved here (explicitly excluded): a
    # partial or interrupted commit cannot leave a READY sentinel in the
    # committed namespace, and committed/<gen> without READY is not a
    # visible/recoverable snapshot. The next run re-commits idempotently.
    # `--create-empty-src-dirs` is required so the moves preserve the empty
    # directories the verification counts.
    log_info "Remote content verified; committing payload of $gen_id"
    if ! rclone move "$uncommitted" "$committed" --exclude "/READY" \
        --create-empty-src-dirs --config "$RCLONE_CONFIG_FILE"; then
        log_error "commit move of $gen_id failed; NOT acknowledged"
        return 1
    fi

    # Verify the COMMITTED payload BEFORE publishing READY: the bytes that
    # will become visible must themselves validate. The READY sentinel is
    # still in the uncommitted namespace at this point (it was validated
    # there before the commit move), so require_ready=0 validates the
    # payload without it; the sentinel is published in the very next step.
    rm -rf "$VERIFY_TMP"; mkdir -p "$VERIFY_TMP"
    if ! rclone copy "$committed" "$VERIFY_TMP" --create-empty-src-dirs \
        --config "$RCLONE_CONFIG_FILE"; then
        log_error "committed payload verification download of $gen_id failed; READY NOT published"
        return 1
    fi
    if ! validate_generation "$VERIFY_TMP" "$gen_id" "loose" "0"; then
        log_error "committed payload validation failed for $gen_id; READY NOT published"
        return 1
    fi

    # Publish READY last: the committed payload is verified and immutable;
    # the sentinel is the only thing that makes it visible/recoverable.
    if ! rclone move "$uncommitted/READY" "$committed/" \
        --config "$RCLONE_CONFIG_FILE"; then
        log_error "commit of $gen_id incomplete: READY sentinel not published; NOT acknowledged"
        return 1
    fi

    # ACK only now: verification + commit both succeeded. The ledger entry
    # is appended FIRST and the `last-uploaded` pointer is written LAST
    # (READY-last): if the process dies between the two writes, the pointer
    # stays stale (pointing at the previous generation) and the next run
    # re-validates the READY-visible committed payload and acknowledges it —
    # the pointer never claims an acknowledgement the ledger does not
    # record, and a published payload is never mutated.
    # The digests bind the ack to the VERIFIED remote payload: the manifest
    # was downloaded and fully validated from the COMMITTED payload above
    # (byte-identical to the remote manifest), and the READY bytes were
    # validated locally and moved to the committed namespace unchanged (the
    # published sentinel is byte-identical to the staged one).
    ack_m="$(sha256sum "$VERIFY_TMP/manifest.json" | cut -d' ' -f1)"
    ack_r="$(sha256sum "$staged_dir/READY" | cut -d' ' -f1)"
    append_uploaded_ledger "$gen_id" "$ack_m" "$ack_r"
    write_last_uploaded "$gen_id"
    log_info "Upload complete and acknowledged: generation $gen_id (manifest $ack_m)"

    # Retention: prune the local staging (ack-based) and the remote
    # committed namespace after inventory validation.
    prune_local_generations
    prune_old_generations "$base"
    return 0
}

# ---------------------------------------------------------------------------
# Retention: keep the newest RETENTION committed generations that carry a
# VALID READY marker bound to the manifest. Prune ONLY after a clean MACHINE
# inventory listing of the committed namespace (rclone `lsjson --dirs-only`
# parsed by the strict shared parser — never the human lsd columns; every
# listed name a valid generation id, every entry a directory) AND only
# generations whose acknowledgement is CONFIRMED against the current remote
# payload right now (digest-bound ledger entry matching the remote
# manifest/READY digests — a wiped/repointed payload never authorizes a
# prune). Incomplete committed dirs (interrupted commits: no READY, invalid
# marker, or unbound manifest) are NEVER counted toward retention, NEVER
# evict valid generations, and NEVER pruned — they are preserved for the
# next idempotent commit. An INDETERMINATE remote READY/manifest read
# failure (rclone transport/auth/backend error, not a confirmed "not found")
# skips the ENTIRE prune with a visible error. Any doubt -> no prune
# (safety over convenience).
# ---------------------------------------------------------------------------

prune_old_generations() {
    base="$1"
    # A FAILED inventory listing is NOT a clean listing: pruning must be
    # skipped (safety over convenience) but the failure must be VISIBLE —
    # silently treating an unreachable remote as "nothing to prune" hides
    # retention failures from the operator. A CONFIRMED absent committed
    # namespace (rclone exit 3, "directory not found") is a clean empty
    # inventory: nothing to prune. (The explicit rc capture is required:
    # `$?` after a negated `if !` condition is the NEGATED status.)
    listing="$(rclone lsjson "${base}/${COMMITTED_NS}" --dirs-only \
        --config "$RCLONE_CONFIG_FILE" 2>&1)" && list_rc=0 || list_rc=$?
    if [ "$list_rc" -ne 0 ]; then
        if [ "$list_rc" -eq 3 ]; then
            log_info "Committed namespace confirmed absent on the remote; nothing to prune"
            return 0
        fi
        log_error "remote committed inventory listing FAILED; pruning skipped: $(printf '%s\n' "$listing" | head -n1)"
        return 0
    fi
    # A zero-byte response is a PROTOCOL failure, never an empty
    # inventory: a successful rclone lsjson always emits at least a valid
    # JSON array (`[]` for an empty namespace). A zero-byte listing cannot
    # be parsed (the strict parser rejects empty input) and must not be
    # mistaken for "nothing to prune" — prune is skipped with a visible
    # error (fail closed, safety over convenience).
    if [ -z "$listing" ]; then
        log_error "remote committed inventory listing returned a ZERO-BYTE response; pruning skipped (an empty inventory is a valid JSON array '[]', not zero bytes)"
        return 0
    fi
    # Strict machine parsing: well-formed lsjson, every entry an object with
    # string Name + boolean IsDir. Anything else is a suspect inventory.
    if ! gens="$(printf '%s\n' "$listing" | awk -f "$LSJSON_PARSER")"; then
        log_error "committed inventory is not strict rclone lsjson output; pruning skipped"
        return 0
    fi
    if [ -z "$gens" ]; then
        return 0
    fi
    # Every entry must be a directory (IsDir=1): a --dirs-only listing with
    # a file entry is suspect.
    if ! printf '%s\n' "$gens" | awk -F '\t' '$2 == "1" { next } { bad = 1 } END { exit (bad ? 1 : 0) }'; then
        log_error "committed inventory contains a non-directory entry; pruning skipped"
        return 0
    fi
    gens="$(printf '%s\n' "$gens" | cut -f1)"
    count="$(printf '%s\n' "$gens" | grep -c . || true)"
    if [ "$count" -le "$RETENTION" ]; then
        return 0
    fi
    for g in $gens; do
        if ! is_valid_generation_id "$g"; then
            log_error "committed inventory contains an invalid name: $g; skipping prune"
            return 0
        fi
    done
    # Only committed dirs with a VALID READY marker bound to the manifest
    # count toward retention and are prune candidates. Incomplete dirs are
    # preserved and logged — they must never consume retention slots. An
    # INDETERMINATE remote read failure (remote_ready_valid rc 2) is never
    # treated as markerless: the marker state is unknown for that
    # generation, so the ENTIRE prune is skipped and the failure is
    # reported (a prune computed on partially-unknown state could evict
    # generations a healthy read would have kept).
    valid_gens=""
    for g in $(printf '%s\n' "$gens" | sort -r); do
        if remote_ready_valid "$g" "${base}/${COMMITTED_NS}/${g}"; then
            valid_gens="$valid_gens$g\n"
        else
            g_rc=$?
            if [ "$g_rc" -eq 2 ]; then
                log_error "remote READY/manifest check FAILED for committed $g; marker state UNKNOWN — skipping the ENTIRE prune (fail closed, NOT markerless)"
                return 0
            fi
            log_info "Preserving incomplete committed dir $g (no valid READY marker bound to the manifest); never counted, never pruned"
        fi
    done
    valid_count="$(printf '%b' "$valid_gens" | grep -c . || true)"
    if [ "$valid_count" -le "$RETENTION" ]; then
        return 0
    fi
    log_info "Retention: keeping the newest $RETENTION of $valid_count committed READY generations"
    keep=0
    for g in $(printf '%b' "$valid_gens"); do
        if [ "$keep" -lt "$RETENTION" ]; then
            keep=$((keep + 1))
            continue
        fi
        # Prune ONLY generations whose acknowledgement is CONFIRMED against
        # the current remote payload RIGHT NOW (digest-bound ledger entry
        # matching the remote manifest/READY digests). A stale ack — remote
        # wiped or repointed — never authorizes a prune; an INDETERMINATE
        # remote read failure skips the ENTIRE prune (fail closed).
        if remote_ack_confirmed "$g" "${base}/${COMMITTED_NS}/${g}"; then
            log_info "Pruning committed generation $g (beyond newest $RETENTION, acknowledged and digest-confirmed against the remote payload)"
            rclone purge "${base}/${COMMITTED_NS}/${g}" --config "$RCLONE_CONFIG_FILE" \
                || log_error "purge of committed/$g failed; re-run later"
            # A generation pruned from committed must not linger in the
            # uncommitted namespace either (empty leftover after the move).
            rclone purge "${base}/${UNCOMMITTED_NS}/${g}" --config "$RCLONE_CONFIG_FILE" \
                >/dev/null 2>&1 || true
        else
            g_rc=$?
            if [ "$g_rc" -eq 2 ]; then
                log_error "remote payload check FAILED for committed $g; marker state UNKNOWN — skipping the ENTIRE prune (fail closed, NOT markerless)"
                return 0
            fi
            log_info "NOT pruning $g: acknowledgement not confirmed against the current remote payload (no digest-bound ledger record, or remote wiped/repointed)"
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# LOCAL staged retention (ack-based): after the remote committed upload was
# acknowledged, prune the LOCAL staging generations beyond the newest
# VAULT_RECOVERY_LOCAL_RETENTION FULL generations. Only generations
# acknowledged in the local ledger (the remote committed them) are ever
# removed, and only through the dedicated writable PRUNE_DIR mount (the
# read-only staging mount is never written). Every candidate generation is
# FULLY validated first (strict id, READY sentinel binding, manifest
# generation_id/schema, entries-index digests, and the exact tree/hashes of
# both trees) — invalid states (invalid directory names, missing/unbound
# READY, manifest mismatch, tampered/missing entries or tree content) SKIP
# the ENTIRE local prune with a visible error: valid old state is never
# removed while any staged state is suspect (any doubt -> no prune).
# `latest` and non-generation artifacts are never touched (non-directory
# entries are skipped, never pruned).
# ---------------------------------------------------------------------------

prune_local_generations() {
    if [ ! -d "$PRUNE_DIR" ]; then
        log_error "Local retention: prune dir not found ($PRUNE_DIR); local pruning skipped"
        return 0
    fi
    gens=""
    for entry in "$PRUNE_DIR"/*; do
        [ -e "$entry" ] || continue
        if [ ! -d "$entry" ]; then
            # `latest` and other artifacts are never pruned.
            continue
        fi
        name="$(basename "$entry")"
        if ! is_valid_generation_id "$name"; then
            log_error "Local retention: invalid directory name in staging: $name; skipping the ENTIRE local prune"
            return 0
        fi
        # FULL local validation before any deletion (council fix): READY
        # sentinel binding (content == generation id), manifest
        # generation_id/schema, entries-index digests, and the exact
        # tree/hashes of BOTH trees must all hold. A single invalid entry
        # SKIPS the ENTIRE local prune — valid old state is never removed
        # while any staged state is suspect (any doubt -> no prune).
        if ! validate_generation "$entry" "$name"; then
            log_error "Local retention: invalid generation entry $name (full validation failed); skipping the ENTIRE local prune"
            return 0
        fi
        gens="$gens$name\n"
    done
    count="$(printf '%b' "$gens" | grep -c . || true)"
    if [ "$count" -le "$LOCAL_RETENTION" ]; then
        return 0
    fi
    log_info "Local retention: keeping the newest $LOCAL_RETENTION of $count staged generations (acknowledged generations beyond that are pruned)"
    base="$(remote_base)"
    keep=0
    for g in $(printf '%b' "$gens" | sort -r); do
        if [ "$keep" -lt "$LOCAL_RETENTION" ]; then
            keep=$((keep + 1))
            continue
        fi
        # A local generation is deleted ONLY when its acknowledgement is
        # CONFIRMED against the CURRENT remote payload right now (digest-
        # bound ledger entry matching the remote manifest/READY digests).
        # A stale ack — remote wiped or repointed — NEVER deletes locally;
        # an INDETERMINATE remote read failure skips the ENTIRE local
        # prune (fail closed, NOT markerless).
        if remote_ack_confirmed "$g" "${base}/${COMMITTED_NS}/${g}"; then
            log_info "Local retention: pruning staged generation $g (beyond newest $LOCAL_RETENTION, acknowledged and digest-confirmed against the remote payload)"
            rm -rf "$PRUNE_DIR/$g" || log_error "Local retention: could not remove $PRUNE_DIR/$g"
        else
            g_rc=$?
            if [ "$g_rc" -eq 2 ]; then
                log_error "remote payload check FAILED for staged $g; marker state UNKNOWN — skipping the ENTIRE local prune (fail closed, NOT markerless)"
                return 0
            fi
            log_info "Local retention: NOT pruning staged generation $g (acknowledgement not confirmed against the current remote payload; remote wipe/repoint never deletes locally)"
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# Run once (shared by daemon and one-shot modes): reconcile the FULL staged
# backlog. Every staged generation NOT acknowledged in the local ledger is
# uploaded and acknowledged, oldest first (generation ids are lexically
# sortable UTC timestamps, so plain sort == chronological order).
#
# Fail-closed contract:
#   - The `latest` pointer is bounded-read and traversal-rejected first;
#     a dangling pointer (its generation neither staged nor acked) aborts.
#   - The staging root is enumerated BEFORE any upload: any directory
#     whose name is not a strict generation id (e.g. a crashed export's
#     leftover, or a tampered entry) ABORTS the whole run — a staging root
#     that cannot be fully accounted for is never partially processed.
#     `latest` and other non-directory artifacts are skipped, never
#     uploaded, never an error.
#   - Acknowledged generations are skipped ONLY after the remote committed
#     payload is re-validated against the ledger-bound manifest/READY
#     digests (remote_ack_confirmed): a remote wipe or repoint invalidates
#     the ack and re-queues the generation for re-upload; an indeterminate
#     remote read failure aborts the run before any mutation. The ledger is
#     only written after verification + commit both succeeded.
#   - The run succeeds (exit 0) ONLY when every unacknowledged staged
#     generation was uploaded, committed and acknowledged. A failure
#     aborts before the next generation (oldest-first order), leaving the
#     remaining backlog untouched; the next run resumes from the same
#     oldest unacknowledged generation (incremental catch-up).
# ---------------------------------------------------------------------------

run_once() {
    latest_file="$STAGING_DIR/latest"
    if [ ! -f "$latest_file" ]; then
        log_info "No latest pointer yet; nothing to upload"
        return 0
    fi
    latest_gen="$(head -c 4096 "$latest_file" | tr -d '\r\n' | head -n1)"
    if [ -z "$latest_gen" ]; then
        log_info "Empty latest pointer; nothing to upload"
        return 0
    fi
    if ! is_valid_generation_id "$latest_gen"; then
        log_error "Invalid latest pointer (rejected to prevent path traversal): $latest_gen"
        return 1
    fi

    # Enumerate the staged generations, failing closed on ANY unrecognized
    # directory name BEFORE any upload.
    staged=""
    for entry in "$STAGING_DIR"/*; do
        [ -e "$entry" ] || continue
        [ -d "$entry" ] || continue
        name="$(basename "$entry")"
        if ! is_valid_generation_id "$name"; then
            log_error "Invalid directory name in staging (aborting backlog reconciliation): $name"
            return 1
        fi
        staged="$staged$name\n"
    done

    # A dangling latest pointer (its generation neither staged nor
    # acknowledged) must never be silently skipped by the backlog scan.
    # An IDENTITY-ONLY ledger entry is NOT enough to report success: the
    # acknowledgement is honored ONLY while the CURRENT remote still holds
    # a committed payload matching the ledger-bound manifest/READY digests
    # (remote_ack_confirmed). A stale identity-only record, a remote wipe,
    # or a repoint leaves the latest generation neither staged (nothing to
    # re-upload) nor confirmable -> the run FAILS CLOSED instead of
    # reporting a false no-op success.
    base="$(remote_base)"
    if [ ! -d "$STAGING_DIR/$latest_gen" ]; then
        if ! ledger_has "$latest_gen"; then
            log_error "Generation dir not found for latest pointer: $STAGING_DIR/$latest_gen"
            return 1
        fi
        if remote_ack_confirmed "$latest_gen" "${base}/${COMMITTED_NS}/${latest_gen}"; then
            : # confirmed against the CURRENT remote payload: no-op success is honest
        else
            latest_rc=$?
            if [ "$latest_rc" -eq 2 ]; then
                log_error "remote payload check FAILED for latest generation $latest_gen; marker state UNKNOWN — aborting (NOT treated as confirmed)"
                return 1
            fi
            log_error "Latest generation $latest_gen is not staged and its acknowledgement is NOT confirmed against the current remote payload (stale identity-only ack, remote wipe, or repoint); refusing to report success"
            return 1
        fi
    fi

    # Upload every staged generation NOT acknowledged in the ledger,
    # oldest first. An acknowledged generation is skipped ONLY when the
    # CURRENT remote still holds a committed payload matching the
    # ledger-bound manifest/READY digests (remote_ack_confirmed): a remote
    # WIPE or REPOINT invalidates the ack and re-queues the generation for
    # re-upload (the ledger is only written after verification + commit
    # both succeeded, so a confirmed ack is always a real prior upload). An
    # INDETERMINATE remote read failure is NEVER treated as a confirmed
    # mismatch: the ack state is unknown, so the run aborts BEFORE any
    # remote mutation (fail closed).
    pending=""
    for g in $(printf '%b' "$staged" | sort); do
        if ! ledger_has "$g"; then
            pending="$pending$g\n"
            continue
        fi
        if remote_ack_confirmed "$g" "${base}/${COMMITTED_NS}/${g}"; then
            continue
        else
            # `$?` is captured in the else branch on purpose: after a failed
            # `if` WITHOUT an else, `$?` is the if-statement's status (0),
            # which would mask an INDETERMINATE ack state and fail open.
            ack_rc=$?
        fi
        if [ "$ack_rc" -eq 2 ]; then
            log_error "remote payload check FAILED for acknowledged generation $g; marker state UNKNOWN — aborting backlog reconciliation BEFORE any remote mutation (NOT treated as markerless)"
            return 1
        fi
        log_info "Acknowledged generation $g no longer matches the remote payload (remote wipe or repoint); re-uploading"
        pending="$pending$g\n"
    done
    pending_count="$(printf '%b' "$pending" | grep -c . || true)"
    if [ "$pending_count" -eq 0 ]; then
        log_info "Latest generation $latest_gen already uploaded; no-op"
        return 0
    fi
    log_info "Reconciling staged backlog: $pending_count unacknowledged generation(s), oldest first"
    for g in $(printf '%b' "$pending"); do
        upload_one "$g" || {
            log_error "Upload of generation $g failed; backlog reconciliation ABORTED (remaining unacknowledged generations untouched)"
            return 1
        }
    done
    return 0
}

acquire_upload_lock() {
    # KERNEL-RELEASED lock: a non-blocking exclusive flock(1) on a regular
    # file in the state volume. The kernel drops the lock automatically when
    # the owning process dies — even SIGKILL — so a container killed
    # mid-upload can never leave a stale lease that blocks future starts.
    # The lock file is NEVER deleted (deleting it while another process
    # holds the lock would break mutual exclusion). The runtime must provide
    # flock(1); without it the uploader fails closed instead of falling back
    # to a lease that could survive process death.
    #
    # NOTE: plain if/then/else only — NO `if ! cmd` here. Under dash's
    # `set -e`, a function called from an inverted condition (`if ! func`)
    # does not get errexit suppression, and a nested `if ! failing-cmd`
    # then kills the whole shell silently instead of running the branch.
    if command -v flock >/dev/null 2>&1; then
        :
    else
        log_error "flock(1) is not available in this runtime; refusing to run without a kernel-released upload lock (fail closed)"
        exit 2
    fi
    # Open (create if needed) the lock file on the dedicated lock fd (9 —
    # see UPLOAD_LOCK_FD; a redirection fd must be literal in POSIX sh);
    # the open file description carries the kernel lock for the process
    # lifetime. A crash releases the lock with the fd.
    #
    # NEVER attach `2>/dev/null` (or any stderr redirection) to this `exec`:
    # redirections on special builtins PERSIST after the command, so it
    # would permanently redirect the whole shell's stderr to /dev/null and
    # silently swallow every later log_error. A redirection failure on the
    # `exec` special builtin is FATAL in POSIX sh: dash exits 2 with its own
    # visible message — fail closed, which is the intent.
    exec 9>"$UPLOAD_LOCK_FILE"
    if flock -n "$UPLOAD_LOCK_FD" 2>/dev/null; then
        _LOCK_HELD="1"
        return 0
    else
        log_error "Upload already in progress (lock: $UPLOAD_LOCK_FILE is held by a live process). The kernel releases the lock automatically when the holder dies; no manual lock removal is ever needed."
        return 1
    fi
}

startup_checks() {
    validate_retention
    validate_local_retention
    validate_poll_interval
    # OAuth-refresh fix: the published seed stays read-only; rclone runs
    # against a private writable active copy in the state volume (preserved
    # across restarts while the seed is unchanged, atomically reseeded when
    # the seed changes). MUST run before require_remote so the remote
    # validation reads the ACTIVE config.
    rclone_active_config_ensure "$RCLONE_CONFIG_FILE" "$STATE_DIR/rclone.active.conf"
    require_remote
    mkdir -p "$STATE_DIR"
    if [ ! -d "$STAGING_DIR" ]; then
        log_error "Staging dir not found at $STAGING_DIR"
        exit 2
    fi
}

main() {
    if [ "$ONCE" = "true" ]; then
        log_info "Uploader one-shot mode (staging=$STAGING_DIR prune=$PRUNE_DIR state=$STATE_DIR remote=$REMOTE_NAME retention=$RETENTION local-retention=$LOCAL_RETENTION)"
        # Fail fast on a malicious/invalid local pointer BEFORE any remote or
        # config interaction (mirrors the recover step's order: validate
        # local input first, then the remote). `startup_checks` calls
        # `require_remote`, which talks to rclone.
        if [ -f "$STAGING_DIR/latest" ]; then
            latest_gen="$(head -c 4096 "$STAGING_DIR/latest" | tr -d '\r\n' | head -n1)"
            if [ -n "$latest_gen" ] && ! is_valid_generation_id "$latest_gen"; then
                log_error "Invalid latest pointer (rejected to prevent path traversal): $latest_gen"
                exit 1
            fi
        fi
        startup_checks
        # NOTE: no `if ! acquire_upload_lock` here — under dash `set -e` a
        # function called from an inverted condition loses errexit
        # suppression inside its body (see acquire_upload_lock).
        if acquire_upload_lock; then
            :
        else
            exit 1
        fi
        run_once
        rc=$?
        exit $rc
    fi

    startup_checks
    log_info "Uploader started (staging=$STAGING_DIR state=$STATE_DIR remote=$REMOTE_NAME retention=$RETENTION poll=${POLL_INTERVAL}s)"
    if [ "$RUN_ON_START" = "true" ]; then
        if acquire_upload_lock; then
            run_once || true
            _cleanup
        fi
    fi
    while true; do
        sleep "$POLL_INTERVAL"
        if acquire_upload_lock; then
            run_once || true
            _cleanup
        fi
    done
}

main "$@"
