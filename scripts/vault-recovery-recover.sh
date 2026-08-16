#!/bin/sh
# vault-recovery-recover.sh - Operator recovery DOWNLOAD step (Phase 2).
#
# Short-lived, least-privilege rclone step (rclone image + crypt config),
# NEVER in Hermes:
#   - NEVER mounts hermes-data or /opt/data.
#   - The published rclone crypt config (obsidian-rclone-config volume) is
#     READ-ONLY: rclone runs against an EPHEMERAL PRIVATE writable copy of
#     the config in a fresh temp dir (OAuth-refresh fix,
#     rclone-active-config.sh) — never inside the recovery handoff volume,
#     removed on exit; the seed itself is never modified.
#   - Only the disposable recovery handoff volume is writable.
#   - Only COMMITTED remote generations are listable/recoverable.
#
# Commands:
#   list-remote             - list committed remote generations that carry a
#                             VALID READY marker bound to the manifest
#                             (READY content == generation id == manifest
#                             generation_id). The inventory is read as
#                             MACHINE JSON (`rclone lsjson --dirs-only`,
#                             parsed by the shared strict
#                             vault-recovery-lsjson.awk — never the
#                             human-readable `lsd` columns) and every
#                             manifest binding is validated against the
#                             strict full schema (unknown keys + doctor
#                             metadata). Markerless or invalid committed
#                             dirs (interrupted commits) are invisible. An
#                             INDETERMINATE remote READY/manifest read
#                             failure (rclone transport/auth/backend error,
#                             not a confirmed "not found") FAILS the listing
#                             closed — a possibly-valid generation is never
#                             hidden as if it were markerless.
#   download <gen-id>       - download one committed generation into the
#                             disposable recovery dir, FULLY validate it
#                             (strict gen id + READY + manifest + entries
#                             index digest + full tree/hashes), then write
#                             the RECOVERY_READY handoff sentinel. The remote
#                             READY marker bound to the manifest is queried
#                             and validated BEFORE any payload transfer — a
#                             markerless/invalid committed dir is refused
#                             up front, and an INDETERMINATE remote read
#                             failure is refused the same way but reported
#                             as an error, not as markerless. Any
#                             failure/partial download leaves NO sentinel
#                             and is never considered a snapshot.
#
# Env:
#   VAULT_RECOVERY_RCLONE_REMOTE - REQUIRED crypt remote name
#   VAULT_RECOVERY_RCLONE_PATH   - remote base path (default Josemar/vault-recovery)
#   VAULT_RECOVERY_RECOVERY_DIR  - recovery handoff dir (default /recovery)
#   RCLONE_CONFIG                - rclone config SEED path (default
#                                  /config/rclone/rclone.conf; the published
#                                  read-only config). The recover step runs
#                                  rclone against an ephemeral private
#                                  writable copy (OAuth-refresh fix).
#
# Exit codes: 0 success, 2 validation/known error, 3 unexpected error.

set -eu

REMOTE_NAME="${VAULT_RECOVERY_RCLONE_REMOTE:-}"
REMOTE_PATH="${VAULT_RECOVERY_RCLONE_PATH:-Josemar/vault-recovery}"
RECOVERY_DIR="${VAULT_RECOVERY_RECOVERY_DIR:-/recovery}"
RCLONE_CONFIG_FILE="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
COMMITTED_NS="committed"
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

log_info() { echo "[vault-recovery-recover] $1"; }
log_error() { echo "[vault-recovery-recover] ERROR: $1" >&2; }

# Shared rclone OAuth-refresh runtime helper (see rclone-active-config.sh):
# rclone runs against a private writable ACTIVE copy of the config, never
# the read-only seed.
. "$(dirname "$0")/rclone-active-config.sh"

