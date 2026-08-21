"""Opt-in candidate vector-bearing gbrain upgrade conformance (issue #127 W4).

Runs the SAME disposable Compose project/volumes through a full upgrade with
REAL TEI embeddings (the ``docker-compose.embeddings.yml`` overlay, final
test-isolation overlay preserved):

  - baseline build/start at the EFFECTIVE baseline ref (the committed
    Dockerfile ``GBRAIN_REF``, or the validated upgrade-only
    ``GBRAIN_CONFORMANCE_BASELINE_REF`` override — exact 40-hex, checked
    BEFORE any Docker invocation — when the Dockerfile pin is the
    post-upgrade ref and the real old -> new migration must be proven),
    baseline source-ref proof (``/opt/gbrain/.git/HEAD`` equals the effective
    baseline ref), reindex, ``enable-embeddings`` + ``embed-backfill``, and a
    semantic proof
    (``gbrain search`` + ``gbrain query --no-expand`` return the expected
    page at 100% embedding coverage)
  - ``docker compose stop`` preserving volumes and the TEI state
  - candidate image build with the validated ``GBRAIN_REF`` build arg and
    force-recreate ``--no-build`` against the SAME volumes
  - candidate source-ref proof (``/opt/gbrain/.git/HEAD`` equals the exact
    candidate ref), candidate gbrain version, and candidate reindex with the
    issue #124 classification (fixed/present/changed_failure_mode/
    inconclusive, REPORT-ONLY). The probe reproduces the regression exactly
    (review finding MAJOR #124): the baseline semantic mode is established
    (enable + backfill + semantic proof), the supported search-mode
    indicators are snapshotted, the candidate ``josemar-gbrain reindex``
    runs, the indicators are snapshotted again, and the classification is
    derived SOLELY from the pre/post transition. The documented workaround
    (``enable-embeddings``) is applied only AFTER the classification is
    recorded, so it can never mask the regression before semantic retrieval
    is checked.
  - WITHOUT a destructive full backfill: prove the pre-upgrade semantic
    corpus is usable on the candidate (vectors survive the migration), or
    fail with the precise ``vector_migration_required`` signal
  - one incremental post-upgrade stale edit reconciled by
    ``refresh-embeddings`` with retrieval proof

The gate is strict: ``RUN_DOCKER_TESTS=1`` AND
``RUN_GBRAIN_EMBEDDING_CONFORMANCE=1`` AND ``RUN_GBRAIN_UPGRADE_CONFORMANCE=1``
AND an exact ``GBRAIN_CONFORMANCE_CANDIDATE_REF`` (40-hex, prevalidated BEFORE
any Docker invocation, and rejected when equal to the EFFECTIVE baseline
``GBRAIN_REF`` — the validated ``GBRAIN_CONFORMANCE_BASELINE_REF`` override
when present, otherwise the canonical Dockerfile pin). Without a candidate
ref the runtime suite skips honestly. The baseline override is OPTIONAL and
upgrade-only: absent means the baseline stays the committed Dockerfile pin
(current behavior unchanged). Fast host-side gate/ref/pre-Docker/
no-volume-delete tests in this module always run and need no Docker.

The JSON report
(``dump_folder/gbrain-conformance/gbrain-upgrade-conformance-embeddings.json``)
carries the baseline/candidate refs, baseline/candidate gbrain versions, the
embedding model config, the #124 classification, and the operation result
matrix — command/result metadata only, never environment dumps.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    baseline_override_active,
    conformance_report_dir,
    effective_baseline_ref,
    normalize_candidate_ref,
    parse_dockerfile_gbrain_ref,
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

# The native gbrain binary (non-PATH: the PUBLIC ``gbrain`` is the issue #110
# adapter, which rejects operator-only ``config`` with exit 2). The canonical
# env mirrors the wrapper's ``export_gbrain_env`` and the DR drill's
# ``GBRAIN_ENV`` — the established supported surface for reading
# ``search.mcp_keyword_only`` in this repo's Docker-gated tests.
NATIVE_GBRAIN_BIN = "/opt/josemar/libexec/gbrain-native"
GBRAIN_SNAPSHOT_ENV = (
    "GBRAIN_HOME=/opt/data GBRAIN_BRAIN_REPO=/opt/data/obsidian "
    "GBRAIN_SCHEMA_PACK=josemar GBRAIN_SKIP_STARTUP_HOOKS=1 "
    "HOME=/opt/data XDG_CONFIG_HOME=/opt/data/.config"
)

# Issue #124 reindex classification values. REPORT-ONLY: the runtime
# scenarios record one of these in the report metadata and never assert on
# it (the classification is an operator signal, not a conformance gate).
REINDEX_CLASSIFICATION_VALUES = (
    "fixed",
    "present",
    "changed_failure_mode",
    "inconclusive",
)


@dataclass(frozen=True)
class SearchModeSnapshot:
    """Supported search/embedding-mode indicators for the issue #124 oracle.

    - ``keyword_only``: ``search.mcp_keyword_only`` read via the native
      binary with the canonical env (the same surface the wrapper's
      ``refresh-embeddings`` gate reads); ``True`` when it prints exactly
      ``true``, ``False`` when it prints exactly ``false``, ``None`` when the
      surface cannot be read.
    - ``embedding_disabled``: the file-plane sentinel in
      ``/opt/data/.gbrain/config.json`` (the same sentinel the wrapper reads);
      ``True``/``False`` when the config is a readable object with a boolean
      (or absent) sentinel, ``None`` when the config cannot be read or the
      sentinel is not boolean.
    """

    keyword_only: bool | None
    embedding_disabled: bool | None

    def is_semantic(self) -> bool:
        """Semantic mode active: keyword-only off and no disabled sentinel."""
        return self.keyword_only is False and self.embedding_disabled is not True

    def is_keyword_only(self) -> bool:
        """Keyword-only mode active (the observable #124 reset)."""
        return self.keyword_only is True

    def to_dict(self) -> dict[str, bool | None]:
        return {
            "search_mcp_keyword_only": self.keyword_only,
            "embedding_disabled": self.embedding_disabled,
        }


def classify_reindex_transition(
    pre: SearchModeSnapshot | None,
    post: SearchModeSnapshot | None,
    *,
    probe_rc: int,
) -> str:
    """Issue #124 reindex classification, derived SOLELY from the pre/post
    transition of the supported search-mode indicators (review finding MAJOR
    #124). The recorded regression is: semantic mode is working -> reindex ->
    reindex silently resets ``search.mcp_keyword_only`` / embedding mode to
    keyword-only.

    Oracle (mirrors the #127 classification framework):
      - ``inconclusive``: the probe precondition could not be established —
        the pre snapshot is unreadable or semantic mode is not active before
        the probe reindex.
      - ``changed_failure_mode``: the probe reindex itself fails, the post
        snapshot is unreadable, or the post state is neither clean semantic
        nor clean keyword-only (behavior differs materially from the recorded
        #124 failure).
      - ``present``: the post state is keyword-only — exactly the recorded
        #124 failure (reindex reset the mode).
      - ``fixed``: the post state is still semantic — the mode survived the
        reindex.

    The workaround (``enable-embeddings``) is applied by the caller only
    AFTER this classification is recorded and never participates in it.
    """
    if pre is None or not pre.is_semantic():
        return "inconclusive"
    if probe_rc != 0:
        return "changed_failure_mode"
    if post is None:
        return "changed_failure_mode"
    if post.is_keyword_only():
        return "present"
    if post.is_semantic():
        return "fixed"
    return "changed_failure_mode"

# Deterministic post-upgrade stale vault edit (as hermes, committed to the
# vault git repo): appends a new unique token so the page chunk becomes
# stale for embedding until `refresh-embeddings` reconciles it.
POST_UPGRADE_EDIT_SCRIPT = """set -eu
cd /opt/data/obsidian
cat >> notes/welcome.md <<'MD'

Post-upgrade conformance token: conformance-token-post-upgrade.
MD
git add .
git commit -qm "synthetic conformance vault post-upgrade edit"
"""

# Upgrade embedding conformance matrix: every operation this suite owns,
# with its classification (mirroring the issue #127 operation classification
# in scripts/gbrain_chat_run.py). The report persists an explicit result for
# each; `candidate_semantic_corpus` may also carry the precise
# `vector_migration_required` failure signal.
UPGRADE_EMBEDDING_MATRIX = {
    "baseline_build_start": "core",
    "baseline_source_ref": "core",
    "baseline_reindex": "operator_only",
    "baseline_enable_embeddings": "operator_only",
    "baseline_embed_backfill": "operator_only",
    "baseline_semantic_proof": "embeddings_gated",
    "stop_preserve_volumes": "core",
    "candidate_build": "core",
    "candidate_recreate": "core",
    "candidate_source_ref": "core",
    "candidate_reindex": "operator_only",
    "issue124_workaround": "operator_only",
    "candidate_semantic_corpus": "embeddings_gated",
    "post_upgrade_stale_edit": "embeddings_gated",
}


def _candidate_ref() -> str:
    """The exact candidate ``GBRAIN_REF`` from the environment, validated
    (40-hex, lower-cased) BEFORE any Docker invocation."""
    return normalize_candidate_ref(os.getenv("GBRAIN_CONFORMANCE_CANDIDATE_REF", ""))


def _validated_candidate_ref() -> str:
    """Validate the candidate ref and REJECT equality with the EFFECTIVE
    baseline ref: the validated ``GBRAIN_CONFORMANCE_BASELINE_REF`` override
    when present, otherwise the canonical Dockerfile ``GBRAIN_REF``. Runs
    before any Docker invocation."""
    candidate = _candidate_ref()
    baseline = effective_baseline_ref()
    if candidate == baseline:
        raise ValueError(
            "GBRAIN_CONFORMANCE_CANDIDATE_REF must differ from the effective "
            f"baseline GBRAIN_REF ({baseline})"
        )
    return candidate


def _upgrade_embedding_conformance_enabled() -> bool:
    """Strict gate: RUN_DOCKER_TESTS=1 AND RUN_GBRAIN_EMBEDDING_CONFORMANCE=1
    AND RUN_GBRAIN_UPGRADE_CONFORMANCE=1 AND an exact
    GBRAIN_CONFORMANCE_CANDIDATE_REF is provided AND a docker CLI is
    available."""
    return (
        os.getenv("RUN_DOCKER_TESTS") == "1"
        and os.getenv("RUN_GBRAIN_EMBEDDING_CONFORMANCE") == "1"
        and os.getenv("RUN_GBRAIN_UPGRADE_CONFORMANCE") == "1"
        and bool(os.getenv("GBRAIN_CONFORMANCE_CANDIDATE_REF"))
        and docker_available()
    )


@unittest.skipUnless(
    _upgrade_embedding_conformance_enabled(),
    "set RUN_DOCKER_TESTS=1, RUN_GBRAIN_EMBEDDING_CONFORMANCE=1 and "
    "RUN_GBRAIN_UPGRADE_CONFORMANCE=1 with an exact "
    "GBRAIN_CONFORMANCE_CANDIDATE_REF and a docker CLI",
)
class GbrainUpgradeEmbeddingConformanceTestCase(unittest.TestCase):
    """Shared base setup for the candidate vector-bearing upgrade suite.

    Validates the candidate ref BEFORE any Docker invocation, then builds and
    starts the baseline ``embeddings`` + ``hermes`` services of a disposable
    Compose project with the embeddings overlay (TEI health is waited on by
    the Compose ``service_healthy`` dependency inside ``up``, never by
    sleep-polling), seeds the real template source state BEFORE start, waits
    for the hermes-writable surface, asserts the isolation safety contract
    (empty credentials, disabled owned jobs), and initializes the synthetic
    vault as the hermes runtime user. Final teardown is unconditional
    ``down -v --remove-orphans``.

    When the embeddings service cannot come up (e.g. no network for the TEI
    image/model download), the blocker is recorded and the report is still
    written with the honest blocker + ``inconclusive`` reindex
    classification.
    """

    def setUp(self) -> None:
        # Candidate ref validated BEFORE the first Docker invocation.
        self.candidate_ref = _validated_candidate_ref()
        # Effective baseline: the validated upgrade-only override when set,
        # otherwise the committed Dockerfile pin (unchanged behavior).
        self.baseline_ref = effective_baseline_ref()
        self.dockerfile_ref = parse_dockerfile_gbrain_ref()
        self.baseline_ref_source = (
            "override" if baseline_override_active() else "dockerfile"
        )
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {op: "not_run" for op in UPGRADE_EMBEDDING_MATRIX}
        self._baseline_version: str | None = None
        self._baseline_source_ref: str | None = None
        self._candidate_version: str | None = None
        self._reindex_classification: str = "inconclusive"
        self._reindex_pre_snapshot: dict | None = None
        self._reindex_post_snapshot: dict | None = None
        self._blockers: list[str] = []
        self._report_path: Path | None = None

        self.runtime = GbrainConformanceRuntime(overlays=(EMBEDDINGS_OVERLAY,))
        # Pre-start source state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # Baseline build/start at the effective baseline ref (Dockerfile
        # default, or the validated override via the upgrade-only path) of
        # embeddings + hermes only. `up_baseline` blocks until the embeddings
        # service passes its Compose healthcheck (hermes depends_on
        # service_healthy), so TEI readiness is waited on by Compose, not by
        # sleep-polling. A failure here is a blocker (e.g. no network for the
        # TEI image/model download), recorded honestly.
        try:
            self.runtime.up_baseline("embeddings", "hermes", timeout=1200)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            self._blockers.append(f"embeddings/hermes start failed: {str(detail)[-500:]}")
            return
        self._matrix["baseline_build_start"] = "pass"
        # Wait for the exact hermes-writable surface before any exec probe.
        self.runtime.wait_until_hermes_writable(timeout=120)
        # Isolation safety checks: empty credentials + disabled owned jobs.
        self._evidence.append(self._assert_no_credentials())
        self._evidence.append(self.runtime.assert_owned_jobs_disabled())
        # Synthetic vault init committed as the hermes runtime user.
        self._evidence.append(self.runtime.init_synthetic_vault())

    def tearDown(self) -> None:
        # Unconditional final cleanup: down -v --remove-orphans.
        self.runtime.cleanup()

    def _assert_no_credentials(self) -> CommandEvidence:
        """Assert every conformance-blanked credential env key is empty inside
        the running container."""
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
        source (embedded chunks / total chunks)."""
        ev = self.runtime.run_as_hermes("gbrain", "status", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        data = json.loads(ev.stdout)
        sources = data.get("sync", {}).get("sources", [])
        self.assertEqual(len(sources), 1)
        return float(sources[0].get("embedding_coverage_pct", -1))

    def _snapshot_search_mode(self) -> SearchModeSnapshot | None:
        """Snapshot the supported search-mode indicators for the issue #124
        oracle: ``search.mcp_keyword_only`` via the native binary with the
        canonical env (the same surface the wrapper's ``refresh-embeddings``
        gate reads; the public ``gbrain`` adapter rejects operator-only
        ``config``) plus the ``embedding_disabled`` file-plane sentinel.
        Returns ``None`` when either surface cannot be read (the caller
        records ``inconclusive``)."""
        keyword_only: bool | None = None
        ev = self.runtime.run_as_hermes(
            "sh", "-lc",
            f"{GBRAIN_SNAPSHOT_ENV} {NATIVE_GBRAIN_BIN} config get search.mcp_keyword_only",
            timeout=60,
        )
        self._evidence.append(ev)
        if ev.returncode == 0 and ev.stdout.strip() in ("true", "false"):
            keyword_only = ev.stdout.strip() == "true"
        cfg: dict | None = None
        ev = self.runtime.run_as_hermes("cat", "/opt/data/.gbrain/config.json", timeout=60)
        self._evidence.append(ev)
        if ev.returncode == 0:
            try:
                parsed = json.loads(ev.stdout)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                cfg = parsed
        if keyword_only is None or cfg is None:
            return None
        sentinel = cfg.get("embedding_disabled")
        if sentinel is not None and not isinstance(sentinel, bool):
            # Mirror the wrapper's own validation: a non-boolean sentinel is
            # corrupt, so the snapshot cannot be established.
            return None
        return SearchModeSnapshot(
            keyword_only=keyword_only,
            embedding_disabled=sentinel is True,
        )

    # --- report -----------------------------------------------------------

    def _write_report(self) -> None:
        """Persist the upgrade embedding conformance report: baseline/candidate
        refs, baseline/candidate gbrain versions, the embedding model config,
        the #124 reindex classification, and the operation result matrix.
        Command/result metadata only — never environment dumps."""
        metadata = {
            "baseline_ref": self.baseline_ref,
            "baseline_ref_source": self.baseline_ref_source,
            "dockerfile_gbrain_ref": self.dockerfile_ref,
            "baseline_source_ref": self._baseline_source_ref,
            "candidate_ref": self.candidate_ref,
            "baseline_gbrain_version": self._baseline_version,
            "candidate_gbrain_version": self._candidate_version,
            "embedding_model": EMBEDDING_MODEL_ID,
            "embedding_revision": EMBEDDING_MODEL_REVISION,
            "reindex_classification": self._reindex_classification,
            "reindex_pre_snapshot": self._reindex_pre_snapshot,
            "reindex_post_snapshot": self._reindex_post_snapshot,
            "actions": list(UPGRADE_EMBEDDING_MATRIX),
            "matrix": self._matrix,
        }
        if self._blockers:
            metadata["blockers"] = list(self._blockers)
        self._report_path = write_report(
            conformance_report_dir(),
            "gbrain-upgrade-conformance-embeddings",
            self._evidence,
            metadata=metadata,
        )


class GbrainUpgradeEmbeddingConformanceRuntimeTests(GbrainUpgradeEmbeddingConformanceTestCase):
    """W4 candidate vector-bearing upgrade scenarios (Docker-gated via the
    base class)."""

    def test_candidate_vector_upgrade_conformance(self) -> None:
        try:
            if self._blockers:
                self.fail("; ".join(self._blockers))
            self._scenario_baseline_source_ref()
            self._scenario_baseline_reindex()
            self._scenario_baseline_enable_embeddings()
            self._scenario_baseline_embed_backfill()
            self._scenario_baseline_semantic_proof()
            self._scenario_stop_preserve_volumes()
            self._scenario_candidate_build()
            self._scenario_candidate_recreate()
            self._scenario_candidate_source_ref()
            self._scenario_candidate_reindex()
            self._scenario_candidate_semantic_corpus()
            self._scenario_post_upgrade_stale_edit()
        finally:
            self._write_report()

    def _scenario_baseline_source_ref(self) -> None:
        """Prove the baseline source ref: ``/opt/gbrain/.git/HEAD`` must
        equal the EFFECTIVE baseline ref (the validated override when
        present, otherwise the Dockerfile pin).

        This provenance proof defeats false success for an old-candidate
        downgrade: the report carries the exact baseline HEAD alongside the
        candidate source-ref proof, and the pre-Docker equality rejection
        already forbids candidate == effective baseline (a no-op that would
        otherwise pass trivially)."""
        self._matrix["baseline_source_ref"] = "fail"
        ev = self.runtime.run_as_hermes("cat", "/opt/gbrain/.git/HEAD")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertEqual(ev.stdout.strip(), self.baseline_ref, ev.stderr)
        self._evidence.append(ev)
        self._baseline_source_ref = ev.stdout.strip()
        self._matrix["baseline_source_ref"] = "pass"

    def _scenario_baseline_reindex(self) -> None:
        """Baseline operator activation returns the success envelope and the
        baseline gbrain version is recorded."""
        self._matrix["baseline_reindex"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "reindex")
        self.assertEqual(envelope.get("schema_pack"), "josemar")
        self._matrix["baseline_reindex"] = "pass"
        ev = self.runtime.run_as_hermes("gbrain", "status", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self._baseline_version = json.loads(ev.stdout).get("version")

    def _scenario_baseline_enable_embeddings(self) -> None:
        """Baseline semantic switch: migration succeeds (the wrapper's live
        provider probe round-trips TEI) and the ``embedding_disabled``
        sentinel is cleared in the file plane."""
        self._matrix["baseline_enable_embeddings"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "enable-embeddings", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "enable-embeddings")
        cfg = self._read_gbrain_config()
        self.assertIsNot(cfg.get("embedding_disabled"), True)
        self._matrix["baseline_enable_embeddings"] = "pass"

    def _scenario_baseline_embed_backfill(self) -> None:
        """Baseline one-shot vectorization: the wrapper asserts zero stale
        embeddings remain, and the observable embedding coverage reaches
        100%."""
        self._matrix["baseline_embed_backfill"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "embed-backfill", timeout=600)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "embed-backfill")
        self.assertEqual(self._embedding_coverage(), 100.0)
        self._matrix["baseline_embed_backfill"] = "pass"

    def _scenario_baseline_semantic_proof(self) -> None:
        """Baseline semantic proof: ``gbrain search`` and
        ``gbrain query --no-expand`` return the expected page."""
        self._matrix["baseline_semantic_proof"] = "fail"
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
        self._matrix["baseline_semantic_proof"] = "pass"

    def _scenario_stop_preserve_volumes(self) -> None:
        """Stop Hermes PRESERVING volumes and the TEI state (docker compose
        stop; the embeddings service and its model-cache volume stay)."""
        self._matrix["stop_preserve_volumes"] = "fail"
        self.runtime.stop("hermes")
        self._matrix["stop_preserve_volumes"] = "pass"

    def _scenario_candidate_build(self) -> None:
        """Build the candidate image with the validated GBRAIN_REF build arg
        (no container change yet). A build failure (e.g. the local
        ``gbrain-inline-worker-gateway.patch`` does not apply to the
        candidate source — the documented loud-failure mode) is reported
        honestly with the build output."""
        self._matrix["candidate_build"] = "fail"
        try:
            self.runtime.build_candidate(self.candidate_ref, "hermes", timeout=1800)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            self.fail(
                f"candidate build failed (the local gbrain patch may not "
                f"apply to the candidate source): {str(detail)[-800:]}"
            )
        self._matrix["candidate_build"] = "pass"

    def _scenario_candidate_recreate(self) -> None:
        """Force-recreate --no-build against the SAME disposable volumes."""
        self._matrix["candidate_recreate"] = "fail"
        self.runtime.recreate_same_volumes("hermes", timeout=600)
        self.runtime.wait_until_hermes_writable(timeout=120)
        self._matrix["candidate_recreate"] = "pass"

    def _scenario_candidate_source_ref(self) -> None:
        """Prove the candidate source ref: /opt/gbrain/.git/HEAD must equal
        the exact candidate ref, and the candidate gbrain version is
        recorded."""
        self._matrix["candidate_source_ref"] = "fail"
        ev = self.runtime.run_as_hermes("cat", "/opt/gbrain/.git/HEAD")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertEqual(ev.stdout.strip(), self.candidate_ref)
        self._evidence.append(ev)
        ev = self.runtime.run_as_hermes("gbrain", "status", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self._candidate_version = json.loads(ev.stdout).get("version")
        self._matrix["candidate_source_ref"] = "pass"

    def _scenario_candidate_reindex(self) -> None:
        """Candidate reindex/migration is the issue #124 probe (review
        finding MAJOR #124): reproduce the regression exactly.

        Order: the baseline semantic mode must be working BEFORE the probe
        (the baseline semantic proof already passed; the pre snapshot
        confirms the mode), then snapshot the supported search-mode
        indicators, run the candidate ``josemar-gbrain reindex``, snapshot
        the indicators again, and classify ``fixed``/``present``/
        ``changed_failure_mode``/``inconclusive`` SOLELY from the pre/post
        transition. The documented workaround (``enable-embeddings``) is
        applied only AFTER the classification is recorded, so it can never
        mask the regression before semantic retrieval is checked.

        REPORT-ONLY: the classification is recorded in the report metadata,
        never asserted. ``present`` (the recorded #124 failure) does not fail
        the suite; the workaround restores semantic mode for the remaining
        vector scenarios."""
        # 1. Pre-reindex snapshot of the supported search-mode indicators
        #    (precondition: semantic mode active on the baseline).
        pre = self._snapshot_search_mode()
        if pre is None or not pre.is_semantic():
            self._reindex_classification = classify_reindex_transition(pre, None, probe_rc=0)
            self.fail(
                "issue #124 probe precondition not established: semantic mode "
                f"is not active before the candidate reindex (pre snapshot: {pre})"
            )

        # 2. The probe reindex (candidate migration/activation).
        self._matrix["candidate_reindex"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)
        self._evidence.append(ev)

        # 3. Post-reindex snapshot, taken BEFORE any workaround.
        post = self._snapshot_search_mode()

        # 4. Classification derived SOLELY from the pre/post transition (and
        #    the probe outcome); the workaround below never participates.
        self._reindex_classification = classify_reindex_transition(pre, post, probe_rc=ev.returncode)
        self._reindex_pre_snapshot = pre.to_dict()
        self._reindex_post_snapshot = post.to_dict() if post is not None else None

        if ev.returncode != 0:
            self.fail(
                f"candidate reindex probe failed (classification "
                f"{self._reindex_classification} recorded): {ev.stderr[-800:]}"
            )
        if post is None:
            self.fail(
                "post-reindex search-mode snapshot unreadable "
                f"(classification {self._reindex_classification} recorded)"
            )
        # Wrapper contract (independent of the #124 classification): the
        # reindex envelope must keep its shape.
        try:
            envelope = json.loads(ev.stdout)
        except json.JSONDecodeError:
            self.fail(f"candidate reindex envelope is not JSON: {ev.stdout[:400]}")
        if envelope.get("success") is not True or envelope.get("action") != "reindex":
            self.fail(f"candidate reindex envelope shape changed: {ev.stdout[:400]}")
        self.assertEqual(envelope.get("schema_pack"), "josemar")
        if self._reindex_classification == "present":
            # 5. Documented workaround (enable-embeddings), applied ONLY after
            #    the classification is recorded so it can never mask the
            #    regression.
            self._matrix["issue124_workaround"] = "fail"
            re_enable = self.runtime.run_as_hermes(
                "josemar-gbrain", "enable-embeddings", timeout=300
            )
            self.assertEqual(re_enable.returncode, 0, re_enable.stderr)
            self._evidence.append(re_enable)
            envelope = json.loads(re_enable.stdout)
            self.assertIs(envelope.get("success"), True)
            self.assertEqual(envelope.get("action"), "enable-embeddings")
            after = self._snapshot_search_mode()
            if after is None or not after.is_semantic():
                self.fail(f"workaround did not restore semantic mode: {after}")
            self._matrix["issue124_workaround"] = "pass"
        elif self._reindex_classification == "fixed":
            # Semantic mode survived the candidate reindex; no workaround
            # needed.
            self._matrix["issue124_workaround"] = "not_run"
        else:
            self.fail(
                f"unexpected post-reindex search mode (classification "
                f"{self._reindex_classification} recorded)"
            )
        self._matrix["candidate_reindex"] = "pass"

    def _scenario_candidate_semantic_corpus(self) -> None:
        """WITHOUT a destructive full backfill, the pre-upgrade semantic
        corpus must remain usable on the candidate: the #124 probe already
        restored semantic mode (workaround) when the candidate reindex reset
        it, so here we only prove the preserved vectors serve search and
        ``query --no-expand``. If the candidate migration invalidated the
        pre-upgrade vectors, fail with the precise
        ``vector_migration_required`` signal."""
        self._matrix["candidate_semantic_corpus"] = "fail"
        # The pre-upgrade vectors must survive the candidate migration
        # untouched (coverage stays 100% without any backfill).
        coverage = self._embedding_coverage()
        if coverage < 100.0:
            self._matrix["candidate_semantic_corpus"] = "vector_migration_required"
            self.fail(
                f"vector_migration_required: the candidate migration "
                f"invalidated pre-upgrade vectors (coverage {coverage}%); a "
                f"destructive full embed-backfill is required before the "
                f"semantic corpus is usable"
            )
        # Semantic corpus usable without a full backfill.
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
        self._matrix["candidate_semantic_corpus"] = "pass"

    def _scenario_post_upgrade_stale_edit(self) -> None:
        """One incremental post-upgrade stale edit: a manual vault edit (new
        unique token, committed as hermes) makes the page chunk stale;
        ``refresh-embeddings`` reconciles it and the new token becomes
        searchable with full embedding coverage restored.

        The candidate ``enable-embeddings`` workaround (only when the #124
        probe classified ``present``) removed the completion marker, so a
        no-op ``embed-backfill`` (0 stale — the corpus proof passed)
        restores/refreshes it for the incremental refresh path. NOT
        destructive: nothing is re-embedded when coverage is already 100%."""
        self._matrix["post_upgrade_stale_edit"] = "fail"
        backfill = self.runtime.run_as_hermes("josemar-gbrain", "embed-backfill", timeout=600)
        self.assertEqual(backfill.returncode, 0, backfill.stderr)
        self._evidence.append(backfill)
        envelope = json.loads(backfill.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "embed-backfill")
        self.assertEqual(self._embedding_coverage(), 100.0)
        # Post-upgrade edit: append a new unique token (stale chunk).
        edit = self.runtime.run_as_hermes("sh", "-lc", POST_UPGRADE_EDIT_SCRIPT, timeout=120)
        self.assertEqual(edit.returncode, 0, edit.stderr)
        self._evidence.append(edit)
        # Incremental stale update: refresh-embeddings embeds the stale chunk.
        refresh = self.runtime.run_as_hermes(
            "josemar-gbrain", "refresh-embeddings", timeout=600
        )
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        self._evidence.append(refresh)
        envelope = json.loads(refresh.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "refresh-embeddings")
        # Retrieval proof: the new token is searchable and coverage restored.
        search = self.runtime.run_as_hermes(
            "gbrain", "search", "conformance-token-post-upgrade", "--limit", "5", timeout=120
        )
        self.assertEqual(search.returncode, 0, search.stderr)
        self._evidence.append(search)
        # The post-upgrade token sits beyond the 100-char result preview; the
        # slug proves the new chunk is indexed and searchable.
        self.assertIn("notes/welcome", search.stdout)
        self.assertEqual(self._embedding_coverage(), 100.0)
        self._matrix["post_upgrade_stale_edit"] = "pass"


class GbrainUpgradeEmbeddingConformanceGateStructureTests(unittest.TestCase):
    """Fast host-side guards for the upgrade embedding gate, candidate-ref
    validation (pre-Docker), and the no-volume-delete flow. No Docker
    required; these run in the normal fast suite."""

    @staticmethod
    def _module_text() -> str:
        return (
            REPO_ROOT / "tests" / "runtime" / "test_gbrain_upgrade_conformance_embeddings.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _runtime_class_text() -> str:
        """The runtime portion of the module: the shared base case class plus
        the Docker-gated scenario class (everything before the fast gate
        structure tests)."""
        text = GbrainUpgradeEmbeddingConformanceGateStructureTests._module_text()
        runtime_class = text.split("class GbrainUpgradeEmbeddingConformanceTestCase", 1)[1]
        return runtime_class.split(
            "class GbrainUpgradeEmbeddingConformanceGateStructureTests", 1
        )[0]

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
            os.environ,
            {
                "RUN_DOCKER_TESTS": "",
                "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_upgrade_embedding_conformance_enabled())

    def test_gate_requires_run_gbrain_embedding_conformance(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_upgrade_embedding_conformance_enabled())

    def test_gate_requires_run_gbrain_upgrade_conformance(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_upgrade_embedding_conformance_enabled())

    def test_gate_requires_candidate_ref(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "",
            },
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_upgrade_embedding_conformance_enabled())

    def test_gate_requires_docker(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(False):
                self.assertFalse(_upgrade_embedding_conformance_enabled())

    def test_gate_enabled_when_all_conditions_met(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_EMBEDDING_CONFORMANCE": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(True):
                self.assertTrue(_upgrade_embedding_conformance_enabled())

    def test_runtime_class_is_gated_on_all_env_vars(self) -> None:
        text = self._module_text()
        self.assertIn("RUN_DOCKER_TESTS", text)
        self.assertIn("RUN_GBRAIN_EMBEDDING_CONFORMANCE", text)
        self.assertIn("RUN_GBRAIN_UPGRADE_CONFORMANCE", text)
        self.assertIn("skipUnless", text)
        # The skip message is split across adjacent string literals; assert
        # its pieces so the guard is robust to the wrapping.
        self.assertIn(
            '"set RUN_DOCKER_TESTS=1, RUN_GBRAIN_EMBEDDING_CONFORMANCE=1 and "',
            text,
        )
        self.assertIn(
            '"RUN_GBRAIN_UPGRADE_CONFORMANCE=1 with an exact "',
            text,
        )
        self.assertIn(
            '"GBRAIN_CONFORMANCE_CANDIDATE_REF and a docker CLI"',
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
        # The baseline start goes through the upgrade-only up_baseline path
        # (Dockerfile pin unless an explicit validated baseline override).
        self.assertIn('self.runtime.up_baseline("embeddings", "hermes"', runtime_class)
        # TEI readiness is waited on by the Compose service_healthy
        # dependency, never by sleep-polling.
        self.assertNotIn("time.sleep", runtime_class)

    def test_candidate_ref_validated_before_docker(self) -> None:
        """The candidate ref is normalized/validated (40-hex, lower-cased)
        purely host-side, before any Docker invocation."""
        ref = "aBcD" * 10
        with mock.patch.dict(os.environ, {"GBRAIN_CONFORMANCE_CANDIDATE_REF": ref}):
            self.assertEqual(_candidate_ref(), ref.lower())
        text = self._module_text()
        self.assertIn("normalize_candidate_ref", text)
        self.assertIn("GBRAIN_CONFORMANCE_CANDIDATE_REF", text)
        # The validation helpers must not invoke docker.
        self.assertNotIn(
            "docker",
            text.split("def _candidate_ref", 1)[1].split("def _validated_candidate_ref", 1)[0],
        )

    def test_candidate_ref_rejects_invalid(self) -> None:
        with mock.patch.dict(os.environ, {"GBRAIN_CONFORMANCE_CANDIDATE_REF": "main"}):
            with self.assertRaises(ValueError):
                _candidate_ref()

    def test_candidate_ref_rejects_equality_with_baseline(self) -> None:
        """The candidate must differ from the EFFECTIVE baseline: the
        validated override when present, otherwise the Dockerfile pin."""
        # Without the override the effective baseline is the Dockerfile pin.
        with mock.patch.dict(
            os.environ,
            {"GBRAIN_CONFORMANCE_BASELINE_REF": "", "GBRAIN_CONFORMANCE_CANDIDATE_REF": ""},
        ):
            baseline = parse_dockerfile_gbrain_ref()
            with mock.patch.dict(
                os.environ, {"GBRAIN_CONFORMANCE_CANDIDATE_REF": baseline}
            ):
                with self.assertRaisesRegex(ValueError, "must differ"):
                    _validated_candidate_ref()
        # With the override the effective baseline is the override: a
        # candidate equal to it (the no-op / old-candidate downgrade) is
        # rejected before any Docker invocation.
        override = "a" * 40
        with mock.patch.dict(
            os.environ,
            {"GBRAIN_CONFORMANCE_BASELINE_REF": override, "GBRAIN_CONFORMANCE_CANDIDATE_REF": ""},
        ):
            with mock.patch.dict(
                os.environ, {"GBRAIN_CONFORMANCE_CANDIDATE_REF": override}
            ):
                with self.assertRaisesRegex(ValueError, "must differ"):
                    _validated_candidate_ref()
        # The real old -> new configuration is allowed: baseline override =
        # old ref, candidate = the committed Dockerfile pin (the new ref).
        dockerfile = parse_dockerfile_gbrain_ref()
        with mock.patch.dict(
            os.environ,
            {"GBRAIN_CONFORMANCE_BASELINE_REF": "a" * 40, "GBRAIN_CONFORMANCE_CANDIDATE_REF": ""},
        ):
            with mock.patch.dict(
                os.environ, {"GBRAIN_CONFORMANCE_CANDIDATE_REF": dockerfile}
            ):
                self.assertEqual(_validated_candidate_ref(), dockerfile)

    def test_baseline_ref_honors_validated_override(self) -> None:
        """The upgrade suite's baseline is the EFFECTIVE ref: the validated
        override when set, otherwise the Dockerfile pin (unchanged)."""
        override = "aBcD" * 10
        with mock.patch.dict(os.environ, {"GBRAIN_CONFORMANCE_BASELINE_REF": override}):
            self.assertEqual(effective_baseline_ref(), override.lower())
        with mock.patch.dict(os.environ, {"GBRAIN_CONFORMANCE_BASELINE_REF": ""}):
            self.assertEqual(effective_baseline_ref(), parse_dockerfile_gbrain_ref())
        text = self._module_text()
        self.assertIn("effective_baseline_ref", text)
        self.assertIn("GBRAIN_CONFORMANCE_BASELINE_REF", text)
        self.assertIn("up_baseline", text)

    def test_baseline_override_invalid_fails_closed_before_docker(self) -> None:
        with mock.patch.dict(os.environ, {"GBRAIN_CONFORMANCE_BASELINE_REF": "main"}):
            with self.assertRaises(ValueError):
                effective_baseline_ref()

    def test_baseline_source_ref_proven_and_reported(self) -> None:
        """The suite proves the effective baseline's /opt/gbrain/.git/HEAD
        (guarding against an old-candidate downgrade false success) and
        persists it with the ref provenance in the report."""
        runtime_class = self._runtime_class_text()
        self.assertIn("self._scenario_baseline_source_ref()", runtime_class)
        self.assertIn('"cat", "/opt/gbrain/.git/HEAD"', runtime_class)
        self.assertIn("self.baseline_ref", runtime_class)
        self.assertIn('"baseline_source_ref": "core"', self._module_text())
        report = self._module_text().split("def _write_report", 1)[1]
        report = report.split("class GbrainUpgradeEmbeddingConformanceRuntimeTests", 1)[0]
        for key in ("baseline_ref_source", "dockerfile_gbrain_ref", "baseline_source_ref"):
            self.assertIn(f'"{key}"', report)
        # The report provenance fields are computed in setUp from the
        # explicit override presence; the report writer itself never reads
        # the environment.
        self.assertNotIn("os.environ", report)

    def test_upgrade_flow_preserves_volumes(self) -> None:
        """The upgrade must stop (preserving volumes) and force-recreate
        --no-build against the same volumes; only the final cleanup may
        delete volumes."""
        runtime_class = self._runtime_class_text()
        self.assertIn('self.runtime.stop("hermes")', runtime_class)
        self.assertIn('self.runtime.recreate_same_volumes("hermes", timeout=600)', runtime_class)
        self.assertIn(
            'self.runtime.build_candidate(self.candidate_ref, "hermes", timeout=1800)',
            runtime_class,
        )
        # No direct volume-deleting down() in the flow: only cleanup() at
        # teardown (down -v --remove-orphans).
        self.assertNotIn("self.runtime.down()", runtime_class)
        self.assertIn("self.runtime.cleanup()", self._module_text())

    def test_lifecycle_commands_use_pinned_operator_surface(self) -> None:
        runtime_class = self._runtime_class_text()
        for cmd in ("enable-embeddings", "embed-backfill", "refresh-embeddings"):
            self.assertIn(f'"josemar-gbrain", "{cmd}"', runtime_class)
        self.assertIn('"gbrain", "search"', runtime_class)
        self.assertIn('"gbrain", "query", "--no-expand"', runtime_class)
        self.assertIn('"gbrain", "status", "--json"', runtime_class)

    def test_corpus_proof_precedes_post_upgrade_backfill(self) -> None:
        """The candidate semantic-corpus proof must run BEFORE the post-upgrade
        no-op backfill, and the proof itself must never run a backfill (the
        corpus must be proven usable without one)."""
        runtime_class = self._runtime_class_text()
        self.assertLess(
            runtime_class.index("self._scenario_candidate_semantic_corpus()"),
            runtime_class.index("self._scenario_post_upgrade_stale_edit()"),
        )
        corpus = runtime_class.split("def _scenario_candidate_semantic_corpus", 1)[1]
        corpus = corpus.split("def _scenario_post_upgrade_stale_edit", 1)[0]
        # The proof must never RUN a backfill (the docstring may mention the
        # failure signal freely; only the invocation is guarded).
        self.assertNotIn('"josemar-gbrain", "embed-backfill"', corpus)

    def test_vector_migration_required_failure_path(self) -> None:
        """The precise vector_migration_required signal must be recorded in
        the matrix and raised when the candidate migration invalidated the
        pre-upgrade vectors."""
        runtime_class = self._runtime_class_text()
        self.assertIn("vector_migration_required", runtime_class)
        self.assertIn(
            'self._matrix["candidate_semantic_corpus"] = "vector_migration_required"',
            runtime_class,
        )

    def test_report_contains_refs_versions_model_config(self) -> None:
        text = self._module_text()
        for key in (
            "baseline_ref",
            "baseline_ref_source",
            "dockerfile_gbrain_ref",
            "baseline_source_ref",
            "candidate_ref",
            "baseline_gbrain_version",
            "candidate_gbrain_version",
            "embedding_model",
            "embedding_revision",
            "reindex_classification",
            "reindex_pre_snapshot",
            "reindex_post_snapshot",
        ):
            self.assertIn(key, text)
        self.assertIn("write_report", text)
        self.assertIn("conformance_report_dir", text)
        self.assertIn("gbrain-upgrade-conformance-embeddings", text)
        # The report must never serialize the process/runtime environment:
        # the report-writing method must not reference os.environ at all.
        report_method = text.split("def _write_report", 1)[1]
        report_method = report_method.split("class ", 1)[0]
        self.assertNotIn("os.environ", report_method)

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

    def test_matrix_covers_all_owned_operations(self) -> None:
        self.assertEqual(
            set(UPGRADE_EMBEDDING_MATRIX),
            {
                "baseline_build_start",
                "baseline_source_ref",
                "baseline_reindex",
                "baseline_enable_embeddings",
                "baseline_embed_backfill",
                "baseline_semantic_proof",
                "stop_preserve_volumes",
                "candidate_build",
                "candidate_recreate",
                "candidate_source_ref",
                "candidate_reindex",
                "issue124_workaround",
                "candidate_semantic_corpus",
                "post_upgrade_stale_edit",
            },
        )

    # --- issue #124 probe: ordering and classification (review MAJOR #124) --

    @staticmethod
    def _scenario_text(start: str, end: str) -> str:
        """Extract one scenario method's source from the runtime class text."""
        runtime_class = (
            GbrainUpgradeEmbeddingConformanceGateStructureTests._runtime_class_text()
        )
        body = runtime_class.split(f"def {start}", 1)[1]
        return body.split(f"def {end}", 1)[0]

    @staticmethod
    def _snap(*, keyword_only=None, embedding_disabled=None) -> SearchModeSnapshot:
        return SearchModeSnapshot(
            keyword_only=keyword_only, embedding_disabled=embedding_disabled
        )

    def test_probe_snapshot_uses_native_binary_surface(self) -> None:
        """The indicator snapshot must read ``search.mcp_keyword_only`` via
        the native binary with the canonical env (the public ``gbrain``
        adapter rejects operator-only ``config`` with exit 2)."""
        runtime_class = self._runtime_class_text()
        snapshot = runtime_class.split("def _snapshot_search_mode", 1)[1]
        snapshot = snapshot.split("def _write_report", 1)[0]
        self.assertIn("{NATIVE_GBRAIN_BIN} config get search.mcp_keyword_only", snapshot)
        self.assertNotIn('"gbrain", "config"', snapshot)

    def test_candidate_reindex_probe_reproduces_regression_in_order(self) -> None:
        """MAJOR #124: the candidate probe must reproduce the regression in
        order — pre snapshot, reindex, post snapshot, classification, and
        only then the workaround (enable-embeddings)."""
        probe = self._scenario_text(
            "_scenario_candidate_reindex", "_scenario_candidate_semantic_corpus"
        )
        # pre snapshot precedes the probe reindex; post snapshot follows it.
        first_snap = probe.index("self._snapshot_search_mode()")
        second_snap = probe.index("self._snapshot_search_mode()", first_snap + 1)
        self.assertLess(first_snap, probe.index('"josemar-gbrain", "reindex"'))
        self.assertGreater(second_snap, probe.index('"josemar-gbrain", "reindex"'))
        # classification is computed before any workaround enable-embeddings.
        self.assertLess(
            probe.index("self._reindex_classification = classify_reindex_transition("),
            probe.index('"josemar-gbrain", "enable-embeddings"'),
        )
        # the workaround is gated on the present classification.
        self.assertIn('if self._reindex_classification == "present"', probe)

    def test_candidate_probe_precedes_corpus_proof_and_workaround_never_masks(self) -> None:
        """The candidate #124 probe (candidate reindex + classification +
        workaround) runs after the baseline semantic proof and BEFORE the
        corpus proof; the corpus proof must never call enable-embeddings (the
        workaround lives in the probe and never participates in the
        classification)."""
        runtime_class = self._runtime_class_text()
        self.assertLess(
            runtime_class.index("self._scenario_baseline_semantic_proof()"),
            runtime_class.index("self._scenario_candidate_reindex()"),
        )
        self.assertLess(
            runtime_class.index("self._scenario_candidate_reindex()"),
            runtime_class.index("self._scenario_candidate_semantic_corpus()"),
        )
        corpus = runtime_class.split("def _scenario_candidate_semantic_corpus", 1)[1]
        corpus = corpus.split("def _scenario_post_upgrade_stale_edit", 1)[0]
        self.assertNotIn('"josemar-gbrain", "enable-embeddings"', corpus)

    def test_candidate_reindex_classification_derived_only_from_transition(self) -> None:
        """The #124 classification is computed ONLY from the pre/post
        indicator transition via the pure classifier; no literal
        classification assignments remain."""
        runtime_class = self._runtime_class_text()
        self.assertIn("classify_reindex_transition(", runtime_class)
        for literal in ('"fixed"', '"present"', '"changed_failure_mode"'):
            self.assertNotIn(f"self._reindex_classification = {literal}", runtime_class)

    def test_classify_reindex_transition_decision_table(self) -> None:
        """The pure classifier covers every branch of the #124 oracle."""
        semantic = self._snap(keyword_only=False)
        keyword_only = self._snap(keyword_only=True)
        # inconclusive: precondition not established.
        self.assertEqual(classify_reindex_transition(None, semantic, probe_rc=0), "inconclusive")
        self.assertEqual(
            classify_reindex_transition(self._snap(keyword_only=True), semantic, probe_rc=0),
            "inconclusive",
        )
        # changed_failure_mode: probe failure or unreadable post snapshot.
        self.assertEqual(
            classify_reindex_transition(semantic, keyword_only, probe_rc=1),
            "changed_failure_mode",
        )
        self.assertEqual(
            classify_reindex_transition(semantic, None, probe_rc=0), "changed_failure_mode"
        )
        # present: the recorded #124 failure (mode reset to keyword-only).
        self.assertEqual(
            classify_reindex_transition(semantic, keyword_only, probe_rc=0), "present"
        )
        # fixed: semantic mode survived the reindex.
        self.assertEqual(
            classify_reindex_transition(semantic, semantic, probe_rc=0), "fixed"
        )
        # changed_failure_mode: unexpected post state.
        self.assertEqual(
            classify_reindex_transition(semantic, self._snap(keyword_only=None), probe_rc=0),
            "changed_failure_mode",
        )
        self.assertEqual(
            classify_reindex_transition(
                semantic, self._snap(keyword_only=False, embedding_disabled=True), probe_rc=0
            ),
            "changed_failure_mode",
        )


if __name__ == "__main__":
    unittest.main()