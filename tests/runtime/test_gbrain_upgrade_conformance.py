"""Opt-in baseline-to-candidate gbrain upgrade conformance (issue #127 W3).

Runs the SAME disposable Compose project/volumes through a full upgrade:

  - baseline build/start (Dockerfile default ``GBRAIN_REF``), reindex, and
    representative logical state created through the supported public APIs
    (capture/put/link)
  - ``docker compose stop`` preserving volumes
  - candidate image build with the validated ``GBRAIN_REF`` build arg
  - force-recreate ``--no-build`` against the SAME volumes
  - candidate source-ref proof (``/opt/gbrain/.git/HEAD`` equals the exact
    candidate ref) and candidate gbrain version
  - candidate reindex/migration success envelope
  - logical-state manifest survival (page content + manual link edge)
  - core post-upgrade writes and reindex idempotency
  - issue #125 dedicated git-move probe with a fixed/present/
    changed_failure_mode/inconclusive classification that never hard-fails
    just because the issue is open

The gate is strict: ``RUN_DOCKER_TESTS=1`` AND ``RUN_GBRAIN_UPGRADE_CONFORMANCE=1``
AND an exact ``GBRAIN_CONFORMANCE_CANDIDATE_REF`` (40-hex, prevalidated BEFORE
any Docker invocation, and rejected when equal to the canonical baseline
``GBRAIN_REF``). Fast host-side gate/ref/pre-Docker/no-volume-delete tests in
this module always run and need no Docker.

The JSON report (``dump_folder/gbrain-conformance/gbrain-upgrade-conformance.json``)
carries the baseline/candidate refs, the action list, the logical result, the
#125 classification, and the operation result matrix — command/result metadata
only, never environment dumps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from .gbrain_conformance_support import (
    CONFORMANCE_EMPTY_ENV_KEYS,
    CommandEvidence,
    GbrainConformanceRuntime,
    conformance_report_dir,
    normalize_candidate_ref,
    parse_dockerfile_gbrain_ref,
    write_report,
)
from .helpers import REPO_ROOT, docker_available


# Deterministic representative logical state created through supported APIs
# before the upgrade and asserted to survive it.
UPGRADE_STATE_SLUG = "inbox/upgrade-state"
UPGRADE_STATE_V1 = "upgrade state v1 conformance-upgrade-state-v1"
UPGRADE_STATE_V2 = (
    "# Upgrade State\n"
    "\n"
    "Upgrade state v2 conformance-upgrade-state-v2.\n"
)
UPGRADE_LINK_CONTEXT = "conformance-upgrade-link-ctx"

# Issue #125 git-move probe facts (classification, never a hard failure).
GIT_MOVE_SLUG = "inbox/git-move-probe"
GIT_MOVE_NEW_SLUG = "notes/git-move-probe"
GIT_MOVE_TOKEN = "conformance-git-move-token"

# Upgrade conformance matrix: every operation this suite owns, with its
# classification. The report persists an explicit result for each.
UPGRADE_MATRIX = {
    "baseline_build_start": "core",
    "baseline_reindex": "operator_only",
    "baseline_state_create": "core",
    "stop_preserve_volumes": "core",
    "candidate_build": "core",
    "candidate_recreate": "core",
    "candidate_source_ref": "core",
    "candidate_reindex_migration": "operator_only",
    "state_manifest_survives": "core",
    "post_upgrade_write": "core",
    "reindex_idempotency": "operator_only",
    "issue_125_git_move": "probe",
}


def _candidate_ref() -> str:
    """The exact candidate ``GBRAIN_REF`` from the environment, validated
    (40-hex, lower-cased) BEFORE any Docker invocation."""
    return normalize_candidate_ref(os.getenv("GBRAIN_CONFORMANCE_CANDIDATE_REF", ""))


def _validated_candidate_ref() -> str:
    """Validate the candidate ref and REJECT equality with the canonical
    baseline ``GBRAIN_REF``. Runs before any Docker invocation."""
    candidate = _candidate_ref()
    baseline = parse_dockerfile_gbrain_ref()
    if candidate == baseline:
        raise ValueError(
            "GBRAIN_CONFORMANCE_CANDIDATE_REF must differ from the canonical "
            f"baseline GBRAIN_REF ({baseline})"
        )
    return candidate


def _upgrade_conformance_enabled() -> bool:
    """Strict gate: RUN_DOCKER_TESTS=1 AND RUN_GBRAIN_UPGRADE_CONFORMANCE=1
    AND an exact GBRAIN_CONFORMANCE_CANDIDATE_REF is provided AND a docker CLI
    is available."""
    return (
        os.getenv("RUN_DOCKER_TESTS") == "1"
        and os.getenv("RUN_GBRAIN_UPGRADE_CONFORMANCE") == "1"
        and bool(os.getenv("GBRAIN_CONFORMANCE_CANDIDATE_REF"))
        and docker_available()
    )


@unittest.skipUnless(
    _upgrade_conformance_enabled(),
    "set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_UPGRADE_CONFORMANCE=1 with an exact "
    "GBRAIN_CONFORMANCE_CANDIDATE_REF and a docker CLI",
)
class GbrainUpgradeConformanceTestCase(unittest.TestCase):
    """Shared base setup for the upgrade conformance runtime suite.

    Validates the candidate ref BEFORE any Docker invocation, then builds and
    starts the baseline Hermes-only runtime against a disposable Compose
    project (pre-start source-state seeding, hermes-writable wait, isolation
    safety checks, synthetic vault init). Final teardown is unconditional
    ``down -v --remove-orphans``.
    """

    def setUp(self) -> None:
        # Candidate ref validated BEFORE the first Docker invocation.
        self.candidate_ref = _validated_candidate_ref()
        self.baseline_ref = parse_dockerfile_gbrain_ref()
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {op: "not_run" for op in UPGRADE_MATRIX}
        self._baseline_version: str | None = None
        self._candidate_version: str | None = None
        self._logical_result: str = "not_run"
        self._issue_125_classification: str = "inconclusive"
        self._report_path: Path | None = None

        self.runtime = GbrainConformanceRuntime()
        # Pre-start source state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # Baseline build/start (Dockerfile default GBRAIN_REF).
        self.runtime.up("hermes", timeout=900)
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

    def _write_report(self) -> None:
        """Persist the upgrade conformance report: baseline/candidate refs,
        actions, logical result, #125 classification, and the operation result
        matrix. Command/result metadata only — never environment dumps."""
        metadata = {
            "baseline_ref": self.baseline_ref,
            "candidate_ref": self.candidate_ref,
            "baseline_gbrain_version": self._baseline_version,
            "candidate_gbrain_version": self._candidate_version,
            "actions": list(UPGRADE_MATRIX),
            "logical_result": self._logical_result,
            "issue_125_git_move": self._issue_125_classification,
            "matrix": self._matrix,
        }
        self._report_path = write_report(
            conformance_report_dir(),
            "gbrain-upgrade-conformance",
            self._evidence,
            metadata=metadata,
        )


class GbrainUpgradeConformanceRuntimeTests(GbrainUpgradeConformanceTestCase):
    """W3 baseline-to-candidate upgrade scenarios (Docker-gated via the base
    class)."""

    def test_baseline_to_candidate_upgrade_conformance(self) -> None:
        try:
            self._scenario_baseline_reindex()
            self._scenario_baseline_state()
            self._scenario_stop_preserve_volumes()
            self._scenario_candidate_build()
            self._scenario_candidate_recreate()
            self._scenario_candidate_source_ref()
            self._scenario_candidate_reindex()
            self._scenario_state_manifest_survives()
            self._scenario_post_upgrade_write()
            self._scenario_reindex_idempotency()
            self._scenario_issue_125_probe()
        finally:
            self._write_report()

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

    def _scenario_baseline_state(self) -> None:
        """Create representative logical state through the supported public
        APIs: capture v1, put v2, and a manual link to projects/atlas."""
        self._matrix["baseline_state_create"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "capture", UPGRADE_STATE_V1,
            "--slug", UPGRADE_STATE_SLUG, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        ev = self.runtime.run_as_hermes(
            "gbrain", "put", UPGRADE_STATE_SLUG, "--content", UPGRADE_STATE_V2,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self.assertIs(json.loads(ev.stdout).get("write_through", {}).get("written"), True)
        ev = self.runtime.run_as_hermes(
            "gbrain", "link", UPGRADE_STATE_SLUG, "projects/atlas",
            "--link-type", "related",
            "--context", UPGRADE_LINK_CONTEXT,
            "--link-source", "manual",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self.assertEqual(json.loads(ev.stdout).get("status"), "ok")
        self._matrix["baseline_state_create"] = "pass"

    def _scenario_stop_preserve_volumes(self) -> None:
        """Stop Hermes PRESERVING volumes (docker compose stop)."""
        self._matrix["stop_preserve_volumes"] = "fail"
        self.runtime.stop("hermes")
        self._matrix["stop_preserve_volumes"] = "pass"

    def _scenario_candidate_build(self) -> None:
        """Build the candidate image with the validated GBRAIN_REF build arg
        (no container change yet)."""
        self._matrix["candidate_build"] = "fail"
        self.runtime.build_candidate(self.candidate_ref, "hermes", timeout=1800)
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
        """Candidate reindex/migration returns the success envelope."""
        self._matrix["candidate_reindex_migration"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "reindex")
        self.assertEqual(envelope.get("schema_pack"), "josemar")
        self._matrix["candidate_reindex_migration"] = "pass"

    def _scenario_state_manifest_survives(self) -> None:
        """The logical-state manifest must survive the upgrade: the page
        content (v2) and the manual link edge still resolve."""
        self._matrix["state_manifest_survives"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "get", UPGRADE_STATE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("conformance-upgrade-state-v2", ev.stdout)
        self._evidence.append(ev)
        ev = self.runtime.run_as_hermes("gbrain", "backlinks", "projects/atlas")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        edges = json.loads(ev.stdout)
        manual = [
            e for e in edges
            if e.get("from_slug") == UPGRADE_STATE_SLUG
            and e.get("link_source") == "manual"
        ]
        self.assertEqual(len(manual), 1, ev.stdout)
        self.assertEqual(manual[0].get("context"), UPGRADE_LINK_CONTEXT)
        self._matrix["state_manifest_survives"] = "pass"
        self._logical_result = "survived"

    def _scenario_post_upgrade_write(self) -> None:
        """Core post-upgrade writes work through the public API."""
        self._matrix["post_upgrade_write"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "capture", "post-upgrade write conformance-post-upgrade-token",
            "--slug", "inbox/post-upgrade", "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        ev = self.runtime.run_as_hermes("gbrain", "get", "inbox/post-upgrade")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("conformance-post-upgrade-token", ev.stdout)
        self._evidence.append(ev)
        self._matrix["post_upgrade_write"] = "pass"

    def _scenario_reindex_idempotency(self) -> None:
        """A second candidate reindex is idempotent (success envelope)."""
        self._matrix["reindex_idempotency"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "reindex")
        self._matrix["reindex_idempotency"] = "pass"

    def _scenario_issue_125_probe(self) -> None:
        """Issue #125 dedicated git-move probe. Classifies
        fixed/present/changed_failure_mode/inconclusive and NEVER hard-fails
        just because the issue is open."""
        self._matrix["issue_125_git_move"] = "fail"
        classification = self._probe_issue_125_git_move()
        self._issue_125_classification = classification
        self._matrix["issue_125_git_move"] = classification

    def _probe_issue_125_git_move(self) -> str:
        """Run the git-move probe on the candidate and classify issue #125.

        Scenario: create a page via the public API, commit it, ``git mv`` it
        to a new path, commit, refresh, then probe both slugs.

        Classification:
          - ``fixed``: the new slug resolves with the token and the old slug
            does not (the move is fully reconciled).
          - ``present``: the old slug still resolves (stale index entry) —
            the issue's failure mode is present.
          - ``changed_failure_mode``: neither slug resolves, or the failure
            mode changed (e.g. the page vanished entirely).
          - ``inconclusive``: the probe itself could not run (infrastructure
            failure), so no classification is possible.
        """
        try:
            ev = self.runtime.run_as_hermes(
                "gbrain", "capture", "git-move probe token " + GIT_MOVE_TOKEN,
                "--slug", GIT_MOVE_SLUG, "--json", check=False,
            )
            if ev.returncode != 0:
                return "inconclusive"
            self._evidence.append(ev)
            ev = self.runtime.run_as_hermes(
                "sh", "-lc",
                "set -eu; cd /opt/data/obsidian; git add " + GIT_MOVE_SLUG
                + ".md; git commit -qm 'git-move probe capture'",
                check=False,
            )
            if ev.returncode != 0:
                return "inconclusive"
            self._evidence.append(ev)
            ev = self.runtime.run_as_hermes(
                "sh", "-lc",
                "set -eu; cd /opt/data/obsidian; git mv " + GIT_MOVE_SLUG
                + ".md " + GIT_MOVE_NEW_SLUG + ".md; "
                "git commit -qm 'git-move probe move'",
                check=False,
            )
            if ev.returncode != 0:
                return "inconclusive"
            self._evidence.append(ev)
            ev = self.runtime.run_as_hermes(
                "josemar-gbrain", "refresh", timeout=300, check=False,
            )
            if ev.returncode != 0:
                return "inconclusive"
            self._evidence.append(ev)
            new_ev = self.runtime.run_as_hermes(
                "gbrain", "get", GIT_MOVE_NEW_SLUG, check=False,
            )
            old_ev = self.runtime.run_as_hermes(
                "gbrain", "get", GIT_MOVE_SLUG, check=False,
            )
            self._evidence.append(new_ev)
            self._evidence.append(old_ev)
            new_resolves = new_ev.returncode == 0 and GIT_MOVE_TOKEN in new_ev.stdout
            old_resolves = old_ev.returncode == 0 and GIT_MOVE_TOKEN in old_ev.stdout
            if new_resolves and not old_resolves:
                return "fixed"
            if old_resolves:
                return "present"
            return "changed_failure_mode"
        except Exception:
            return "inconclusive"


class GbrainUpgradeConformanceGateStructureTests(unittest.TestCase):
    """Fast host-side guards for the upgrade gate, candidate-ref validation
    (pre-Docker), and the no-volume-delete flow. No Docker required."""

    @staticmethod
    def _module_text() -> str:
        return (
            REPO_ROOT / "tests" / "runtime" / "test_gbrain_upgrade_conformance.py"
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
            os.environ,
            {
                "RUN_DOCKER_TESTS": "",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_upgrade_conformance_enabled())

    def test_gate_requires_run_gbrain_upgrade_conformance(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_upgrade_conformance_enabled())

    def test_gate_requires_candidate_ref(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "",
            },
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_upgrade_conformance_enabled())

    def test_gate_requires_docker(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(False):
                self.assertFalse(_upgrade_conformance_enabled())

    def test_gate_enabled_when_all_conditions_met(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_UPGRADE_CONFORMANCE": "1",
                "GBRAIN_CONFORMANCE_CANDIDATE_REF": "a" * 40,
            },
        ):
            with self._docker_available_patch(True):
                self.assertTrue(_upgrade_conformance_enabled())

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
        self.assertNotIn("docker", text.split("def _candidate_ref", 1)[1].split("def _validated_candidate_ref", 1)[0])

    def test_candidate_ref_rejects_invalid(self) -> None:
        with mock.patch.dict(os.environ, {"GBRAIN_CONFORMANCE_CANDIDATE_REF": "main"}):
            with self.assertRaises(ValueError):
                _candidate_ref()

    def test_candidate_ref_rejects_equality_with_baseline(self) -> None:
        baseline = parse_dockerfile_gbrain_ref()
        with mock.patch.dict(os.environ, {"GBRAIN_CONFORMANCE_CANDIDATE_REF": baseline}):
            with self.assertRaisesRegex(ValueError, "must differ"):
                _validated_candidate_ref()

    def test_upgrade_flow_preserves_volumes(self) -> None:
        """The upgrade must stop (preserving volumes) and force-recreate
        --no-build against the same volumes; only the final cleanup may
        delete volumes."""
        text = self._module_text()
        runtime_class = text.split("class GbrainUpgradeConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainUpgradeConformanceGateStructureTests", 1)[0]
        self.assertIn('self.runtime.stop("hermes")', runtime_class)
        self.assertIn('self.runtime.recreate_same_volumes("hermes", timeout=600)', runtime_class)
        self.assertIn("self.runtime.build_candidate(self.candidate_ref, \"hermes\", timeout=1800)", runtime_class)
        # No direct volume-deleting down() in the flow: only cleanup() at
        # teardown (down -v --remove-orphans).
        self.assertNotIn("self.runtime.down()", runtime_class)
        self.assertIn("self.runtime.cleanup()", text)

    def test_report_contains_refs_actions_and_classification(self) -> None:
        text = self._module_text()
        self.assertIn("baseline_ref", text)
        self.assertIn("candidate_ref", text)
        self.assertIn("logical_result", text)
        self.assertIn("issue_125_git_move", text)
        self.assertIn("write_report", text)
        self.assertIn("conformance_report_dir", text)
        self.assertIn("gbrain-upgrade-conformance", text)

    def test_issue_125_probe_never_hard_fails(self) -> None:
        """The #125 probe must classify (fixed/present/changed_failure_mode/
        inconclusive) without hard failing just because the issue is open."""
        text = self._module_text()
        runtime_class = text.split("class GbrainUpgradeConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainUpgradeConformanceGateStructureTests", 1)[0]
        for classification in ("fixed", "present", "changed_failure_mode", "inconclusive"):
            self.assertIn(f'return "{classification}"', runtime_class)
        self.assertIn("git mv", runtime_class)
        self.assertIn("GIT_MOVE_TOKEN", runtime_class)


if __name__ == "__main__":
    unittest.main()