# OAuth-refresh fix (recover lane): the active copy lives in an EPHEMERAL
# PRIVATE temp dir created on first use and removed on exit — never inside
# the recovery handoff volume (which is handed off to Hermes-side
# verify/install steps without any rclone credentials).
_ACTIVE_CONFIG_DIR=""
prepare_active_config() {
    if [ -z "$_ACTIVE_CONFIG_DIR" ]; then
        _ACTIVE_CONFIG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vault-recovery-rclone.XXXXXX")"
        trap 'rm -rf "$_ACTIVE_CONFIG_DIR"' EXIT
        trap 'rm -rf "$_ACTIVE_CONFIG_DIR"; exit 143' INT TERM
    fi
    rclone_active_config_ensure "$RCLONE_CONFIG_FILE" "$_ACTIVE_CONFIG_DIR/rclone.conf"
}

require_remote() {
    # Prepare the ephemeral private writable config BEFORE validating the
    # remote, so the validation reads the ACTIVE config.
    prepare_active_config
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
        log_error "Remote '$REMOTE_NAME' is not rclone type 'crypt' (got: '${remote_type:-missing}'). Recovery refuses to download from a non-crypt remote."
        exit 2
    fi
    if [ -z "$underlying" ]; then
        log_error "Remote '$REMOTE_NAME' is crypt but has an EMPTY underlying remote; refusing recovery"
        exit 2
    fi
    if [ -z "$password" ]; then
        log_error "Remote '$REMOTE_NAME' is crypt but has an EMPTY password; refusing recovery"
        exit 2
    fi
    # Metadata-encryption standard: `standard` filename encryption (never
    # `off`/`obfuscate`) and directory-name encryption enabled. An absent
    # filename_encryption means rclone's default `standard`; an absent
    # directory_name_encryption means rclone's default `true`.
    if [ -n "$filename_encryption" ] && [ "$filename_encryption" != "standard" ]; then
        log_error "Remote '$REMOTE_NAME' filename_encryption is '${filename_encryption}' (must be 'standard'): plaintext file names would leak in the ciphertext metadata; refusing recovery"
        exit 2
    fi
    if [ "$directory_name_encryption" = "false" ]; then
        log_error "Remote '$REMOTE_NAME' directory_name_encryption is 'false' (must be 'true'): plaintext directory names would leak in the ciphertext metadata; refusing recovery"
        exit 2
    fi
    log_info "Remote '$REMOTE_NAME' validated as type 'crypt' (underlying + password set, standard filename + directory-name encryption)"
}

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
# A committed generation is a recoverable snapshot ONLY when its remote
# READY sentinel exists, its content equals the generation id, and the
# remote manifest's generation_id binds to the same id. Markerless or
# invalid committed dirs are interrupted commits: list-remote makes them
# invisible, download refuses them, and the uploader never mutates a
# payload once this marker is visible.
#
# Tri-state result — callers MUST distinguish, never treat as a boolean:
#   0 - READY valid and manifest-bound (confirmed published snapshot).
#   1 - READY CONFIRMED missing/invalid: rclone reported file/directory
#       not found (exit 3/4), the marker content does not match the
#       generation id, or the manifest does not bind to it.
#   2 - INDETERMINATE: the rclone cat itself FAILED (transport, auth,
#       backend, or uncategorised error: any other non-zero exit). The
#       marker state is UNKNOWN and must never be treated as markerless.
#       Callers must fail closed: list-remote refuses the listing and
#       download refuses any payload transfer.
#
# Real rclone exits 3 (directory not found) / 4 (file not found) when the
# object is confirmed absent; every other non-zero exit code means the
# read itself failed. The rclone stderr is captured SEPARATELY from the
# parsed stdout (see rclone_machine above) so the failure reason is
# visible in the logs instead of being silenced — without contaminating
# the sentinel/manifest bytes the strict validation consumes.

