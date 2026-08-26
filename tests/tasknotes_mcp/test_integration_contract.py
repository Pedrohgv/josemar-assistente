"""Static runtime-wiring contracts for the TaskNotes MCP adapter."""

from __future__ import annotations

import datetime
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# Canonical effective-week Base formula (issue #128): resolves to the ISO
# Monday date of the effective week using officially supported Bases
# primitives (isEmpty/date/duration/number/format with the Moment-style "E"
# ISO weekday token, Monday=1). Pinned verbatim in docs/tasknotes-mcp.md;
# this constant must stay byte-identical to the documented formula body.
EFFECTIVE_WEEK_FORMULA = (
    "if((scheduled.isEmpty() == false), (date(scheduled) - "
    '(duration("1d") * (number(date(scheduled).format("E")) - 1)))'
    '.format("YYYY-MM-DD"), '
    'if((planned_week.isEmpty() == false), date(planned_week)'
    '.format("YYYY-MM-DD"), "Backlog"))'
)


class IntegrationContractTests(unittest.TestCase):
    def test_hermes_registers_serial_stdio_server_with_bounded_timeouts(self) -> None:
        text = (REPO_ROOT / "config" / "hermes-config.yaml").read_text(encoding="utf-8")
        self.assertIn("mcp_servers:", text)
        self.assertIn('command: "/opt/hermes/.venv/bin/python3"', text)
        self.assertIn('"/opt/josemar/scripts/tasknotes_mcp.py"', text)
        for name in (
            "LLAMA_SERVER_BASE_URL",
            "GBRAIN_EMBEDDING_MODEL_REVISION",
            "GBRAIN_EMBEDDING_MODEL",
            "GBRAIN_EMBEDDING_DIMENSIONS",
        ):
            self.assertIn(f'{name}: "${{{name}}}"', text)
        self.assertIn("connect_timeout: 30", text)
        self.assertIn("timeout: 180", text)
        self.assertIn("supports_parallel_tool_calls: false", text)

    def test_image_installs_server_core_lock_runner_and_skill(self) -> None:
        text = (REPO_ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")
        self.assertIn("COPY skills-factory/tasknotes /opt/josemar/skills/tasknotes", text)
        for name in (
            "tasknotes_mcp_core.py",
            "tasknotes_mcp.py",
            "tasknotes_lock_run.py",
        ):
            self.assertIn(name, text)
        self.assertIn("/opt/hermes/.venv/bin/python3 -m compileall -q", text)

    def test_image_installs_gbrain_public_layout(self) -> None:
        """The PUBLIC /usr/local/bin/gbrain must be the issue #110 adapter;
        gbrain-chat-run must remain only as a backwards-compatible symlink
        alias, not a duplicate script."""
        text = (REPO_ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")
        self.assertIn(
            "COPY scripts/gbrain_chat_run.py /usr/local/bin/gbrain", text
        )
        self.assertIn(
            "ln -s /usr/local/bin/gbrain /usr/local/bin/gbrain-chat-run", text
        )
        self.assertNotIn(
            "COPY scripts/gbrain_chat_run.py /usr/local/bin/gbrain-chat-run", text
        )

    def test_image_installs_native_cli_at_private_path(self) -> None:
        """The native CLI must live at the private non-PATH path; no native
        wrapper may be installed at the public /usr/local/bin/gbrain."""
        text = (REPO_ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")
        self.assertIn("/opt/josemar/libexec/gbrain-native", text)
        self.assertNotIn(
            "> /usr/local/bin/gbrain", text
        )

    def test_tasknotes_mcp_uses_private_native_path_without_env_override(self) -> None:
        """TaskNotes is a trusted internal native user: it must invoke the
        private native CLI directly (never the public adapter) through a
        fixed constant with no environment escape hatch."""
        text = (REPO_ROOT / "scripts" / "tasknotes_mcp.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('TASKNOTES_GBRAIN_NATIVE = "/opt/josemar/libexec/gbrain-native"', text)
        self.assertNotIn("TASKNOTES_GBRAIN_BIN", text)
        self.assertNotIn("/usr/local/bin/gbrain", text)

    def test_tasknotes_mcp_locations_are_fixed(self) -> None:
        """The vault, gbrain state, and the shared lock must be fixed
        constants — no GBRAIN_BRAIN_REPO / GBRAIN_HOME / TASKNOTES_LOCK_DIR
        environment overrides."""
        text = (REPO_ROOT / "scripts" / "tasknotes_mcp.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('TASKNOTES_VAULT = "/opt/data/obsidian"', text)
        self.assertIn('TASKNOTES_GBRAIN_HOME = "/opt/data"', text)
        self.assertIn('TASKNOTES_LOCK_DIR = "/opt/data/.locks"', text)
        self.assertNotIn('os.environ.get("GBRAIN_BRAIN_REPO"', text)
        self.assertNotIn('os.environ.get("GBRAIN_HOME"', text)
        self.assertNotIn('os.environ.get("TASKNOTES_LOCK_DIR"', text)

    def test_tasknotes_mcp_enforces_runtime_identity(self) -> None:
        text = (REPO_ROOT / "scripts" / "tasknotes_mcp.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _assert_runtime_identity", text)
        self.assertIn("os.geteuid() == 0", text)
        self.assertIn("refuses to run as root", text)

    def test_crons_enforce_non_root_identity_before_lock(self) -> None:
        """Both cron entrypoints must enforce the hermes runtime identity
        (fail-closed non-root check) before touching the lock, instead of
        relying on base-image behavior."""
        for cron in (
            "hermes-gbrain-refresh-cron.sh",
            "hermes-gbrain-embedding-refresh-cron.sh",
        ):
            text = (REPO_ROOT / "scripts" / cron).read_text(encoding="utf-8")
            self.assertIn("/usr/bin/id -u", text)
            self.assertIn("refuses to run as root", text)
            self.assertIn("*[!0-9]*", text)

    def test_refresh_cron_uses_shared_nonblocking_lock(self) -> None:
        text = (REPO_ROOT / "scripts" / "hermes-gbrain-refresh-cron.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("tasknotes.lock", text)
        self.assertIn("--nonblocking", text)
        self.assertIn("--timeout", text)
        self.assertIn('if [ "$status" -eq 75 ]', text)
        self.assertIn("refresh skipped", text)

    def test_refresh_cron_invokes_runner_with_fixed_isolated_interpreter(self) -> None:
        """The cron must start the lock runner with the literal fixed image
        interpreter in isolated mode (-I) so PYTHONPATH/sitecustomize from
        the cron environment cannot execute code before the flock; no
        interpreter override may exist."""
        text = (REPO_ROOT / "scripts" / "hermes-gbrain-refresh-cron.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"/opt/hermes/.venv/bin/python3" -I "$lock_runner"', text)
        self.assertNotIn("GBRAIN_PYTHON_BIN", text)

    def test_skill_names_only_the_six_supported_tools(self) -> None:
        text = (REPO_ROOT / "skills-factory" / "tasknotes" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "task_create",
            "task_get",
            "task_list",
            "task_update",
            "task_complete",
            "task_archive",
        ):
            self.assertIn(f"`{name}`", text)
        self.assertIn("Do not use native `gbrain", text)
        self.assertIn("Hermes cron", text)
        self.assertIn("One author at a time", text)
        self.assertIn("YYYY-MM-DD-HHmmss-slugified-title", text)
        self.assertIn("auto-generated", text)
        self.assertIn("https://tasknotes.dev/", text)
        self.assertIn("Plugin configuration", text)
        self.assertIn("native recurrence", text)
        self.assertIn("task_add_tag", text)
        self.assertIn("task_remove_tag", text)
        self.assertIn("custom_fields", text)

    def test_runbook_documents_external_prerequisites_and_recovery(self) -> None:
        text = (REPO_ROOT / "docs" / "tasknotes-mcp.md").read_text(encoding="utf-8")
        self.assertIn("https://tasknotes.dev/", text)
        self.assertIn("Verify the existing gbrain Git repository", text)
        self.assertIn("reinitialize an existing vault", text)
        self.assertIn("Exclude `.git/` from Syncthing", text)
        self.assertIn("TaskNotes `4.11.1`", text)
        self.assertIn("config-adaptive", text)
        self.assertIn("moveArchivedTasks` is supported", text)
        self.assertIn("tasknotes-recovery.marker", text)
        self.assertIn("git -C \"$GBRAIN_BRAIN_REPO\" gc", text)
        self.assertIn("gbrain sources harden", text)
        self.assertIn("local-only gbrain", text)
        self.assertIn("pulls or pushes", text)
        self.assertIn("Task naming convention", text)
        self.assertIn("YYYY-MM-DD-HHmmss-slugified-title", text)
        self.assertIn("issues/3034", text)
        self.assertIn("Current limitations", text)
        self.assertIn("Unarchive", text)
        self.assertIn("recurrence", text)
        self.assertIn("status", text)
        self.assertIn("archived", text)

    def test_skill_documents_three_planning_states(self) -> None:
        """Issue #128: the always-loaded skill must present exactly the
        three effective planning states, the first-class/reserved
        planned_week argument, mutual exclusivity with automatic clearing,
        and pointers to the detailed reference/runbook."""
        text = (REPO_ROOT / "skills-factory" / "tasknotes" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## Planning states", text)
        self.assertIn("Backlog", text)
        self.assertIn("Week-planned", text)
        self.assertIn("Day-scheduled", text)
        self.assertIn("`planned_week`", text)
        self.assertIn("Monday", text)
        self.assertIn("mutually exclusive", text)
        self.assertIn("automatically clears the other", text)
        self.assertIn("Never pass `planned_week` through `custom_fields`", text)
        self.assertIn("reserved", text)
        self.assertIn("type `date`", text)
        self.assertIn("`clear_planned_week`", text)
        self.assertIn("references/custom-fields.md", text)
        self.assertIn("docs/tasknotes-mcp.md", text)

    def test_skill_stays_concise(self) -> None:
        """The skill organization policy keeps the always-loaded SKILL.md
        small; deep configuration/migration detail lives in references/ and
        the runbook."""
        text = (REPO_ROOT / "skills-factory" / "tasknotes" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertLess(len(text.splitlines()), 160)

    def test_custom_fields_reference_locks_planned_week_contract(self) -> None:
        """The reference must document the reserved key, the date user-field
        prerequisite with a safe configuration snippet, transition
        semantics, the scheduled-wins normalization trade-off, the read
        visibility exception, and legacy scheduled_week migration."""
        text = (
            REPO_ROOT
            / "skills-factory"
            / "tasknotes"
            / "references"
            / "custom-fields.md"
        ).read_text(encoding="utf-8")
        self.assertIn("`planned_week`", text)
        self.assertIn("rejected inside `custom_fields`", text)
        self.assertIn('"type": "date"', text)
        self.assertIn("Monday", text)
        self.assertIn("clear_planned_week", text)
        # R2: the profile constraint must present planned_week as a
        # required/valid date userFields entry while the generic MCP
        # custom_fields argument reserves it — no contradictory ban.
        self.assertIn("required — `userFields`", text)
        self.assertNotIn("must not be the semantic week-planning key", text)
        self.assertIn("scheduled wins", text)
        self.assertIn("never mutate", text)
        self.assertIn("task_list", text)
        self.assertIn("scheduled_week", text)
        # R2: legacy metadata is cleared through the bounded MCP path while
        # the field is still configured, before retiring it.
        self.assertIn('custom_fields={"scheduled_week": null}', text)
        self.assertIn("Rollback", text)
        self.assertIn("operator-owned", text)
        self.assertIn("docs/tasknotes-mcp.md", text)

    def test_runbook_documents_week_planning_migration_and_bases(self) -> None:
        """The runbook must document the semantic model, precise
        transitions, direct-edit trade-off, read visibility, effective-week
        Base guidance with citations, and the legacy migration/rollback."""
        text = (REPO_ROOT / "docs" / "tasknotes-mcp.md").read_text(encoding="utf-8")
        self.assertIn("## Week planning (issue #128)", text)
        self.assertIn("Backlog", text)
        self.assertIn("Week-planned", text)
        self.assertIn("Day-scheduled", text)
        self.assertIn("Monday", text)
        self.assertIn("clear_planned_week", text)
        # R1: canonical Monday-date grouping key, not a week-number label.
        self.assertIn(EFFECTIVE_WEEK_FORMULA, text)
        self.assertNotIn("YYYY-[W]WW", text)
        self.assertIn('format("E")', text)
        self.assertIn("Monday=`1`", text)
        self.assertIn("2025-12-29", text)
        self.assertIn("formula.effectiveWeek", text)
        self.assertIn("cannot reschedule", text)
        self.assertIn("default-base-templates", text)
        self.assertIn("help.obsidian.md/bases/functions", text)
        self.assertIn("https://tasknotes.dev/", text)
        self.assertIn("scheduled_week", text)
        # R2: legacy metadata is cleared through the bounded MCP path while
        # the field is still configured, before retiring it.
        self.assertIn('custom_fields={"scheduled_week": null}', text)
        self.assertIn("operator-owned", text)
        self.assertIn("Rollback", text)

    def test_effective_week_formula_reference_behavior(self) -> None:
        """R1 regression: the pinned effectiveWeek formula is present
        verbatim in the runbook, and a pure-Python equivalent of its
        arithmetic (datetime only — no Obsidian runtime) proves the
        grouping key is boundary-correct: any scheduled date maps to its
        ISO Monday, which is exactly what planned_week stores."""
        text = (REPO_ROOT / "docs" / "tasknotes-mcp.md").read_text(encoding="utf-8")
        self.assertIn(EFFECTIVE_WEEK_FORMULA, text)

        def effective_week(
            *,
            scheduled: str | None = None,
            planned_week: str | None = None,
        ) -> str:
            if scheduled is not None:
                day = datetime.date.fromisoformat(scheduled)
                return str(day - datetime.timedelta(days=day.isoweekday() - 1))
            if planned_week is not None:
                return planned_week
            return "Backlog"

        # Cross-year boundary: Friday 2026-01-02 belongs to the ISO week
        # starting Monday 2025-12-29; a task week-planned for that same week
        # stores exactly that Monday. Both must yield one identical key.
        self.assertEqual(effective_week(scheduled="2026-01-02"), "2025-12-29")
        self.assertEqual(effective_week(planned_week="2025-12-29"), "2025-12-29")
        self.assertEqual(
            effective_week(scheduled="2026-01-02"),
            effective_week(planned_week="2025-12-29"),
        )
        # Precedence and Backlog bucket mirror the formula's if/else order.
        self.assertEqual(
            effective_week(scheduled="2026-01-05", planned_week="2025-12-29"),
            "2026-01-05",
        )
        self.assertEqual(effective_week(), "Backlog")

    def test_runbook_pauses_all_three_owned_jobs_for_maintenance(self) -> None:
        """Issue #110 maintenance/recovery wording must require pausing ALL
        THREE owned jobs (both refresh crons plus vault-recovery-export);
        the narrower 'both crons' phrasing must not survive."""
        text = (REPO_ROOT / "docs" / "tasknotes-mcp.md").read_text(encoding="utf-8")
        self.assertIn("ALL THREE", text)
        for job in (
            "gbrain-refresh",
            "gbrain-embedding-refresh",
            "vault-recovery-export",
        ):
            self.assertIn(f"`{job}`", text)
        self.assertNotIn("BOTH", text)
        self.assertNotIn("Resume both crons", text)

    def test_compose_passes_refresh_timeout(self) -> None:
        text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("GBRAIN_REFRESH_TIMEOUT=${GBRAIN_REFRESH_TIMEOUT:-240}", text)


if __name__ == "__main__":
    unittest.main()
