#!/bin/sh
# rclone-active-config.sh - shared rclone runtime-config helper
# (OAuth-refresh fix).
#
# PROBLEM
#   rclone must be able to PERSIST an OAuth token refresh back into its
#   config file (e.g. a Google Drive `token = {...}` whose access token
#   expired mid-run). The published seed (the `obsidian-rclone-config`
#   volume) is mounted READ-ONLY, so rclone cannot rewrite it and the
#   refresh fails at runtime.
#
# DESIGN
#   Every consumer runs rclone against a PRIVATE WRITABLE ACTIVE copy of the
#   config instead of the read-only seed:
#     - long-running uploaders keep the active copy in their OWN state
#       volume (persistent across container restarts, so a refreshed token
#       survives restarts);
#     - short-lived recover steps keep it in an ephemeral private directory
#       (never inside the recovery handoff volume).
#   The published seed stays READ-ONLY and is NEVER modified.
#
# CONTRACT (rclone_active_config_ensure <seed_path> <active_path>)
#   1. The seed is never written; the active copy is created with mode 0600.
#   2. Seeding is ATOMIC: the seed is copied to a temp file in the active
#      directory, chmod 600, then `mv` (rename) over the active path — a
#      crash can never leave a partial active config.
#   3. The "seed changed?" test compares the CURRENT seed against a recorded
#      SEED FINGERPRINT (sha256 of the seed bytes stored in a private 0600
#      sidecar next to the active copy) — NEVER against the active config:
#      rclone may have rewritten the active copy in place to persist a
#      refreshed OAuth token, so seed-vs-active comparison would see a
#      "difference" after every refresh and wrongly discard it.
#   4. When the seed fingerprint is UNCHANGED, the active copy is PRESERVED:
#      a refreshed OAuth token survives restarts (the whole point of the
#      fix). Only a missing fingerprint (first run) or a CHANGED fingerprint
#      (operator rotated the secret) triggers an atomic reseed, so a stale
#      refreshed token is never used with new credentials.
#   5. The comparison NEVER prints config content or hashes (secrets must
#      not leak into logs); the fingerprint is only stored privately.
#   6. On success, RCLONE_CONFIG and RCLONE_CONFIG_FILE are exported to the
#      active path, so every subsequent `rclone --config "$RCLONE_CONFIG_FILE"`
#      invocation (and every rclone process inheriting RCLONE_CONFIG) runs
#      against the writable active copy.
#
# The caller MUST define log_info()/log_error() before sourcing this file
# (the uploader/recover scripts already do). POSIX sh only (dash-compatible;
# no `local`). Failures exit 2 (config/validation error convention).

# rclone_active_config_ensure <seed_path> <active_path>
#   Seeds (or preserves) the private active config and repoints
#   RCLONE_CONFIG / RCLONE_CONFIG_FILE at it.
rclone_active_config_ensure() {
    _rac_seed="$1"
    _rac_active="$2"
    if [ -z "$_rac_seed" ] || [ -z "$_rac_active" ]; then
        log_error "rclone_active_config_ensure: seed and active config paths are required"
        exit 2
    fi
    if [ ! -f "$_rac_seed" ]; then
        log_error "rclone config seed not found at $_rac_seed"
        exit 2
    fi
    _rac_dir="$(dirname "$_rac_active")"
    if ! mkdir -p "$_rac_dir"; then
        log_error "cannot create active config directory: $_rac_dir"
        exit 2
    fi
    # The seed fingerprint sidecar records WHICH seed bytes the active copy
    # was seeded from (0600, atomic). The current seed is compared against
    # this record — never against the active config (which rclone may have
    # rewritten to persist a refreshed OAuth token).
    _rac_fp="$_rac_active.seed-fp"
    _rac_seed_hash="$(sha256sum "$_rac_seed" | cut -d' ' -f1)"
    if [ -f "$_rac_fp" ] && [ -f "$_rac_active" ] \
        && [ "$(cat "$_rac_fp" 2>/dev/null || true)" = "$_rac_seed_hash" ]; then
        # Seed unchanged (same fingerprint as the last seeding): PRESERVE
        # the active copy, whatever rclone did to it — a refreshed OAuth
        # token must survive restarts.
        chmod 600 "$_rac_active" 2>/dev/null || true
        log_info "rclone active config preserved: the published seed is unchanged (refreshed OAuth tokens retained)"
    else
        # First run (no fingerprint), active copy missing, or the seed
        # CHANGED (operator rotation): atomically reseed (temp file +
        # rename, never in place). No content or hash of either config is
        # ever logged.
        _rac_tmp="$_rac_dir/.rclone-active-config.tmp.$$"
        if ! cp "$_rac_seed" "$_rac_tmp" 2>/dev/null; then
            log_error "cannot stage the rclone active config at $_rac_tmp"
            rm -f "$_rac_tmp"
            exit 2
        fi
        chmod 600 "$_rac_tmp"
        if ! mv -f "$_rac_tmp" "$_rac_active"; then
            log_error "cannot install the rclone active config at $_rac_active"
            rm -f "$_rac_tmp"
            exit 2
        fi
        # Record the fingerprint of the seed this active copy was seeded
        # from (atomic, 0600), AFTER the active install: a crash between
        # the two leaves a stale/missing fingerprint, which only forces a
        # harmless reseed on the next run.
        _rac_fp_tmp="$_rac_fp.tmp.$$"
        if printf '%s\n' "$_rac_seed_hash" > "$_rac_fp_tmp" 2>/dev/null; then
            chmod 600 "$_rac_fp_tmp"
            mv -f "$_rac_fp_tmp" "$_rac_fp" 2>/dev/null || rm -f "$_rac_fp_tmp"
        fi
        log_info "rclone active config (re)seeded from the published seed"
    fi
    RCLONE_CONFIG="$_rac_active"
    RCLONE_CONFIG_FILE="$_rac_active"
    export RCLONE_CONFIG RCLONE_CONFIG_FILE
}