# ---------------------------------------------------------------------------
# Machine-readable rclone calls: parser input is STDOUT ONLY
# ---------------------------------------------------------------------------
#
# The READY/manifest `cat` and `lsjson` inventory calls feed strict machine
# parsers. rclone writes human-oriented status text to STDERR (e.g.
# periodic transfer stats when the operator sets RCLONE_STATS=30s /
# RCLONE_STATS_ONE_LINE=true / RCLONE_STATS_LOG_LEVEL=NOTICE, plus
# backend/transport diagnostics). Merging stderr into stdout with `2>&1`
# contaminates the parsed payload (sentinel line, manifest JSON, lsjson
# array) and causes fail-closed FALSE rejects BEFORE any transfer.
# rclone_machine therefore keeps the streams apart: stdout is captured by
# the caller via command substitution (parser input), stderr goes to a
# private scratch file inside the ephemeral active-config dir (removed by
# the EXIT trap). The caller surfaces that stderr in the logs ONLY when the
# command FAILS (non-zero) — the LAST line, which is the final ERROR line
# even when periodic status text preceded it — so benign status text never
# pollutes parsing while real failure reasons stay visible.
# (Invariant: require_remote -> prepare_active_config runs before every
# machine call, so $_ACTIVE_CONFIG_DIR is always set here.)
rclone_machine() {
    _rm_err="$1"
    shift
    "$@" 2>"$_rm_err"
}

remote_ready_valid() {
    gen_id="$1"
    gen_remote="$2"
    ready="$(rclone_machine "$_ACTIVE_CONFIG_DIR/ready.stderr" \
        rclone cat "$gen_remote/READY" --config "$RCLONE_CONFIG_FILE")" && rc=0 || rc=$?
    if [ "$rc" -ne 0 ]; then
        case "$rc" in
            3|4) return 1 ;;
            *)
                _diag="$(cat "$_ACTIVE_CONFIG_DIR/ready.stderr" 2>/dev/null | tail -n1)"
                [ -n "$_diag" ] || _diag="$(printf '%s' "$ready" | head -n1)"
                log_error "remote READY read FAILED for $gen_remote (rclone exit $rc): $_diag"
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
    manifest="$(rclone_machine "$_ACTIVE_CONFIG_DIR/manifest.stderr" \
        rclone cat "$gen_remote/manifest.json" --config "$RCLONE_CONFIG_FILE")" && mrc=0 || mrc=$?
    if [ "$mrc" -ne 0 ]; then
        case "$mrc" in
            3|4) return 1 ;;
            *)
                _diag="$(cat "$_ACTIVE_CONFIG_DIR/manifest.stderr" 2>/dev/null | tail -n1)"
                [ -n "$_diag" ] || _diag="$(printf '%s' "$manifest" | head -n1)"
                log_error "remote manifest read FAILED for $gen_remote (rclone exit $mrc): $_diag"
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

# Bounded first-line read for sentinel files (READY, RECOVERY_READY): a
# sentinel is machine-written with a few short lines; anything larger is
# suspect and refused (truncated reads would fail the exact-match checks
# anyway, but the bound also prevents unbounded input processing).
read_first_line_bounded() {
    file="$1"
    max="${2:-4096}"
    head -c "$max" "$file" | tr -d '\r\n' | head -n1
}

remote_base() {
    remote_path_clean="${REMOTE_PATH%/}"
    if [ -n "$remote_path_clean" ]; then
        printf '%s:%s' "$REMOTE_NAME" "$remote_path_clean"
    else
        printf '%s:' "$REMOTE_NAME"
    fi
}

