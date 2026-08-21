"""Policy contract: agent-facing instruction docs must use the public `gbrain`
command (issue #110 transparent safe wrapper), never the internal private
native gbrain path or the temporary compatibility alias.

Policy (issue #110, transparent-wrapper UX):
- ALL chat/skill/external general vault access uses the public `gbrain`
  command, which is safe by default: it transparently provides the
  safe-adapter behavior (runs as the `hermes` runtime user under the shared
  TaskNotes/gbrain lock).
- `gbrain-chat-run` is a TEMPORARY COMPATIBILITY ALIAS: it may appear only on
  lines that document it as such (alias/compatibility wording) or in dated
  historical incident records. New instructions must use the public `gbrain`.
- The internal private native gbrain path (used by the `josemar-gbrain`
  operator wrapper, both refresh crons, and the TaskNotes MCP) must never be
  presented as an agent command: internal operator commands (`init`, `sync`,
  `extract`, `schema`, `embed`, `migrate`, `sources`) may appear only in the
  operator documentation files that describe those locked paths.
- TaskNotes mutations go through the `task_*` MCP tools only; the TaskNotes
  internal source-routed write (`gbrain capture --stdin --slug`) is documented
  only in TaskNotes docs and the operator runbook.
- Prohibitions ("never use `gbrain put --stdin`") and upstream terminology
  (`gbrain sources harden`, `gbrain autopilot --install`, skillpack
  references) remain allowed and need no exception.

Agent command inventory (must match the runtime public surface):
- ALLOWED agent-facing spellings include `gbrain restore` and
  `gbrain schema-status` (hyphenated).
- NOT agent-facing: `gbrain schema_status` (underscore spelling is not a
  command), `gbrain jobs` and `gbrain chronicle-backfill` (operator-only),
  and `gbrain put --stdin` (forbidden; use `capture --stdin`/`capture --file`).
  These may appear only in operator documentation or in explicit
  prohibition/operator-only statements.

The scan is deliberately conservative: in prose, a bare `gbrain <cmd>` counts
only when backticked (a command token); inside fenced code blocks every
occurrence counts (command context). When in doubt, change the example to the
public `gbrain` command rather than waiving it here.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PRIVATE_STATE_ROOT = REPO_ROOT / "agent-state"

SCANNED = [
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / ".env.example",
    *sorted((REPO_ROOT / "docs").glob("*.md")),
    *sorted((REPO_ROOT / "skills-factory").rglob("*.md")),
]

# agent-state is a private nested repository. It is present in local/runtime
# checkouts but deliberately absent from public CI checkouts.
if PRIVATE_STATE_ROOT.is_dir():
    SCANNED.extend(
        [
            PRIVATE_STATE_ROOT / "AGENTS.md",
            *sorted((PRIVATE_STATE_ROOT / "skills").rglob("*.md")),
        ]
    )

# Operator documentation files that legitimately describe the internal
# operator/cron implementation paths (init/sync/extract/schema/embed/sources).
OPERATOR_FILES = {
    "docs/gbrain-operations.md",
    "docs/tasknotes-mcp.md",
    "docs/memory-embeddings-evaluation.md",
    ".env.example",
    "skills-factory/gbrain/SKILL.md",
}

# Files documenting the TaskNotes MCP internal implementation (sole task
# writer) — the only place the internal source-routed write path appears.
TASKNOTES_FILES = {
    "skills-factory/tasknotes/SKILL.md",
    "skills-factory/tasknotes/references/custom-fields.md",
    "docs/tasknotes-mcp.md",
}

# Dated incident record: the tool trace documents what actually ran on
# 2026-07-03, before the safe wrapper shipped. Rewriting it would falsify the
# record; it must carry the annotation added by the issue #110 docs pass.
HISTORICAL_FILES = {
    "agent-state/skills/calendar-report/references/incident-2026-07-03.md",
}

# Native gbrain subcommands that are operator/cron-internal only; presenting
# them as agent-facing commands would expose the internal private native path.
_INTERNAL_CMDS = (
    "init", "sync", "extract", "schema", "embed", "migrate", "sources",
)
_INTERNAL_ALT = "|".join(sorted(_INTERNAL_CMDS, key=len, reverse=True))

_ALIAS_RE = re.compile(r"gbrain-chat-run")
_ALIAS_DOC_RE = re.compile(r"alias|compatib", re.IGNORECASE)
# The lookbehind excludes "josemar-gbrain ..." and "gbrain-chat-run ..." (the
# hyphen binds "gbrain" to a longer token); the trailing guard keeps
# "gbrain schema-status" (an agent-facing command) from matching "schema".
_INTERNAL_FENCE_RE = re.compile(
    rf"(?<![\w-])gbrain\s+({_INTERNAL_ALT})(?![-\w])"
)
_INTERNAL_PROSE_RE = re.compile(
    rf"`(?<![\w-])gbrain\s+({_INTERNAL_ALT})(?![-\w])"
)
_TASKNOTES_WRITE_RE = re.compile(r"gbrain capture --stdin --slug[^\n]*--source")

# Agent command inventory guards (see module docstring).
_PROHIBITION_WORDS = re.compile(
    r"never|not|banned|unsafe|forbid|avoid|don'?t", re.IGNORECASE
)
_OPERATOR_CTX_WORDS = re.compile(
    r"operator|maintenance|chat action|agent-facing surface", re.IGNORECASE
)
_SCHEMA_STATUS_UNDERSCORE_RE = re.compile(r"gbrain schema_status\b")
_PUT_STDIN_RE = re.compile(r"(?<![\w-])gbrain put --stdin\b")
_JOBS_RE = re.compile(r"(?<![\w-])gbrain jobs\b")
_CHRONICLE_BACKFILL_RE = re.compile(r"(?<![\w-])gbrain chronicle-backfill\b")


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _iter_matches(lines, fence_re, prose_re):
    in_fence = False
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        regex = fence_re if in_fence else prose_re
        for m in regex.finditer(raw):
            yield lineno, raw


class GbrainTransparentWrapperPolicyTest(unittest.TestCase):
    """Agent-facing docs must use public `gbrain`, not the alias or the
    internal private native path."""

    def test_gbrain_chat_run_alias_not_recommended(self):
        violations = []
        for path in SCANNED:
            if not path.exists() or _rel(path) in HISTORICAL_FILES:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, start=1):
                if not _ALIAS_RE.search(line):
                    continue
                # The alias-documentation wording may wrap across lines; check
                # a small window around the mention.
                window = "\n".join(lines[max(0, lineno - 2): lineno + 1])
                if not _ALIAS_DOC_RE.search(window):
                    violations.append((_rel(path), lineno, line.strip()))
        self.assertEqual(
            [],
            violations,
            "`gbrain-chat-run` is a temporary compatibility alias and must not "
            "be recommended in instructions; every mention must document it as "
            "such (alias/compatibility wording) or live in a dated historical "
            "incident record.\n"
            + "\n".join(f"{rel}:{lineno}: {text}" for rel, lineno, text in violations),
        )

    def test_internal_native_commands_scoped_to_operator_docs(self):
        violations = []
        for path in SCANNED:
            if not path.exists() or _rel(path) in HISTORICAL_FILES:
                continue
            rel = _rel(path)
            if rel in OPERATOR_FILES:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for lineno, line in _iter_matches(lines, _INTERNAL_FENCE_RE, _INTERNAL_PROSE_RE):
                violations.append((rel, lineno, line.strip()))
        self.assertEqual(
            [],
            violations,
            "Internal operator/cron native gbrain commands (init/sync/extract/"
            "schema/embed/migrate/sources) describe the internal private native "
            "path and must not appear in agent-facing instructions outside the "
            "operator documentation (docs/gbrain-operations.md, "
            "docs/tasknotes-mcp.md, docs/memory-embeddings-evaluation.md, "
            ".env.example, skills-factory/gbrain/SKILL.md).\n"
            + "\n".join(f"{rel}:{lineno}: {text}" for rel, lineno, text in violations),
        )

    def test_tasknotes_internal_write_scoped_to_tasknotes_docs(self):
        allowed = TASKNOTES_FILES | {"docs/gbrain-operations.md"}
        violations = []
        for path in SCANNED:
            if not path.exists() or _rel(path) in HISTORICAL_FILES:
                continue
            rel = _rel(path)
            if rel in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            if _TASKNOTES_WRITE_RE.search(text):
                violations.append(rel)
        self.assertEqual(
            [],
            violations,
            "The TaskNotes internal source-routed write "
            "(`gbrain capture --stdin --slug`) is TaskNotes implementation "
            "detail and may be documented only in TaskNotes docs or the "
            "operator runbook.",
        )

    def test_rejected_agent_commands_only_in_operator_or_prohibition_context(
        self,
    ):
        """schema_status/jobs/chronicle-backfill/put --stdin must not appear as
        agent-facing command examples; only operator documentation,
        prohibition statements, or explicit operator-only context allows
        them."""
        violations = []
        for path in SCANNED:
            if not path.exists() or _rel(path) in HISTORICAL_FILES:
                continue
            rel = _rel(path)
            lines = path.read_text(encoding="utf-8").splitlines()
            for idx, line in enumerate(lines):
                if _SCHEMA_STATUS_UNDERSCORE_RE.search(line):
                    violations.append(
                        (rel, idx + 1, line.strip(),
                         "schema_status is not a command; agent-facing "
                         "spelling is `gbrain schema-status`")
                    )
                if _PUT_STDIN_RE.search(line) and not _PROHIBITION_WORDS.search(
                    line
                ):
                    violations.append(
                        (rel, idx + 1, line.strip(),
                         "gbrain put --stdin is forbidden; use capture "
                         "--stdin/--file (prohibition wording required)")
                    )
                if rel in OPERATOR_FILES:
                    continue
                window = "\n".join(lines[max(0, idx - 2): idx + 1])
                if _JOBS_RE.search(line) and not (
                    _OPERATOR_CTX_WORDS.search(window)
                    or _PROHIBITION_WORDS.search(window)
                ):
                    violations.append(
                        (rel, idx + 1, line.strip(),
                         "gbrain jobs is operator-only (chronicle extraction)")
                    )
                if _CHRONICLE_BACKFILL_RE.search(line) and not (
                    _OPERATOR_CTX_WORDS.search(window)
                    or _PROHIBITION_WORDS.search(window)
                ):
                    violations.append(
                        (rel, idx + 1, line.strip(),
                         "gbrain chronicle-backfill is operator-only")
                    )
        self.assertEqual(
            [],
            violations,
            "Rejected agent commands found outside their allowed "
            "operator/prohibition context.\n"
            + "\n".join(
                f"{rel}:{lineno}: {text} ({why})"
                for rel, lineno, text, why in violations
            ),
        )

    def test_agent_command_inventory_spellings(self):
        """Agent-facing docs must use the real runtime spellings: hyphenated
        `schema-status` and `restore` are documented agent commands."""
        agent_state_path = PRIVATE_STATE_ROOT / "AGENTS.md"
        if agent_state_path.exists():
            agent_state = agent_state_path.read_text(encoding="utf-8")
            for token in ("`schema-status`", "`restore`"):
                self.assertIn(
                    token,
                    agent_state,
                    f"agent-state/AGENTS.md must document agent-facing `{token}`",
                )
        skill = (REPO_ROOT / "skills-factory/gbrain/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "gbrain restore",
            skill,
            "gbrain skill must document agent-facing `gbrain restore`",
        )

    def test_public_gbrain_required_in_policy_docs(self):
        required_docs = [
            "AGENTS.md",
            "docs/gbrain-operations.md",
            "skills-factory/gbrain/SKILL.md",
        ]
        if (PRIVATE_STATE_ROOT / "AGENTS.md").exists():
            required_docs.append("agent-state/AGENTS.md")
        for rel in required_docs:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(
                "public `gbrain`",
                text,
                f"{rel}: must state that the public `gbrain` command is the "
                "agent-facing interface (issue #110)",
            )
        # The compatibility alias must be documented somewhere in the policy.
        runbook = (REPO_ROOT / "docs/gbrain-operations.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "temporary compatibility alias",
            runbook,
            "docs/gbrain-operations.md: must document `gbrain-chat-run` as a "
            "temporary compatibility alias",
        )

    def test_historical_incident_record_annotated(self):
        for rel in HISTORICAL_FILES:
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "> **Historical record.**",
                text,
                f"{rel}: historical incident record lost its annotation",
            )

    def test_scanned_files_exist(self):
        missing = [str(p) for p in SCANNED if not p.exists()]
        self.assertEqual([], missing, "Expected scan targets are missing")

    def test_runtime_allowlist_matches_policy_inventory(self):
        """The runtime adapter exports a mechanical command inventory; this
        policy suite asserts against it instead of a fragile duplicate list:
        internal operator commands are absent, restore is public, jobs and
        chronicle-backfill are operator-only, put --stdin is rejected."""
        import importlib.util

        repo_root = Path(__file__).resolve().parents[2]
        adapter = repo_root / "scripts" / "gbrain_chat_run.py"
        spec = importlib.util.spec_from_file_location("gbrain_chat_run_inv", adapter)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        inventory = mod.CHAT_COMMAND_INVENTORY
        subcommands = set(inventory["subcommands"])
        for internal in _INTERNAL_CMDS:
            if internal == "sources":
                # sources is public ONLY as read-only `sources list`.
                self.assertEqual(inventory["subsubcommands"]["sources"], ["list"])
            else:
                self.assertNotIn(internal, subcommands, internal)
        for op in inventory["operator_only"]:
            self.assertNotIn(op, subcommands, op)
        self.assertIn("restore", subcommands)
        self.assertNotIn("jobs", subcommands)
        self.assertNotIn("chronicle-backfill", subcommands)
        self.assertIn("--stdin", inventory["rejected_arguments"]["put"])


class GbrainOperationClassificationTests(unittest.TestCase):
    """The gbrain operation classification (issue #127 W2a) is the single
    machine-readable record of the documented Josemar public/operator surface.

    These guards prevent a classified supported command from drifting from the
    actual allowlist/policy and ensure every documented operation is accounted
    for without brittle arbitrary Markdown prose parsing: the classification
    manifest in scripts/gbrain_chat_run.py IS the explicit documented record,
    and the adapter's allowlist/rejection sets are asserted against it."""

    @classmethod
    def setUpClass(cls):
        import importlib.util

        repo_root = Path(__file__).resolve().parents[2]
        adapter = repo_root / "scripts" / "gbrain_chat_run.py"
        spec = importlib.util.spec_from_file_location("gbrain_chat_run_cls", adapter)
        assert spec is not None and spec.loader is not None
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.classification = cls.mod.GBRAIN_OPERATION_CLASSIFICATION
        cls.subcommands = set(cls.mod.CHAT_SUBCOMMANDS)
        cls.rejected = cls.mod.CHAT_REJECTED_ARGUMENTS

    @classmethod
    def _category(cls, category):
        return frozenset(
            name
            for name, cat in cls.classification.items()
            if cat == category
        )

    def test_classification_categories_are_exactly_the_defined_set(self):
        allowed = {
            "core",
            "chronicle_read",
            "embeddings_gated",
            "operator_only",
            "forbidden",
            "probe_unavailable",
        }
        self.assertTrue(set(self.classification.values()) <= allowed)

    def test_supported_commands_are_allowlisted(self):
        """Every command classified as core/chronicle_read/embeddings_gated
        must be on the actual agent-facing allowlist (no drift where docs say
        supported but the adapter rejects)."""
        supported = (
            self._category("core")
            | self._category("chronicle_read")
            | self._category("embeddings_gated")
        )
        for cmd in sorted(supported):
            self.assertIn(cmd, self.subcommands, cmd)

    def test_probe_commands_are_allowlisted(self):
        """probe_unavailable commands are still public (allowlisted) but carry
        a known discrepancy, so they are probes, not hard assertions."""
        for cmd in sorted(self._category("probe_unavailable")):
            self.assertIn(cmd, self.subcommands, cmd)

    def test_operator_only_commands_not_allowlisted(self):
        for cmd in sorted(self._category("operator_only")):
            self.assertNotIn(cmd, self.subcommands, cmd)

    def test_forbidden_forms_match_rejected_arguments(self):
        """Every forbidden classification must be an actual rejected argument
        form in the adapter policy."""
        for form in sorted(self._category("forbidden")):
            parent, _, flag = form.partition(" ")
            self.assertIn(parent, self.rejected, form)
            self.assertIn(flag, self.rejected[parent], form)

    def test_every_allowlisted_command_is_classified(self):
        """A newly allowlisted command must have a classification/coverage
        entry (the issue #127 guard against unclassified supported ops)."""
        missing = sorted(self.subcommands - set(self.classification))
        self.assertEqual([], missing, "allowlisted commands without a classification entry")

    def test_classification_covers_full_documented_surface(self):
        """Every classified command is either allowlisted, operator-only, or
        forbidden — no classified command may sit in a limbo state."""
        known = (
            set(self.subcommands)
            | self._category("operator_only")
            | self._category("forbidden")
        )
        self.assertEqual(set(self.classification), known)

    def test_schema_status_classified_probe_unavailable(self):
        self.assertEqual(self.classification["schema-status"], "probe_unavailable")
        self.assertIn("schema-status", self.subcommands)

    def test_put_stdin_forbidden_and_rejected(self):
        self.assertEqual(self.classification["put --stdin"], "forbidden")
        self.assertIn("--stdin", self.rejected["put"])

    def test_query_classified_embeddings_gated(self):
        self.assertEqual(self.classification["query"], "embeddings_gated")

    def test_chronicle_read_commands_classified(self):
        for cmd in (
            "day",
            "since",
            "last-seen",
            "on-this-day",
            "orient",
            "timeline",
            "ontology",
        ):
            self.assertEqual(self.classification[cmd], "chronicle_read", cmd)

    def test_inventory_exports_classification(self):
        """CHAT_COMMAND_INVENTORY carries the classification and derives
        operator_only from it (single source of truth)."""
        inventory = self.mod.CHAT_COMMAND_INVENTORY
        self.assertEqual(
            inventory["classification"],
            dict(sorted(self.classification.items())),
        )
        self.assertEqual(
            set(inventory["operator_only"]),
            self._category("operator_only"),
        )

    def test_every_supported_operation_has_coverage(self):
        """Every classified SUPPORTED operation (core / chronicle_read /
        embeddings_gated / probe_unavailable) must have a coverage entry
        mapping it to a real scenario symbol and a known gate env. A newly
        classified/documented supported operation with no mechanical runtime
        coverage fails here (PR #129 MAJOR finding: the exhaustive-coverage
        guard must prove runtime coverage, not just classification)."""
        supported = (
            self._category("core")
            | self._category("chronicle_read")
            | self._category("embeddings_gated")
            | self._category("probe_unavailable")
        )
        missing = sorted(supported - set(self.mod.GBRAIN_OPERATION_COVERAGE))
        self.assertEqual(
            [],
            missing,
            "supported operations without a runtime coverage entry",
        )

    def test_coverage_only_for_supported_surfaces(self):
        """operator_only / forbidden surfaces must NOT have coverage entries:
        they are rejected by the adapter, never exercised as supported
        operations."""
        unsupported = self._category("operator_only") | self._category("forbidden")
        extra = sorted(set(self.mod.GBRAIN_OPERATION_COVERAGE) & unsupported)
        self.assertEqual(
            [],
            extra,
            "unsupported (operator_only/forbidden) operations must not have "
            "coverage entries",
        )

    def test_coverage_gates_are_known(self):
        """Every coverage entry's gate must be a known conformance gate env
        (the opt-in Docker runtime gates defined in the Makefile)."""
        for op, (scenario, gate) in self.mod.GBRAIN_OPERATION_COVERAGE.items():
            self.assertIn(
                gate,
                self.mod.KNOWN_CONFORMANCE_GATES,
                f"{op} ({scenario}): unknown gate {gate!r}",
            )

    def test_coverage_scenario_symbols_exist_in_owning_modules(self):
        """Every coverage entry's scenario symbol must exist (as a real
        method definition) in the runtime test module(s) owned by its gate.
        This is the mechanical proof that a classified supported surface has
        actual runtime coverage, not just a classification entry."""
        for op, (scenario, gate) in self.mod.GBRAIN_OPERATION_COVERAGE.items():
            modules = _GATE_COVERAGE_MODULES.get(gate)
            self.assertIsNotNone(
                modules,
                f"{op}: no coverage modules registered for gate {gate!r}",
            )
            assert modules is not None
            texts = [
                (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in modules
            ]
            self.assertTrue(
                any(f"def {scenario}" in text for text in texts),
                f"{op}: scenario {scenario!r} not defined in {modules}",
            )

    def test_inventory_exports_coverage(self):
        """CHAT_COMMAND_INVENTORY exports the coverage manifest and the known
        gate envs (single machine-readable source of truth)."""
        inventory = self.mod.CHAT_COMMAND_INVENTORY
        self.assertEqual(
            inventory["coverage"],
            dict(sorted(self.mod.GBRAIN_OPERATION_COVERAGE.items())),
        )
        self.assertEqual(
            set(inventory["known_gates"]),
            set(self.mod.KNOWN_CONFORMANCE_GATES),
        )


class GbrainOperatorCoverageTests(unittest.TestCase):
    """The operator-surface coverage manifest (PR #129 re-review) governs the
    six supported `josemar-gbrain` wrapper operations (reindex, refresh,
    refresh-embeddings, embed-backfill, enable-embeddings, disable-embeddings)
    WITHOUT leaking them into the public adapter or the native coverage
    manifest.

    These guards prevent a supported wrapper operation from drifting from the
    actual wrapper dispatch or from the runtime scenarios that exercise it:
    the manifest in scripts/gbrain_chat_run.py IS the explicit machine-readable
    record, and the fast guards assert it against the wrapper source and the
    owning runtime test modules."""

    WRAPPER_OPERATIONS = (
        "reindex",
        "refresh",
        "refresh-embeddings",
        "embed-backfill",
        "enable-embeddings",
        "disable-embeddings",
    )

    @classmethod
    def setUpClass(cls):
        import importlib.util

        repo_root = Path(__file__).resolve().parents[2]
        adapter = repo_root / "scripts" / "gbrain_chat_run.py"
        spec = importlib.util.spec_from_file_location("gbrain_chat_run_op", adapter)
        assert spec is not None and spec.loader is not None
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.operator_coverage = cls.mod.JOSEMAR_GBRAIN_OPERATOR_COVERAGE
        cls.wrapper_src = (repo_root / "scripts" / "josemar-gbrain").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _extract_shell_function(src, name):
        header = re.compile(rf"^{name}\(\)\s*\{{", re.MULTILINE)
        start = header.search(src)
        assert start is not None, f"Could not find function {name}"
        body_start = start.end()
        close = re.compile(r"^}$", re.MULTILINE)
        match = close.search(src, body_start)
        assert match is not None, f"Could not find end of function {name}"
        return src[body_start:match.start()]

    def test_operator_coverage_exactly_six_supported_wrapper_operations(self):
        self.assertEqual(
            set(self.operator_coverage),
            set(self.WRAPPER_OPERATIONS),
            "the operator coverage manifest must cover exactly the six "
            "supported josemar-gbrain wrapper operations",
        )

    def test_operator_coverage_operations_not_in_public_adapter_surface(self):
        """Scope: the wrapper operations are operator-only and must never be
        exposed through the public adapter (not allowlisted). They may appear
        in the native classification ONLY as operator_only (the adapter
        rejects them), never as a supported agent-facing surface."""
        subcommands = set(self.mod.CHAT_SUBCOMMANDS)
        for op in self.WRAPPER_OPERATIONS:
            self.assertNotIn(op, subcommands, op)
            classification = self.mod.GBRAIN_OPERATION_CLASSIFICATION.get(op)
            if classification is not None:
                self.assertEqual(
                    classification,
                    "operator_only",
                    f"{op}: wrapper operation must not be classified as a "
                    "supported agent-facing surface",
                )

    def test_operator_coverage_operations_not_in_native_coverage(self):
        """Scope: the wrapper operations must never be added to the native
        GBRAIN_OPERATION_COVERAGE manifest (public/native vs operator)."""
        for op in self.WRAPPER_OPERATIONS:
            self.assertNotIn(op, self.mod.GBRAIN_OPERATION_COVERAGE, op)

    def test_operator_coverage_gates_are_known(self):
        """Every operator coverage entry's gate must be a known conformance
        gate env (the opt-in Docker runtime gates defined in the Makefile)."""
        for op, (scenario, gate) in self.operator_coverage.items():
            self.assertIn(
                gate,
                self.mod.KNOWN_CONFORMANCE_GATES,
                f"{op} ({scenario}): unknown gate {gate!r}",
            )

    def test_operator_coverage_scenario_symbols_exist_in_owning_modules(self):
        """Every operator coverage entry's scenario symbol must exist (as a
        real method definition) in the runtime test module(s) owned by its
        gate — the mechanical proof that a supported wrapper operation has
        actual runtime coverage."""
        for op, (scenario, gate) in self.operator_coverage.items():
            modules = _GATE_COVERAGE_MODULES.get(gate)
            self.assertIsNotNone(
                modules,
                f"{op}: no coverage modules registered for gate {gate!r}",
            )
            assert modules is not None
            texts = [
                (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in modules
            ]
            self.assertTrue(
                any(f"def {scenario}" in text for text in texts),
                f"{op}: scenario {scenario!r} not defined in {modules}",
            )

    def test_operator_coverage_tied_to_wrapper_usage_dispatch(self):
        """Every supported wrapper operation must be dispatched by
        scripts/josemar-gbrain: it appears in the usage() string and in the
        main() case dispatch (source contract)."""
        usage_body = self._extract_shell_function(self.wrapper_src, "usage")
        main_body = self._extract_shell_function(self.wrapper_src, "main")
        for op in self.WRAPPER_OPERATIONS:
            self.assertIn(op, usage_body, f"{op} missing from wrapper usage()")
            self.assertIn(
                f"{op})",
                main_body,
                f"{op} missing from wrapper main() dispatch",
            )

    def test_inventory_exports_operator_coverage(self):
        """CHAT_COMMAND_INVENTORY exports the operator coverage manifest
        (single machine-readable source of truth)."""
        inventory = self.mod.CHAT_COMMAND_INVENTORY
        self.assertEqual(
            inventory["operator_coverage"],
            dict(sorted(self.operator_coverage.items())),
        )


# Runtime test modules that own the coverage scenarios, keyed by the gate env
# a coverage entry can reference. The core gate's scenarios live in the
# reusable CoreScenarioMixin (tests/runtime/gbrain_conformance_scenarios.py)
# plus the core runtime test module; the other gates own their scenarios
# directly in their test modules.
_GATE_COVERAGE_MODULES = {
    "RUN_GBRAIN_CONFORMANCE": (
        "tests/runtime/gbrain_conformance_scenarios.py",
        "tests/runtime/test_gbrain_conformance.py",
    ),
    "RUN_GBRAIN_CHRONICLE_CONFORMANCE": (
        "tests/runtime/test_gbrain_conformance_chronicle.py",
    ),
    "RUN_GBRAIN_EMBEDDING_CONFORMANCE": (
        "tests/runtime/test_gbrain_conformance_embeddings.py",
    ),
    "RUN_GBRAIN_UPGRADE_CONFORMANCE": (
        "tests/runtime/test_gbrain_upgrade_conformance.py",
    ),
}


if __name__ == "__main__":
    unittest.main()
