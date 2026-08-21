"""Opt-in current-pin gbrain embedding conformance (issue #127 W4).

Runs the REAL TEI embedding lifecycle against the pinned gbrain build
(``GBRAIN_REF`` in ``Dockerfile.hermes``) inside the disposable conformance
runtime, using the optional embeddings overlay
(``docker-compose.embeddings.yml``) with the final test-isolation overlay
preserved (ComposeRuntime always applies it last). Scope:

  - baseline isolation contract (empty credentials, disabled owned jobs,
    synthetic vault) with the ``embeddings`` + ``hermes`` services started
    together; TEI health is waited on via the Compose ``service_healthy``
    dependency, never by sleep-polling
  - minimal core activation (``josemar-gbrain reindex``)
  - real TEI lifecycle: ``enable-embeddings``, ``embed-backfill``,
    semantic/hybrid ``gbrain search`` + ``gbrain query --no-expand``
    returning the expected page, a stale vault edit reconciled by
    ``refresh-embeddings``, ``disable-embeddings`` (keyword mode +
    ``embedding_disabled`` sentinel + preserved vectors), and re-enable
    WITHOUT a full backfill proving the preserved semantic corpus is usable
  - issue #124 reindex classification (fixed/present/changed_failure_mode/
    inconclusive) is REPORT-ONLY: recorded in the report metadata, never
    asserted
  - a synthetic report under ``dump_folder/gbrain-conformance`` with
    command/result metadata only (never environment dumps); blockers (e.g.
    the TEI model service cannot come up without network) are recorded
    honestly in the report

The candidate embedding upgrade (a different ``GBRAIN_REF``) is deliberately
NOT implemented here.

Runtime execution is gated strictly on ``RUN_DOCKER_TESTS=1`` AND
``RUN_GBRAIN_EMBEDDING_CONFORMANCE=1`` and skips when the docker CLI is
absent. Fast host-side gate/structure tests in this module always run and
need no Docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from .gbrain_conformance_support import (
    CONFORMANCE_EMPTY_ENV_KEYS,
    CommandEvidence,
    GbrainConformanceRuntime,
    conformance_report_dir,
    write_report,
)
from .helpers import REPO_ROOT, docker_available


# The pinned TEI model tuple (docker-compose.embeddings.yml defaults; the
# migration tuple the gbrain E5 signature is stamped against). Dimensions
# are validated internally by the operator wrapper (completion marker), not
# exposed by the TEI /info surface.
EMBEDDING_MODEL_ID = "intfloat/multilingual-e5-small"
EMBEDDING_MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"

# The embeddings overlay applied on top of the base compose; the final
# test-isolation overlay is always appended last by ComposeRuntime.
EMBEDDINGS_OVERLAY = REPO_ROOT / "docker-compose.embeddings.yml"

# Issue #124 reindex classification values. REPORT-ONLY: the runtime
# scenarios record one of these in the report metadata and never assert on
# it (the classification is an operator signal, not a conformance gate).
REINDEX_CLASSIFICATION_VALUES = (
    "fixed",
    "present",
    "changed_failure_mode",
    "inconclusive",
)

# Deterministic stale vault edit (as hermes, committed to the vault git
# repo): appends a new unique token so the page chunk becomes stale for
# embedding until `refresh-embeddings` reconciles it.
STALE_EDIT_SCRIPT = """set -eu
cd /opt/data/obsidian
cat >> notes/welcome.md <<'MD'