# ---------------------------------------------------------------------------
# FULL validation of a downloaded generation (mirrors the uploader contract:
# strict gen id, READY, manifest, entries digest, full tree/hashes).
# ---------------------------------------------------------------------------

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
    tmp_hashes="$(mktemp)"
    tmp_stats="$(mktemp)"
    tmp_dirstats="$(mktemp)"
    : > "$tmp_hashes"; : > "$tmp_stats"; : > "$tmp_dirstats"
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
        rm -f "$tmp_hashes" "$tmp_stats" "$tmp_dirstats"
        return 1
    fi

    awk_diag="$(awk -F '\t' -v MODE_CHECK="$mode_check" '
        NR == FNR { e_mode[$5] = $2; e_size[$5] = $3; e_sha[$5] = $4; next }
        FNR == 1 && NR > 1 { phase++ }
        phase == 1 {
            if (!($2 in e_sha)) { print "extra file on disk: " $2; bad = 1; next }
            if (e_sha[$2] != $1) { print "sha256 mismatch for " $2; bad = 1 }
            next
        }
        phase == 2 {
            if (!($3 in e_sha)) { print "extra file stat: " $3; bad = 1; next }
            if (MODE_CHECK == "modes" && e_mode[$3] != $1) { print "mode mismatch for " $3; bad = 1 }
            if (e_size[$3] != $2) { print "size mismatch for " $3; bad = 1 }
        }
        END { exit (bad ? 1 : 0) }
    ' "$entries_file" "$tmp_hashes" "$tmp_stats")" || true
    if [ -n "$awk_diag" ]; then
        log_error "file validation failed for $tree_root:"
        log_error "$awk_diag"
        rm -f "$tmp_hashes" "$tmp_stats" "$tmp_dirstats"
        return 1
    fi

    awk_diag="$(awk -F '\t' -v MODE_CHECK="$mode_check" '
        NR == FNR { e_type[$5] = $1; e_mode[$5] = $2; next }
        FNR == 1 && NR > 1 { phase++ }
        phase == 1 {
            if (!($2 in e_type)) { print "extra directory on disk: " $2; bad = 1; next }
            if (e_type[$2] != "dir") { print "type mismatch (expected dir): " $2; bad = 1 }
            if (MODE_CHECK == "modes" && e_mode[$2] != $1) { print "mode mismatch for dir " $2; bad = 1 }
        }
        END { exit (bad ? 1 : 0) }
    ' "$entries_file" "$tmp_dirstats")" || true
    if [ -n "$awk_diag" ]; then
        log_error "directory validation failed for $tree_root:"
        log_error "$awk_diag"
        rm -f "$tmp_hashes" "$tmp_stats" "$tmp_dirstats"
        return 1
    fi
    rm -f "$tmp_hashes" "$tmp_stats" "$tmp_dirstats"
    return 0
}

