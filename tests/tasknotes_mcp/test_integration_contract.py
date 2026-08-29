"""Static runtime-wiring contracts for the TaskNotes MCP adapter."""

from __future__ import annotations

import dataclasses
import datetime
import importlib.util
import inspect
import sys
import tempfile
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

# Issue #139: documented projection outcome states (docs/tasknotes-mcp.md
# "Projection outcomes" and references/daily-notes.md). The core constants
# must match this exact set.
DAILY_LINK_STATES = (
    "applied_and_committed",
    "not_applicable",
    "not_applied",
    "conflict",
    "write_failed",
    "commit_failed",
    "committed_sync_failed",
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


class DailyNoteProjectionDocsContract(unittest.TestCase):
    """Issue #139 W5: duplicated safety documentation and bounded-feature
    contracts must stay consistent across root AGENTS.md, both runbooks,
    the skill, and the deep-dive reference."""

    def setUp(self) -> None:
        self.runbook = (
            REPO_ROOT / "docs" / "tasknotes-mcp.md"
        ).read_text(encoding="utf-8")
        self.reference = (
            REPO_ROOT
            / "skills-factory"
            / "tasknotes"
            / "references"
            / "daily-notes.md"
        ).read_text(encoding="utf-8")
        self.skill = (
            REPO_ROOT / "skills-factory" / "tasknotes" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.gbrain_ops = (
            REPO_ROOT / "docs" / "gbrain-operations.md"
        ).read_text(encoding="utf-8")

    def test_task_markdown_stays_gbrain_only_in_every_invariant_copy(self) -> None:
        """The sole-writer invariant plus the single narrow Daily Note
        exception must be updated together in AGENTS.md, both runbooks,
        and the runbook intro — no copy may keep the old unqualified
        wording, and none may widen the exception into a generic writer."""
        for name, text, fragments in (
            (
                "AGENTS.md ownership bullet",
                self.agents,
                (
                    "default-off Daily Note task-link projection (issue #139)",
                    "never a generic note writer and never nests the public `gbrain` wrapper",
                ),
            ),
            (
                "AGENTS.md non-negotiable 6",
                self.agents,
                (
                    "filesystem exception (issue #139",
                    "mandatory native incremental gbrain reconciliation",
                    "generic note-write tool",
                ),
            ),
            (
                "docs/tasknotes-mcp.md intro",
                self.runbook,
                (
                    "sole task-file writer",
                    "never a generic note writer",
                ),
            ),
            (
                "docs/gbrain-operations.md non-negotiable 5",
                self.gbrain_ops,
                (
                    "derived Daily Note task-link projection",
                    "generic note-write tool",
                ),
            ),
        ):
            for fragment in fragments:
                self.assertIn(fragment, text, f"{name} missing: {fragment}")
        # The task runbook must state the architectural boundary section.
        self.assertIn("### Architectural boundary", self.runbook)
        self.assertIn("the sole projection source", self.runbook)

    def test_runbook_documents_daily_note_projection_contract(self) -> None:
        """The runbook must document the default-off flag, dynamic core
        Daily Notes config, the bounded date/template subset, structural
        `## Tasks` rules, canonical links, the transition matrix, ordering
        guarantees, concurrency/atomic/Git/sync behavior, projection-only
        outcome states, disabling behavior, and the v1 limitations."""
        text = self.runbook
        # Prose assertions run against whitespace-normalized text so
        # Markdown line wrapping cannot mask a wording change.
        prose = " ".join(text.split())
        # Section + flag contract.
        self.assertIn("## Daily Note task links (issue #139)", text)
        self.assertIn("TASKNOTES_DAILY_LINKS_ENABLED", text)
        self.assertIn("### Feature flag (default off)", text)
        self.assertIn("Missing or empty resolves to disabled", prose)
        self.assertIn("carry no `daily_link_*` fields", prose)
        # Dynamic core config, fail-closed, no second config source,
        # no Periodic Notes fallback.
        self.assertIn("### Daily Notes configuration (dynamic, fail-closed)", text)
        self.assertIn("daily-notes.json", text)
        self.assertIn("`folder`", text)
        self.assertIn("`format`", text)
        self.assertIn("`template`", text)
        self.assertIn("never read as a fallback", text)
        # R1 (issue #140): config freshness is per-transaction — the
        # config is read and strictly validated exactly once per
        # projection-bearing task transaction, before task side effects,
        # and the immutable snapshot carries through apply/commit/sync.
        # No engine/MCP lifetime cache and no restart-to-apply claim.
        self.assertIn("no engine or MCP lifetime cache", prose)
        self.assertIn("exactly once, before any task side effect", prose)
        self.assertIn("immutable snapshot", prose)
        self.assertIn("next projection-bearing task operation", prose)
        self.assertNotIn("lifetime of the TaskNotes MCP process", prose)
        self.assertNotIn("only after an MCP restart", prose)
        # R5 (issue #140): projection targets under the configured
        # `tasksFolder` or the active archive folder are rejected before
        # any task side effect (task Markdown is gbrain-only).
        self.assertIn("`tasksFolder`", text)
        self.assertIn("`archiveFolder`", text)
        self.assertIn("active archive folder", prose)
        # Date-format subset: supported tokens, safe separators, rejection.
        self.assertIn("### Supported date-format subset", text)
        self.assertIn("`YYYY YY MM M DD D`", text)
        self.assertIn("is rejected before any task side effect", prose)
        # Template variables and the no-execution prohibition.
        self.assertIn("### Template rendering (bounded, no execution)", text)
        self.assertIn("`{{date}}`", text)
        self.assertIn("`{{title}}`", text)
        self.assertIn("`{{date:FORMAT}}`", text)
        self.assertIn("Templater or JavaScript execution never happens", prose)
        # Structural contract on existing notes.
        self.assertIn("### Existing-note structural contract", text)
        self.assertIn("exactly one level-2 `## Tasks` heading", prose)
        self.assertIn("duplicated `## Tasks` section fails closed", prose)
        # R2 (issue #140): existing notes retain all bytes outside the
        # `## Tasks` transformation; frontmatter is never normalized or
        # reserialized, and the null/empty date/title fill is
        # creation-only.
        self.assertIn(
            "existing frontmatter is never normalized or reserialized", prose
        )
        self.assertIn("only when a missing note is created", prose)
        # Canonical link semantics.
        self.assertIn("### Canonical link semantics", text)
        self.assertIn("- [[<task-slug>|<display-alias>]]", text)
        self.assertIn("by exact wikilink target slug", prose)
        # R6 (issue #140): the alias is a derived serialized encoding —
        # reversible percent encoding of the structural metacharacters
        # only (% first), never matched; title semantics unchanged.
        for token in ("`%25`", "`%5B`", "`%5D`", "`%7C`"):
            self.assertIn(token, text)
        self.assertIn("derived, serialized encoding of the title", prose)
        self.assertIn("deterministic, reversible percent encoding", prose)
        self.assertIn("full enabled-mode TaskNotes title domain", prose)
        self.assertIn("does not alter task Markdown or title semantics", prose)
        self.assertIn("never by alias text", prose)
        self.assertIn("idempotent regardless of the encoded alias", prose)
        # Transition matrix and ordering guarantees.
        self.assertIn("### Transition matrix", text)
        self.assertIn("add under D2 **first**, then remove from D1", text)
        self.assertIn("remove link **after** verified deletion", text)
        self.assertIn("link retained while `scheduled` remains", text)
        self.assertIn("no future links pre-created", text)
        self.assertIn("never from caller intent alone", prose)
        # R4 (issue #140): plans are composed by resolved target path —
        # dates mapping to the same note perform one ensure, never
        # ensure+remove of the same link.
        self.assertIn("composed by resolved target path", prose)
        self.assertIn(
            "never an ensure followed by a remove of the same link", prose
        )
        # Concurrency/atomicity/Git/reconciliation.
        self.assertIn("### Concurrency, atomicity, Git, and reconciliation", text)
        self.assertIn("two attempts total", prose)
        self.assertIn("instead of overwriting concurrent edits", prose)
        # R3 (issue #140): missing-note creation publishes atomically
        # with no-clobber semantics; a competing creator is never
        # overwritten and the bounded retry follows.
        self.assertIn("no-clobber", prose)
        self.assertIn("is never overwritten", prose)
        self.assertIn("atomic `os.replace`", prose)
        self.assertIn("tasknotes-mcp: daily note projection", text)
        self.assertIn("staging only the affected Daily Note paths", prose)
        self.assertIn(
            "native incremental gbrain sync while the lock is still held", prose
        )
        # Projection outcome states and recovery-marker boundary.
        self.assertIn("### Projection outcomes", text)
        for state in DAILY_LINK_STATES:
            self.assertIn(f"`{state}`", text)
        self.assertIn("`daily_link_state`", text)
        self.assertIn("`daily_link_detail`", text)
        self.assertIn("`daily_link_dates`", text)
        self.assertIn(
            "never create the TaskNotes global recovery marker", prose
        )
        self.assertIn("`committed_sync_failed`", text)
        # Disabling behavior and v1 limitations.
        self.assertIn("### Disabling, manual edits, and v1 limitations", text)
        self.assertIn("no watcher or backfill in v1", prose)
        self.assertIn("are never bulk-removed", prose)
        self.assertIn("No Periodic Notes compatibility is", prose)

    def test_daily_notes_reference_documents_bounded_contract(self) -> None:
        """The deep-dive reference must carry the full bounded contract and
        stay pointed to by the always-loaded skill."""
        text = self.reference
        prose = " ".join(text.split())
        self.assertIn("# Daily Note task-link projection (issue #139)", text)
        self.assertIn("TASKNOTES_DAILY_LINKS_ENABLED", text)
        self.assertIn("default `false`", text)
        self.assertIn("/opt/data/.locks/tasknotes.lock", text)
        self.assertIn("Task files remain gbrain-only", prose)
        self.assertIn("wrapper nesting", prose)
        self.assertIn("daily-notes.json", text)
        self.assertIn("Periodic Notes config is never read", prose)
        # R1 (issue #140): per-transaction config read/validation — no
        # engine/MCP lifetime cache and no restart-to-apply claim.
        self.assertIn("no engine or MCP lifetime cache", prose)
        self.assertIn("exactly once, before any task side effect", prose)
        self.assertIn("immutable snapshot", prose)
        self.assertIn("next projection-bearing task operation", prose)
        self.assertNotIn("lifetime of the TaskNotes MCP process", prose)
        self.assertNotIn("only after an MCP restart", prose)
        # R5 (issue #140): task/archive folder targets are rejected
        # before any task side effect (task Markdown is gbrain-only).
        self.assertIn("`tasksFolder`", text)
        self.assertIn("`archiveFolder`", text)
        self.assertIn("active archive folder", prose)
        self.assertIn("`YYYY` (4-digit year)", prose)
        self.assertIn("`MM`/`M`", text)
        self.assertIn("`DD`/`D`", text)
        self.assertIn("`{{date}}`", text)
        self.assertIn("`{{title}}`", text)
        self.assertIn("`{{date:FORMAT}}`", text)
        self.assertIn("Templater/JavaScript is never executed", prose)
        self.assertIn("top-level `date` frontmatter key", prose)
        self.assertIn("exactly one `## Tasks` level-2 section", prose)
        # R2 (issue #140): byte preservation outside the section and
        # creation-only frontmatter fill.
        self.assertIn(
            "existing frontmatter is never normalized or reserialized", prose
        )
        self.assertIn("only when a missing note is created", prose)
        self.assertIn("- [[<task-slug>|<display-alias>]]", text)
        # R6 (issue #140): derived serialized alias — reversible percent
        # encoding of the structural metacharacters only (% first),
        # never matched or compared; title semantics unchanged.
        for token in ("`%25`", "`%5B`", "`%5D`", "`%7C`"):
            self.assertIn(token, text)
        self.assertIn("deterministic, reversible percent encoding", prose)
        self.assertIn("keeps the mapping injective", prose)
        self.assertIn("full enabled-mode TaskNotes title domain", prose)
        self.assertIn("does not alter task Markdown or title semantics", prose)
        self.assertIn("never matched or compared", prose)
        self.assertIn("idempotent regardless of the encoded alias", prose)
        self.assertIn("add D2 **first**, then remove D1", text)
        self.assertIn("remove after **verified** deletion", text)
        # R4 (issue #140): plan composition by resolved target path.
        self.assertIn("composed by resolved target path", prose)
        self.assertIn(
            "never an ensure followed by a remove of the same link", prose
        )
        self.assertIn("two attempts total", prose)
        # R3 (issue #140): atomic no-clobber creation publication.
        self.assertIn("no-clobber", prose)
        self.assertIn("is never overwritten", prose)
        self.assertIn("`os.replace`", text)
        self.assertIn("tasknotes-mcp: daily note projection", text)
        self.assertIn("at most 16 targets per commit", prose)
        for state in DAILY_LINK_STATES:
            self.assertIn(f"`{state}`", text)
        self.assertIn(
            "never create the global TaskNotes recovery marker", prose
        )
        self.assertIn("No watcher, cron, or bulk backfill in v1", prose)
        self.assertIn("never bulk-removed", prose)
        self.assertIn("No Periodic Notes compatibility", prose)
        # Cross-pointers.
        self.assertIn("`SKILL.md`", text)
        self.assertIn("`docs/tasknotes-mcp.md`", text)

    def test_skill_documents_daily_note_projection_boundary(self) -> None:
        """The always-loaded skill must present the concise boundary: the
        projection is adapter-owned, opt-in/default-off, never manually
        maintained during task workflows, with the deep-dive pointer —
        while keeping the skill under the organization line limit."""
        text = self.skill
        prose = " ".join(text.split())
        self.assertIn("## Daily Note links (opt-in)", text)
        self.assertIn("TASKNOTES_DAILY_LINKS_ENABLED", text)
        self.assertIn("default off", text)
        self.assertIn("Never edit a projected link manually", prose)
        self.assertIn("`- [[slug|title]]`", text)
        self.assertIn("`## Tasks`", text)
        self.assertIn("direct Obsidian task edits are not re-projected", prose)
        self.assertIn("Backlog and week-planned tasks are never projected", prose)
        self.assertIn("`references/daily-notes.md`", text)
        self.assertLess(len(text.splitlines()), 160)

    def test_runbook_mutation_outcomes_document_daily_fields(self) -> None:
        """The mutation-outcome section must tie the optional daily fields
        to the enabled feature without changing the authoritative states."""
        text = self.runbook
        prose = " ".join(text.split())
        self.assertIn(
            "`daily_link_state`, `daily_link_detail`, and `daily_link_dates`",
            prose,
        )
        self.assertIn(
            "They never change the authoritative task `state`", prose
        )
        self.assertIn("## Current limitations", text)
        self.assertIn(
            "Daily Note projection backfill/reconciliation (issue #139)", text
        )


class DailyProjectionCoreContract(unittest.TestCase):
    """Issue #139: the implemented core projection contract must match the
    documented bounded behavior (defaults, states, ordering, constants)."""

    @classmethod
    def setUpClass(cls) -> None:
        core_path = REPO_ROOT / "scripts" / "tasknotes_mcp_core.py"
        spec = importlib.util.spec_from_file_location(
            "tasknotes_mcp_core_contract", core_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["tasknotes_mcp_core_contract"] = module
        spec.loader.exec_module(module)
        cls.core = module

    def test_engine_flag_defaults_to_disabled_and_is_strict(self) -> None:
        signature = inspect.signature(self.core.TaskNotesEngine.__init__)
        param = signature.parameters["daily_links_enabled"]
        self.assertIs(param.default, False)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.core.ValidationError):
                self.core.TaskNotesEngine(
                    vault=Path(tmp),
                    gbrain_bin="gbrain-native",
                    gbrain_home=Path(tmp),
                    daily_links_enabled="true",  # type: ignore[arg-type]
                )

    def test_mutation_result_daily_fields_are_optional_additions(self) -> None:
        fields = {
            field.name: field
            for field in dataclasses.fields(self.core.MutationResult)
        }
        for name in ("daily_link_state", "daily_link_detail", "daily_link_dates"):
            self.assertIn(name, fields)
            self.assertEqual(fields[name].default, None)
        # Pre-existing fields remain authoritative and unchanged.
        self.assertEqual(fields["state"].default, dataclasses.MISSING)
        self.assertEqual(fields["slug"].default, dataclasses.MISSING)

    def test_daily_link_states_match_documented_set(self) -> None:
        constants = {
            "applied_and_committed": self.core.DAILY_LINK_APPLIED,
            "not_applicable": self.core.DAILY_LINK_NOT_APPLICABLE,
            "not_applied": self.core.DAILY_LINK_NOT_APPLIED,
            "conflict": self.core.DAILY_LINK_CONFLICT,
            "write_failed": self.core.DAILY_LINK_WRITE_FAILED,
            "commit_failed": self.core.DAILY_LINK_COMMIT_FAILED,
            "committed_sync_failed": self.core.DAILY_LINK_SYNC_FAILED,
        }
        self.assertEqual(set(constants.values()), set(DAILY_LINK_STATES))
        for documented, actual in constants.items():
            self.assertEqual(actual, documented)

    def test_daily_projection_constants_match_documented_behavior(self) -> None:
        self.assertEqual(self.core.DAILY_NOTE_TASKS_HEADING, "## Tasks")
        self.assertEqual(self.core.DAILY_NOTES_DEFAULT_FORMAT, "YYYY-MM-DD")
        self.assertEqual(
            self.core.DAILY_PROJECTION_COMMIT_MSG,
            "tasknotes-mcp: daily note projection",
        )
        self.assertEqual(self.core.DAILY_PROJECTION_MAX_ATTEMPTS, 2)
        self.assertEqual(
            self.core._DAILY_FORMAT_TOKENS,
            ("YYYY", "YY", "MM", "M", "DD", "D"),
        )

    def test_transition_plan_matches_documented_matrix(self) -> None:
        plan = self.core._daily_link_plan
        ensure = self.core.DAILY_PROJECTION_OP_ENSURE
        remove = self.core.DAILY_PROJECTION_OP_REMOVE
        # create/switch to scheduled D: ensure D.
        self.assertEqual(plan(None, "2026-01-05"), [(ensure, "2026-01-05")])
        # reschedule D1 -> D2: ensure new FIRST, then remove old.
        self.assertEqual(
            plan("2026-01-01", "2026-01-05"),
            [(ensure, "2026-01-05"), (remove, "2026-01-01")],
        )
        # same-day update: idempotent ensure only, no duplicate removal.
        self.assertEqual(plan("2026-01-05", "2026-01-05"), [(ensure, "2026-01-05")])
        # scheduled -> planned_week/backlog: remove old only.
        self.assertEqual(plan("2026-01-01", None), [(remove, "2026-01-01")])
        # Backlog/week: no projection at all.
        self.assertIsNone(plan(None, None))

    def test_transition_plan_composes_by_resolved_target_path(self) -> None:
        """R4 (issue #140): date-level plans are composed by resolved
        target path — dates mapping to the same Daily Note collapse to
        the first step, so a reschedule whose old and new dates share one
        note emits exactly one ensure and never an ensure followed by a
        remove of the same link."""
        compose = self.core._compose_daily_link_plan_by_target
        ensure = self.core.DAILY_PROJECTION_OP_ENSURE
        remove = self.core.DAILY_PROJECTION_OP_REMOVE
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            monthly = self.core.DailyNotesConfig(folder="", format="YYYY-MM")
            # Same resolved target: single ensure, no remove of the link.
            self.assertEqual(
                compose(
                    vault,
                    monthly,
                    [(ensure, "2026-01-05"), (remove, "2026-01-20")],
                ),
                [(ensure, "2026-01-05")],
            )
            # Distinct resolved targets: both steps kept in plan order.
            self.assertEqual(
                compose(
                    vault,
                    monthly,
                    [(ensure, "2026-01-05"), (remove, "2026-02-20")],
                ),
                [(ensure, "2026-01-05"), (remove, "2026-02-20")],
            )

    def test_link_alias_encoding_matches_documented_metacharacters(self) -> None:
        """R6 (issue #140): the display alias is a deterministic,
        reversible percent encoding of the structural metacharacters
        only — `%` first as `%25`, then `[`/`]`/`|`; ordinary title text
        is unchanged and ownership matching stays alias-independent."""
        encode = self.core.encode_daily_note_link_alias
        self.assertEqual(encode("plain title"), "plain title")
        self.assertEqual(encode("a|b[c]d%e"), "a%7Cb%5Bc%5Dd%25e")
        # `%` encodes first: a literal `%5B` title cannot collide with an
        # encoded `[` (`%5B`) — the mapping is injective (reversible).
        self.assertEqual(encode("%5B"), "%255B")
        self.assertEqual(encode("["), "%5B")
        self.assertNotEqual(encode("%5B"), encode("["))

    def test_projection_targets_in_task_or_archive_folders_rejected(self) -> None:
        """R5 (issue #140): a resolved Daily Note target under the
        configured `tasksFolder` — or the active archive folder — is
        rejected before any side effect; the archive folder is protected
        only while `moveArchivedTasks` is true and the folder is set."""
        def profile(**overrides: object) -> object:
            values: dict = dict(
                version="4.11.1",
                tasks_folder="Tasks",
                task_tag="task",
                archive_tag="archive",
                statuses=("open", "done"),
                completed_status="done",
                default_status="open",
                priorities=("high", "normal"),
                default_priority="normal",
                mappings={"title": "title"},
                brain_repo="/repo",
                profile_hash="hash",
                source_id="source",
                raw_manifest={},
                raw_data={},
            )
            values.update(overrides)
            return self.core.TaskNotesProfile(**values)

        reject = self.core._reject_daily_projection_collision
        active = profile(move_archived_tasks=True, archive_folder="Archive")
        with self.assertRaises(self.core.ValidationError):
            reject(active, "Tasks/2026-01-05.md")
        with self.assertRaises(self.core.ValidationError):
            reject(active, "Archive/2026-01-05.md")
        with self.assertRaises(self.core.ValidationError):
            reject(active, "Tasks")
        # Inactive archive folder is not protected; unrelated paths pass.
        inactive = profile(move_archived_tasks=False, archive_folder="Archive")
        reject(inactive, "Archive/2026-01-05.md")
        reject(inactive, "Journal/2026-01-05.md")

    def test_format_subset_matches_documented_tokens(self) -> None:
        self.assertEqual(
            self.core.format_daily_note_date("2026-01-05", "YYYY/MM/DD"),
            "2026/01/05",
        )
        self.assertEqual(
            self.core.format_daily_note_date("2026-01-05", "YYYY-M-D"),
            "2026-1-5",
        )
        for unsupported in ("MMMM", "MMM DD", "dddd", "YYYY [W] WW"):
            with self.assertRaises(self.core.ValidationError):
                self.core.validate_daily_note_format(unsupported)
        with self.assertRaises(self.core.ValidationError):
            self.core.render_daily_note_template(
                "{{ date:MMMM }}", date="2026-01-05", title="2026-01-05"
            )


if __name__ == "__main__":
    unittest.main()
