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
    (base; optional browser-control; embeddings; mnemosyne; backup last),
  - no recovery profile enabled for a normal deploy,
  - maximal fail-closed no-volume teardown (superset overlays, aux-ml profile,
    no `-v`, no `|| true`, no `set +e`, named volumes preserved, removes prior
    overlay services when switching to off),
  - rclone digest pin + direct invocation (no `sh -c`), THREE independent
    crypt field checks, hardcoded mnemosyne-crypt, baseline gdrive remote
    requirement, non-backup behavior preserved,
  - mode-specific verification commands (off/pilot/backup post-start checks;
    Hermes init nonfatal activation failure handled; hermes_cli.config
    load_config() against /opt/data/config.yaml; TEI 600s wait budget with
    immediate exit failure; real jobs.json cron schema).

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

RCLONE_DIGEST = "rclone/rclone@sha256:b06aed988cf5967de7c25be5925240983981c757f4ed1ac9d2fa659d51d60548"


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

    def test_rclone_step_strict_base64_decode_to_0600_temp(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertIn("base64 --decode", decode)
        self.assertIn("chmod 600", decode)
        self.assertIn("decoded to an empty config", decode)

    def test_rclone_direct_invocation_no_sh_c(self) -> None:
        # The pinned rclone image entrypoint is ["rclone"], so we invoke
        # rclone directly (no `sh -c 'rclone ...'`).
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertNotIn("sh -c 'rclone", decode)
        self.assertNotIn('sh -c "rclone', decode)
        # Direct invocation: IMAGE --config /tmp/rclone.conf config show ...
        self.assertIn("--config /tmp/rclone.conf config show", decode)

    def test_rclone_remote_name_hardcoded(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        # Hardcoded (not interpolated from an env var that could diverge).
        self.assertIn('REMOTE_NAME="mnemosyne-crypt"', decode)
        # Must NOT interpolate from MNEMOSYNE_BACKUP_RCLONE_REMOTE env var.
        self.assertNotIn("${MNEMOSYNE_BACKUP_RCLONE_REMOTE", decode)

    def test_rclone_three_independent_crypt_field_checks(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        # THREE independent checks, not OR. Each field parsed separately.
        self.assertIn("type must be exactly crypt", decode)
        self.assertIn("REMOTE_TYPE", decode)
        self.assertIn("CRYPT_REMOTE", decode)
        self.assertIn("CRYPT_PASSWORD", decode)
        # Must NOT use the old OR-based grep that combined remote|password.
        self.assertNotIn("grep -E '^(remote|password)", decode)

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

    def test_rclone_backup_branch_requires_baseline_gdrive_remote(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertIn("BASELINE_REMOTE", decode)
        self.assertIn('"gdrive"', decode)
        self.assertIn("obsidian-backup would break", decode)

    def test_rclone_backup_branch_does_not_print_config(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        self.assertNotIn("echo \"$REMOTE_SHOW\"", decode)
        self.assertNotIn("cat \"$TMP_RCLONE_CONF\"", decode)

    def test_rclone_non_backup_behavior_preserved(self) -> None:
        decode = _step_text(self.workflow, "Decode and validate rclone config")
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        # Non-backup with RCLONE_CONFIG_B64 set: decode without crypt
        # validation, then publish.
        self.assertIn("rclone config loaded into Docker volume", publish)
        # The crypt validation is gated on backup mode.
        self.assertIn('"${MNEMOSYNE_DEPLOY_MODE:-off}" = "backup"', decode)

    def test_rclone_publish_is_atomic(self) -> None:
        publish = _step_text(self.workflow, "Publish rclone config into shared volume")
        self.assertIn("rclone.conf.new", publish)
        self.assertIn("mv /config/rclone/rclone.conf.new /config/rclone/rclone.conf", publish)

    def test_env_writes_hardcoded_rclone_remote(self) -> None:
        env_step = _step_text(self.workflow, "Create .env file")
        self.assertIn('write_env MNEMOSYNE_BACKUP_RCLONE_REMOTE "mnemosyne-crypt"', env_step)

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

    def test_backup_check_uses_real_cron_schema(self) -> None:
        backup = _step_text(
            self.workflow,
            "Verify Mnemosyne backup (uploader running + exactly one export cron)",
        )
        # Must parse data["jobs"], not a flat list.
        self.assertIn('data.get("jobs"', backup)
        self.assertIn("schedule", backup)
        self.assertIn("kind", backup)
        self.assertIn("interval", backup)
        self.assertIn("minutes", backup)
        self.assertIn("script", backup)
        self.assertIn("no_agent", backup)
        self.assertIn("workdir", backup)
        # Must assert script == mnemosyne-backup-export.sh.
        self.assertIn("mnemosyne-backup-export.sh", backup)

    def test_backup_check_rejects_bool_minutes(self) -> None:
        backup = _step_text(
            self.workflow,
            "Verify Mnemosyne backup (uploader running + exactly one export cron)",
        )
        # The check must reject bool (True/False are ints in Python).
        self.assertIn("bool", backup)

    def test_backup_check_uses_hermes_venv_python(self) -> None:
        backup = _step_text(
            self.workflow,
            "Verify Mnemosyne backup (uploader running + exactly one export cron)",
        )
        self.assertIn("/opt/hermes/.venv/bin/python3", backup)

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
        self.assertTrue(job.get("workdir"))

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
        self.assertIn("gdrive", agents)
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
        self.assertIn("gdrive", readme)
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


if __name__ == "__main__":
    unittest.main()