validate_generation() {
    gen_dir="$1"
    gen_id="$2"
    if [ ! -f "$gen_dir/READY" ]; then
        log_error "downloaded generation $gen_id missing READY sentinel"
        return 1
    fi
    ready="$(read_first_line_bounded "$gen_dir/READY")"
    if [ "$ready" != "$gen_id" ]; then
        log_error "READY generation mismatch: READY=$ready dir=$gen_id"
        return 1
    fi
    manifest="$gen_dir/manifest.json"
    if [ ! -f "$manifest" ]; then
        log_error "downloaded generation $gen_id missing manifest"
        return 1
    fi
    # Strict JSON well-formedness (council fix): the manifest must be REAL
    # JSON before any field is extracted. A malformed document whose
    # required fields are grep-visible is rejected here — the Python
    # restore core would reject it too, so it must never be handed off as
    # a recoverable snapshot.
    if ! awk -f "$JSON_VALIDATOR" "$manifest" 2>/dev/null; then
        log_error "downloaded manifest.json of $gen_id is not well-formed JSON; refusing recovery"
        return 1
    fi
    # Strict FULL-SCHEMA validation (council fix): the manifest must match
    # the Python-authoritative schema EXACTLY — unknown keys anywhere,
    # wrong types, invalid digests, or broken doctor metadata reject the
    # bundle the same way the restore core would. Only the validator's
    # output is used for field extraction (no raw greps).
    if ! schema_out="$(manifest_schema_validate "$manifest")"; then
        log_error "downloaded manifest.json of $gen_id does not match the strict manifest schema; refusing recovery"
        return 1
    fi
    manifest_gen="$(schema_field "$schema_out" "generation_id")"
    if [ "$manifest_gen" != "$gen_id" ]; then
        log_error "manifest generation_id mismatch: manifest=$manifest_gen dir=$gen_id"
        return 1
    fi
    schema="$(schema_field "$schema_out" "schema_version")"
    if [ "$schema" != "1" ]; then
        log_error "manifest schema_version is '${schema:-missing}' (expected 1); refusing recovery"
        return 1
    fi
    for tree in vault .gbrain; do
        entries_file="$gen_dir/$tree.entries.txt"
        if [ ! -f "$entries_file" ]; then
            log_error "downloaded generation $gen_id has no entries index for tree '$tree'; refusing recovery"
            return 1
        fi
        expected_digest="$(schema_field "$schema_out" "entries_digest" "$tree")"
        if [ -z "$expected_digest" ]; then
            log_error "manifest has no entries_digest for tree '$tree'; refusing recovery"
            return 1
        fi
        # Strict format check on the bound digest: exactly 64 lowercase hex
        # chars (the sha256 of the entries index the validator will rehash).
        if ! printf '%s' "$expected_digest" | grep -Eq '^[0-9a-f]{64}$'; then
            log_error "manifest entries_digest for tree '$tree' is not a 64-hex sha256: '$expected_digest'; refusing recovery"
            return 1
        fi
        actual_digest="$(sha256sum "$entries_file" | cut -d' ' -f1)"
        if [ "$actual_digest" != "$expected_digest" ]; then
            log_error "entries index digest mismatch for tree '$tree': manifest=$expected_digest file=$actual_digest"
            return 1
        fi
        # Path containment: every entry path must stay inside the recovery
        # handoff. The generation id is strictly validated (no slash/..), so
        # the resolved bundle dir is bounded by construction; assert it.
        case "$gen_dir" in
            "$RECOVERY_DIR"|"$RECOVERY_DIR"/*) ;;
            *)
                log_error "resolved generation path escapes recovery dir: $gen_dir"
                return 1
                ;;
        esac
        if ! validate_tree "$gen_dir/$tree" "$entries_file" "loose"; then
            return 1
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_list_remote() {
    require_remote
    base="$(remote_base)"
    # A FAILED inventory listing must fail closed: reporting "no committed
    # generations" when the remote is unreachable would be a false negative
    # on backup existence. Only a CLEAN listing that happens to be empty is
    # reported as empty. A CONFIRMED absent committed namespace (rclone
    # exit 3, "directory not found") IS a clean empty inventory. (The
    # explicit rc capture is required: `$?` after a negated `if !`
    # condition is the NEGATED status.) The lsjson payload is parsed from
    # stdout ONLY — rclone status text on stderr (rclone_machine) must
    # never reach the strict inventory parser.
    listing="$(rclone_machine "$_ACTIVE_CONFIG_DIR/lsjson.stderr" \
        rclone lsjson "${base}/${COMMITTED_NS}" --dirs-only \
        --config "$RCLONE_CONFIG_FILE")" && list_rc=0 || list_rc=$?
    if [ "$list_rc" -ne 0 ]; then
        if [ "$list_rc" -eq 3 ]; then
            log_info "No committed remote generations"
            exit 0
        fi
        _diag="$(cat "$_ACTIVE_CONFIG_DIR/lsjson.stderr" 2>/dev/null | tail -n1)"
        [ -n "$_diag" ] || _diag="$(printf '%s\n' "$listing" | head -n1)"
        log_error "remote inventory listing FAILED: $_diag"
        exit 2
    fi
    # A zero-byte response is a PROTOCOL failure, never an empty
    # inventory: a successful rclone lsjson always emits at least a valid
    # JSON array (`[]` for an empty namespace). Reporting "no committed
    # generations" on a zero-byte response would be a false negative on
    # backup existence — the listing fails closed instead.
    if [ -z "$listing" ]; then
        log_error "remote inventory listing returned a ZERO-BYTE response; refusing to list (an empty inventory is a valid JSON array '[]', not zero bytes)"
        exit 2
    fi
    # Strict MACHINE parsing (rclone lsjson -> shared strict parser; never
    # the human-readable lsd columns): the inventory must be well-formed
    # lsjson with every entry an object carrying string Name + boolean
    # IsDir — anything else is a suspect listing and fails closed.
    if ! gens="$(printf '%s\n' "$listing" | awk -f "$LSJSON_PARSER")"; then
        log_error "committed inventory is not strict rclone lsjson output; refusing to list"
        exit 2
    fi
    if [ -z "$gens" ]; then
        log_info "No committed remote generations"
        exit 0
    fi
    # Every entry must be a directory (IsDir=1): a --dirs-only listing with
    # a file entry is suspect.
    if ! printf '%s\n' "$gens" | awk -F '\t' '$2 == "1" { next } { bad = 1 } END { exit (bad ? 1 : 0) }'; then
        log_error "committed inventory contains a non-directory entry; refusing to list"
        exit 2
    fi
    gens="$(printf '%s\n' "$gens" | cut -f1)"
    # Every listed name MUST be a valid generation id, otherwise the
    # inventory is suspect -> fail closed.
    for g in $gens; do
        if ! is_valid_generation_id "$g"; then
            log_error "committed inventory contains an invalid name: $g; refusing to list"
            exit 2
        fi
    done
    # Only committed generations carrying a VALID READY marker bound to the
    # manifest are listable/recoverable. Markerless or invalid committed
    # dirs (interrupted commits) are INVISIBLE: they are never listed and
    # never fail the listing — they are expected transient states. An
    # INDETERMINATE remote READY/manifest read failure is the opposite:
    # the marker state is UNKNOWN, so the listing FAILS CLOSED — a
    # possibly-valid generation must never be hidden as if it were
    # markerless (that would be a false negative on backup existence).
    valid_gens=""
    for g in $(printf '%s\n' "$gens" | sort -r); do
        if remote_ready_valid "$g" "${base}/${COMMITTED_NS}/${g}"; then
            valid_gens="$valid_gens$g\n"
        else
            g_rc=$?
            if [ "$g_rc" -eq 2 ]; then
                log_error "remote READY/manifest check FAILED for committed $g; marker state UNKNOWN — refusing to list (fail closed, NOT markerless)"
                exit 2
            fi
            log_info "Committed dir $g not listed: no valid READY marker bound to the manifest"
        fi
    done
    if [ -z "$valid_gens" ]; then
        log_info "No committed remote generations"
        exit 0
    fi
    printf '%b' "$valid_gens"
    exit 0
}

cmd_download() {
    gen_id="${1:-}"
    if [ -z "$gen_id" ]; then
        log_error "download requires a generation id: vault-recovery-recover.sh download <gen-id>"
        exit 2
    fi
    if ! is_valid_generation_id "$gen_id"; then
        log_error "Invalid generation id (rejected to prevent path traversal): $gen_id"
        exit 2
    fi
    require_remote

    # The disposable recovery volume is consumed by short-lived HERMES runs
    # (uid HERMES_UID, default 10000) for the verify/install steps, which
    # must WRITE the VERIFIED_READY handoff into it. This container runs as
    # root (rclone image default), so chown the volume root to that uid when
    # it already exists; a non-root invocation skips the chown (the volume
    # must then already be writable by the hermes runtime user).
    if [ "$(id -u)" = "0" ] && [ -d "$RECOVERY_DIR" ]; then
        chown "${HERMES_UID:-10000}:${HERMES_GID:-10000}" "$RECOVERY_DIR" 2>/dev/null \
            || log_info "could not chown $RECOVERY_DIR to HERMES_UID; verify/install must run with write access to it"
    fi

    # Clear the disposable handoff volume so a stale partial download from a
    # previous recovery attempt can never be mistaken for a fresh one.
    if [ -d "$RECOVERY_DIR" ]; then
        for entry in "$RECOVERY_DIR"/* "$RECOVERY_DIR"/.[!.]*; do
            [ -e "$entry" ] || continue
            rm -rf "$entry"
        done
    fi
    mkdir -p "$RECOVERY_DIR/$gen_id"

    base="$(remote_base)"
    src="${base}/${COMMITTED_NS}/${gen_id}"
    # READY-marker pre-check: a committed generation is recoverable ONLY
    # when its remote READY sentinel exists and is bound to the manifest
    # (content == generation id == manifest generation_id). Markerless or
    # invalid committed dirs are interrupted commits: they are refused
    # BEFORE any payload transfer, so a partial commit is never downloaded.
    # An INDETERMINATE remote read failure (rclone transport/auth/backend
    # error, rc 2) is refused the same way but is NOT reported as
    # markerless: the marker state is unknown, so the refusal is explicit
    # (fail closed, no payload transfer).
    if remote_ready_valid "$gen_id" "$src"; then
        ready_rc=0
    else
        ready_rc=$?
    fi
    if [ "$ready_rc" -ne 0 ]; then
        if [ "$ready_rc" -eq 2 ]; then
            log_error "remote READY/manifest check FAILED for committed $gen_id; marker state UNKNOWN — refusing download (fail closed, NOT markerless)"
        else
            log_error "committed generation $gen_id has no valid READY marker bound to the manifest; refusing download"
        fi
        exit 2
    fi
    log_info "Downloading committed generation $gen_id from $src"
    # `--create-empty-src-dirs` is REQUIRED: the staged trees carry empty
    # directories (e.g. the PGLite pg_* layout) that are part of the
    # entries-index count; without the flag rclone drops them and the
    # validation fails.
    if ! rclone copy "$src" "$RECOVERY_DIR/$gen_id" --create-empty-src-dirs \
        --config "$RCLONE_CONFIG_FILE"; then
        log_error "rclone download of $gen_id failed; no RECOVERY_READY written"
        exit 2
    fi
    if ! validate_generation "$RECOVERY_DIR/$gen_id" "$gen_id"; then
        log_error "downloaded generation $gen_id failed validation; no RECOVERY_READY written"
        exit 2
    fi
    manifest_sha="$(sha256sum "$RECOVERY_DIR/$gen_id/manifest.json" | cut -d' ' -f1)"
    # Write the handoff sentinel ONLY after full validation passed.
    printf '%s\n%s\n' "$gen_id" "$manifest_sha" > "$RECOVERY_DIR/RECOVERY_READY"
    log_info "Recovery handoff ready: generation $gen_id (manifest sha256 $manifest_sha)"
    exit 0
}

usage() {
    cat <<EOF
Usage: vault-recovery-recover.sh <command>

Commands:
  list-remote
      List committed remote generations that carry a VALID READY marker
      bound to the manifest (READY content == generation id == manifest
      generation_id). Markerless/invalid committed dirs (interrupted
      commits) are invisible; any invalid inventory NAME fails closed.

  download <generation-id>
      Download one committed generation into the disposable recovery dir,
      fully validate it (READY, manifest, entries digest, full tree/hashes,
      path containment) and write RECOVERY_READY. The remote READY marker
      bound to the manifest is validated BEFORE any payload transfer.
      Partial or failed downloads never produce a sentinel.
EOF
}

main() {
    if [ "$#" -lt 1 ]; then
        usage
        exit 2
    fi
    cmd="$1"
    shift
    case "$cmd" in
        list-remote) cmd_list_remote ;;
        download) cmd_download "$@" ;;
        -h|--help|help) usage; exit 0 ;;
        *) log_error "Unknown command: $cmd"; usage; exit 2 ;;
    esac
}

main "$@"