Stale-edit conformance token: conformance-token-stale-edit.
MD
git add .
git commit -qm "synthetic conformance vault stale edit"
"""

# W4 conformance matrix: every operation this increment owns, with its
# classification (mirroring the issue #127 operation classification in
# scripts/gbrain_chat_run.py). The report persists an explicit result for
# each.
EMBEDDING_CONFORMANCE_MATRIX = {
    "baseline_seed": "core",
    "baseline_build_start": "core",
    "baseline_writable": "core",
    "baseline_credentials": "core",
    "baseline_jobs": "core",
    "baseline_vault": "core",
    "tei_health": "core",
    "reindex": "operator_only",
    "enable_embeddings": "operator_only",
    "embed_backfill": "operator_only",
    "semantic_search": "embeddings_gated",
    "query_no_expand": "embeddings_gated",
    "stale_edit_refresh": "embeddings_gated",
    "disable_embeddings": "operator_only",
    "disable_keyword_sentinel": "operator_only",
    "disable_vectors_preserved": "embeddings_gated",
    "reenable_no_backfill": "operator_only",
    "reenable_semantic_usable": "embeddings_gated",
}


def _embedding_conformance_enabled() -> bool:
    """Strict gate: RUN_DOCKER_TESTS=1 AND RUN_GBRAIN_EMBEDDING_CONFORMANCE=1
    AND a docker CLI is available."""
    return (
        os.getenv("RUN_DOCKER_TESTS") == "1"
        and os.getenv("RUN_GBRAIN_EMBEDDING_CONFORMANCE") == "1"
        and docker_available()
    )


@unittest.skipUnless(
    _embedding_conformance_enabled(),
    "set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_EMBEDDING_CONFORMANCE=1 with a docker CLI",
)
class GbrainEmbeddingConformanceTestCase(unittest.TestCase):
    """Shared base setup for the embedding conformance runtime suite.

    Builds/starts the ``embeddings`` + ``hermes`` services of a disposable
    Compose project with the embeddings overlay (TEI health is waited on by
    the Compose ``service_healthy`` dependency inside ``up``, never by
    sleep-polling), seeds the real template source state BEFORE start, waits
    for the hermes-writable surface, asserts the isolation safety contract
    (empty credentials, disabled owned jobs), initializes the synthetic
    vault as the hermes runtime user, and unconditionally tears the project
    down with ``down -v --remove-orphans``.

    When the embeddings service cannot come up (e.g. no network for the TEI
    image/model download), the blocker is recorded and the report is still
    written with the honest blocker + ``inconclusive`` reindex
    classification.
    """

    def setUp(self) -> None:
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {
            op: "pass" if op.startswith("baseline_") else "not_run"
            for op in EMBEDDING_CONFORMANCE_MATRIX
        }
        self._gbrain_version: str | None = None
        self._report_path: Path | None = None
        self._reindex_classification: str = "inconclusive"
        self._blockers: list[str] = []

        self.runtime = GbrainConformanceRuntime(overlays=(EMBEDDINGS_OVERLAY,))
        # Pre-start source state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # Start embeddings + hermes only. `up` blocks until the embeddings
        # service passes its Compose healthcheck (hermes depends_on
        # service_healthy), so TEI readiness is waited on by Compose, not by
        # sleep-polling. A failure here is a blocker (e.g. no network for the
        # TEI image/model download), recorded honestly in the report.
        try:
            self.runtime.up("embeddings", "hermes", timeout=1200)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            self._blockers.append(f"embeddings/hermes start failed: {str(detail)[-500:]}")
            return
        # Wait for the exact hermes-writable surface before any exec probe.
        self.runtime.wait_until_hermes_writable(timeout=120)
        # Safety checks: empty credentials + disabled owned jobs.
        self._evidence.append(self._assert_no_credentials())
        self._evidence.append(self.runtime.assert_owned_jobs_disabled())
        # Synthetic vault init committed as the hermes runtime user.
        self._evidence.append(self.runtime.init_synthetic_vault())

    def tearDown(self) -> None:
        # Unconditional final cleanup: down -v --remove-orphans.
        self.runtime.cleanup()

    # --- safety helpers ---------------------------------------------------

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

    # --- observable helpers ----------------------------------------------

    def _read_gbrain_config(self) -> dict:
        """Read the gbrain file-plane config (``/opt/data/.gbrain/config.json``)
        as hermes. This is the same file plane the operator wrapper reads for
        the ``embedding_disabled`` sentinel."""
        ev = self.runtime.run_as_hermes("cat", "/opt/data/.gbrain/config.json")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        return json.loads(ev.stdout)

    def _embedding_coverage(self) -> float:
        """``gbrain status --json`` embedding coverage for the single vault
        source (embedded chunks / total chunks). Also captures the runtime
        gbrain version for the report."""
        ev = self.runtime.run_as_hermes("gbrain", "status", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        data = json.loads(ev.stdout)
        if self._gbrain_version is None:
            self._gbrain_version = data.get("version")
        sources = data.get("sync", {}).get("sources", [])
        self.assertEqual(len(sources), 1)
        return float(sources[0].get("embedding_coverage_pct", -1))

    # --- report -----------------------------------------------------------

    def _write_report(self) -> None:
        """Persist the synthetic conformance report under
        ``dump_folder/gbrain-conformance``. Contains command/result metadata
        only (argv, rc, stdout, stderr, elapsed) plus the explicit matrix,
        the issue #124 reindex classification, and any honest blockers —
        never the process or runtime environment."""
        metadata = {
            "baseline_ref": self.runtime.baseline_gbrain_ref(),
            "gbrain_version": self._gbrain_version,
            "matrix": self._matrix,
            "reindex_classification": self._reindex_classification,
        }
        if self._blockers:
            metadata["blockers"] = list(self._blockers)
        self._report_path = write_report(
            conformance_report_dir(),
            "gbrain-conformance-embeddings",
            self._evidence,
            metadata=metadata,
        )


class GbrainEmbeddingConformanceRuntimeTests(GbrainEmbeddingConformanceTestCase):
    """W4 current-pin embedding lifecycle scenarios (Docker-gated via the
    base class)."""

    def test_embedding_lifecycle_conformance(self) -> None:
        try:
            if self._blockers:
                self.fail("; ".join(self._blockers))
            self._scenario_tei_health()
            self._scenario_reindex()
            self._scenario_enable_embeddings()
            self._scenario_embed_backfill()
            self._scenario_semantic_search()
            self._scenario_query_no_expand()
            self._scenario_stale_edit_refresh()
            self._scenario_disable_embeddings()
            self._scenario_reenable_no_backfill()
        finally:
            self._write_report()

    def _scenario_tei_health(self) -> None:
        """TEI serves the pinned model tuple: /health returns HTTP 200 (the
        TEI 1.9 health contract — empty body; the compose healthcheck probes
        it with ``curl -f``) and /info reports the exact migration-tuple
        model id + revision (``model_sha``)."""
        self._matrix["tei_health"] = "fail"
        health = self.runtime.run_as_hermes(
            "curl", "-fsS", "http://embeddings:80/health", timeout=60
        )
        self.assertEqual(health.returncode, 0, health.stderr)
        self._evidence.append(health)
        info = self.runtime.run_as_hermes(
            "curl", "-fsS", "http://embeddings:80/info", timeout=60
        )
        self.assertEqual(info.returncode, 0, info.stderr)
        self._evidence.append(info)
        data = json.loads(info.stdout)
        self.assertEqual(data.get("model_id"), EMBEDDING_MODEL_ID)
        self.assertEqual(data.get("model_sha"), EMBEDDING_MODEL_REVISION)
        self._matrix["tei_health"] = "pass"

    def _scenario_reindex(self) -> None:
        """Minimal core activation. The issue #124 reindex classification is
        REPORT-ONLY: derived from the outcome and recorded in the report
        metadata, never asserted."""
        self._matrix["reindex"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)
        self._evidence.append(ev)
        if ev.returncode != 0:
            self._reindex_classification = "present"
            self.fail(f"reindex failed: {ev.stderr[-800:]}")
        try:
            envelope = json.loads(ev.stdout)
        except json.JSONDecodeError:
            self._reindex_classification = "changed_failure_mode"
            self.fail(f"reindex envelope is not JSON: {ev.stdout[:400]}")
        if envelope.get("success") is not True or envelope.get("action") != "reindex":
            self._reindex_classification = "changed_failure_mode"
            self.fail(f"reindex envelope shape changed: {ev.stdout[:400]}")
        self._reindex_classification = "fixed"
        self._matrix["reindex"] = "pass"

    def _scenario_enable_embeddings(self) -> None:
        """Non-destructive semantic switch: migration succeeds (the wrapper's
        live provider probe round-trips TEI) and the ``embedding_disabled``
        sentinel is cleared in the file plane."""
        self._matrix["enable_embeddings"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "enable-embeddings", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "enable-embeddings")
        cfg = self._read_gbrain_config()
        self.assertIsNot(cfg.get("embedding_disabled"), True)
        self._matrix["enable_embeddings"] = "pass"

    def _scenario_embed_backfill(self) -> None:
        """One-shot vectorization: the wrapper asserts zero stale embeddings
        remain, and the observable embedding coverage reaches 100%."""
        self._matrix["embed_backfill"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "embed-backfill", timeout=600)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "embed-backfill")
        self.assertEqual(self._embedding_coverage(), 100.0)
        self._matrix["embed_backfill"] = "pass"

    def _scenario_semantic_search(self) -> None:
        """Semantic/hybrid ``gbrain search`` returns the expected page for
        the deterministic unique token (hybrid path active after enable +
        backfill)."""
        self._matrix["semantic_search"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "search", "conformance-token-welcome", "--limit", "5", timeout=120
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self.assertIn("notes/welcome", ev.stdout)
        self.assertIn("conformance-token-welcome", ev.stdout)
        self._matrix["semantic_search"] = "pass"

    def _scenario_query_no_expand(self) -> None:
        """``gbrain query --no-expand`` (hybrid without expansion) returns
        the expected page."""
        self._matrix["query_no_expand"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "query", "--no-expand", "conformance-token-welcome",
            "--limit", "5", timeout=120,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self.assertIn("notes/welcome", ev.stdout)
        self.assertIn("conformance-token-welcome", ev.stdout)
        self._matrix["query_no_expand"] = "pass"

    def _scenario_stale_edit_refresh(self) -> None:
        """A manual vault edit (new unique token, committed as hermes) makes
        the page chunk stale; ``refresh-embeddings`` reconciles it (sync +
        stale-only embed) and the new token becomes searchable with full
        embedding coverage restored."""
        self._matrix["stale_edit_refresh"] = "fail"
        edit = self.runtime.run_as_hermes("sh", "-lc", STALE_EDIT_SCRIPT, timeout=120)
        self.assertEqual(edit.returncode, 0, edit.stderr)
        self._evidence.append(edit)
        refresh = self.runtime.run_as_hermes(
            "josemar-gbrain", "refresh-embeddings", timeout=600
        )
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        self._evidence.append(refresh)
        envelope = json.loads(refresh.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "refresh-embeddings")
        search = self.runtime.run_as_hermes(
            "gbrain", "search", "conformance-token-stale-edit", "--limit", "5", timeout=120
        )
        self.assertEqual(search.returncode, 0, search.stderr)
        self._evidence.append(search)
        # The stale token sits beyond the 100-char result preview; the slug
        # proves the new chunk is indexed and searchable.
        self.assertIn("notes/welcome", search.stdout)
        self.assertEqual(self._embedding_coverage(), 100.0)
        self._matrix["stale_edit_refresh"] = "pass"

    def _scenario_disable_embeddings(self) -> None:
        """Safe rollback: keyword mode (the wrapper's own refresh-embeddings
        gate reports ``keyword_only``), the ``embedding_disabled`` sentinel
        in the file plane, keyword search still serving the expected page,
        and vectors preserved (coverage unchanged at 100%)."""
        self._matrix["disable_embeddings"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "disable-embeddings", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "disable-embeddings")
        self._matrix["disable_embeddings"] = "pass"

        self._matrix["disable_keyword_sentinel"] = "fail"
        # Keyword mode: refresh-embeddings reads search.mcp_keyword_only from
        # the DB and must skip with reason keyword_only.
        skip = self.runtime.run_as_hermes(
            "josemar-gbrain", "refresh-embeddings", timeout=300
        )
        self.assertEqual(skip.returncode, 0, skip.stderr)
        self._evidence.append(skip)
        skip_env = json.loads(skip.stdout)
        self.assertEqual(skip_env.get("status"), "skipped")
        self.assertEqual(skip_env.get("reason"), "keyword_only")
        # Sentinel in the file plane.
        cfg = self._read_gbrain_config()
        self.assertIs(cfg.get("embedding_disabled"), True)
        # Keyword search still serves the expected page.
        search = self.runtime.run_as_hermes(
            "gbrain", "search", "conformance-token-welcome", "--limit", "5", timeout=120
        )
        self.assertEqual(search.returncode, 0, search.stderr)
        self._evidence.append(search)
        self.assertIn("notes/welcome", search.stdout)
        self.assertIn("conformance-token-welcome", search.stdout)
        self._matrix["disable_keyword_sentinel"] = "pass"

        self._matrix["disable_vectors_preserved"] = "fail"
        self.assertEqual(self._embedding_coverage(), 100.0)
        self._matrix["disable_vectors_preserved"] = "pass"

    def _scenario_reenable_no_backfill(self) -> None:
        """Re-enable WITHOUT a full backfill: the migration clears the
        sentinel, and the preserved vectors serve semantic/hybrid search and
        ``query --no-expand`` immediately (coverage stays 100%)."""
        self._matrix["reenable_no_backfill"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "enable-embeddings", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "enable-embeddings")
        cfg = self._read_gbrain_config()
        self.assertIsNot(cfg.get("embedding_disabled"), True)
        self._matrix["reenable_no_backfill"] = "pass"

        self._matrix["reenable_semantic_usable"] = "fail"
        search = self.runtime.run_as_hermes(
            "gbrain", "search", "conformance-token-welcome", "--limit", "5", timeout=120
        )
        self.assertEqual(search.returncode, 0, search.stderr)
        self._evidence.append(search)
        self.assertIn("notes/welcome", search.stdout)
        self.assertIn("conformance-token-welcome", search.stdout)
        query = self.runtime.run_as_hermes(
            "gbrain", "query", "--no-expand", "conformance-token-welcome",
            "--limit", "5", timeout=120,
        )
        self.assertEqual(query.returncode, 0, query.stderr)
        self._evidence.append(query)
        self.assertIn("notes/welcome", query.stdout)
        self.assertIn("conformance-token-welcome", query.stdout)
        self.assertEqual(self._embedding_coverage(), 100.0)
        self._matrix["reenable_semantic_usable"] = "pass"


class GbrainEmbeddingConformanceGateStructureTests(unittest.TestCase):
    """Fast host-side guards for the embedding conformance gate and module
    structure. No Docker required; these run in the normal fast suite."""

    @staticmethod
    def _module_text() -> str:
        return (
            REPO_ROOT / "tests" / "runtime" / "test_gbrain_conformance_embeddings.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _runtime_class_text() -> str:
        """The runtime portion of the module: the shared base case class plus
        the Docker-gated scenario class (everything before the fast gate
        structure tests)."""
        text = GbrainEmbeddingConformanceGateStructureTests._module_text()
        runtime_class = text.split("class GbrainEmbeddingConformanceTestCase", 1)[1]
        return runtime_class.split("class GbrainEmbeddingConformanceGateStructureTests", 1)[0]

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
            os.environ, {"RUN_DOCKER_TESTS": "", "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_embedding_conformance_enabled())

    def test_gate_requires_run_gbrain_embedding_conformance(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_EMBEDDING_CONFORMANCE": ""}
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_embedding_conformance_enabled())

    def test_gate_requires_docker(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(False):
                self.assertFalse(_embedding_conformance_enabled())

    def test_gate_enabled_when_all_conditions_met(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(True):
                self.assertTrue(_embedding_conformance_enabled())

    def test_runtime_class_is_gated_on_both_env_vars(self) -> None:
        text = self._module_text()
        self.assertIn("RUN_DOCKER_TESTS", text)
        self.assertIn("RUN_GBRAIN_EMBEDDING_CONFORMANCE", text)
        self.assertIn("skipUnless", text)
        self.assertIn(
            '"set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_EMBEDDING_CONFORMANCE=1 with a docker CLI"',
            text,
        )

    def test_runtime_uses_embeddings_overlay(self) -> None:
        """The runtime must drive the embeddings overlay through the
        conformance runtime's explicit-overlay seam; the final test-isolation
        overlay is always appended last by ComposeRuntime."""
        text = self._module_text()
        self.assertIn("GbrainConformanceRuntime(", text)
        self.assertIn("overlays=(", text)
        self.assertIn("docker-compose.embeddings.yml", text)

    def test_runtime_starts_embeddings_and_hermes_without_sleep_polling(self) -> None:
        runtime_class = self._runtime_class_text()
        self.assertIn('self.runtime.up("embeddings", "hermes"', runtime_class)
        # TEI readiness is waited on by the Compose service_healthy
        # dependency, never by sleep-polling.
        self.assertNotIn("time.sleep", runtime_class)

    def test_lifecycle_commands_use_pinned_operator_surface(self) -> None:
        runtime_class = self._runtime_class_text()
        for cmd in (
            "enable-embeddings", "embed-backfill", "refresh-embeddings", "disable-embeddings",
        ):
            self.assertIn(f'"josemar-gbrain", "{cmd}"', runtime_class)
        self.assertIn('"gbrain", "search"', runtime_class)
        self.assertIn('"gbrain", "query", "--no-expand"', runtime_class)
        self.assertIn('"gbrain", "status", "--json"', runtime_class)

    def test_no_candidate_embedding_upgrade_implemented(self) -> None:
        """The candidate embedding upgrade (a different GBRAIN_REF) is
        deliberately out of scope for this increment."""
        runtime_class = self._runtime_class_text()
        for forbidden in ("build_candidate", "recreate_same_volumes", "normalize_candidate_ref"):
            self.assertNotIn(forbidden, runtime_class)

    def test_report_uses_support_without_env_dump(self) -> None:
        text = self._module_text()
        self.assertIn("write_report", text)
        self.assertIn("conformance_report_dir", text)
        # The report must never serialize the process/runtime environment:
        # the report-writing method must not reference os.environ at all.
        report_method = text.split("def _write_report", 1)[1]
        report_method = report_method.split("class ", 1)[0]
        self.assertNotIn("os.environ", report_method)

    def test_matrix_covers_all_owned_operations(self) -> None:
        self.assertEqual(
            set(EMBEDDING_CONFORMANCE_MATRIX),
            {
                "baseline_seed",
                "baseline_build_start",
                "baseline_writable",
                "baseline_credentials",
                "baseline_jobs",
                "baseline_vault",
                "tei_health",
                "reindex",
                "enable_embeddings",
                "embed_backfill",
                "semantic_search",
                "query_no_expand",
                "stale_edit_refresh",
                "disable_embeddings",
                "disable_keyword_sentinel",
                "disable_vectors_preserved",
                "reenable_no_backfill",
                "reenable_semantic_usable",
            },
        )

    def test_reindex_classification_is_report_only(self) -> None:
        """Issue #124 reindex classification values are fixed and recorded in
        the report metadata; the runtime scenarios must never assert on the
        value."""
        self.assertEqual(
            set(REINDEX_CLASSIFICATION_VALUES),
            {"fixed", "present", "changed_failure_mode", "inconclusive"},
        )
        runtime_class = self._runtime_class_text()
        self.assertIn('"reindex_classification"', runtime_class)
        self.assertNotIn("self.assertEqual(self._reindex_classification", runtime_class)


if __name__ == "__main__":
    unittest.main()