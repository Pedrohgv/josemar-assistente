"""Source/structure contract tests for the Mnemosyne deploy-mode integration
in `.github/workflows/deploy-to-home-server.yml` and the backup compose overlay.

These tests are pure-source: they parse the workflow YAML and the compose
overlay text without executing GitHub Actions or any Docker lifecycle action.
They cover the remediated behaviors from the architecture review:

  - default-off / backward compatibility (no Mnemosyne overlays when unset),
  - allowed/invalid mode behavior (off/pilot/backup accepted; others rejected
    before any volume mutation or teardown),
  - backup prerequisites (positive-integer export interval, no leading zeros,
    <= 10080 minutes, + RCLONE_CONFIG_B64),
  - validation before state mutation (COMPOSE_FILE derivation, compose config
    --quiet, rclone decode+validation all precede any docker volume create /
    volume write / service teardown),
  - required compose ordering with and without browser-control
    (base; vault-recovery always; optional browser-control; embeddings;
    mnemosyne; backup last),
  - no recovery profile enabled for a normal deploy,
  - maximal fail-closed no-volume teardown (superset overlays incl.
    vault-recovery, aux-ml profile, no `-v`, no `|| true`, no `set +e`,
    named volumes preserved, removes prior overlay services when switching
    to off),
  - rclone digest pin + direct invocation (no `sh -c`), FOUR independent
    crypt field checks (type, remote, password + the metadata-encryption
    standard: filename_encryption `standard`, directory_name_encryption
    enabled), hardcoded vault-recovery-crypt (validated on EVERY
    deployment, independent of MNEMOSYNE_DEPLOY_MODE) and mnemosyne-crypt
    (backup mode only); RCLONE_CONFIG_B64 required for every deployment
    (Phase 3: fail rather than silently lose backups),
  - rclone OAuth-refresh fix: the decoded config lives in a disposable
    temp DIRECTORY (mode-0600 file) mounted WRITABLE into the probe
    containers, because rclone refreshes the working config via sibling
    temp file + atomic rename (impossible with a read-only single-file
    mount); every writable temp-dir rclone container runs as the host
    runner UID:GID (`--user "$(id -u):$(id -g)"`) so a rewritten config
    stays runner-owned and host-readable for the publish step's checksum
    (live ownership regression); every cleanup owner recursively
    removes the temp directory on every exit/cancellation, and when
    rclone changes the working config during the probes the publish step
    detects it via the pre-probe checksum and publishes the UPDATED file
    atomically,
  - Oracle upgrade-migration blocker: after old services are stopped and
    before Hermes/new services start, a fail-closed step migrates legacy
    deployments that retain `/state/rclone.active.conf` and
    `/state/rclone.active.conf.seed-fp` in the persistent
    mnemosyne-backup-state volume (mounted read-only into Hermes): it
    deletes ONLY those two exact legacy files at the volume root (never
    the ledger/slot acknowledgement state), resolves the Compose project
    name from Compose's rendered config metadata (honoring
    COMPOSE_PROJECT_NAME, never `basename "$PWD"`) and locates the volume
    by project + `com.docker.compose.volume` label — 0 matches is the
    only safe skip, while a Docker list/inspect operational failure or >1
    matches fails the deploy; it never prints secrets and fails the
    deploy when the removal does not happen,
  - override-safe seed publish: the shared obsidian-rclone-config seed
    volume is resolved from Compose's rendered config metadata + the
    `com.docker.compose.volume` label (honoring COMPOSE_PROJECT_NAME /
    `-p`, never `basename "$PWD"`); 0 matches creates the volume with
    compose labels (or reuses a pre-labeling volume of the same
    compose-convention name), while a Docker list/inspect failure or >1
    matches fails the deploy,
  - stale vault-recovery uploader lock migration: after all prior services
    stop and before new services start, a fail-closed step resolves
    vault-recovery-uploader-state by Compose metadata + labels (same
    rigor as the other migrations), verifies NO container (running or
    stopped) still mounts it, and removes ONLY an EMPTY legacy
    `/state/.upload.lock` via `rmdir` inside a disposable named-volume
    container — a nonempty lock, a non-directory at the lock path, any
    mounting container, an ambiguous/failed resolve/list/inspect all fail
    the deploy; clean skip only when the volume or lock is absent; no
    broad rm/rm -rf and no secret/state deletion,
  - mode-specific verification commands (off/pilot/backup post-start checks;
    Hermes init nonfatal activation failure handled; hermes_cli.config
    load_config() against /opt/data/config.yaml; TEI 600s wait budget with
    immediate exit failure; real jobs.json cron schema),
  - Phase-3 vault-recovery post-start checks (uploader running, exactly one
    vault-recovery-export cron with the real cron schema, plaintext
    obsidian-backup absence) and the retirement of the plaintext lane from
    compose/.env/stop-service.

No Docker, no GitHub Actions execution.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-to-home-server.yml"
BACKUP_OVERLAY = REPO_ROOT / "docker-compose.mnemosyne-backup.yml"
MNEMOSYNE_OVERLAY = REPO_ROOT / "docker-compose.mnemosyne.yml"
EMBEDDINGS_OVERLAY = REPO_ROOT / "docker-compose.embeddings.yml"
BROWSER_OVERLAY = REPO_ROOT / "docker-compose.browser-control.yml"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
VAULT_RECOVERY_OVERLAY = REPO_ROOT / "docker-compose.vault-recovery.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile.hermes"

RCLONE_DIGEST = "rclone/rclone@sha256:b06aed988cf5967de7c25be5925240983981c757f4ed1ac9d2fa659d51d60548"

REVIEWED_HERMES_BASE_IMAGE = "nousresearch/hermes-agent:v2026.8.31"

LEGACY_MIGRATION_STEP_NAME = (
    "Migrate legacy rclone active config out of Mnemosyne backup state volume"
)

STALE_LOCK_MIGRATION_STEP_NAME = "Migrate stale vault-recovery uploader lock"


def _step_text(workflow: dict, name: str) -> str:
    """Return the run-script text of a named workflow step.

    Raises AssertionError if the step is missing or has no `run` block.
    """
    steps = workflow["jobs"]["deploy"]["steps"]
    for step in steps:
        if step.get("name") == name:
            run = step.get("run")
            assert run is not None, f"step {name!r} has no run block"
            return run
    raise AssertionError(f"workflow step {name!r} not found")


def _step_names(workflow: dict) -> list[str]:
    return [step.get("name", "") for step in workflow["jobs"]["deploy"]["steps"]]


def _step_index(workflow: dict, name: str) -> int:
    names = _step_names(workflow)
    assert name in names, f"step {name!r} not found"
    return names.index(name)


class DeployWorkflowContractTests(unittest.TestCase):
    """Source-level contract for deploy-to-home-server.yml."""

    def setUp(self) -> None:
        assert yaml is not None, "PyYAML is required for these contract tests"
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.text = WORKFLOW.read_text(encoding="utf-8")

    # --- default off / backward compatibility ---

    def test_mnemosyne_deploy_mode_default_is_off(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn('MNEMOSYNE_DEPLOY_MODE="$MNEMOSYNE_DEPLOY_MODE_INPUT"', validate)
        self.assertIn('if [ -z "$MNEMOSYNE_DEPLOY_MODE" ]; then', validate)
        self.assertIn('MNEMOSYNE_DEPLOY_MODE="off"', validate)

    def test_off_mode_preserves_baseline_compose_file(self) -> None:
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        self.assertIn('case "${MNEMOSYNE_DEPLOY_MODE:-off}" in', derive)
        # off branch is empty (no overlays appended).
        self.assertRegex(derive, r"off\)[^\n]*;;")
        # Base file is always first.
        self.assertIn('COMPOSE_FILE_VALUE="docker-compose.yml"', derive)

    def test_off_mode_post_start_check_present(self) -> None:
        names = _step_names(self.workflow)
        self.assertIn("Verify Mnemosyne off (overlays absent, provider disabled)", names)

    # --- allowed / invalid mode behavior ---

    def test_allowed_modes_are_off_pilot_backup(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn("off|pilot|backup) ;;", validate)
        self.assertIn(
            "ERROR: MNEMOSYNE_DEPLOY_MODE must be one of: off, pilot, backup",
            validate,
        )

    def test_derive_step_also_rejects_invalid_mode(self) -> None:
        # The derive step has a defensive default case that rejects invalid
        # modes before any volume mutation.
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        self.assertIn("*)", derive)
        self.assertIn("ERROR: MNEMOSYNE_DEPLOY_MODE must be one of: off, pilot, backup", derive)

    def test_invalid_mode_exits_before_teardown(self) -> None:
        validate_idx = _step_index(self.workflow, "Validate required repository variables")
        stop_idx = _step_index(self.workflow, "Stop existing services")
        self.assertLess(validate_idx, stop_idx)
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn("exit 1", validate.split("MNEMOSYNE_DEPLOY_MODE must be one of")[1].split("esac")[0])

    def test_mode_case_branches_are_only_off_pilot_backup(self) -> None:
        # The MNEMOSYNE_DEPLOY_MODE case statement has exactly the three mode
        # branches (no extra mode). Standalone gbrain embeddings is NOT a mode
        # branch: it is selected via GBRAIN_EMBEDDINGS_ENABLED outside the case
        # (see test_embeddings_overlay_matrix_permits_standalone_off).
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        branches = re.findall(r"(backup|pilot|off)\)", derive)
        self.assertEqual(set(branches), {"backup", "pilot", "off"})

    # --- backup prerequisites ---

    def test_backup_requires_positive_integer_interval(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        backup_section = validate.split('MNEMOSYNE_DEPLOY_MODE" = "backup"')[1]
        self.assertIn("MNEMOSYNE_BACKUP_EXPORT_INTERVAL repo variable is required", backup_section)
        self.assertIn("must be a positive integer", backup_section)
        self.assertIn("grep -Eq '^[0-9]+$'", backup_section)
        self.assertIn('"$EXPORT_INTERVAL" -le 0', backup_section)

    def test_backup_rejects_leading_zeros(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        backup_section = validate.split('MNEMOSYNE_DEPLOY_MODE" = "backup"')[1]
        self.assertIn("leading zeros", backup_section)
        # The leading-zero guard uses a case statement on 0*.
        self.assertIn("0*)", backup_section)

    def test_backup_rejects_values_exceeding_max(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        backup_section = validate.split('MNEMOSYNE_DEPLOY_MODE" = "backup"')[1]
        self.assertIn("10080", backup_section)
        self.assertIn('"$EXPORT_INTERVAL" -gt 10080', backup_section)

    def test_backup_requires_rclone_config_b64(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        backup_section = validate.split('MNEMOSYNE_DEPLOY_MODE" = "backup"')[1]
        self.assertIn("RCLONE_CONFIG_B64 secret is required", backup_section)

    def test_backup_prerequisites_checked_before_teardown(self) -> None:
        validate_idx = _step_index(self.workflow, "Validate required repository variables")
        stop_idx = _step_index(self.workflow, "Stop existing services")
        self.assertLess(validate_idx, stop_idx)

    # --- validation before state mutation (blocker #2) ---

    def test_compose_config_validation_precedes_volume_population(self) -> None:
        names = _step_names(self.workflow)
        derive_idx = names.index("Derive compose file and validate config")
        populate_idx = names.index("Populate browser-control persistent volumes")
        self.assertLess(derive_idx, populate_idx)

    def test_compose_config_validation_precedes_rclone_volume_publish(self) -> None:
        names = _step_names(self.workflow)
        derive_idx = names.index("Derive compose file and validate config")
        publish_idx = names.index("Publish rclone config into shared volume")
        self.assertLess(derive_idx, publish_idx)

    def test_rclone_decode_validation_precedes_volume_population(self) -> None:
        names = _step_names(self.workflow)
        decode_idx = names.index("Decode and validate rclone config")
        populate_idx = names.index("Populate browser-control persistent volumes")
        self.assertLess(decode_idx, populate_idx)

    def test_all_preflight_precedes_teardown(self) -> None:
        names = _step_names(self.workflow)
        derive_idx = names.index("Derive compose file and validate config")
        decode_idx = names.index("Decode and validate rclone config")
        populate_idx = names.index("Populate browser-control persistent volumes")
        publish_idx = names.index("Publish rclone config into shared volume")
        stop_idx = names.index("Stop existing services")
        # All preflight steps precede teardown.
        for preflight in (derive_idx, decode_idx, populate_idx, publish_idx):
            self.assertLess(preflight, stop_idx,
                            f"preflight step must precede teardown (stop_idx={stop_idx})")

    def test_compose_config_quiet_in_derive_step(self) -> None:
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        self.assertIn("docker compose", derive)
        self.assertIn("config --quiet", derive)
        self.assertIn("set -euo pipefail", derive)

    def test_no_standalone_validate_compose_config_step(self) -> None:
        # The old standalone "Validate selected compose config" step must be
        # gone (folded into "Derive compose file and validate config").
        names = _step_names(self.workflow)
        self.assertNotIn("Validate selected compose config", names)

    # --- required compose ordering ---

    def test_pilot_ordering_embeddings_before_mnemosyne(self) -> None:
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        self.assertIn('COMPOSE_FILE_VALUE="${COMPOSE_FILE_VALUE}:docker-compose.embeddings.yml"', derive)
        pilot_branch = derive.split("pilot)")[1].split(";;")[0]
        self.assertIn("docker-compose.mnemosyne.yml", pilot_branch)
        self.assertLess(
            derive.index("docker-compose.embeddings.yml"),
            derive.index("docker-compose.mnemosyne.yml"),
        )

    def test_backup_ordering_base_embeddings_mnemosyne_backup_last(self) -> None:
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        backup_branch = derive.split("backup)")[1].split(";;")[0]
        self.assertIn("docker-compose.mnemosyne.yml", backup_branch)
        self.assertIn("docker-compose.mnemosyne-backup.yml", backup_branch)
        self.assertLess(
            derive.index("docker-compose.embeddings.yml"),
            derive.index("docker-compose.mnemosyne.yml"),
        )
        self.assertLess(
            backup_branch.index("docker-compose.mnemosyne.yml"),
            backup_branch.index("docker-compose.mnemosyne-backup.yml"),
        )

    def test_browser_control_optional_and_before_embeddings(self) -> None:
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        bc_idx = derive.index('docker-compose.browser-control.yml')
        case_idx = derive.index('case "${MNEMOSYNE_DEPLOY_MODE:-off}" in')
        self.assertLess(bc_idx, case_idx)
        self.assertIn('"${BROWSER_CONTROL_ENABLED}" = "true"', derive[:case_idx])

    def test_compose_file_value_starts_with_base(self) -> None:
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        self.assertIn('COMPOSE_FILE_VALUE="docker-compose.yml"', derive)

    # --- no recovery profile for normal deploy ---

    def test_recovery_profile_not_in_normal_up(self) -> None:
        start = _step_text(self.workflow, "Start services")
        self.assertNotIn("--profile recovery", start)
        self.assertNotIn("recovery", start)

    def test_recovery_profile_only_in_teardown_superset(self) -> None:
        stop = _step_text(self.workflow, "Stop existing services")
        superset_part = stop.split("Tearing down any prior overlay-enabled stack")[1].split("Tearing down with selected config")[0]
        self.assertIn("--profile recovery", superset_part)
        selected_part = stop.split("Tearing down with selected config")[1]
        self.assertNotIn("--profile recovery", selected_part)

    # --- maximal fail-closed no-volume teardown (blocker #1) ---

    def test_teardown_uses_superset_overlays(self) -> None:
        stop = _step_text(self.workflow, "Stop existing services")
        superset = stop.split("Tearing down any prior overlay-enabled stack")[1].split("Tearing down with selected config")[0]
        for overlay in (
            "docker-compose.yml",
            "docker-compose.vault-recovery.yml",
            "docker-compose.browser-control.yml",
            "docker-compose.embeddings.yml",
            "docker-compose.mnemosyne.yml",
            "docker-compose.mnemosyne-backup.yml",
        ):
            self.assertIn(overlay, superset, f"superset teardown missing {overlay}")

    def test_teardown_includes_aux_ml_profile(self) -> None:
        stop = _step_text(self.workflow, "Stop existing services")
        superset = stop.split("Tearing down any prior overlay-enabled stack")[1].split("Tearing down with selected config")[0]
        self.assertIn("--profile aux-ml", superset)
        self.assertIn("--profile browser-control", superset)
        self.assertIn("--profile recovery", superset)

    def test_teardown_is_fail_closed_no_set_plus_e(self) -> None:
        stop = _step_text(self.workflow, "Stop existing services")
        # Must NOT use set +e.
        self.assertNotIn("set +e", stop)
        # Must use set -euo pipefail.
        self.assertIn("set -euo pipefail", stop)

    def test_teardown_is_fail_closed_no_or_true(self) -> None:
        stop = _step_text(self.workflow, "Stop existing services")
        # No `|| true` anywhere in the teardown step.
        self.assertNotIn("|| true", stop)
        self.assertNotIn("2>/dev/null || true", stop)

    def test_teardown_never_uses_v_flag(self) -> None:
        stop = _step_text(self.workflow, "Stop existing services")
        self.assertNotIn("down --remove-orphans -v", stop)
        self.assertNotIn("down -v", stop)
        self.assertNotIn(" -v ", stop.replace("--remove-orphans", ""))

    def test_teardown_removes_prior_overlays_when_switching_to_off(self) -> None:
        # The superset teardown runs unconditionally (fail-closed, no || true)
        # before the selected-config teardown, so switching to off still
        # removes prior overlay services.
        stop = _step_text(self.workflow, "Stop existing services")
        self.assertIn("down --remove-orphans", stop)
        # The superset teardown must come before the selected-config teardown.
        superset_idx = stop.index("Tearing down any prior overlay-enabled stack")
        selected_idx = stop.index("Tearing down with selected config")
        self.assertLess(superset_idx, selected_idx)

    # --- rclone digest + direct invocation + independent fields (blockers #3,#4) ---

    def test_rclone_step_uses_pinned_digest(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertIn(RCLONE_DIGEST, decode)

    def test_rclone_step_strict_base64_decode_to_0600_config(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertIn("base64 --decode", decode)
        self.assertIn("chmod 600", decode)
        self.assertIn("decoded to an empty config", decode)
        # The 0600 file lives inside a disposable temp DIRECTORY.
        self.assertIn('RCLONE_CONF="$TMP_RCLONE_DIR/rclone.conf"', decode)
        self.assertIn('chmod 600 "$RCLONE_CONF"', decode)

    def test_rclone_direct_invocation_no_sh_c(self) -> None:
        # The pinned rclone image entrypoint is ["rclone"], so we invoke
        # rclone directly (no `sh -c 'rclone ...'`).
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertNotIn("sh -c 'rclone", decode)
        self.assertNotIn('sh -c "rclone', decode)
        # Direct invocation: IMAGE --config <in-container path> config show ...
        self.assertIn("--config /tmp/rclone-conf/rclone.conf config show", decode)

    def test_rclone_remote_names_hardcoded(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        # Hardcoded (not interpolated from an env var that could diverge).
        self.assertIn('VAULT_RECOVERY_REMOTE_NAME="vault-recovery-crypt"', decode)
        self.assertIn('MNEMOSYNE_REMOTE_NAME="mnemosyne-crypt"', decode)
        # Must NOT interpolate from the runtime env vars.
        self.assertNotIn("${VAULT_RECOVERY_RCLONE_REMOTE", decode)
        self.assertNotIn("${MNEMOSYNE_BACKUP_RCLONE_REMOTE", decode)

    def test_rclone_four_independent_crypt_field_checks(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        # FOUR independent checks (type, remote, password + the
        # metadata-encryption standard), not OR. Each field parsed
        # separately.
        self.assertIn("type must be exactly crypt", decode)
        self.assertIn("REMOTE_TYPE", decode)
        self.assertIn("CRYPT_REMOTE", decode)
        self.assertIn("CRYPT_PASSWORD", decode)
        self.assertIn("CRYPT_FILENAME_ENC", decode)
        self.assertIn("CRYPT_DIRNAME_ENC", decode)
        # Must NOT use the old OR-based grep that combined remote|password.
        self.assertNotIn("grep -E '^(remote|password)", decode)

    def test_rclone_vault_recovery_crypt_validated_independent_of_mnemosyne_mode(self) -> None:
        # The vault-recovery-crypt remote is validated on EVERY deployment,
        # OUTSIDE the MNEMOSYNE_DEPLOY_MODE=backup gate (Phase 3: the
        # encrypted vault-recovery lane is the default backup composition).
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        # The unconditional validation call happens before the backup gate.
        before_gate = decode.split('"${MNEMOSYNE_DEPLOY_MODE:-off}" = "backup"')[0]
        self.assertIn('validate_crypt_remote "$VAULT_RECOVERY_REMOTE_NAME"', before_gate)
        # mnemosyne-crypt stays gated on backup mode.
        backup_gate = decode.split('"${MNEMOSYNE_DEPLOY_MODE:-off}" = "backup"')[1]
        self.assertIn('validate_crypt_remote "$MNEMOSYNE_REMOTE_NAME"', backup_gate)

    def test_rclone_backup_branch_validates_crypt_before_publish(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        # Validation is in the decode step; publish is separate and later.
        self.assertIn("config show", decode)
        self.assertIn("type crypt", decode)
        # The decode step does NOT create the volume.
        self.assertNotIn("docker volume create", decode)
        # The publish step creates the volume.
        self.assertIn("docker volume create", publish)

    def test_rclone_config_required_for_every_deployment(self) -> None:
        # Phase 3: RCLONE_CONFIG_B64 is required even when
        # MNEMOSYNE_DEPLOY_MODE=off — the vault-recovery lane is the default
        # backup and the deploy must FAIL rather than silently lose backups.
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn(
            "ERROR: RCLONE_CONFIG_B64 secret is required: the vault-recovery encrypted backup lane is the default deployment composition",
            validate,
        )
        # The requirement is OUTSIDE the backup-mode section: it appears
        # after the backup-mode `fi` (the mnemosyne export-interval block)
        # and before the GITHUB_ENV write.
        tail = validate.split('MNEMOSYNE_DEPLOY_MODE="$MNEMOSYNE_DEPLOY_MODE" >> "$GITHUB_ENV"')[0]
        after_backup_block = tail.split("RCLONE_CONFIG_B64 secret is required when MNEMOSYNE_DEPLOY_MODE=backup")[1]
        self.assertNotIn(
            'if [ "$MNEMOSYNE_DEPLOY_MODE" = "backup" ]; then', after_backup_block
        )
        # Defensive duplicate in the decode step too.
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertIn(
            "ERROR: RCLONE_CONFIG_B64 secret is required: the vault-recovery encrypted backup lane is the default deployment composition",
            decode,
        )

    def test_rclone_backup_branch_does_not_require_baseline_gdrive(self) -> None:
        # The retired plaintext obsidian-backup lane no longer exists, so the
        # deploy must NOT require a baseline gdrive remote for it.
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertNotIn("BASELINE_REMOTE", decode)
        self.assertNotIn("obsidian-backup would break", decode)
        self.assertNotIn('"gdrive"', decode)

    def test_rclone_backup_branch_does_not_print_config(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertNotIn("echo \"$REMOTE_SHOW\"", decode)
        self.assertNotIn("cat \"$RCLONE_CONF\"", decode)

    def test_rclone_publish_is_atomic(self) -> None:
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        self.assertIn("rclone.conf.new", publish)
        self.assertIn("mv /config/rclone/rclone.conf.new /config/rclone/rclone.conf", publish)

    # --- crypt metadata-encryption standard (council fix 1) ---

    def test_rclone_validates_metadata_encryption_standard(self) -> None:
        """The deploy preflight enforces the metadata-encryption standard on
        the crypt remote: filename_encryption must be `standard` (absent =
        rclone default `standard`; `off`/`obfuscate` rejected) and
        directory_name_encryption must not be `false` (absent = default
        `true`) — otherwise plaintext file/directory names would leak in
        the ciphertext metadata and the ciphertext non-leak proof would be
        void."""
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertIn("CRYPT_FILENAME_ENC", decode)
        self.assertIn("CRYPT_DIRNAME_ENC", decode)
        self.assertIn(
            "must be 'standard'", decode,
        )
        self.assertIn(
            "plaintext file names would leak in the ciphertext metadata", decode,
        )
        self.assertIn(
            "directory_name_encryption is 'false'", decode,
        )
        self.assertIn(
            "plaintext directory names would leak in the ciphertext metadata", decode,
        )

    # --- real remote readiness gate (council fix 2: migration cutover) ---

    def test_remote_readiness_gate_runs_before_teardown(self) -> None:
        """A syntactically valid but UNREACHABLE remote must abort the
        deploy BEFORE any teardown: the readiness gate runs between the
        decode step and the publish step, all before the first mutation
        ("Stop existing services"). The existing deployment and any legacy
        lane state are retained untouched on failure."""
        names = _step_names(self.workflow)
        gate_name = "Vault-recovery remote readiness gate (real upload probe)"
        self.assertIn(gate_name, names)
        decode_idx = names.index("Decode and validate rclone config")
        gate_idx = names.index(gate_name)
        publish_idx = names.index("Publish rclone config into shared volume")
        stop_idx = names.index("Stop existing services")
        self.assertLess(decode_idx, gate_idx)
        self.assertLess(gate_idx, publish_idx)
        self.assertLess(publish_idx, stop_idx)
        # The gate runs immediately after the decode step: no step between
        # them can fail with the decoded secret temp file still on disk.
        self.assertEqual(gate_idx, decode_idx + 1)

    def test_remote_readiness_gate_does_real_round_trip(self) -> None:
        gate = _step_text(self.workflow, "Vault-recovery remote readiness gate (real upload probe)")
        # REAL write (rcat), read-back (cat) and cleanup (deletefile)
        # against the production crypt remote — not just config syntax.
        self.assertIn("rcat", gate)
        self.assertIn("cat \"$REMOTE_BASE/$PROBE\"", gate)
        self.assertIn("deletefile", gate)
        # The probe never lands in the committed namespace (it can never be
        # mistaken for a generation by listing/retention/recovery).
        self.assertIn("vault-recovery-crypt:Josemar/vault-recovery", gate)
        self.assertNotIn("committed/", gate)
        # Unreachable remote -> abort BEFORE any teardown.
        self.assertIn("UNREACHABLE", gate)
        self.assertIn("ABORTED BEFORE ANY TEARDOWN", gate)
        self.assertIn("legacy lane state are retained untouched", gate)
        # Read-back mismatch also aborts.
        self.assertIn("read-back probe", gate)

    # --- deploy temp rclone config cleanup (council fix 6) ---

    def test_decode_step_traps_temp_dir_cleanup(self) -> None:
        """The decode step traps RECURSIVE removal of the decoded-secret
        temp DIRECTORY on ALL failure paths (after mktemp -d) and disarms
        the trap on success so the publish step can still use it."""
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertIn("trap 'rm -rf \"$TMP_RCLONE_DIR\"' EXIT", decode)
        # Disarmed on success right before the GITHUB_ENV export.
        success_part = decode.split('echo "RCLONE_TEMP_DIR=$TMP_RCLONE_DIR"')[1]
        self.assertIn("trap - EXIT", success_part)

    def test_publish_step_traps_temp_dir_cleanup_on_every_exit(self) -> None:
        """The publish step owns the temp DIRECTORY and traps its RECURSIVE
        removal on EVERY exit (success AND failure): the decoded secret
        never lingers on the runner."""
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        self.assertIn("trap 'rm -rf \"$TMP_RCLONE_DIR\"' EXIT", publish)
        self.assertIn("Cleanup on EVERY exit path", publish)
        # No explicit rm outside the trap: the trap is the single cleanup
        # path.
        self.assertNotIn("rm -rf \"$TMP_RCLONE_DIR\"\n          echo", publish)

    def test_readiness_gate_traps_temp_dir_cleanup_on_every_exit(self) -> None:
        """The readiness gate is the step between the decode step (trap
        disarmed on success) and the publish step (final owner). It can
        itself FAIL (unreachable remote -> deploy aborted BEFORE any
        teardown), so it must keep a cleanup trap active through the remote
        readiness failure and the final probe cleanup, and disarm it ONLY
        on success (so the publish step can still use the temp directory).
        Otherwise the decoded secret would linger on the runner whenever
        the remote readiness gate aborts the deploy."""
        gate = _step_text(self.workflow, "Vault-recovery remote readiness gate (real upload probe)")
        # The trap is registered right after the temp dir/config are read
        # and BEFORE any failure path (missing/empty config, write probe,
        # read-back probe) — so every readiness failure cleans the secret.
        self.assertIn("trap 'rm -rf \"$TMP_RCLONE_DIR\"' EXIT", gate)
        self.assertLess(
            gate.index("trap 'rm -rf \"$TMP_RCLONE_DIR\"' EXIT"),
            gate.index('if [ -z "$TMP_RCLONE_DIR" ] || [ ! -s "$RCLONE_CONF" ]'),
        )
        self.assertLess(
            gate.index("trap 'rm -rf \"$TMP_RCLONE_DIR\"' EXIT"),
            gate.index("UNREACHABLE"),
        )
        # Disarmed ONLY after the final success line (the publish step owns
        # the directory from then on); no failure path disarms before
        # exiting.
        success_part = gate.split("readiness gate PASSED")[1]
        self.assertIn("trap - EXIT", success_part)
        self.assertNotIn("trap - EXIT", gate.split("readiness gate PASSED")[0])

    def test_env_writes_hardcoded_rclone_remotes(self) -> None:
        env_step = _step_text(self.workflow, "Create .env file")
        self.assertIn('write_env MNEMOSYNE_BACKUP_RCLONE_REMOTE "mnemosyne-crypt"', env_step)
        # Phase 3: the vault-recovery remote is written for EVERY deployment.
        self.assertIn('write_env VAULT_RECOVERY_RCLONE_REMOTE "vault-recovery-crypt"', env_step)

    def test_final_cleanup_removes_rclone_temp_dir(self) -> None:
        """The always() final cleanup removes BOTH `.env` and the decoded
        rclone temp DIRECTORY (recursively): a run cancelled BETWEEN the
        rclone steps (after the decode step exported RCLONE_TEMP_DIR but
        before the publish step's trap armed) would otherwise leave the
        decoded secret on the runner — the step-local traps only cover
        in-step failures, this final sweep covers the between-step
        cancellation window."""
        names = _step_names(self.workflow)
        self.assertIn("Cleanup sensitive files", names)
        cleanup = self.workflow["jobs"]["deploy"]["steps"][names.index("Cleanup sensitive files")]
        self.assertEqual(cleanup.get("if"), "always()")
        run = cleanup["run"]
        self.assertIn("rm -f .env", run)
        self.assertIn('"${RCLONE_TEMP_DIR:-}"', run)
        self.assertIn("rm -rf \"$RCLONE_TEMP_DIR\"", run)
        # It is the LAST step of the job (nothing after it can fail with
        # the temp directory still on disk).
        self.assertEqual(names[-1], "Cleanup sensitive files")

    def test_rclone_config_lives_in_writable_temp_dir(self) -> None:
        """The rclone config must live in a disposable temp DIRECTORY
        (mode-0600 file) mounted WRITABLE into the probe containers: rclone
        refreshes the working config (OAuth token refresh) via sibling
        temp file + atomic rename, which is impossible with the former
        read-only single-file mount."""
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        gate = _step_text(self.workflow, "Vault-recovery remote readiness gate (real upload probe)")
        # Disposable writable directory, not a single file.
        self.assertIn('TMP_RCLONE_DIR="$(mktemp -d)"', decode)
        self.assertIn('RCLONE_CONF="$TMP_RCLONE_DIR/rclone.conf"', decode)
        self.assertIn('chmod 600 "$RCLONE_CONF"', decode)
        # The temp directory is mounted WRITABLE (explicit :rw, never :ro)
        # into the rclone containers in BOTH the decode validation and the
        # readiness probes, with the config at a fixed in-container path.
        for step_text in (decode, gate):
            self.assertIn('-v "$TMP_RCLONE_DIR:/tmp/rclone-conf:rw"', step_text)
            self.assertIn("--config /tmp/rclone-conf/rclone.conf", step_text)
        # The old read-only single-file mount is gone everywhere.
        self.assertNotIn(":/tmp/rclone.conf:ro", decode + gate)
        self.assertNotIn('TMP_RCLONE_CONF="$(mktemp)"', decode)

    def test_rclone_writable_probe_containers_run_as_runner_user(self) -> None:
        """Regression: every rclone container that
        mounts the disposable config directory WRITABLE must execute with
        the host runner's UID:GID (`--user "$(id -u):$(id -g)"`). rclone
        rewrites the working config via sibling temp file + atomic rename,
        and as container root the renamed file becomes root-owned 0600 —
        unreadable by the host runner, which broke the publish step's
        checksum. The writable-dir OAuth refresh behavior is preserved
        (the mounts stay :rw)."""
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        gate = _step_text(self.workflow, "Vault-recovery remote readiness gate (real upload probe)")
        for step_text in (decode, gate):
            rw_mounts = step_text.count('-v "$TMP_RCLONE_DIR:/tmp/rclone-conf:rw"')
            user_flags = step_text.count('--user "$(id -u):$(id -g)"')
            self.assertGreater(rw_mounts, 0, "writable temp-dir rclone mounts must exist")
            self.assertEqual(
                user_flags,
                rw_mounts,
                "every writable temp-dir rclone invocation must run as the host runner UID:GID",
            )
        # The identity derives from the HOST runner (never guessed), and
        # the writable mounts themselves are preserved.
        self.assertIn('--user "$(id -u):$(id -g)"', decode)
        self.assertIn('--user "$(id -u):$(id -g)"', gate)
        self.assertIn('-v "$TMP_RCLONE_DIR:/tmp/rclone-conf:rw"', decode)
        self.assertIn('-v "$TMP_RCLONE_DIR:/tmp/rclone-conf:rw"', gate)
        # The config modes are never weakened (still 0600, no chown).
        self.assertIn('chmod 600 "$RCLONE_CONF"', decode)
        self.assertNotIn("chown", decode)
        self.assertNotIn("chown", gate)

    def test_publish_remains_host_readable(self) -> None:
        """Regression: the publish step compares the
        working config checksum ON THE HOST as the runner user, so the
        config must stay runner-owned. The alpine publish container mounts
        the temp dir READ-ONLY (it can never rewrite or re-own the config)
        and stays root (it must write the named volume atomically); it is
        NOT a writable temp-dir rclone invocation and gets no runner-user
        flag."""
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        # Host-side checksum of the working config (no docker wrapper),
        # executed before any container runs.
        self.assertIn('CURRENT_SHA="$(sha256sum "$RCLONE_CONF"', publish)
        self.assertLess(
            publish.index('CURRENT_SHA="$(sha256sum "$RCLONE_CONF"'),
            publish.index("docker run"),
        )
        # The publish container mounts the temp dir READ-ONLY: it can never
        # mutate or re-own the working config.
        self.assertIn('-v "$TMP_RCLONE_DIR:/config/source:ro"', publish)
        self.assertNotIn('-v "$TMP_RCLONE_DIR:/config/source:rw"', publish)
        # It is not a writable temp-dir rclone invocation (no runner-user
        # flag, no rw rclone temp-dir mount).
        self.assertNotIn('--user "$(id -u):$(id -g)"', publish)
        self.assertNotIn(':/tmp/rclone-conf:rw', publish)

    def test_temp_dir_cleanup_is_recursive_on_every_owner(self) -> None:
        """Every cleanup owner recursively removes the temp DIRECTORY
        (rm -rf, never rm -f): the decode failure trap, the readiness
        gate's every-exit trap, the publish step's every-exit trap, and
        the always() final sweep. A single-file rm -f would leave the
        directory (and any rclone-refreshed sibling files) behind."""
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        gate = _step_text(self.workflow, "Vault-recovery remote readiness gate (real upload probe)")
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        for step_text in (decode, gate, publish):
            self.assertIn("trap 'rm -rf \"$TMP_RCLONE_DIR\"' EXIT", step_text)
            # No stale single-file cleanup may survive in any rclone step.
            self.assertNotIn('rm -f "$TMP_RCLONE_CONF"', step_text)
        cleanup = self.workflow["jobs"]["deploy"]["steps"][
            _step_index(self.workflow, "Cleanup sensitive files")
        ]
        self.assertIn('rm -rf "$RCLONE_TEMP_DIR"', cleanup["run"])

    def test_publish_publishes_rclone_refreshed_config(self) -> None:
        """If rclone changes the working config (OAuth token refresh during
        the readiness probes), the publish step must detect it via the
        pre-probe checksum and publish the UPDATED file atomically into the
        shared volume, so the runtime services consume the refreshed
        tokens."""
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        # The decode step records the pre-probe checksum of the working
        # config for the publish step's change detection.
        self.assertIn('RCLONE_CONFIG_SHA256="$(sha256sum "$RCLONE_CONF"', decode)
        self.assertIn(
            'echo "RCLONE_CONFIG_SHA256=$RCLONE_CONFIG_SHA256" >> "$GITHUB_ENV"',
            decode,
        )
        # The publish step recomputes it against the working config (which
        # reflects any rclone refresh) and logs the updated-config path.
        self.assertIn('sha256sum "$RCLONE_CONF"', publish)
        self.assertIn("UPDATED config", publish)
        # The (possibly refreshed) working file is published atomically:
        # temp file + chmod 600 + mv inside the shared volume.
        self.assertIn(
            "cp /config/source/rclone.conf /config/rclone/rclone.conf.new",
            publish,
        )
        self.assertIn("chmod 600 /config/rclone/rclone.conf.new", publish)
        self.assertIn(
            "mv /config/rclone/rclone.conf.new /config/rclone/rclone.conf",
            publish,
        )

    # --- override-safe seed publish resolution (final Oracle blocker) ---

    def test_seed_publish_resolves_volume_via_compose_metadata(self) -> None:
        """The shared obsidian-rclone-config seed volume must be resolved
        from Compose's rendered config metadata (top-level `name`,
        honoring COMPOSE_PROJECT_NAME / -p) and the compose volume label —
        never guessed from `basename "$PWD"`."""
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        # Project name from Compose's own rendered config metadata.
        self.assertIn(
            'PROJECT_NAME="$(docker compose config --format json',
            publish,
        )
        self.assertIn('json.load(sys.stdin)["name"]', publish)
        # Volume located by project + compose volume label.
        self.assertIn(
            '--filter "label=com.docker.compose.project=$PROJECT_NAME"',
            publish,
        )
        self.assertIn(
            "--filter label=com.docker.compose.volume=obsidian-rclone-config",
            publish,
        )
        self.assertIn("--format '{{.Name}}'", publish)
        # No basename-guessed physical volume construct anywhere in the
        # step.
        self.assertNotIn('COMPOSE_PROJECT="$(basename "$PWD")"', publish)
        self.assertNotIn('"${COMPOSE_PROJECT}_', publish)

    def test_seed_publish_outcome_distinction(self) -> None:
        """Outcome handling analogous to the legacy migration: 0 labeled
        matches creates the volume WITH the compose labels (or reuses a
        pre-labeling volume of the same compose-convention name); a Docker
        volume ls operational failure and >1 matches FAIL the deploy
        (never guess); the resolved volume is re-verified as an
        inspectable named volume before the atomic seed publish."""
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        # 0 matches -> create with compose labels from the RESOLVED project.
        self.assertIn(
            'RCLONE_VOLUME="${PROJECT_NAME}_obsidian-rclone-config"',
            publish,
        )
        self.assertIn('--label "com.docker.compose.project=$PROJECT_NAME"', publish)
        self.assertIn('--label "com.docker.compose.volume=obsidian-rclone-config"', publish)
        self.assertIn("docker volume create", publish)
        # Pre-labeling reuse fallback (an existing unlabeled volume must
        # not break the upgrade).
        self.assertIn("created before compose labeling", publish)
        # Operational failure and ambiguity fail closed.
        self.assertIn("cannot list Docker volumes", publish)
        self.assertIn("expected exactly 1 obsidian-rclone-config volume", publish)
        self.assertIn("exit 1", publish)
        # Named-volume-only guard precedes the atomic publish.
        self.assertIn("not an inspectable Docker named volume", publish)
        self.assertLess(
            publish.index("not an inspectable Docker named volume"),
            publish.index("rclone.conf.new"),
        )
        # The atomic publish mounts the RESOLVED volume.
        self.assertIn('-v "$RCLONE_VOLUME:/config/rclone"', publish)

    def test_rclone_steps_never_guess_physical_volume_from_basename(self) -> None:
        """No rclone seed/probe/publish/migration step may guess a physical
        Compose volume from `basename "$PWD"` (breaks under
        COMPOSE_PROJECT_NAME / -p overrides): the seed publish and the
        legacy migration both resolve volumes via Compose metadata +
        labels, and the decode/readiness steps only use the disposable
        temp directory."""
        for name in (
            "Decode and validate rclone config",
            "Vault-recovery remote readiness gate (real upload probe)",
            "Publish rclone config into shared volume",
            LEGACY_MIGRATION_STEP_NAME,
        ):
            step = _step_text(self.workflow, name)
            self.assertNotIn(
                'COMPOSE_PROJECT="$(basename "$PWD")"', step, msg=name
            )
            self.assertNotIn('"${COMPOSE_PROJECT}_', step, msg=name)

    # --- legacy rclone active config migration (Oracle upgrade blocker) ---

    def test_legacy_migration_runs_after_teardown_and_before_start(self) -> None:
        """The migration must run AFTER the old services are stopped (so no
        container holds the volume) and BEFORE Hermes/new services start
        (so the legacy secret files are gone before the state volume is
        mounted read-only into Hermes again)."""
        names = _step_names(self.workflow)
        self.assertIn(LEGACY_MIGRATION_STEP_NAME, names)
        self.assertLess(
            names.index("Stop existing services"),
            names.index(LEGACY_MIGRATION_STEP_NAME),
        )
        self.assertLess(
            names.index(LEGACY_MIGRATION_STEP_NAME),
            names.index("Start services"),
        )
        # It also precedes the build of the new images.
        self.assertLess(
            names.index(LEGACY_MIGRATION_STEP_NAME),
            names.index("Build Docker image"),
        )

    def test_legacy_migration_deletes_only_exact_legacy_files(self) -> None:
        """Only the two exact legacy files at the volume root are removed:
        rclone.active.conf + rclone.active.conf.seed-fp. The ledger/slot
        acknowledgement state is never touched, and there is no recursive
        delete at the volume root."""
        step = _step_text(self.workflow, LEGACY_MIGRATION_STEP_NAME)
        # Exactly one removal command, targeting exactly the two legacy
        # files at the volume root (/state is the named-volume mount point
        # inside the migration container).
        self.assertEqual(step.count("rm -f"), 1)
        self.assertIn(
            "rm -f /state/rclone.active.conf /state/rclone.active.conf.seed-fp",
            step,
        )
        # No recursive removal anywhere: the ledger/slot state and every
        # other volume member must survive.
        self.assertNotIn("rm -rf", step)
        # The ledger/slot acknowledgement state is never a deletion target.
        self.assertNotIn("uploaded-generations.jsonl", step)
        self.assertNotIn("next-slot", step)
        self.assertNotIn("last-uploaded-generation", step)
        # Both exact files are re-checked after removal (still-present
        # detection uses both regular-file and symlink checks).
        self.assertIn("still present after removal", step)
        self.assertIn("[ -e \"$f\" ] || [ -L \"$f\" ]", step)

    def test_legacy_migration_project_resolution_respects_compose_overrides(self) -> None:
        """The project name is resolved from Compose's rendered config
        metadata (`docker compose config --format json` -> top-level
        `name`), which honors COMPOSE_PROJECT_NAME / `-p` overrides. The
        physical volume is NEVER guessed from `basename "$PWD"`."""
        step = _step_text(self.workflow, LEGACY_MIGRATION_STEP_NAME)
        # Project name comes from Compose's own rendered config metadata.
        self.assertIn(
            'PROJECT_NAME="$(docker compose config --format json',
            step,
        )
        self.assertIn('docker compose config --format json', step)
        self.assertIn('json.load(sys.stdin)["name"]', step)
        # An empty resolved name fails closed instead of guessing.
        self.assertIn("Compose project name resolved to empty", step)
        # No directory-basename guessing for the volume anywhere in the
        # step (basename is used only to report removed file names).
        self.assertNotIn('COMPOSE_PROJECT="$(basename "$PWD")"', step)
        self.assertNotIn('STATE_VOLUME="${COMPOSE_PROJECT}_', step)

    def test_legacy_migration_outcome_distinction(self) -> None:
        """The three outcomes are distinguished explicitly: 0 matching
        volumes is the ONLY safe skip (after a SUCCESSFUL listing); a
        Docker volume ls operational failure and >1 matching volumes both
        FAIL the deploy. The skip path is reachable only after the list
        succeeded and before any deletion."""
        step = _step_text(self.workflow, LEGACY_MIGRATION_STEP_NAME)
        # Listing outcome is captured, then branched on its count.
        self.assertIn('if ! MATCHES="$(docker volume ls', step)
        self.assertIn("MATCH_COUNT=", step)
        self.assertIn('if [ "$MATCH_COUNT" -eq 0 ]; then', step)
        self.assertIn('if [ "$MATCH_COUNT" -ne 1 ]; then', step)
        # Operational failure is a hard error, never a skip.
        self.assertIn("cannot list Docker volumes", step)
        self.assertIn("exit 1", step.split("cannot list Docker volumes")[1])
        # Ambiguity (>1 matches) is a hard error, never a guess.
        self.assertIn("expected exactly 1", step)
        # Ordering: list failure first, then skip, then ambiguity guard,
        # then deletion.
        self.assertLess(
            step.index("cannot list Docker volumes"),
            step.index("migration skipped"),
        )
        self.assertLess(
            step.index("migration skipped"),
            step.index("rm -f /state/rclone.active.conf"),
        )
        self.assertLess(
            step.index("expected exactly 1"),
            step.index("rm -f /state/rclone.active.conf"),
        )

    def test_legacy_migration_resolves_volume_via_docker_not_host_path(self) -> None:
        """The intended Compose volume is located by the Compose project
        AND the com.docker.compose.volume label — never guessed from a
        host path; the deletion runs INSIDE a container mounting the named
        volume, so an accidental host-path deletion is impossible."""
        step = _step_text(self.workflow, LEGACY_MIGRATION_STEP_NAME)
        # Volume located by project + compose volume label.
        self.assertIn(
            '--filter "label=com.docker.compose.project=$PROJECT_NAME"',
            step,
        )
        self.assertIn(
            "--filter label=com.docker.compose.volume=mnemosyne-backup-state",
            step,
        )
        self.assertIn("--format '{{.Name}}'", step)
        # The resolved name is re-verified as an existing named volume
        # before anything is mounted or deleted.
        self.assertIn('docker volume inspect "$STATE_VOLUME"', step)
        # No basename-guessed physical volume construct anywhere in the
        # step (the comment only names the guess that is avoided).
        self.assertNotIn('COMPOSE_PROJECT="$(basename "$PWD")"', step)
        self.assertNotIn('STATE_VOLUME="${COMPOSE_PROJECT}_', step)
        self.assertNotIn('"${COMPOSE_PROJECT}_mnemosyne-backup-state"', step)
        # The deletion runs inside a container mounting the NAMED volume at
        # /state — never against a host path.
        self.assertIn('-v "$STATE_VOLUME:/state"', step)
        self.assertIn("alpine:3.20", step)
        self.assertLess(
            step.index('-v "$STATE_VOLUME:/state"'),
            step.index("rm -f /state/rclone.active.conf"),
        )
        # The mountpoint is never resolved to delete on the host.
        self.assertNotIn("Mountpoint", step)
        self.assertNotIn("rm -f /var/lib/docker", step)
        self.assertNotIn("rm -rf /", step)

    def test_legacy_migration_fail_closed(self) -> None:
        """A migration failure FAILS the deploy: project-resolution
        failure, empty project name, list operational failure, >1 matches,
        a resolved-but-not-inspectable volume, and a legacy file still
        present after removal all exit non-zero under `set -euo pipefail`.
        A missing volume (0 matches) is the ONLY clean skip."""
        step = _step_text(self.workflow, LEGACY_MIGRATION_STEP_NAME)
        self.assertIn("set -euo pipefail", step)
        # Project-resolution failures fail closed.
        self.assertIn("cannot resolve the Compose project name", step)
        self.assertIn("resolved to empty", step)
        # 0 matches -> clean, explicit skip (not a failure).
        self.assertIn("migration skipped", step)
        self.assertIn("exit 0", step)
        # >1 matches and list/inspect failures fail closed.
        self.assertIn("expected exactly 1", step)
        self.assertIn("cannot list Docker volumes", step)
        self.assertIn("not an inspectable Docker named volume", step)
        self.assertIn("exit 1", step)
        # Still-present legacy file after removal fails the deploy.
        self.assertIn("still present after removal", step)
        # The skip happens BEFORE any deletion.
        self.assertLess(
            step.index("exit 0"),
            step.index("rm -f /state/rclone.active.conf"),
        )

    def test_legacy_migration_never_prints_secrets(self) -> None:
        """The legacy config files are only removed, never read or
        printed: no cat/head/hash of their content; only their basenames
        are reported."""
        step = _step_text(self.workflow, LEGACY_MIGRATION_STEP_NAME)
        self.assertNotIn("cat /state/rclone.active.conf", step)
        self.assertNotIn("sha256sum", step)
        self.assertNotIn("head -", step)
        # Only basenames of the removed files are logged.
        self.assertIn("basename", step)

    def test_legacy_migration_runs_unconditionally(self) -> None:
        """The legacy volume can exist from a prior deploy even when the
        current MNEMOSYNE_DEPLOY_MODE is off or the backup overlay is not
        selected, so the migration step must not be gated on mode/overlay
        variables."""
        steps = self.workflow["jobs"]["deploy"]["steps"]
        step = next(
            s for s in steps if s.get("name") == LEGACY_MIGRATION_STEP_NAME
        )
        self.assertNotIn("MNEMOSYNE_DEPLOY_MODE", step.get("if", ""))
        self.assertNotIn("vars.", step.get("if", ""))

    # --- stale vault-recovery uploader lock migration (audited issue) ---

    def test_stale_lock_migration_runs_after_stop_before_start(self) -> None:
        """The stale-lock migration must run AFTER all prior services are
        stopped (no live uploader can hold or recreate the lock) and
        BEFORE new services start (the new uploader must never fail closed
        on the stale lock)."""
        names = _step_names(self.workflow)
        self.assertIn(STALE_LOCK_MIGRATION_STEP_NAME, names)
        self.assertLess(
            names.index("Stop existing services"),
            names.index(STALE_LOCK_MIGRATION_STEP_NAME),
        )
        self.assertLess(
            names.index(STALE_LOCK_MIGRATION_STEP_NAME),
            names.index("Start services"),
        )
        self.assertLess(
            names.index(STALE_LOCK_MIGRATION_STEP_NAME),
            names.index("Build Docker image"),
        )

    def test_stale_lock_migration_resolves_volume_via_compose_metadata(self) -> None:
        """The vault-recovery-uploader-state volume is resolved from
        Compose's rendered config metadata (top-level `name`, honoring
        COMPOSE_PROJECT_NAME / -p) plus the compose volume label — never
        guessed from `basename "$PWD"`."""
        step = _step_text(self.workflow, STALE_LOCK_MIGRATION_STEP_NAME)
        self.assertIn(
            'PROJECT_NAME="$(docker compose config --format json',
            step,
        )
        self.assertIn('json.load(sys.stdin)["name"]', step)
        self.assertIn(
            '--filter "label=com.docker.compose.project=$PROJECT_NAME"',
            step,
        )
        self.assertIn(
            "--filter label=com.docker.compose.volume=vault-recovery-uploader-state",
            step,
        )
        self.assertIn("--format '{{.Name}}'", step)
        self.assertNotIn('COMPOSE_PROJECT="$(basename "$PWD")"', step)
        self.assertNotIn('"${COMPOSE_PROJECT}_', step)

    def test_stale_lock_migration_outcome_distinction(self) -> None:
        """0 matching volumes is the ONLY safe skip (after a SUCCESSFUL
        listing); project-resolution failure, empty name, a Docker
        volume ls operational failure, >1 matches, and an uninspectable
        resolved volume all FAIL the deploy (never guess)."""
        step = _step_text(self.workflow, STALE_LOCK_MIGRATION_STEP_NAME)
        self.assertIn("set -euo pipefail", step)
        self.assertIn("cannot resolve the Compose project name", step)
        self.assertIn("resolved to empty", step)
        self.assertIn("cannot list Docker volumes", step)
        self.assertIn("expected exactly 1 vault-recovery-uploader-state volume", step)
        self.assertIn("not an inspectable Docker named volume", step)
        # Clean skip only for 0 matches, before any removal.
        self.assertIn("stale-lock migration skipped", step)
        self.assertIn("exit 0", step)
        self.assertLess(
            step.index("exit 0"),
            step.index("docker ps -aq"),
        )

    def test_stale_lock_migration_mount_check_fails_closed(self) -> None:
        """ANY container (running or stopped) still mounting the volume
        FAILS the deploy: the mount check runs before the lock removal and
        a docker ps operational failure is never treated as "no users"."""
        step = _step_text(self.workflow, STALE_LOCK_MIGRATION_STEP_NAME)
        self.assertIn(
            'MOUNTED_CONTAINERS="$(docker ps -aq --filter "volume=$STATE_VOLUME"',
            step,
        )
        self.assertIn("cannot list containers mounting", step)
        self.assertIn("still mount volume", step)
        self.assertIn("racing a live user", step)
        # The mount check precedes the lock-removal container.
        self.assertLess(
            step.index("docker ps -aq"),
            step.index("rmdir /state/.upload.lock"),
        )

    def test_stale_lock_migration_removes_only_empty_lock_via_rmdir(self) -> None:
        """The migration removes ONLY an EMPTY legacy lock directory, via
        `rmdir` inside a disposable named-volume container: no broad
        rm/rm -rf, no secret/state deletion, clean skip when the lock is
        absent, and every abnormal lock state fails the deploy."""
        step = _step_text(self.workflow, STALE_LOCK_MIGRATION_STEP_NAME)
        # Removal happens inside a container mounting the NAMED volume.
        self.assertIn('-v "$STATE_VOLUME:/state"', step)
        self.assertIn("alpine:3.20", step)
        self.assertLess(
            step.index('-v "$STATE_VOLUME:/state"'),
            step.index("rmdir /state/.upload.lock"),
        )
        # rmdir is the ONLY removal primitive; no broad rm anywhere.
        self.assertIn("rmdir /state/.upload.lock", step)
        self.assertNotIn("rm -f", step)
        self.assertNotIn("rm -rf", step)
        # Abnormal lock states fail closed.
        self.assertIn("is not a directory; refusing to remove it", step)
        self.assertIn("is not empty; refusing to remove it", step)
        self.assertIn("still present after rmdir", step)
        # Clean skip when the lock is absent.
        self.assertIn("no legacy .upload.lock present", step)
        # No secret/state files are ever referenced for deletion.
        self.assertNotIn("rclone.active.conf", step)
        self.assertNotIn("uploaded-generations.jsonl", step)
        self.assertNotIn("ledger", step)
        self.assertNotIn("next-slot", step)
        # The lock is never read or printed (only its state is described).
        self.assertNotIn("cat /state/.upload.lock", step)

    def test_vault_recovery_overlay_hardcodes_remote_identity(self) -> None:
        """Runtime remote immutability (council fix): the overlay wires the
        validated/probed remote identity as LITERALS in BOTH services. The
        values are never ${...}-interpolated, so the runner environment or
        a stale `.env` (which Compose interpolation prefers over the `.env`
        file) can never silently re-route backups to a different remote
        than the one the deploy preflight validated and the readiness gate
        probed."""
        overlay = VAULT_RECOVERY_OVERLAY.read_text(encoding="utf-8")
        for service in ("vault-recovery-uploader", "vault-recovery-recover"):
            self.assertIn(
                f"- VAULT_RECOVERY_RCLONE_REMOTE=vault-recovery-crypt",
                overlay,
                f"{service} must hardcode the validated remote name",
            )
            self.assertIn(
                f"- VAULT_RECOVERY_RCLONE_PATH=Josemar/vault-recovery",
                overlay,
                f"{service} must hardcode the validated remote path",
            )
        # No interpolation may re-introduce override precedence for either
        # key anywhere in the overlay.
        self.assertNotIn("${VAULT_RECOVERY_RCLONE_REMOTE", overlay)
        self.assertNotIn("${VAULT_RECOVERY_RCLONE_PATH", overlay)

    # --- Phase 3: vault-recovery as the default deployment lane ---

    def test_compose_file_always_includes_vault_recovery_overlay(self) -> None:
        # The vault-recovery overlay is appended unconditionally right after
        # the base file, BEFORE any optional overlay and before the
        # MNEMOSYNE_DEPLOY_MODE case.
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        self.assertIn(
            'COMPOSE_FILE_VALUE="${COMPOSE_FILE_VALUE}:docker-compose.vault-recovery.yml"',
            derive,
        )
        self.assertEqual(derive.count("docker-compose.vault-recovery.yml"), 1)
        case_idx = derive.index('case "${MNEMOSYNE_DEPLOY_MODE:-off}" in')
        self.assertLess(derive.index("docker-compose.vault-recovery.yml"), case_idx)
        # The overlay is applied regardless of mode: it is outside the case.
        case_branches = derive[case_idx:]
        self.assertNotIn("docker-compose.vault-recovery.yml", case_branches)

    def test_vault_recovery_portability_gate_precedes_teardown(self) -> None:
        names = _step_names(self.workflow)
        self.assertIn("Vault-recovery portability proof (mandatory release gate)", names)
        gate_idx = names.index("Vault-recovery portability proof (mandatory release gate)")
        stop_idx = names.index("Stop existing services")
        self.assertLess(gate_idx, stop_idx)
        gate = self.workflow["jobs"]["deploy"]["steps"][gate_idx]
        self.assertIn("test_vault_recovery_portability", gate["run"])
        self.assertEqual(gate["env"].get("RUN_DOCKER_TESTS"), "1")
        self.assertEqual(gate["env"].get("VAULT_RECOVERY_PORTABILITY_REQUIRED"), "1")

    def test_vault_recovery_dr_drill_gate_precedes_teardown_and_is_mandatory(self) -> None:
        # Phase 3: the FULL disaster-recovery drill is a MANDATORY release
        # gate (not recommended): it runs before any teardown/mutation with
        # VAULT_RECOVERY_DR_DRILL_REQUIRED=1, so a missing docker CLI or any
        # failed assertion FAILS the deploy.
        names = _step_names(self.workflow)
        self.assertIn("Vault-recovery disaster-recovery drill (mandatory release gate)", names)
        gate_idx = names.index("Vault-recovery disaster-recovery drill (mandatory release gate)")
        stop_idx = names.index("Stop existing services")
        self.assertLess(gate_idx, stop_idx)
        gate = self.workflow["jobs"]["deploy"]["steps"][gate_idx]
        self.assertIn("test_vault_recovery_dr_drill", gate["run"])
        self.assertEqual(gate["env"].get("RUN_DOCKER_TESTS"), "1")
        self.assertEqual(gate["env"].get("VAULT_RECOVERY_DR_DRILL_REQUIRED"), "1")

    def test_post_start_vault_recovery_checks_present(self) -> None:
        step = _step_text(self.workflow, "Verify vault-recovery deployment (uploader + export cron + plaintext absence)")
        self.assertIn("vault-recovery-uploader is running", step)
        self.assertIn("ps vault-recovery-uploader", step)
        # Export cron with the real jobs.json schema.
        self.assertIn("vault-recovery-export", step)
        self.assertIn("schedule.get(\"kind\") != \"cron\"", step)
        self.assertIn('expected_expr = "0 4 * * *"', step)
        self.assertIn("hermes-vault-recovery-export-cron.sh", step)
        self.assertIn("no_agent is not true", step)
        # workdir must equal /opt/data EXACTLY (not merely nonempty).
        self.assertIn('if workdir != "/opt/data":', step)
        self.assertIn('workdir is not \'/opt/data\'', step)
        # Plaintext absence.
        self.assertIn("retired plaintext obsidian-backup container still exists", step)
        # Runs after start.
        self.assertLess(
            _step_index(self.workflow, "Start services"),
            _step_index(self.workflow, "Verify vault-recovery deployment (uploader + export cron + plaintext absence)"),
        )

    def test_vault_recovery_export_cron_wait_is_bounded_with_clear_timeout(self) -> None:
        """Regression (false-negative deploy race): the verify step read
        jobs.json at 15:55:18-19 with 0 jobs while the Hermes init only
        created the vault-recovery-export cron job at 15:55:24 (health
        verified at 15:55:14). The step must poll for the named job with a
        BOUNDED budget instead of a single read, and a job still missing
        after the budget must fail the deploy with a clear diagnostic
        (Hermes init logs cron creation failures nonfatally)."""
        step = _step_text(self.workflow, "Verify vault-recovery deployment (uploader + export cron + plaintext absence)")
        # Bounded polling loop for the named job (60s budget, 5s interval).
        self.assertIn('job_name = "vault-recovery-export"', step)
        self.assertIn("deadline = time.monotonic() + 60", step)
        self.assertIn("while time.monotonic() < deadline:", step)
        self.assertIn("time.sleep(5)", step)
        # Timeout path: clear diagnostic + non-zero exit (missing job fails).
        self.assertIn("timed out after 60s waiting for the {job_name!r} cron job", step)
        self.assertIn("docker compose logs hermes", step)
        timeout_part = step.split("timed out after 60s waiting for the {job_name!r} cron job")[1]
        self.assertIn("sys.exit(1)", timeout_part)

    def test_vault_recovery_export_cron_strict_validation_runs_after_wait(self) -> None:
        """The bounded wait only establishes PRESENCE; the strict schema
        validation (schedule expr 0 4 * * *, script, no_agent, workdir) must
        still run after the wait and must reject an invalid, duplicated or
        missing job."""
        step = _step_text(self.workflow, "Verify vault-recovery deployment (uploader + export cron + plaintext absence)")
        # The wait loop precedes the strict assertions.
        self.assertLess(
            step.index("while time.monotonic() < deadline:"),
            step.index('expected_expr = "0 4 * * *"'),
        )
        # Strict validation preserved: schedule, script, no_agent, workdir.
        self.assertIn('expected_expr = "0 4 * * *"', step)
        self.assertIn('schedule.get("kind") != "cron"', step)
        self.assertIn("expr != expected_expr", step)
        self.assertIn("hermes-vault-recovery-export-cron.sh", step)
        self.assertIn('job.get("no_agent") is not True:', step)
        self.assertIn('if workdir != "/opt/data":', step)
        # Invalid/missing/duplicated job after the wait still fails.
        self.assertIn("expected exactly 1 vault-recovery-export cron job, got", step)
        self.assertIn("schedule.expr {expr!r} does not match VAULT_RECOVERY_EXPORT_SCHEDULE", step)

    def test_maximal_compose_set_includes_vault_recovery_overlay(self) -> None:
        verify = _step_text(self.workflow, "Verify embeddings overlay selection")
        self.assertIn("docker-compose.vault-recovery.yml", verify)
        off = _step_text(self.workflow, "Verify Mnemosyne off (overlays absent, provider disabled)")
        self.assertIn("docker-compose.vault-recovery.yml", off)

    def test_stop_workflow_retires_obsidian_backup(self) -> None:
        stop_wf = (REPO_ROOT / ".github" / "workflows" / "stop-service.yml").read_text(encoding="utf-8")
        self.assertNotIn("obsidian-backup", stop_wf)
        self.assertIn("vault-recovery-uploader", stop_wf)
        self.assertIn("docker-compose.vault-recovery.yml", stop_wf)

    def test_stop_workflow_tears_down_maximal_superset_with_orphans(self) -> None:
        # Blocker: stop-service must down the MAXIMAL overlay set/profiles (or
        # remove orphans), not just the base + vault-recovery composition, so
        # any prior overlay service (browser-control, embeddings, mnemosyne,
        # mnemosyne-backup) is removed too.
        stop_wf = (REPO_ROOT / ".github" / "workflows" / "stop-service.yml").read_text(encoding="utf-8")
        for overlay in (
            "docker-compose.yml",
            "docker-compose.vault-recovery.yml",
            "docker-compose.browser-control.yml",
            "docker-compose.embeddings.yml",
            "docker-compose.mnemosyne.yml",
            "docker-compose.mnemosyne-backup.yml",
        ):
            self.assertIn(overlay, stop_wf, f"stop workflow missing {overlay}")
        for profile in ("--profile aux-ml", "--profile browser-control", "--profile recovery"):
            self.assertIn(profile, stop_wf, f"stop workflow missing {profile}")
        self.assertIn("down --remove-orphans", stop_wf)
        # No -v: named volumes are preserved.
        self.assertNotIn("down --remove-orphans -v", stop_wf)
        self.assertNotIn("down -v", stop_wf)

    def test_stop_workflow_verifies_all_overlay_services_absent(self) -> None:
        # The stop workflow must verify browser/embeddings/Mnemosyne/
        # vault-recovery overlay services are absent, not just the base
        # services.
        stop_wf = (REPO_ROOT / ".github" / "workflows" / "stop-service.yml").read_text(encoding="utf-8")
        for service in (
            "hermes",
            "aux-ml",
            "syncthing",
            "tailscale",
            "vault-recovery-uploader",
            "vault-recovery-recover",
            "browser-tunnel",
            "embeddings",
            "mnemosyne-backup-uploader",
            "mnemosyne-backup-recover",
        ):
            self.assertIn(service, stop_wf, f"stop workflow does not verify {service} absent")

    def test_env_example_retires_plaintext_vars(self) -> None:
        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("OBSIDIAN_BACKUP_TIME=", env_example)
        self.assertNotIn("OBSIDIAN_BACKUP_SLOTS=", env_example)
        self.assertNotIn("OBSIDIAN_GDRIVE_REMOTE=", env_example)
        self.assertNotIn("OBSIDIAN_GDRIVE_PATH=", env_example)
        # The retired lane is documented, and the vault-recovery remote is
        # the default.
        self.assertIn("RETIRED: plaintext obsidian-backup lane", env_example)
        self.assertIn("VAULT_RECOVERY_RCLONE_REMOTE=vault-recovery-crypt", env_example)

    # --- mode-specific verification commands (blockers #5,#6,#7,#8) ---

    def test_off_check_uses_maximal_compose_set_for_stale_detection(self) -> None:
        off = _step_text(self.workflow, "Verify Mnemosyne off (overlays absent, provider disabled)")
        # Must query with the MAXIMAL compose file set, not the selected off config.
        self.assertIn("MAXIMAL_ARGS", off)
        for overlay in (
            "docker-compose.yml",
            "docker-compose.browser-control.yml",
            "docker-compose.embeddings.yml",
            "docker-compose.mnemosyne.yml",
            "docker-compose.mnemosyne-backup.yml",
        ):
            self.assertIn(overlay, off)

    def test_off_check_uses_hermes_cli_load_config(self) -> None:
        off = _step_text(self.workflow, "Verify Mnemosyne off (overlays absent, provider disabled)")
        self.assertIn("hermes_cli.config", off)
        self.assertIn("load_config", off)
        # Must use /opt/data/config.yaml (via load_config), NOT /opt/hermes/config.yaml.
        self.assertNotIn("/opt/hermes/config.yaml", off)

    def test_off_check_asserts_provider_blank_and_static_restored(self) -> None:
        off = _step_text(self.workflow, "Verify Mnemosyne off (overlays absent, provider disabled)")
        self.assertIn("memory", off)
        self.assertIn("provider", off)
        self.assertIn("memory_enabled", off)
        self.assertIn("user_profile_enabled", off)
        # Must assert nested mnemosyne config absent.
        self.assertIn("mnemosyne", off)

    def test_off_check_asserts_no_export_cron(self) -> None:
        off = _step_text(self.workflow, "Verify Mnemosyne off (overlays absent, provider disabled)")
        self.assertIn("mnemosyne-backup-export", off)
        self.assertIn("jobs.json", off)

    def test_pilot_check_uses_hermes_cli_load_config(self) -> None:
        pilot = _step_text(
            self.workflow,
            "Verify Mnemosyne pilot (TEI healthy + provider/policy active)",
        )
        self.assertIn("hermes_cli.config", pilot)
        self.assertIn("load_config", pilot)
        self.assertNotIn("/opt/hermes/config.yaml", pilot)

    def test_pilot_check_asserts_provider_and_policy_and_nested_config(self) -> None:
        pilot = _step_text(
            self.workflow,
            "Verify Mnemosyne pilot (TEI healthy + provider/policy active)",
        )
        self.assertIn("mnemosyne", pilot)
        self.assertIn("memory_enabled", pilot)
        self.assertIn("user_profile_enabled", pilot)
        # Must assert nested mnemosyne config present.
        self.assertIn("nested", pilot.lower())

    def test_pilot_check_handles_nonfatal_init_activation_failure(self) -> None:
        pilot = _step_text(
            self.workflow,
            "Verify Mnemosyne pilot (TEI healthy + provider/policy active)",
        )
        self.assertIn("mnemosyne runtime config activation failed", pilot)

    def test_pilot_check_waits_for_init_to_write_config(self) -> None:
        # The hermes cont-init (00-josemar-setup) finishes after the cheap
        # healthcheck passes, so the provider check must poll (bounded retry)
        # instead of reading the config once. Regression: run 30866789430 saw
        # provider '' because the verify step read the config 1s after the init
        # started.
        pilot = _step_text(
            self.workflow,
            "Verify Mnemosyne pilot (TEI healthy + provider/policy active)",
        )
        # The provider check is wrapped in a retry loop that waits for the init.
        self.assertIn("Waiting for Hermes init to write Mnemosyne config", pilot)
        self.assertIn("PROVIDER_OK", pilot)
        self.assertIn("seq 1 12", pilot)
        # The single-shot check must NOT be used (regression guard): the
        # success sentinel must only print after the retry loop.
        self.assertIn("PILOT_PROVIDER_POLICY_OK", pilot)

    def test_tei_wait_budget_at_least_450_seconds(self) -> None:
        pilot = _step_text(
            self.workflow,
            "Verify Mnemosyne pilot (TEI healthy + provider/policy active)",
        )
        # 120 iterations x 5s = 600s >= 450s.
        self.assertIn("seq 1 120", pilot)
        self.assertIn("sleep 5", pilot)
        # The wait budget comment must document 600s.
        self.assertIn("600s", pilot)

    def test_tei_check_fails_immediately_on_container_exit(self) -> None:
        pilot = _step_text(
            self.workflow,
            "Verify Mnemosyne pilot (TEI healthy + provider/policy active)",
        )
        self.assertIn("exited", pilot)
        self.assertIn("embeddings container exited", pilot)

    def test_backup_check_confirms_uploader_running(self) -> None:
        backup = _step_text(
            self.workflow,
            "Verify Mnemosyne backup (uploader running + exactly one export cron)",
        )
        self.assertIn("mnemosyne-backup-uploader", backup)
        self.assertIn("Up", backup)

    def test_backup_check_uses_canonical_cron_validator(self) -> None:
        backup = _step_text(
            self.workflow,
            "Verify Mnemosyne backup (uploader running + exactly one export cron)",
        )
        self.assertIn("/opt/hermes/.venv/bin/python3", backup)
        self.assertIn(
            "/opt/josemar/scripts/verify_mnemosyne_backup_cron.py", backup
        )
        self.assertIn("--jobs-file /opt/data/cron/jobs.json", backup)
        self.assertIn(
            '--expected-interval "${MNEMOSYNE_BACKUP_EXPORT_INTERVAL}"', backup
        )
        self.assertNotIn("<<'PY'", backup)
        self.assertNotIn('data.get("jobs")', backup)
        self.assertNotIn("schedule.get", backup)

    def test_dockerfile_copies_canonical_cron_validator(self) -> None:
        self.assertIn(
            "COPY scripts/verify_mnemosyne_backup_cron.py "
            "/opt/josemar/scripts/verify_mnemosyne_backup_cron.py",
            DOCKERFILE.read_text(encoding="utf-8"),
        )

    def test_mode_specific_steps_gated_correctly(self) -> None:
        steps = self.workflow["jobs"]["deploy"]["steps"]
        off_step = next(s for s in steps if s.get("name") == "Verify Mnemosyne off (overlays absent, provider disabled)")
        pilot_step = next(s for s in steps if s.get("name") == "Verify Mnemosyne pilot (TEI healthy + provider/policy active)")
        backup_step = next(s for s in steps if s.get("name") == "Verify Mnemosyne backup (uploader running + exactly one export cron)")
        self.assertIn("vars.MNEMOSYNE_DEPLOY_MODE != 'pilot'", off_step.get("if", ""))
        self.assertIn("vars.MNEMOSYNE_DEPLOY_MODE != 'backup'", off_step.get("if", ""))
        self.assertIn("vars.MNEMOSYNE_DEPLOY_MODE == 'pilot'", pilot_step.get("if", ""))
        self.assertIn("vars.MNEMOSYNE_DEPLOY_MODE == 'backup'", pilot_step.get("if", ""))
        self.assertEqual(backup_step.get("if"), "vars.MNEMOSYNE_DEPLOY_MODE == 'backup'")

    def test_embeddings_variable_is_strict_default_off_before_teardown(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn('GBRAIN_EMBEDDINGS_ENABLED="$GBRAIN_EMBEDDINGS_ENABLED_INPUT"', validate)
        self.assertIn('GBRAIN_EMBEDDINGS_ENABLED="false"', validate)
        self.assertIn("must be 'true' or 'false'", validate)
        self.assertLess(
            _step_index(self.workflow, "Validate required repository variables"),
            _step_index(self.workflow, "Stop existing services"),
        )

    def test_embeddings_overlay_is_selected_once_and_in_order(self) -> None:
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        self.assertIn('GBRAIN_EMBEDDINGS_ENABLED}" = "true"', derive)
        self.assertEqual(derive.count("docker-compose.embeddings.yml"), 1)
        self.assertLess(derive.index("docker-compose.browser-control.yml"), derive.index("docker-compose.embeddings.yml"))
        self.assertLess(derive.index("docker-compose.embeddings.yml"), derive.index("docker-compose.mnemosyne.yml"))

    def test_effective_embeddings_value_is_persisted(self) -> None:
        env = _step_text(self.workflow, "Create .env file")
        self.assertIn('write_env GBRAIN_EMBEDDINGS_ENABLED "$GBRAIN_EMBEDDINGS_ENABLED"', env)
        self.assertIn('echo "GBRAIN_EMBEDDINGS_ENABLED=$GBRAIN_EMBEDDINGS_ENABLED" >> "$GITHUB_ENV"', env)

    # --- TaskNotes MCP daily-note task links (issue #139) ---

    def test_tasknotes_daily_links_variable_is_strict_default_on_before_mutation(
        self,
    ) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        # Repository variable feeds the strict validation.
        self.assertIn(
            "TASKNOTES_DAILY_LINKS_ENABLED_INPUT: "
            "${{ vars.TASKNOTES_DAILY_LINKS_ENABLED }}",
            self.text,
        )
        self.assertIn(
            'TASKNOTES_DAILY_LINKS_ENABLED="$TASKNOTES_DAILY_LINKS_ENABLED_INPUT"',
            validate,
        )
        # Missing/empty normalizes to enabled (stable default-on).
        self.assertIn('if [ -z "$TASKNOTES_DAILY_LINKS_ENABLED" ]; then', validate)
        self.assertIn('TASKNOTES_DAILY_LINKS_ENABLED="true"', validate)
        # Nonempty values are case-insensitive true/false, normalized to
        # lowercase for .env; anything else is rejected before any mutation.
        self.assertIn("tr '[:upper:]' '[:lower:]'", validate)
        self.assertIn(
            "ERROR: TASKNOTES_DAILY_LINKS_ENABLED must be 'true' or 'false'",
            validate,
        )
        # The effective normalized boolean persists for later steps.
        self.assertIn(
            'echo "TASKNOTES_DAILY_LINKS_ENABLED=$TASKNOTES_DAILY_LINKS_ENABLED" >> "$GITHUB_ENV"',
            validate,
        )
        # Preflight-before-mutation ordering (same gate as the embeddings
        # switch): validation runs before any service teardown.
        self.assertLess(
            _step_index(self.workflow, "Validate required repository variables"),
            _step_index(self.workflow, "Stop existing services"),
        )

    def test_tasknotes_daily_links_env_file_value_is_normalized_and_persisted(
        self,
    ) -> None:
        env = _step_text(self.workflow, "Create .env file")
        self.assertIn(
            "TASKNOTES_DAILY_LINKS_ENABLED_INPUT: "
            "${{ vars.TASKNOTES_DAILY_LINKS_ENABLED }}",
            self.text,
        )
        # The .env step re-derives and re-validates independently so the
        # generated .env carries normalized lowercase true/false.
        self.assertIn(
            'TASKNOTES_DAILY_LINKS_ENABLED="$TASKNOTES_DAILY_LINKS_ENABLED_INPUT"',
            env,
        )
        self.assertIn(
            "ERROR: TASKNOTES_DAILY_LINKS_ENABLED must be 'true' or 'false'",
            env,
        )
        self.assertIn(
            'write_env TASKNOTES_DAILY_LINKS_ENABLED "$TASKNOTES_DAILY_LINKS_ENABLED"',
            env,
        )

    def _tasknotes_validation_block(self, step: str, flag: str) -> str:
        """Return the contiguous validation paragraph for a TaskNotes flag.

        Both preflight steps derive each flag from its ``<flag>_INPUT``
        repository variable, validate, and continue with a blank line, so
        slicing from the derivation to the next blank line isolates the
        exact strict-boolean block.
        """
        start = step.index(f'{flag}="${flag}_INPUT"')
        return step[start : step.index("\n\n", start)]

    def test_tasknotes_daily_links_flags_validation_matrix(self) -> None:
        """Issue #139 revision 3 (W4b) flag matrix: master
        (TASKNOTES_DAILY_LINKS_ENABLED) and reconcile slave
        (TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED) each get, in BOTH
        preflight steps (validation + .env write): a stable default-on
        resolution for missing/empty, case-insensitive true/false
        normalization that preserves an explicit false (rollout/rollback
        override), and fail-closed rejection of any invalid value. Both
        flags flow through the single propagation path (repo variable ->
        validation -> GITHUB_ENV/.env -> Compose -> hermes-config); there
        is no parallel config source."""
        flags = (
            "TASKNOTES_DAILY_LINKS_ENABLED",
            "TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED",
        )
        for flag in flags:
            with self.subTest(flag=flag):
                # The repository variable feeds both preflight steps.
                self.assertIn(
                    f"{flag}_INPUT: ${{{{ vars.{flag} }}}}",
                    self.text,
                )
                for step_name in (
                    "Validate required repository variables",
                    "Create .env file",
                ):
                    step = _step_text(self.workflow, step_name)
                    block = self._tasknotes_validation_block(step, flag)
                    # Default: missing/empty resolves to enabled (true).
                    self.assertIn(f'if [ -z "${flag}" ]; then', block)
                    self.assertIn(f'{flag}="true"', block)
                    # Nonempty values: case-insensitive true/false only;
                    # the lowercased value flows through unchanged, so an
                    # explicit FALSE/True stays a real false/true override.
                    self.assertIn("tr '[:upper:]' '[:lower:]'", block)
                    # Invalid values fail closed before any mutation.
                    self.assertIn(
                        f"ERROR: {flag} must be 'true' or 'false'",
                        block,
                    )
                    self.assertIn("exit 1", block)
                # Validation still precedes any service teardown.
                self.assertLess(
                    _step_index(
                        self.workflow, "Validate required repository variables"
                    ),
                    _step_index(self.workflow, "Stop existing services"),
                )
                # Single .env write path (no parallel config source): the
                # normalized value is persisted exactly once, unchanged.
                env = _step_text(self.workflow, "Create .env file")
                self.assertEqual(env.count(f"write_env {flag} "), 1)
                self.assertIn(
                    f'write_env {flag} "${flag}"',
                    env,
                )

        # The reconcile slave is validated/exported alongside the master in
        # the validation step's persistence block.
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn(
            'echo "TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED='
            '$TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED" >> "$GITHUB_ENV"',
            validate,
        )

    def test_post_start_embeddings_presence_health_check(self) -> None:
        verify = _step_text(self.workflow, "Verify embeddings overlay selection")
        self.assertIn('MAXIMAL_ARGS', verify)
        self.assertIn('ps embeddings', verify)
        self.assertIn('healthy', verify)
        self.assertIn('ps --all --format json embeddings', verify)
        self.assertLess(_step_index(self.workflow, "Start services"), _step_index(self.workflow, "Verify embeddings overlay selection"))

    def test_embeddings_overlay_matrix_permits_standalone_off(self) -> None:
        # Matrix: GBRAIN_EMBEDDINGS_ENABLED (true/false) x MNEMOSYNE_DEPLOY_MODE
        # (off/pilot/backup). The embeddings overlay is selected when the
        # variable is true OR the mode is pilot/backup — exactly once, with no
        # duplication and no embeddings-only restriction tied to Mnemosyne.
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        off = _step_text(self.workflow, "Verify Mnemosyne off (overlays absent, provider disabled)")

        # The standalone selection condition (true + off is sufficient).
        self.assertIn(
            'if [ "${GBRAIN_EMBEDDINGS_ENABLED}" = "true" ]'
            ' || [ "${MNEMOSYNE_DEPLOY_MODE:-off}" = "pilot" ]'
            ' || [ "${MNEMOSYNE_DEPLOY_MODE:-off}" = "backup" ]; then',
            derive,
        )

        # The embeddings overlay is appended exactly once, before the mode
        # case statement (never inside a case branch, never duplicated).
        case_idx = derive.index('case "${MNEMOSYNE_DEPLOY_MODE:-off}" in')
        self.assertEqual(derive.count("docker-compose.embeddings.yml"), 1)
        self.assertLess(derive.index("docker-compose.embeddings.yml"), case_idx)
        case_branches = derive[case_idx:]
        self.assertNotIn("docker-compose.embeddings.yml", case_branches)

        # Mode branches append only the Mnemosyne overlays; off appends none,
        # so standalone (true + off) yields base + embeddings only.
        backup_branch = case_branches.split("backup)")[1].split(";;")[0]
        self.assertIn("docker-compose.mnemosyne.yml", backup_branch)
        self.assertIn("docker-compose.mnemosyne-backup.yml", backup_branch)
        pilot_branch = case_branches.split("pilot)")[1].split(";;")[0]
        self.assertIn("docker-compose.mnemosyne.yml", pilot_branch)
        self.assertNotIn("docker-compose.mnemosyne-backup.yml", pilot_branch)
        off_branch = case_branches.split("off)")[1].split(";;")[0]
        self.assertNotIn("docker-compose.mnemosyne", off_branch)

        # Preserved off-mode behavior with GBRAIN_EMBEDDINGS_ENABLED=false:
        # the Mnemosyne-off verifier still runs its maximal-set stale check,
        # provider/cron checks, and forbids Mnemosyne overlay services.
        self.assertIn("MAXIMAL_ARGS", off)
        self.assertIn("mnemosyne-backup-export", off)
        stale_loop = off.split("for svc in")[1].split("; do")[0]
        self.assertIn("mnemosyne-backup-uploader", stale_loop)
        self.assertIn("mnemosyne-backup-recover", stale_loop)
        # Standalone gbrain embeddings must NOT be rejected by the off verifier:
        # the embeddings service is not in the stale-services loop and is
        # verified solely by the dedicated "Verify embeddings overlay
        # selection" step.
        self.assertNotIn("embeddings", stale_loop)
        verify = _step_text(self.workflow, "Verify embeddings overlay selection")
        self.assertIn('GBRAIN_EMBEDDINGS_ENABLED}" = "true"', verify)
        self.assertIn("healthy", verify)
        self.assertIn("is absent", verify)


class HermesBaseImageProductionPinContractTests(unittest.TestCase):
    """R1 contract: the production HERMES_BASE_IMAGE repository variable is
    a reviewed pin, not a free override.

    The pre-mutation validation step must reject any non-empty value other
    than the reviewed Dockerfile.hermes pin BEFORE any .env write/mutation;
    empty remains allowed so Compose uses its repo-authored default. The
    workflow guard literal must stay in lockstep with the Dockerfile ARG to
    prevent drift. Pure-source: YAML/text parsing only, no Docker, no
    network.
    """

    def setUp(self) -> None:
        assert yaml is not None, "PyYAML is required for these contract tests"
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.validate = _step_text(self.workflow, "Validate required repository variables")

    def _workflow_guard_pins(self) -> list[str]:
        """ALL exact-match guard literals in the validation step.

        Returns every `[ "$HERMES_BASE_IMAGE" != "<literal>" ]` match so
        callers can assert exactly one guard exists before comparing — no
        first-match blind spot, no second divergent guard.
        """
        return re.findall(r'\[ "\$HERMES_BASE_IMAGE" != "([^"]+)" \]', self.validate)

    def _dockerfile_arg_pin(self) -> str:
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^ARG\s+HERMES_BASE_IMAGE=(\S+)\s*$", line)
            if match:
                return match.group(1)
        raise AssertionError("ARG HERMES_BASE_IMAGE not found in Dockerfile.hermes")

    def test_validation_step_precedes_create_env(self) -> None:
        # The guard runs in the pre-mutation validation step, strictly
        # before the Create .env file step (any .env write/mutation).
        self.assertLess(
            _step_index(self.workflow, "Validate required repository variables"),
            _step_index(self.workflow, "Create .env file"),
        )
        # The validate step itself never writes/mutates .env.
        self.assertNotIn("write_env", self.validate)
        self.assertNotIn("> .env", self.validate)

    def test_validation_step_exposes_repository_variable_in_env(self) -> None:
        steps = self.workflow["jobs"]["deploy"]["steps"]
        step = next(
            s for s in steps
            if s.get("name") == "Validate required repository variables"
        )
        self.assertEqual(
            step.get("env", {}).get("HERMES_BASE_IMAGE"),
            "${{ vars.HERMES_BASE_IMAGE }}",
        )

    def test_guard_rejects_mismatch_with_exit_1(self) -> None:
        # The guard fires only for a non-empty value (empty stays allowed).
        self.assertIn(
            'if [ -n "$HERMES_BASE_IMAGE" ] && [ "$HERMES_BASE_IMAGE" != "',
            self.validate,
        )
        # The guard body exits nonzero with a reviewed-pin diagnostic.
        guard_body = self.validate.split('if [ -n "$HERMES_BASE_IMAGE" ]')[1].split("\n          fi")[0]
        self.assertIn("exit 1", guard_body)
        self.assertIn("ERROR: HERMES_BASE_IMAGE", guard_body)

    def test_guard_uses_reviewed_exact_pin(self) -> None:
        pins = self._workflow_guard_pins()
        # Exactly one exact-match guard literal: a second (possibly
        # divergent) guard must fail here, not hide behind first-match.
        self.assertEqual(len(pins), 1, f"expected exactly one guard literal, got: {pins}")
        self.assertEqual(pins[0], REVIEWED_HERMES_BASE_IMAGE)

    def test_error_explains_pin_and_tripwire_change_together(self) -> None:
        # The error must explain that upgrades change the Dockerfile pin,
        # the Compose default build arg, and the reviewed tripwire together.
        self.assertIn("Dockerfile.hermes", self.validate)
        self.assertIn("docker-compose.yml", self.validate)
        self.assertIn("EXPECTED_HERMES_BASE_IMAGE", self.validate)
        self.assertIn("tests/skill_state/test_models_overlay.py", self.validate)
        self.assertIn("TOGETHER", self.validate)

    def test_env_write_preserved_and_empty_allowed(self) -> None:
        # Empty falls through the guard so Compose uses its repo-authored
        # default; the later Create .env write behavior is preserved (now
        # guarded by the earlier validation step).
        env_step = _step_text(self.workflow, "Create .env file")
        self.assertIn('write_env HERMES_BASE_IMAGE "$HERMES_BASE_IMAGE"', env_step)

    def test_compose_default_build_arg_matches_reviewed_pin(self) -> None:
        """The effective empty-value production Compose build arg must be
        exactly `HERMES_BASE_IMAGE: ${HERMES_BASE_IMAGE:-<reviewed pin>}`
        (the `:-` operator: empty OR unset falls back to the default). Tied
        to the independently hardcoded reviewed pin AND the Dockerfile ARG
        comparison: a Compose-default-only drift fails here."""
        compose = BASE_COMPOSE.read_text(encoding="utf-8")
        expected = "HERMES_BASE_IMAGE: ${HERMES_BASE_IMAGE:-" + REVIEWED_HERMES_BASE_IMAGE + "}"
        self.assertIn(expected, compose)
        # Exactly one such build arg: no second, divergent default.
        self.assertEqual(compose.count(expected), 1)
        # The `:-` empty-or-unset operator is required; a plain `-`
        # (unset-only) default would pass an EMPTY build arg on an empty
        # repository variable and override the Dockerfile ARG with "".
        self.assertNotIn(
            "HERMES_BASE_IMAGE: ${HERMES_BASE_IMAGE-" + REVIEWED_HERMES_BASE_IMAGE + "}",
            compose,
        )
        # Cross-tie: the Compose default is the same reviewed pin as the
        # Dockerfile ARG (Compose-default-only drift cannot pass).
        pins = self._workflow_guard_pins()
        self.assertEqual(len(pins), 1)
        self.assertEqual(self._dockerfile_arg_pin(), REVIEWED_HERMES_BASE_IMAGE)
        self.assertEqual(pins[0], self._dockerfile_arg_pin())

    def test_pin_matches_dockerfile_arg_to_prevent_drift(self) -> None:
        # The workflow guard literal and the Dockerfile.hermes ARG default
        # must both be the exact reviewed pin: changing one without the
        # other fails here.
        pins = self._workflow_guard_pins()
        self.assertEqual(len(pins), 1, f"expected exactly one guard literal, got: {pins}")
        self.assertEqual(pins[0], self._dockerfile_arg_pin())
        self.assertEqual(self._dockerfile_arg_pin(), REVIEWED_HERMES_BASE_IMAGE)


class GbrainBackfillWorkflowContractTests(unittest.TestCase):
    """Pure-source contract for the manual, non-deploying backfill workflow."""

    def setUp(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "gbrain-embedding-backfill.yml"
        self.text = path.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_manual_self_hosted_workflow_and_exact_confirmation(self) -> None:
        self.assertIn("workflow_dispatch", self.text)
        self.assertEqual(self.workflow["jobs"]["backfill"]["runs-on"], "self-hosted")
        self.assertIn("ENABLE_AND_BACKFILL", self.text)
        self.assertIn('CONFIRMATION" != "ENABLE_AND_BACKFILL"', self.text)

    def test_fail_closed_variable_prefix_and_runtime_checks(self) -> None:
        self.assertIn('GBRAIN_EMBEDDINGS_ENABLED" != "true"', self.text)
        self.assertIn(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", self.text)
        self.assertIn("docker inspect --type container", self.text)
        self.assertIn(".State.Running", self.text)
        self.assertIn("GBRAIN_EMBEDDING_MODEL", self.text)
        self.assertIn("GBRAIN_EMBEDDING_DIMENSIONS", self.text)

    def test_only_explicit_operator_commands_run(self) -> None:
        self.assertIn("docker exec --user hermes --workdir /opt/data", self.text)
        self.assertIn("josemar-gbrain enable-embeddings", self.text)
        self.assertIn("josemar-gbrain embed-backfill", self.text)
        self.assertIn("-e HOME=/opt/data", self.text)
        self.assertIn("-e HERMES_HOME=/opt/data", self.text)
        self.assertIn("-e GBRAIN_HOME=/opt/data", self.text)
        self.assertIn("-e XDG_CONFIG_HOME=/opt/data/.config", self.text)
        self.assertIn("Smoke-test activated embeddings as hermes", self.text)
        self.assertNotIn("docker compose up", self.text)
        self.assertNotIn("docker compose build", self.text)
        self.assertNotIn("docker compose down", self.text)
        self.assertNotIn("secrets.", self.text)

    def test_runtime_identity_and_post_backfill_smoke_contract(self) -> None:
        self.assertIn('test "$(id -un)" = hermes', self.text)
        self.assertIn('test "$(id -u)" != 0', self.text)
        self.assertIn('test "$PWD" = /opt/data', self.text)
        self.assertIn('test "$(gbrain config get search.mcp_keyword_only)" = false', self.text)
        self.assertIn('test -f /opt/data/.gbrain/embedding-backfill-complete.json', self.text)


class BackupOverlayContractTests(unittest.TestCase):
    """Source-level contract for docker-compose.mnemosyne-backup.yml."""

    def setUp(self) -> None:
        self.text = BACKUP_OVERLAY.read_text(encoding="utf-8")

    def test_both_rclone_services_pinned_to_digest(self) -> None:
        self.assertEqual(self.text.count(RCLONE_DIGEST), 2, "both rclone services must be pinned to the digest")
        self.assertNotIn("rclone/rclone:latest", self.text)

    def test_no_stale_no_deployment_workflow_changes_comment(self) -> None:
        self.assertNotIn("NO deployment workflow changes", self.text)
        self.assertNotIn("no deployment workflow changes", self.text)

    def test_header_documents_deploy_mode(self) -> None:
        self.assertIn("MNEMOSYNE_DEPLOY_MODE", self.text)


class CronSchemaFixtureTests(unittest.TestCase):
    """Fixture-based tests for the real jobs.json cron schema the deploy
    workflow parses. These do NOT execute the workflow; they validate the
    schema contract against a fixture matching the actual Hermes cron
    install output (see docker-hermes-init.sh install_mnemosyne_backup_export_cron).
    """

    def test_real_cron_schema_fixture_validates(self) -> None:
        """A fixture matching the real installed job must satisfy the schema
        checks the deploy workflow performs."""
        fixture = {
            "jobs": [
                {
                    "name": "mnemosyne-backup-export",
                    "schedule": {
                        "kind": "interval",
                        "minutes": 30,
                        "display": "every 30m",
                    },
                    "script": "mnemosyne-backup-export.sh",
                    "no_agent": True,
                    "workdir": "/opt/data",
                }
            ],
            "updated_at": "2026-08-03T00:00:00Z",
        }
        data = fixture
        jobs = data.get("jobs")
        self.assertIsInstance(jobs, list)
        export_jobs = [j for j in jobs if isinstance(j, dict) and j.get("name") == "mnemosyne-backup-export"]
        self.assertEqual(len(export_jobs), 1)
        job = export_jobs[0]
        schedule = job.get("schedule")
        self.assertIsInstance(schedule, dict)
        self.assertEqual(schedule.get("kind"), "interval")
        minutes = schedule.get("minutes")
        self.assertIsInstance(minutes, int)
        self.assertNotIsInstance(minutes, bool)
        self.assertEqual(minutes, 30)
        self.assertEqual(job.get("script"), "mnemosyne-backup-export.sh")
        self.assertIs(job.get("no_agent"), True)
        # workdir must equal /opt/data exactly (the deploy check rejects any
        # other value, not just empty).
        self.assertEqual(job.get("workdir"), "/opt/data")

    def test_bool_minutes_rejected_by_schema_contract(self) -> None:
        """A bool `minutes` (True/False are ints in Python) must be rejected."""
        fixture = {
            "jobs": [
                {
                    "name": "mnemosyne-backup-export",
                    "schedule": {"kind": "interval", "minutes": True},
                    "script": "mnemosyne-backup-export.sh",
                    "no_agent": True,
                    "workdir": "/opt/data",
                }
            ],
        }
        job = fixture["jobs"][0]
        minutes = job["schedule"]["minutes"]
        # The contract: isinstance(minutes, int) and not isinstance(minutes, bool).
        self.assertTrue(isinstance(minutes, int))
        self.assertTrue(isinstance(minutes, bool))
        # So the rejection condition (isinstance bool) must trigger.
        self.assertTrue(isinstance(minutes, bool))

    def test_wrong_kind_rejected(self) -> None:
        fixture = {
            "jobs": [
                {
                    "name": "mnemosyne-backup-export",
                    "schedule": {"kind": "cron", "minutes": 30},
                    "script": "mnemosyne-backup-export.sh",
                    "no_agent": True,
                    "workdir": "/opt/data",
                }
            ],
        }
        self.assertNotEqual(fixture["jobs"][0]["schedule"]["kind"], "interval")

    def test_wrong_script_rejected(self) -> None:
        fixture = {
            "jobs": [
                {
                    "name": "mnemosyne-backup-export",
                    "schedule": {"kind": "interval", "minutes": 30},
                    "script": "wrong-script.sh",
                    "no_agent": True,
                    "workdir": "/opt/data",
                }
            ],
        }
        self.assertNotEqual(fixture["jobs"][0]["script"], "mnemosyne-backup-export.sh")

    def test_no_agent_false_rejected(self) -> None:
        fixture = {
            "jobs": [
                {
                    "name": "mnemosyne-backup-export",
                    "schedule": {"kind": "interval", "minutes": 30},
                    "script": "mnemosyne-backup-export.sh",
                    "no_agent": False,
                    "workdir": "/opt/data",
                }
            ],
        }
        self.assertIsNot(fixture["jobs"][0]["no_agent"], True)

    def test_empty_workdir_rejected(self) -> None:
        fixture = {
            "jobs": [
                {
                    "name": "mnemosyne-backup-export",
                    "schedule": {"kind": "interval", "minutes": 30},
                    "script": "mnemosyne-backup-export.sh",
                    "no_agent": True,
                    "workdir": "",
                }
            ],
        }
        self.assertFalse(fixture["jobs"][0]["workdir"])

    def test_multiple_export_jobs_rejected(self) -> None:
        fixture = {
            "jobs": [
                {
                    "name": "mnemosyne-backup-export",
                    "schedule": {"kind": "interval", "minutes": 30},
                    "script": "mnemosyne-backup-export.sh",
                    "no_agent": True,
                    "workdir": "/opt/data",
                },
                {
                    "name": "mnemosyne-backup-export",
                    "schedule": {"kind": "interval", "minutes": 60},
                    "script": "mnemosyne-backup-export.sh",
                    "no_agent": True,
                    "workdir": "/opt/data",
                },
            ],
        }
        export_jobs = [j for j in fixture["jobs"] if j.get("name") == "mnemosyne-backup-export"]
        self.assertEqual(len(export_jobs), 2)

    def test_empty_jobs_json_fixture(self) -> None:
        """The empty jobs.json shape (off mode) has no export job."""
        fixture = {"jobs": [], "updated_at": None}
        export_jobs = [j for j in fixture["jobs"] if isinstance(j, dict) and j.get("name") == "mnemosyne-backup-export"]
        self.assertEqual(len(export_jobs), 0)


class DocsContractTests(unittest.TestCase):
    """Docs must document the three modes and not claim stale state."""

    def test_mnemosyne_operations_documents_modes(self) -> None:
        docs = (REPO_ROOT / "docs" / "mnemosyne-operations.md").read_text(encoding="utf-8")
        for mode in ("off", "pilot", "backup"):
            self.assertIn(mode, docs)
        self.assertIn("MNEMOSYNE_DEPLOY_MODE", docs)
        self.assertIn("MNEMOSYNE_BACKUP_EXPORT_INTERVAL", docs)
        self.assertIn("RCLONE_CONFIG_B64", docs)
        self.assertNotIn("no deployment workflow changes", docs.lower())

    def test_mnemosyne_operations_documents_remediations(self) -> None:
        docs = (REPO_ROOT / "docs" / "mnemosyne-operations.md").read_text(encoding="utf-8")
        # Validation before state mutation.
        self.assertIn("Validation before state mutation", docs)
        # Fail-closed teardown.
        self.assertIn("fail-closed", docs.lower())
        # 600s TEI wait budget.
        self.assertIn("600s", docs)
        # hermes_cli.config.load_config() /opt/data/config.yaml.
        self.assertIn("hermes_cli.config.load_config()", docs)
        self.assertIn("/opt/data/config.yaml", docs)
        # Real cron schema.
        self.assertIn("jobs", docs)
        self.assertIn("interval", docs)
        # Baseline gdrive remote.
        self.assertIn("gdrive", docs)
        # Leading zeros / max.
        self.assertIn("leading zeros", docs.lower())
        self.assertIn("10080", docs)

    def test_env_example_documents_modes_and_backup_variable(self) -> None:
        env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("MNEMOSYNE_DEPLOY_MODE", env)
        self.assertIn("MNEMOSYNE_BACKUP_EXPORT_INTERVAL", env)
        for mode in ("off", "pilot", "backup"):
            self.assertIn(mode, env)
        self.assertNotIn("NO deployment workflow changes", env)
        # Leading zeros and max documented (phrase may span comment line breaks).
        self.assertIn("leading", env.lower())
        self.assertIn("zeros", env.lower())
        self.assertIn("10080", env)
        # Baseline gdrive documented.
        self.assertIn("gdrive", env)

    def test_workflows_agents_md_documents_mnemosyne_deploy_mode(self) -> None:
        agents = (REPO_ROOT / ".github" / "workflows" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("MNEMOSYNE_DEPLOY_MODE", agents)
        self.assertIn("MNEMOSYNE_BACKUP_EXPORT_INTERVAL", agents)
        self.assertNotIn("tears down with base + overlay + browser-control profile before", agents)
        # Remediated behaviors documented.
        self.assertIn("fail-closed", agents.lower())
        self.assertIn("aux-ml", agents)
        # Phase 3: the default-lane crypt remote is documented (the retired
        # baseline gdrive requirement is gone).
        self.assertIn("vault-recovery-crypt", agents)
        self.assertIn("hermes_cli.config.load_config()", agents)
        self.assertIn("/opt/data/config.yaml", agents)

    # --- README deploy-mode / overlay / doc-link drift guards ---

    def test_readme_documents_mnemosyne_deploy_mode_table(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        # The operator-facing deploy-mode table is present.
        self.assertIn("MNEMOSYNE_DEPLOY_MODE", readme)
        for mode in ("off", "pilot", "backup"):
            self.assertIn(mode, readme)
        # Backup prerequisites documented in the table.
        self.assertIn("MNEMOSYNE_BACKUP_EXPORT_INTERVAL", readme)
        self.assertIn("RCLONE_CONFIG_B64", readme)
        self.assertIn("mnemosyne-crypt", readme)
        # Phase 3: the default-lane crypt remote is documented (the retired
        # baseline gdrive requirement is gone).
        self.assertIn("vault-recovery-crypt", readme)
        # The 10080-minute ceiling is referenced.
        self.assertIn("10080", readme)

    def test_readme_documents_overlay_compose_files(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for overlay in (
            "docker-compose.mnemosyne.yml",
            "docker-compose.mnemosyne-backup.yml",
        ):
            self.assertIn(overlay, readme, f"README missing overlay {overlay}")

    def test_readme_documents_backup_uploader_service_and_volumes(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("mnemosyne-backup-uploader", readme)
        for volume in (
            "mnemosyne-backup-staging",
            "mnemosyne-backup-state",
            "mnemosyne-backup-recovery",
        ):
            self.assertIn(volume, readme, f"README missing volume {volume}")

    def test_readme_links_mnemosyne_operations_and_retrieval_docs(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/mnemosyne-operations.md", readme)
        self.assertIn("docs/mnemosyne-retrieval-quality.md", readme)
        # Both linked docs must actually exist.
        self.assertTrue((REPO_ROOT / "docs" / "mnemosyne-operations.md").exists())
        self.assertTrue((REPO_ROOT / "docs" / "mnemosyne-retrieval-quality.md").exists())

    def test_readme_states_local_staging_not_encrypted(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        # Accurate encryption boundary: local staging is NOT encrypted;
        # encryption begins at the rclone crypt remote; recovery is
        # operator-controlled.
        self.assertIn("not encrypted", readme.lower())
        self.assertIn("crypt", readme.lower())
        self.assertIn("operator-controlled", readme.lower())

    def test_readme_preserves_canonical_vault_path(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        # gbrain -> Obsidian vault remains the canonical curated vault path,
        # and Mnemosyne is described as separate / not a vault replacement.
        self.assertIn("canonical curated vault", readme.lower())
        self.assertIn("not a vault replacement", readme.lower())

    def test_readme_documents_tei_no_host_port(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        # TEI embeddings on an internal / no-host-port boundary.
        self.assertIn("no host port", readme.lower())

    def test_readme_avoids_stale_no_deployment_workflow_claims(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("no deployment workflow changes", readme.lower())
        self.assertNotIn("NO deployment workflow changes", readme)


class DashboardAuthWordingContractTests(unittest.TestCase):
    """Static-token wording contract (issue #156 revision 2, W4 alignment).

    The deploy validation must keep REQUIRING
    `HERMES_DASHBOARD_SESSION_TOKEN` unchanged, but its error wording must
    present it as loopback/legacy dashboard compatibility only — never as
    the production non-loopback Hermes Desktop Remote credential (the gated
    REST/WS path rejects the static token; Desktop authenticates via the
    bundled `basic` provider password login).
    """

    def setUp(self) -> None:
        assert yaml is not None, "PyYAML is required for these contract tests"
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        # The dashboard auth secrets are validated (and written to .env) in
        # the "Create .env file" step, not the variables-only validation step.
        self.create_env = _step_text(workflow, "Create .env file")

    def test_static_token_still_required_and_shape_validated(self) -> None:
        # Validation unchanged: non-empty requirement plus the URL-safe
        # minimum-length shape check.
        self.assertIn('if [ -z "$HERMES_DASHBOARD_SESSION_TOKEN" ]; then', self.create_env)
        self.assertIn(
            '[[ ! "$HERMES_DASHBOARD_SESSION_TOKEN" =~ ^[A-Za-z0-9._~-]{32,}$ ]]',
            self.create_env,
        )

    def test_static_token_error_wording_is_loopback_legacy_scoped(self) -> None:
        self.assertIn("loopback/legacy dashboard compatibility", self.create_env)
        self.assertIn(
            "not the production non-loopback Desktop Remote credential",
            self.create_env,
        )
        # The retired claims must not come back.
        self.assertNotIn(
            "required for Hermes Desktop dashboard access", self.create_env
        )
        self.assertNotIn("for Hermes Desktop REST/WebSocket access", self.create_env)


if __name__ == "__main__":
    unittest.main()
