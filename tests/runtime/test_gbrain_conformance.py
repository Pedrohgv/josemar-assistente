"""Opt-in provider-free gbrain core runtime conformance (issue #127 W2b).

Baseline + provenance + activation + provider-free retrieval/tagging. It
covers:

  - pre-start source-state seeding (real template ``.sync-manifest`` +
    canonical ``josemar`` schema pack into the disposable source-agent-state)
  - baseline Hermes-only build/start (no candidate ref, no sidecar profiles)
  - hermes-writable wait and safety checks: empty credentials and disabled
    owned gbrain/vault-recovery jobs
  - synthetic vault init committed as the hermes runtime user, plus a
    deterministic ``#conformance``-tagged note seeded before reindex
  - unconditional final cleanup (``down -v --remove-orphans``)
  - built-image provenance: the canonical ``GBRAIN_REF`` parsed from
    ``Dockerfile.hermes`` and the public/private wrapper paths
  - canonical runtime schema pack byte identity (seeding conformance)
  - ``josemar-gbrain reindex`` success envelope
  - ``gbrain status --json`` valid runtime/schema facts
  - ``gbrain doctor --json`` valid health report: core checks ok and the
    expected no-embedding warning (base deploy runs keyword-only)
  - ``gbrain sources list --json``: the single registered source resolves to
    the vault path (read-only sources surface)
  - ``gbrain schema-status`` probe: the agent-facing spelling is allowlisted
    but the pinned native CLI has no such command (known discrepancy,
    classified probe_unavailable)
  - path-prefix type inference: seeded pages carry the inferred types
    (people/ -> person, projects/ -> project, notes/ -> note) and an inbox/
    page falls back to the default concept type
  - provider-free retrieval/tagging: ``gbrain get`` markdown with the exact
    unique token, ``gbrain search`` TEXT output (never JSON) containing the
    expected slug/token, and ``gbrain tags`` returning the deterministic
    ``#conformance`` association
  - both link sources: the seeded ``[[projects/atlas]]`` wikilink (markdown
    source) and a public manual link, exposed through ``gbrain backlinks`` /
    ``gbrain graph`` and persistent through ``josemar-gbrain refresh``
  - public write contracts: positional capture create/read-back/idempotency,
    ``capture --stdin --slug --source --json`` (TaskNotes-relevant top-level
    ``written`` bool + full body preservation), ``capture --file``
    create/replacement of the same slug, ``put --content`` full-page
    replacement retaining the retained section, and the public
    ``put --stdin`` safety rejection
  - recovery-page lifecycle: create version A, update to B, ``gbrain history``
    discovers a stable revision handle, and ``gbrain revert`` using that
    runtime handle restores the exact A body and leaves the page writable
  - soft delete/restore lifecycle: ``gbrain delete`` hides the page and
    ``gbrain restore`` brings back the exact body
  - direct committed external edit of a fixture Markdown page as the hermes
    runtime user AFTER activation: the public ``get``/``search`` must NOT
    assume the edit before ``josemar-gbrain refresh`` (stale index), and
    after refresh the unique token is visible while an unrelated known page
    survives untouched
  - shared-lock contention: an independent hermes process holds the
    ``/opt/data/.locks/tasknotes.lock`` flock (flock only, no PGLite);
    ``josemar-gbrain refresh`` fails bounded with the ``refresh_lock_busy``
    envelope, and the next refresh succeeds after the holder releases
  - public boundary: the operator-only ``gbrain reindex`` is rejected by the
    public adapter (rc 2, allowlist message) without invoking the private
    native binary — proven by the lock-held rejection returning rc 2 (not
    the lock-busy 75 the native path would hit) and by the rejection
    holding with a free lock too
  - zero-LLM Chronicle smoke: ``timeline``/``day``/``day --week``/``since``/
    ``last-seen``/``on-this-day``/``orient``/``ontology`` all return valid
    results with clearly no synthetic events (empty arrays / explicit empty
    states / null last-seen)
  - a synthetic report persisted under ``dump_folder/gbrain-conformance``
    with command/result metadata only (never environment dumps)

The provider-free operation scenarios live in the reusable
``CoreScenarioMixin`` (``tests/runtime/gbrain_conformance_scenarios.py``) so
the candidate upgrade suite can rerun them against a candidate image without
duplicating them (PR #129 MAJOR finding). This module owns the base runtime
setup, the Docker-gated runtime test class, and the fast structural guards.

Runtime execution is gated strictly on ``RUN_DOCKER_TESTS=1`` AND
``RUN_GBRAIN_CONFORMANCE=1`` and skips when the docker CLI is absent. Fast
host-side gate/structure tests in this module always run and need no Docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from .gbrain_conformance_scenarios import (
    CONFORMANCE_MATRIX,
    LOCK_HOLDER_SCRIPT,
    CoreScenarioMixin,
)
from .gbrain_conformance_support import (
    CONFORMANCE_EMPTY_ENV_KEYS,
    CommandEvidence,
    GbrainConformanceRuntime,
    conformance_report_dir,
    write_report,
)
from .helpers import REPO_ROOT, docker_available


def _conformance_enabled() -> bool:
    """Strict gate: RUN_DOCKER_TESTS=1 AND RUN_GBRAIN_CONFORMANCE=1 AND a
    docker CLI is available."""
    return (
        os.getenv("RUN_DOCKER_TESTS") == "1"
        and os.getenv("RUN_GBRAIN_CONFORMANCE") == "1"
        and docker_available()
    )


@unittest.skipUnless(
    _conformance_enabled(),
    "set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_CONFORMANCE=1 with a docker CLI",
)
class GbrainConformanceTestCase(unittest.TestCase):
    """Shared base setup for the gbrain conformance runtime suite.

    Builds/starts the baseline Hermes-only runtime against a disposable
    Compose project, seeds the real template source state BEFORE start, waits
    for the hermes-writable surface, asserts the isolation safety contract
    (empty credentials, disabled owned jobs), initializes the synthetic vault
    as the hermes runtime user, and unconditionally tears the project down
    with ``down -v --remove-orphans``.
    """

    def setUp(self) -> None:
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {
            op: "pass" if op.startswith("baseline_") else "not_run"
            for op in CONFORMANCE_MATRIX
        }
        self._gbrain_version: str | None = None
        self._report_path: Path | None = None

        self.runtime = GbrainConformanceRuntime()
        # Pre-start source state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # Baseline Hermes-only build/start (no candidate ref, no sidecars).
        self.runtime.up("hermes", timeout=900)
        # Wait for the exact hermes-writable surface before any exec probe.
        self.runtime.wait_until_hermes_writable(timeout=120)
        # Safety checks: empty credentials + disabled owned jobs.
        self._evidence.append(self._assert_no_credentials())
        self._evidence.append(self.runtime.assert_owned_jobs_disabled())
        # Synthetic vault init committed as the hermes runtime user.
        self._evidence.append(self.runtime.init_synthetic_vault())
        # Deterministic tagged note seeded before reindex (safe hermes command).
        self._evidence.append(self._seed_tagged_note())
        # Deterministic wikilink seeded into the welcome fixture page.
        self._evidence.append(self._seed_welcome_wikilink())

    def tearDown(self) -> None:
        # Unconditional final cleanup: down -v --remove-orphans.
        self.runtime.cleanup()

    # --- safety helpers ---------------------------------------------------

    def _seed_tagged_note(self) -> CommandEvidence:
        """Write a deterministic note carrying the ``#conformance`` tag into
        the vault as the hermes runtime user and commit it, so the later
        reindex indexes the tag association."""
        script = (
            "set -eu\n"
            "cd /opt/data/obsidian\n"
            "cat > notes/conformance-tagged.md <<'MD'\n"
            "---\n"
            "tags: [conformance]\n"
            "---\n"
            "\n"
            "# Conformance Tagged Note\n"
            "\n"
            "A deterministic note tagged #conformance.\n"
            "MD\n"
            "git add notes/conformance-tagged.md\n"
            "git commit -qm 'seed conformance tagged note'\n"
        )
        return self.runtime.run_as_hermes("sh", "-lc", script)

    def _seed_welcome_wikilink(self) -> CommandEvidence:
        """Rewrite the welcome fixture page so it carries the deterministic
        ``[[projects/atlas]]`` wikilink (preserving the unique search token),
        committed as the hermes runtime user before reindex."""
        script = (
            "set -eu\n"
            "cd /opt/data/obsidian\n"
            "cat > notes/welcome.md <<'MD'\n"
            "# Welcome\n"
            "\n"
            "Deterministic conformance note with unique search token: "
            "conformance-token-welcome.\n"
            "\n"
            "Links to [[projects/atlas]].\n"
            "MD\n"
            "git add notes/welcome.md\n"
            "git commit -qm 'seed welcome wikilink'\n"
        )
        return self.runtime.run_as_hermes("sh", "-lc", script)

    def _assert_no_credentials(self) -> CommandEvidence:
        """Assert every conformance-blanked credential env key is empty inside
        the running container (defense in depth on top of the host-side
        sanitizer)."""
        script = (
            "set -eu\n"
            "for k in " + " ".join(CONFORMANCE_EMPTY_ENV_KEYS) + "; do\n"
            "  v=$(printenv \"$k\" 2>/dev/null || true)\n"
            "  if [ -n \"$v\" ]; then\n"
            "    echo \"credential env key $k is non-empty\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "done\n"
            "echo no-credentials-present\n"
        )
        ev = self.runtime.run_as_hermes("sh", "-lc", script)
        self.assertIn("no-credentials-present", ev.stdout, ev.stderr)
        return ev

    # --- report -----------------------------------------------------------

    def _write_report(self) -> None:
        """Persist the synthetic conformance report under
        ``dump_folder/gbrain-conformance``. Contains command/result metadata
        only (argv, rc, stdout, stderr, elapsed) plus the explicit matrix —
        never the process or runtime environment."""
        metadata = {
            "baseline_ref": self.runtime.baseline_gbrain_ref(),
            "gbrain_version": self._gbrain_version,
            "matrix": self._matrix,
        }
        self._report_path = write_report(
            conformance_report_dir(),
            "gbrain-conformance",
            self._evidence,
            metadata=metadata,
        )


class GbrainConformanceRuntimeTests(CoreScenarioMixin, GbrainConformanceTestCase):
    """W2b provider-free runtime scenarios (Docker-gated via the base class).

    The scenario methods come from the reusable ``CoreScenarioMixin`` so the
    candidate upgrade suite can rerun the same supported-operation matrix
    against a candidate image without duplicating it.
    """

    def test_provider_free_core_runtime_conformance(self) -> None:
        try:
            self._scenario_provenance()
            self._scenario_pack_identity()
            self._scenario_reindex()
            self._scenario_status()
            self._scenario_doctor()
            self._scenario_sources_list()
            self._scenario_schema_status_probe()
            self._scenario_type_inference()
            self._scenario_get_search_tags()
            self._scenario_links_backlinks_graph()
            self._scenario_public_write_contracts()
            self._scenario_recovery_history_revert()
            self._scenario_soft_delete_restore()
            self._scenario_external_edit_refresh()
            self._scenario_lock_contention()
            self._scenario_public_boundary()
            self._scenario_chronicle_zero_llm()
        finally:
            self._write_report()


class GbrainConformanceGateStructureTests(unittest.TestCase):
    """Fast host-side guards for the conformance gate and module structure.
    No Docker required; these run in the normal fast suite."""

    @staticmethod
    def _module_text() -> str:
        return (REPO_ROOT / "tests" / "runtime" / "test_gbrain_conformance.py").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _scenario_text() -> str:
        """The reusable scenario module text (the CoreScenarioMixin). The
        provider-free operation scenarios live there so the candidate upgrade
        suite can reuse them; the structural guards check it directly."""
        return (
            REPO_ROOT / "tests" / "runtime" / "gbrain_conformance_scenarios.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _docker_available_patch(available: bool):
        """Patch this module's own ``docker_available`` reference.

        Patching the module attribute directly (rather than a dotted import
        path) is robust against double-import under ``discover -s tests``,
        where the module can be imported as both ``runtime.…`` and
        ``tests.runtime.…`` and a dotted target would patch the wrong copy.
        """
        return mock.patch.object(
            sys.modules[__name__], "docker_available", return_value=available
        )

    def test_gate_requires_run_docker_tests(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "", "RUN_GBRAIN_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_conformance_enabled())

    def test_gate_requires_run_gbrain_conformance(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CONFORMANCE": ""}
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_conformance_enabled())

    def test_gate_requires_docker(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(False):
                self.assertFalse(_conformance_enabled())

    def test_gate_enabled_when_all_conditions_met(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(True):
                self.assertTrue(_conformance_enabled())

    def test_runtime_class_is_gated_on_both_env_vars(self) -> None:
        text = self._module_text()
        self.assertIn("RUN_DOCKER_TESTS", text)
        self.assertIn("RUN_GBRAIN_CONFORMANCE", text)
        self.assertIn("skipUnless", text)
        self.assertIn('"set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_CONFORMANCE=1 with a docker CLI"', text)

    def test_shared_base_class_defined(self) -> None:
        self.assertTrue(issubclass(GbrainConformanceTestCase, unittest.TestCase))
        self.assertTrue(issubclass(GbrainConformanceRuntimeTests, GbrainConformanceTestCase))
        self.assertTrue(issubclass(GbrainConformanceRuntimeTests, CoreScenarioMixin))

    def test_base_setup_uses_gbrain_conformance_runtime(self) -> None:
        text = self._module_text()
        self.assertIn("GbrainConformanceRuntime()", text)
        self.assertIn("self.runtime.seed_source_state()", text)
        self.assertIn('self.runtime.up("hermes", timeout=900)', text)
        self.assertIn("self.runtime.wait_until_hermes_writable(timeout=120)", text)
        self.assertIn("self.runtime.assert_owned_jobs_disabled()", text)
        self.assertIn("self.runtime.init_synthetic_vault()", text)
        self.assertIn("self.runtime.cleanup()", text)

    def test_cleanup_is_unconditional_down_v(self) -> None:
        text = self._module_text()
        self.assertIn("self.runtime.cleanup()", text)
        # The base ComposeRuntime.down() runs `down -v --remove-orphans`.
        self.assertIn("down -v --remove-orphans", text)

    def test_reindex_runs_against_seeded_pack_without_runtime_mutation(self) -> None:
        """Reindex must run directly against the byte-identical template-seeded
        canonical pack: the runtime scenario must never replace the runtime
        pack and must not carry a duplicate VALID_JOSEMAR_PACK constant. Only
        the scenario module is inspected so this structural test's own
        assertion strings cannot pollute the check."""
        text = self._scenario_text()
        self.assertNotIn("VALID_JOSEMAR_PACK", text)
        self.assertNotIn("schema-packs/josemar/pack.yaml <<", text)
        self.assertNotIn("> /opt/data/.gbrain/schema-packs/josemar/pack.yaml", text)
        # The reindex scenario must invoke the operator wrapper directly.
        self.assertIn('self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)', text)
        # The active schema marker must be asserted as the runtime source of truth.
        self.assertIn('"/opt/data/.gbrain/active-schema-pack"', text)

    def test_report_uses_support_without_env_dump(self) -> None:
        text = self._module_text()
        self.assertIn("write_report", text)
        self.assertIn("conformance_report_dir", text)
        # The report must never serialize the process/runtime environment:
        # the report-writing method must not reference os.environ at all.
        report_method = text.split("def _write_report", 1)[1]
        report_method = report_method.split("class ", 1)[0]
        self.assertNotIn("os.environ", report_method)

    def test_conformance_matrix_covers_all_owned_operations(self) -> None:
        self.assertEqual(
            set(CONFORMANCE_MATRIX),
            {
                "baseline_seed",
                "baseline_build_start",
                "baseline_writable",
                "baseline_credentials",
                "baseline_jobs",
                "baseline_vault",
                "provenance",
                "pack_identity",
                "reindex",
                "status",
                "get",
                "search",
                "tags",
                "backlinks",
                "capture",
                "link",
                "graph",
                "refresh",
                "put",
                "put --stdin",
                "history",
                "revert",
                "delete",
                "restore",
                "external_edit_pre_refresh",
                "external_edit_post_refresh",
                "refresh_lock_busy",
                "public_reindex_rejected",
                "doctor",
                "sources_list",
                "schema_status_probe",
                "type_inference",
                "chronicle_timeline",
                "chronicle_day",
                "chronicle_day_week",
                "chronicle_since",
                "chronicle_last_seen",
                "chronicle_on_this_day",
                "chronicle_orient",
                "chronicle_ontology",
            },
        )

    def test_doctor_sources_schema_status_type_inference_scenarios_present(self) -> None:
        """The scenario module must exercise the classified supported surfaces
        the review flagged as missing: doctor, sources list, the schema-status
        probe, and path-prefix type inference."""
        text = self._scenario_text()
        self.assertIn("def _scenario_doctor", text)
        self.assertIn('"gbrain", "doctor", "--json"', text)
        self.assertIn("def _scenario_sources_list", text)
        self.assertIn('"gbrain", "sources", "list", "--json"', text)
        self.assertIn("def _scenario_schema_status_probe", text)
        self.assertIn('"gbrain", "schema-status"', text)
        self.assertIn("def _scenario_type_inference", text)
        self.assertIn('f"type: {expected_type}"', text)
        self.assertIn('("people/alice", "person")', text)
        self.assertIn('("projects/atlas", "project")', text)
        self.assertIn('("notes/welcome", "note")', text)
        for key in ("doctor", "sources_list", "schema_status_probe", "type_inference"):
            self.assertIn(f'self._matrix["{key}"]', text)

    def test_get_search_tags_scenarios_present(self) -> None:
        """The scenario module must exercise get/search/tags with the
        deterministic provider-free facts."""
        text = self._scenario_text()
        self.assertIn('"gbrain", "get", "notes/welcome"', text)
        self.assertIn('"gbrain", "search", CONFORMANCE_TOKEN, "--limit", "5"', text)
        self.assertIn('"gbrain", "tags", TAGGED_NOTE_SLUG', text)
        self.assertIn("CONFORMANCE_TOKEN", text)
        self.assertIn("TAGGED_NOTE_SLUG", text)
        self.assertIn("CONFORMANCE_TAG", text)

    def test_search_asserts_text_output_not_json(self) -> None:
        """The search scenario must assert TEXT output (the pinned CLI renders
        search as text lines) and must not demand JSON."""
        text = self._scenario_text()
        self.assertIn('self.assertIn("notes/welcome", ev.stdout)', text)
        self.assertIn('self.assertIn(CONFORMANCE_TOKEN, ev.stdout)', text)
        self.assertIn('self.assertNotIn(\'"slug"\', ev.stdout)', text)
        # The get/search/tags scenario must not parse its output as JSON.
        scenario = text.split("def _scenario_get_search_tags", 1)[1]
        scenario = scenario.split("def _find_backlink", 1)[0]
        self.assertNotIn("json.loads", scenario)

    def test_tagged_note_seeded_before_reindex(self) -> None:
        """The tagged note must be seeded in the base setup (before reindex)
        via a safe hermes command, never by mutating support or template."""
        text = self._module_text()
        self.assertIn("self._seed_tagged_note()", text)
        self.assertIn("self.runtime.run_as_hermes(\"sh\", \"-lc\", script)", text)
        self.assertIn("tags: [conformance]", text)
        self.assertIn("notes/conformance-tagged.md", text)

    def test_welcome_wikilink_seeded_before_reindex(self) -> None:
        """The welcome fixture page must carry the deterministic
        [[projects/atlas]] wikilink, seeded in the base setup before reindex
        via a safe hermes command."""
        text = self._module_text()
        self.assertIn("self._seed_welcome_wikilink()", text)
        self.assertIn("[[projects/atlas]]", text)
        self.assertIn("notes/welcome.md", text)
        self.assertIn("conformance-token-welcome", text)

    def test_links_backlinks_graph_scenarios_present(self) -> None:
        """The scenario module must exercise backlinks/link/graph/refresh with
        the deterministic link facts (semantic assertions, no full
        snapshots)."""
        text = self._scenario_text()
        self.assertIn('"gbrain", "backlinks", WIKILINK_TARGET', text)
        self.assertIn('"gbrain", "link", MANUAL_LINK_SOURCE, WIKILINK_TARGET', text)
        self.assertIn('"--link-source", "manual"', text)
        self.assertIn('"gbrain", "graph", WIKILINK_TARGET', text)
        self.assertIn('"gbrain", "graph", MANUAL_LINK_SOURCE', text)
        self.assertIn('"josemar-gbrain", "refresh", timeout=300', text)
        self.assertIn("_require_backlink", text)
        self.assertIn("MANUAL_LINK_CONTEXT", text)
        # Semantic edge lookup, never a brittle full-snapshot assertion.
        self.assertIn("_find_backlink", text)
        self.assertNotIn("assertEqual(backlinks,", text)
        self.assertNotIn("assertEqual(graph,", text)

    def test_public_write_contracts_scenarios_present(self) -> None:
        """The scenario module must exercise the public write contracts:
        positional capture create/read-back/idempotency, capture --stdin
        --slug --source --json, capture --file create/replacement, and
        put --content retained section."""
        text = self._scenario_text()
        self.assertIn('"gbrain", "capture", POSITIONAL_CAPTURE_BODY', text)
        self.assertIn('"--slug", POSITIONAL_CAPTURE_SLUG, "--type", "note", "--json"', text)
        self.assertIn("gbrain capture --stdin --slug ", text)
        self.assertIn("--source default --json", text)
        self.assertIn("gbrain capture --file ", text)
        self.assertIn('"gbrain", "put", PUT_SLUG, "--content", PUT_CONTENT', text)
        self.assertIn("STDIN_CAPTURE_BODY", text)
        self.assertIn("FILE_BODY_V1", text)
        self.assertIn("FILE_BODY_V2", text)
        self.assertIn("PUT_CONTENT", text)
        # Idempotency: the re-capture must assert the skipped status and the
        # unchanged content hash.
        self.assertIn('again.get("status"), "skipped"', text)
        self.assertIn('again.get("content_hash"), content_hash', text)

    def test_put_stdin_rejected_safety(self) -> None:
        """The public put --stdin rejection must be asserted as a safety
        contract (rc 2, allowlist message)."""
        text = self._scenario_text()
        self.assertIn("gbrain put inbox/evil --stdin", text)
        self.assertIn("self.assertEqual(ev.returncode, 2)", text)
        self.assertIn('"not on the agent-facing allowlist"', text)
        self.assertIn('self._matrix["put --stdin"]', text)

    def test_recovery_history_revert_scenario_present(self) -> None:
        """The scenario module must exercise the recovery-page lifecycle:
        create A, update B, discover a stable revision handle via history,
        revert to it restoring the exact A body, and remain writable."""
        text = self._scenario_text()
        self.assertIn("_scenario_recovery_history_revert", text)
        self.assertIn('"gbrain", "history", RECOVERY_SLUG', text)
        self.assertIn('"gbrain", "revert", RECOVERY_SLUG, revision', text)
        self.assertIn("_extract_revision_handle", text)
        self.assertIn("RECOVERY_BODY_A", text)
        self.assertIn("RECOVERY_BODY_B", text)
        self.assertIn("RECOVERY_BODY_C", text)
        self.assertIn("conformance-recovery-a", text)
        self.assertIn("conformance-recovery-b", text)
        self.assertIn('self._matrix["history"]', text)
        self.assertIn('self._matrix["revert"]', text)
        # The revision handle must be the PLAIN integer id: the pinned CLI
        # rejects the ``#1`` display form (invalid input syntax for integer).
        self.assertIn("return match.group(1)", text)
        self.assertNotIn("return match.group(0)", text)
        # The post-revert write must be a genuinely new write (C), not a
        # re-write of B (which the idempotency check would skip).
        self.assertIn('"gbrain", "put", RECOVERY_SLUG, "--content", RECOVERY_BODY_C', text)
        self.assertIn('put_result.get("status"), "created_or_updated"', text)

    def test_soft_delete_restore_scenario_present(self) -> None:
        """The scenario module must exercise the soft delete/restore lifecycle
        with the exact body."""
        text = self._scenario_text()
        self.assertIn("_scenario_soft_delete_restore", text)
        self.assertIn('"gbrain", "delete", SOFT_DELETE_SLUG', text)
        self.assertIn('"gbrain", "restore", SOFT_DELETE_SLUG', text)
        self.assertIn("SOFT_DELETE_BODY", text)
        self.assertIn("conformance-soft-delete-body", text)
        self.assertIn('self._matrix["delete"]', text)
        self.assertIn('self._matrix["restore"]', text)

    def test_external_edit_refresh_scenario_present(self) -> None:
        """The scenario module must exercise the direct committed external edit
        after activation: public get/search must NOT assume it before
        refresh, and after refresh the unique token is visible while an
        unrelated known page survives."""
        text = self._scenario_text()
        self.assertIn("_scenario_external_edit_refresh", text)
        self.assertIn("EXTERNAL_EDIT_TOKEN", text)
        self.assertIn("EXTERNAL_EDIT_SLUG", text)
        # Pre-refresh: get/search must NOT assume the edit.
        self.assertIn("self.assertNotIn(EXTERNAL_EDIT_TOKEN, ev.stdout)", text)
        self.assertIn('self.assertNotIn("notes/welcome", ev.stdout)', text)
        # Post-refresh: the unique token is visible.
        self.assertIn("self.assertIn(EXTERNAL_EDIT_TOKEN, ev.stdout)", text)
        self.assertIn('self.assertIn("notes/welcome", ev.stdout)', text)
        self.assertIn('self._matrix["external_edit_pre_refresh"]', text)
        self.assertIn('self._matrix["external_edit_post_refresh"]', text)
        # The unrelated known page must survive the refresh.
        self.assertIn('"gbrain", "get", TAGGED_NOTE_SLUG', text)
        self.assertIn('"gbrain", "tags", TAGGED_NOTE_SLUG', text)

    def test_lock_contention_scenario_present(self) -> None:
        """The scenario module must exercise shared-lock contention with an
        independent flock-only holder (no PGLite): refresh fails bounded with
        the lock-busy envelope, then succeeds after release."""
        text = self._scenario_text()
        self.assertIn("_scenario_lock_contention", text)
        self.assertIn("_start_lock_holder", text)
        self.assertIn("_stop_lock_holder", text)
        self.assertIn("refresh_lock_busy", text)
        self.assertIn('self._matrix["refresh_lock_busy"]', text)
        self.assertIn("LOCK_HOLDER_SCRIPT", text)
        # The holder must be flock-only: no PGLite/gbrain access in the body.
        self.assertIn("fcntl.flock", LOCK_HOLDER_SCRIPT)
        self.assertNotIn("gbrain", LOCK_HOLDER_SCRIPT)
        self.assertNotIn("pglite", LOCK_HOLDER_SCRIPT.lower())

    def test_public_boundary_scenario_present(self) -> None:
        """The scenario module must assert the operator-only ``gbrain reindex``
        is rejected by the public adapter (rc 2, allowlist message) without
        invoking the private native binary."""
        text = self._scenario_text()
        self.assertIn("_scenario_public_boundary", text)
        self.assertIn('"gbrain", "reindex", check=False', text)
        self.assertIn("self.assertEqual(ev.returncode, 2", text)
        self.assertIn('"not on the agent-facing allowlist"', text)
        self.assertIn(
            '"operator-only maintenance runs through josemar-gbrain"',
            text,
        )
        self.assertIn('self._matrix["public_reindex_rejected"]', text)

    def test_chronicle_zero_llm_scenario_present(self) -> None:
        """The scenario module must exercise every zero-LLM Chronicle read and
        assert clearly no synthetic events (empty arrays / explicit empty
        states / null last-seen)."""
        text = self._scenario_text()
        self.assertIn("_scenario_chronicle_zero_llm", text)
        for cmd in (
            "timeline", "day", "since", "last-seen", "on-this-day",
            "orient", "ontology",
        ):
            self.assertIn(f'"gbrain", "{cmd}"', text)
        self.assertIn("--week", text)
        self.assertIn("CHRONICLE_DAY", text)
        self.assertIn("CHRONICLE_ENTITY", text)
        self.assertIn("CHRONICLE_TIMELINE_SLUG", text)
        # No synthetic events: empty arrays / explicit empty states.
        self.assertIn("json.loads(ev.stdout), []", text)
        self.assertIn("No timeline entries", text)
        self.assertIn("recent_timeline", text)
        self.assertIn("last_date", text)
        self.assertIn("last_event_slug", text)
        # The loop-driven scenario must carry every chronicle matrix key and
        # mark each one fail/pass through the dynamic key.
        for key in (
            "chronicle_timeline", "chronicle_day", "chronicle_day_week",
            "chronicle_since", "chronicle_last_seen", "chronicle_on_this_day",
            "chronicle_orient", "chronicle_ontology",
        ):
            self.assertIn(key, text)
        self.assertIn('self._matrix[key] = "fail"', text)
        self.assertIn('self._matrix[key] = "pass"', text)


if __name__ == "__main__":
    unittest.main()
