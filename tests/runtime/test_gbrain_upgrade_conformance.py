"""Opt-in baseline-to-candidate gbrain upgrade conformance (issue #127 W3).

Runs the SAME disposable Compose project/volumes through a full upgrade:

  - baseline build/start at the EFFECTIVE baseline ref (the committed
    Dockerfile ``GBRAIN_REF``, or the validated upgrade-only
    ``GBRAIN_CONFORMANCE_BASELINE_REF`` override — exact 40-hex, checked
    BEFORE any Docker invocation — when the Dockerfile pin is the
    post-upgrade ref and the real old -> new migration must be proven),
    baseline source-ref proof (``/opt/gbrain/.git/HEAD`` equals the effective
    baseline ref), reindex, and representative logical state created through
    the supported public APIs (capture/put/link)
  - ``docker compose stop`` preserving volumes
  - candidate image build with the validated ``GBRAIN_REF`` build arg
  - force-recreate ``--no-build`` against the SAME volumes
  - candidate source-ref proof (``/opt/gbrain/.git/HEAD`` equals the exact
    candidate ref) and candidate gbrain version
  - candidate reindex/migration success envelope
  - logical-state manifest survival (page content + manual link edge)
  - candidate supported-operation rerun: after candidate activation/
    migration, the applicable provider-free core scenarios are rerun against
    the SAME candidate runtime through the reusable ``CoreScenarioMixin``
    (never duplicated), with per-operation results and exclusions persisted
    in the report (PR #129 MAJOR finding). Exclusions are limited to
    feature-gated chronicle reads (dedicated chronicle gate) and the unsafe
    concurrent-lock fixture scenarios; the second reindex/idempotency check
    remains in addition to the rerun
  - core post-upgrade writes and reindex idempotency
  - issue #125 dedicated git-move probe enforced as a HARD regression
    contract (W3): once the probe's construction is established
    (capture/commit/``git mv``/file-existence), the FIRST refresh, the
    moved page's resolution at the new slug, the unique-token search with
    the old slug no longer serving, and a second identical refresh must
    all hold — any observed regression FAILS the gate
    (``inconclusive`` is retained only for genuine construction
    failures, never for observed behavior)
  - schema-status probe classification on BOTH runtimes (PR #129 re-review):
    the baseline classification is recorded in the baseline phase and the
    candidate classification after the candidate rerun — report-only
    fixed/present/changed_failure_mode/inconclusive, never a hard assertion
    that could reject a real upstream fix — and both are persisted in the
    report

The gate is strict: ``RUN_DOCKER_TESTS=1`` AND ``RUN_GBRAIN_UPGRADE_CONFORMANCE=1``
AND an exact ``GBRAIN_CONFORMANCE_CANDIDATE_REF`` (40-hex, prevalidated BEFORE
any Docker invocation, and rejected when equal to the EFFECTIVE baseline
``GBRAIN_REF`` — the validated ``GBRAIN_CONFORMANCE_BASELINE_REF`` override
when present, otherwise the canonical Dockerfile pin). The baseline override
is OPTIONAL and upgrade-only: absent means the baseline stays the committed
Dockerfile pin (current behavior unchanged). Fast host-side
gate/ref/pre-Docker/no-volume-delete tests in this module always run and need
no Docker.

The JSON report (``dump_folder/gbrain-conformance/gbrain-upgrade-conformance.json``)
carries the baseline/candidate refs, the action list, the logical result, the
#125 hard-contract result (pass/fail/inconclusive), the baseline/candidate
schema-status probe classifications, and the operation result matrix —
command/result metadata only, never environment dumps.
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
    CoreScenarioMixin,
)
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

# Issue #125 git-move probe facts (W3 hard regression contract: after valid
# setup, any observed regression fails the gate).
GIT_MOVE_SLUG = "inbox/git-move-probe"
GIT_MOVE_NEW_SLUG = "notes/git-move-probe"
GIT_MOVE_TOKEN = "conformance-git-move-token"


def _classify_issue_125_git_move(
    *,
    moved_file_exists: bool,
    new_resolves: bool,
    old_resolves: bool,
    token_search_resolves: bool,
) -> str:
    """Pure #127 oracle for the issue #125 git-move probe classification
    (PR #129 MAJOR finding: the recorded failure mode is that after a
    same-content Git move and refresh NEITHER slug resolves while the file
    still exists).

      - ``fixed``: the new slug resolves, the unique body token search
        resolves, and the old slug no longer serves the moved page.
      - ``present``: the moved file still exists but neither the old nor
        the new slug resolves and the unique token search does not resolve
        either — issue #125's recorded failure mode.
      - ``changed_failure_mode``: any other failure (old slug still live,
        new slug resolves but is not searchable, the page vanished, ...).

    W3: this is the established oracle the hard regression contract
    consults — after valid setup any non-``fixed`` outcome fails the gate.
    """
    if new_resolves and token_search_resolves and not old_resolves:
        return "fixed"
    if (
        moved_file_exists
        and not new_resolves
        and not old_resolves
        and not token_search_resolves
    ):
        return "present"
    return "changed_failure_mode"

# Upgrade conformance matrix: every operation this suite owns, with its
# classification. The report persists an explicit result for each.
UPGRADE_MATRIX = {
    "baseline_build_start": "core",
    "baseline_source_ref": "core",
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
    "issue_125_git_move": "core",
}

# Candidate supported-operation rerun (PR #129 MAJOR finding): after
# candidate activation/migration the applicable provider-free core scenarios
# are rerun against the SAME candidate runtime through the reusable
# CoreScenarioMixin. Every operation here is reported with its actual result
# (``candidate_operations`` in the report); the classifications mirror the
# core conformance matrix.
CANDIDATE_OPERATIONS = {
    "provenance": "core",
    "pack_identity": "core",
    "status": "core",
    "doctor": "core",
    "sources_list": "core",
    "schema_status_probe": "probe_unavailable",
    "type_inference": "core",
    "get": "core",
    "search": "core",
    "tags": "core",
    "backlinks": "core",
    "capture": "core",
    "link": "core",
    "graph": "core",
    "refresh": "operator_only",
    "put": "core",
    "put --stdin": "forbidden",
    "history": "core",
    "revert": "core",
    "delete": "core",
    "restore": "core",
    "external_edit_pre_refresh": "core",
    "external_edit_post_refresh": "core",
}

# Candidate rerun exclusions with reasons. Allowed categories only (PR #129):
# feature gates with dedicated candidate gates (chronicle reads) and the
# unsafe concurrent-lock fixture (a leaked lock holder would block the later
# lock-using probe/idempotency steps on the candidate runtime).
CANDIDATE_EXCLUSIONS = {
    op: "feature-gated chronicle reads (dedicated chronicle gate)"
    for op in (
        "chronicle_timeline",
        "chronicle_day",
        "chronicle_day_week",
        "chronicle_since",
        "chronicle_last_seen",
        "chronicle_on_this_day",
        "chronicle_orient",
        "chronicle_ontology",
    )
}
CANDIDATE_EXCLUSIONS.update(
    {
        "refresh_lock_busy": "unsafe concurrent-lock fixture on the candidate runtime",
        "public_reindex_rejected": "unsafe concurrent-lock fixture on the candidate runtime",
    }
)

# The reusable mixin scenarios rerun on the candidate, in flow order. The
# candidate status is the adapted ``_scenario_candidate_status`` (the mixin
# status scenario pins the baseline version and cannot run on a candidate).
CANDIDATE_RERUN_SCENARIOS = (
    "_scenario_provenance",
    "_scenario_pack_identity",
    "_scenario_candidate_status",
    "_scenario_doctor",
    "_scenario_sources_list",
    "_scenario_schema_status_probe",
    "_scenario_type_inference",
    "_scenario_get_search_tags",
    "_scenario_links_backlinks_graph",
    "_scenario_public_write_contracts",
    "_scenario_recovery_history_revert",
    "_scenario_soft_delete_restore",
    "_scenario_external_edit_refresh",
)


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
        # Effective baseline: the validated upgrade-only override when set,
        # otherwise the committed Dockerfile pin (unchanged behavior).
        self.baseline_ref = effective_baseline_ref()
        self.dockerfile_ref = parse_dockerfile_gbrain_ref()
        self.baseline_ref_source = (
            "override" if baseline_override_active() else "dockerfile"
        )
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {
            op: "not_run" for op in {**UPGRADE_MATRIX, **CANDIDATE_OPERATIONS}
        }
        self._baseline_version: str | None = None
        self._baseline_source_ref: str | None = None
        self._candidate_version: str | None = None
        self._gbrain_version: str | None = None
        self._logical_result: str = "not_run"
        self._issue_125_classification: str = "inconclusive"
        self._schema_status_probe_baseline: str = "inconclusive"
        self._schema_status_probe_candidate: str = "inconclusive"
        self._report_path: Path | None = None

        self.runtime = GbrainConformanceRuntime()
        # Pre-start source state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # Baseline build/start at the effective baseline ref (Dockerfile
        # default, or the validated override via the upgrade-only path).
        self.runtime.up_baseline("hermes", timeout=900)
        self._matrix["baseline_build_start"] = "pass"
        # Wait for the exact hermes-writable surface before any exec probe.
        self.runtime.wait_until_hermes_writable(timeout=120)
        # Isolation safety checks: empty credentials + disabled owned jobs.
        self._evidence.append(self._assert_no_credentials())
        self._evidence.append(self.runtime.assert_owned_jobs_disabled())
        # Synthetic vault init committed as the hermes runtime user.
        self._evidence.append(self.runtime.init_synthetic_vault())
        # Fixture seeds the reusable CoreScenarioMixin scenarios depend on
        # (tagged note + welcome wikilink), committed before baseline
        # reindex so the candidate rerun finds them after the upgrade.
        self._evidence.append(self._seed_tagged_note())
        self._evidence.append(self._seed_welcome_wikilink())

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

    def _seed_tagged_note(self) -> CommandEvidence:
        """Write a deterministic note carrying the ``#conformance`` tag into
        the vault as the hermes runtime user and commit it, so the baseline
        reindex indexes the tag association the reusable scenarios assert on
        the candidate (mirrors the core suite's fixture seeding)."""
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
        committed as the hermes runtime user before baseline reindex (mirrors
        the core suite's fixture seeding)."""
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

    def _write_report(self) -> None:
        """Persist the upgrade conformance report: baseline/candidate refs,
        actions, logical result, #125 hard-contract result (pass/fail/
        inconclusive), the operation result matrix, the candidate supported-
        operation rerun results, and the candidate rerun exclusions.
        Command/result metadata only — never environment dumps."""
        metadata = {
            "baseline_ref": self.baseline_ref,
            "baseline_ref_source": self.baseline_ref_source,
            "dockerfile_gbrain_ref": self.dockerfile_ref,
            "baseline_source_ref": self._baseline_source_ref,
            "candidate_ref": self.candidate_ref,
            "baseline_gbrain_version": self._baseline_version,
            "candidate_gbrain_version": self._candidate_version,
            "actions": list(UPGRADE_MATRIX),
            "logical_result": self._logical_result,
            "issue_125_git_move": self._issue_125_classification,
            "schema_status_probe_baseline": self._schema_status_probe_baseline,
            "schema_status_probe_candidate": self._schema_status_probe_candidate,
            "matrix": self._matrix,
            "candidate_operations": {
                op: self._matrix.get(op, "not_run") for op in CANDIDATE_OPERATIONS
            },
            "candidate_exclusions": dict(CANDIDATE_EXCLUSIONS),
        }
        self._report_path = write_report(
            conformance_report_dir(),
            "gbrain-upgrade-conformance",
            self._evidence,
            metadata=metadata,
        )


class GbrainUpgradeConformanceRuntimeTests(
    CoreScenarioMixin, GbrainUpgradeConformanceTestCase
):
    """W3 baseline-to-candidate upgrade scenarios (Docker-gated via the base
    class). The candidate supported-operation rerun reuses the provider-free
    ``CoreScenarioMixin`` scenarios against the candidate runtime (PR #129
    MAJOR finding: never duplicated copies)."""

    def test_baseline_to_candidate_upgrade_conformance(self) -> None:
        try:
            self._scenario_baseline_source_ref()
            self._scenario_baseline_reindex()
            self._scenario_baseline_schema_status_probe()
            self._scenario_baseline_state()
            self._scenario_stop_preserve_volumes()
            self._scenario_candidate_build()
            self._scenario_candidate_recreate()
            self._scenario_candidate_source_ref()
            self._scenario_candidate_reindex()
            self._scenario_state_manifest_survives()
            self._scenario_candidate_operations_rerun()
            self._scenario_post_upgrade_write()
            self._scenario_reindex_idempotency()
            self._scenario_issue_125_probe()
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

    def _scenario_baseline_schema_status_probe(self) -> None:
        """Baseline schema-status probe classification (PR #129 re-review):
        recorded in the baseline phase (after baseline reindex) so the
        upgrade report can cite BOTH the baseline and the candidate
        classification. Report-only — never a hard assertion."""
        self._scenario_schema_status_probe()
        self._schema_status_probe_baseline = self._schema_status_classification

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

    def _scenario_candidate_operations_rerun(self) -> None:
        """Rerun the applicable provider-free core scenarios against the SAME
        candidate runtime after activation/migration (PR #129 MAJOR finding:
        the candidate must be exercised through the supported-operation
        matrix, not just the migration envelope). Reuses the
        ``CoreScenarioMixin`` scenarios — never duplicated copies; the
        candidate status is the adapted ``_scenario_candidate_status``
        (version recorded, never pinned to the baseline version)."""
        self._scenario_provenance()
        self._scenario_pack_identity()
        self._scenario_candidate_status()
        self._scenario_doctor()
        self._scenario_sources_list()
        self._scenario_schema_status_probe()
        self._schema_status_probe_candidate = self._schema_status_classification
        self._scenario_type_inference()
        self._scenario_get_search_tags()
        self._scenario_links_backlinks_graph()
        self._scenario_public_write_contracts()
        self._scenario_recovery_history_revert()
        self._scenario_soft_delete_restore()
        self._scenario_external_edit_refresh()

    def _scenario_candidate_status(self) -> None:
        """Candidate status facts, adapted from the core status scenario: the
        candidate version is recorded (and must match the source-ref proof),
        never pinned to the baseline version."""
        self._matrix["status"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "status", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        data = json.loads(ev.stdout)
        self.assertEqual(data.get("mode"), "local")
        self.assertEqual(data.get("schema_version"), 1)
        self.assertEqual(data.get("version"), self._candidate_version)
        sources = data.get("sync", {}).get("sources", [])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].get("local_path"), "/opt/data/obsidian")
        self._gbrain_version = data.get("version")
        self._matrix["status"] = "pass"

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
        """Issue #125 dedicated git-move probe, enforced as a HARD
        regression contract (W3): once the probe's construction is
        established (capture/commit/``git mv``/file-existence), any
        observed regression — including a FAILING FIRST refresh, a
        destination get/search failure, old slug still serving,
        duplicates/stale state, or a second refresh that changes or breaks
        the fixed state — FAILS the gate. ``inconclusive`` is retained only
        for genuine construction failures, and is recorded report-only;
        observed behavior is never recorded as ``present``/
        ``changed_failure_mode``."""
        self._matrix["issue_125_git_move"] = "fail"
        classification = self._probe_issue_125_git_move()
        if classification == "inconclusive":
            self._issue_125_classification = "inconclusive"
            self._matrix["issue_125_git_move"] = "inconclusive"
            return
        # The hard contract held: setup established and every probe
        # classified ``fixed`` (including after the second refresh).
        self._issue_125_classification = "fixed"
        self._matrix["issue_125_git_move"] = "pass"

    def _probe_issue_125_git_move(self) -> str:
        """Run the git-move probe on the candidate and enforce the W3 hard
        regression contract for issue #125.

        Setup = the CONSTRUCTION steps only: capture, commit, ``git mv``
        and destination-file existence. A construction failure returns
        ``inconclusive`` — the scenario could not be established, so no
        observation is possible. The FIRST ``josemar-gbrain refresh`` is
        NOT construction: it is the first behavioral surface, so its
        failure raises AssertionError (hard fail) like every later
        regression. Once established, the contract is HARD (no historical
        content-hash assumptions; the public retrieval surfaces alone are
        authoritative):

          - the new slug get must resolve and the unique body-token search
            must resolve (destination get/search must not fail);
          - the old slug must no longer serve the moved page (no stale
            duplicates per the established oracle);
          - a second identical refresh must not change or break the fixed
            state: the re-probe must classify ``fixed`` again.

        Any observed regression (including a failing first refresh) raises
        AssertionError with the raw evidence — it is never recorded as
        ``changed_failure_mode``.
        """
        # --- setup: construction failures => inconclusive ----------------
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
            # The moved file must physically exist for a valid probe.
            ev = self.runtime.run_as_hermes(
                "test", "-f", "/opt/data/obsidian/" + GIT_MOVE_NEW_SLUG + ".md",
                check=False,
            )
            self._evidence.append(ev)
            if ev.returncode != 0:
                return "inconclusive"
        except Exception:
            return "inconclusive"

        # --- hard contract, first refresh: failure => AssertionError -----
        refresh_ev = self.runtime.run_as_hermes(
            "josemar-gbrain", "refresh", timeout=300, check=False,
        )
        self._evidence.append(refresh_ev)
        if refresh_ev.returncode != 0:
            raise AssertionError(
                "issue #125 git-move regression (W3 hard contract): the "
                "FIRST refresh failed after valid construction "
                f"(rc={refresh_ev.returncode} stdout={refresh_ev.stdout!r} "
                f"stderr={refresh_ev.stderr!r})"
            )

        # --- hard contract, first probe: regression => AssertionError ---
        new_ev = self.runtime.run_as_hermes(
            "gbrain", "get", GIT_MOVE_NEW_SLUG, check=False,
        )
        old_ev = self.runtime.run_as_hermes(
            "gbrain", "get", GIT_MOVE_SLUG, check=False,
        )
        search_ev = self.runtime.run_as_hermes(
            "gbrain", "search", GIT_MOVE_TOKEN, "--limit", "5",
            check=False,
        )
        self._evidence.extend((new_ev, old_ev, search_ev))
        self._assert_issue_125_fixed(
            new_ev, old_ev, search_ev,
            probe_label="the first probe",
        )

        # --- second refresh must not change or break the fixed state ----
        refresh_ev = self.runtime.run_as_hermes(
            "josemar-gbrain", "refresh", timeout=300, check=False,
        )
        self._evidence.append(refresh_ev)
        if refresh_ev.returncode != 0:
            raise AssertionError(
                "issue #125 git-move regression (W3 hard contract): the "
                "second refresh failed after a fixed first probe "
                f"(rc={refresh_ev.returncode} stdout={refresh_ev.stdout!r} "
                f"stderr={refresh_ev.stderr!r})"
            )
        new_ev = self.runtime.run_as_hermes(
            "gbrain", "get", GIT_MOVE_NEW_SLUG, check=False,
        )
        old_ev = self.runtime.run_as_hermes(
            "gbrain", "get", GIT_MOVE_SLUG, check=False,
        )
        search_ev = self.runtime.run_as_hermes(
            "gbrain", "search", GIT_MOVE_TOKEN, "--limit", "5",
            check=False,
        )
        self._evidence.extend((new_ev, old_ev, search_ev))
        self._assert_issue_125_fixed(
            new_ev, old_ev, search_ev,
            probe_label="the re-probe after the second refresh",
        )
        return "fixed"

    def _assert_issue_125_fixed(
        self,
        new_ev: CommandEvidence,
        old_ev: CommandEvidence,
        search_ev: CommandEvidence,
        *,
        probe_label: str,
    ) -> None:
        """W3 hard regression assertion for the #125 git-move contract:
        after valid setup the moved page must be reachable ONLY at the new
        slug and through the unique-token search — the established
        oracle's ``fixed``. Any other outcome (destination get/search
        failure, old slug still serving, duplicates/stale state) is an
        observed regression and raises with the raw evidence; it is never
        recorded as ``changed_failure_mode``."""
        new_resolves = new_ev.returncode == 0 and GIT_MOVE_TOKEN in new_ev.stdout
        old_resolves = old_ev.returncode == 0 and GIT_MOVE_TOKEN in old_ev.stdout
        token_search_resolves = (
            search_ev.returncode == 0 and GIT_MOVE_TOKEN in search_ev.stdout
        )
        classification = _classify_issue_125_git_move(
            moved_file_exists=True,
            new_resolves=new_resolves,
            old_resolves=old_resolves,
            token_search_resolves=token_search_resolves,
        )
        if classification == "fixed":
            return
        raise AssertionError(
            "issue #125 git-move regression (W3 hard contract) after valid "
            f"setup ({probe_label}): classification={classification!r} "
            f"new_resolves={new_resolves} old_resolves={old_resolves} "
            f"token_search_resolves={token_search_resolves}\n"
            f"  gbrain get new slug: rc={new_ev.returncode} "
            f"stdout={new_ev.stdout!r} stderr={new_ev.stderr!r}\n"
            f"  gbrain get old slug: rc={old_ev.returncode} "
            f"stdout={old_ev.stdout!r} stderr={old_ev.stderr!r}\n"
            f"  gbrain search token: rc={search_ev.returncode} "
            f"stdout={search_ev.stdout!r} stderr={search_ev.stderr!r}\n"
        )


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
        text = self._module_text()
        runtime_class = text.split("class GbrainUpgradeConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainUpgradeConformanceGateStructureTests", 1)[0]
        self.assertIn("self._scenario_baseline_source_ref()", runtime_class)
        self.assertIn('"cat", "/opt/gbrain/.git/HEAD"', runtime_class)
        self.assertIn("self.baseline_ref", runtime_class)
        self.assertIn('"baseline_source_ref": "core"', text)
        report = text.split("def _write_report", 1)[1]
        report = report.split("def test_baseline_to_candidate_upgrade_conformance", 1)[0]
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

    def test_schema_status_probe_classifications_persisted(self) -> None:
        """The upgrade report persists BOTH the baseline and the candidate
        schema-status probe classifications so the parent can cite them."""
        text = self._module_text()
        self.assertIn("_schema_status_probe_baseline", text)
        self.assertIn("_schema_status_probe_candidate", text)
        report = text.split("def _write_report", 1)[1]
        report = report.split("def test_baseline_to_candidate_upgrade_conformance", 1)[0]
        self.assertIn(
            '"schema_status_probe_baseline": self._schema_status_probe_baseline',
            report,
        )
        self.assertIn(
            '"schema_status_probe_candidate": self._schema_status_probe_candidate',
            report,
        )
        # Both are initialized to inconclusive before any probe runs.
        base = text.split("def setUp", 1)[1]
        base = base.split("def tearDown", 1)[0]
        self.assertIn('self._schema_status_probe_baseline: str = "inconclusive"', base)
        self.assertIn('self._schema_status_probe_candidate: str = "inconclusive"', base)

    def test_baseline_probe_and_candidate_snapshot_never_reject_fix(self) -> None:
        """The baseline probe runs in the baseline phase (after baseline
        reindex), and the candidate rerun snapshots its classification — a
        real upstream fix on the candidate is recorded, never rejected."""
        text = self._module_text()
        runtime_class = text.split("class GbrainUpgradeConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainUpgradeConformanceGateStructureTests", 1)[0]
        self.assertIn("self._scenario_baseline_schema_status_probe()", runtime_class)
        self.assertIn(
            "self._schema_status_probe_candidate = self._schema_status_classification",
            runtime_class,
        )
        flow = runtime_class.split("def test_baseline_to_candidate_upgrade_conformance", 1)[1]
        flow = flow.split("def _scenario_baseline_reindex", 1)[0]
        self.assertLess(
            flow.index("self._scenario_baseline_reindex()"),
            flow.index("self._scenario_baseline_schema_status_probe()"),
        )

    def test_issue_125_probe_is_hard_regression_contract(self) -> None:
        """The #125 probe is a HARD regression contract (W3): after valid
        setup, every observed regression (destination get/search failure,
        old slug still serving, duplicates/stale state, a second refresh
        that changes or breaks the fixed state) raises AssertionError;
        ``inconclusive`` is retained ONLY for genuine unestablished setup
        conditions."""
        text = self._module_text()
        runtime_class = text.split("class GbrainUpgradeConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainUpgradeConformanceGateStructureTests", 1)[0]
        # The hard assertion executes (raises on non-fixed, never records
        # present/changed_failure_mode), and the oracle is still consulted.
        self.assertIn("self._assert_issue_125_fixed(", runtime_class)
        self.assertIn("raise AssertionError", runtime_class)
        self.assertIn("_classify_issue_125_git_move(", runtime_class)
        # The second refresh postconditions are part of the contract: a
        # second refresh + re-probe must stay fixed.
        self.assertIn('"josemar-gbrain", "refresh", timeout=300, check=False',
                      runtime_class)
        # Inconclusive is retained ONLY for construction failures
        # (capture/commit/move/file-existence), never for behavior —
        # including the FIRST refresh, whose failure is a hard regression.
        self.assertIn('return "inconclusive"', runtime_class)
        self.assertIn("git mv", runtime_class)
        self.assertIn("GIT_MOVE_TOKEN", runtime_class)
        self.assertIn('"gbrain", "search", GIT_MOVE_TOKEN, "--limit", "5"', runtime_class)
        # The scenario marks the matrix pass only when the contract held.
        self.assertIn('self._matrix["issue_125_git_move"] = "pass"', runtime_class)
        # The pure oracle still holds the three behavioral classifications.
        classifier = text.split("def _classify_issue_125_git_move", 1)[1]
        classifier = classifier.split("def _candidate_ref", 1)[0]
        for classification in ("fixed", "present", "changed_failure_mode"):
            self.assertIn(f'return "{classification}"', classifier)

    def test_candidate_rerun_mixes_in_reusable_scenarios(self) -> None:
        """The candidate rerun must reuse the ``CoreScenarioMixin`` scenarios
        (never duplicated copies), run after candidate activation/migration,
        and keep the second reindex/idempotency in addition to the rerun."""
        self.assertTrue(
            issubclass(GbrainUpgradeConformanceRuntimeTests, CoreScenarioMixin)
        )
        text = self._module_text()
        self.assertIn("from .gbrain_conformance_scenarios import", text)
        runtime_class = text.split("class GbrainUpgradeConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainUpgradeConformanceGateStructureTests", 1)[0]
        for scenario in CANDIDATE_RERUN_SCENARIOS:
            self.assertIn(f"self.{scenario}()", runtime_class)
        flow = runtime_class.split("def test_baseline_to_candidate_upgrade_conformance", 1)[1]
        flow = flow.split("def _scenario_baseline_reindex", 1)[0]
        self.assertLess(
            flow.index("self._scenario_candidate_reindex()"),
            flow.index("self._scenario_candidate_operations_rerun()"),
        )
        self.assertLess(
            flow.index("self._scenario_candidate_operations_rerun()"),
            flow.index("self._scenario_reindex_idempotency()"),
        )

    def test_candidate_rerun_fixtures_seeded_in_base_setup(self) -> None:
        """The reusable scenarios require the tagged note and the welcome
        wikilink fixtures; they must be seeded in the base setup before
        baseline reindex (never re-created by the candidate rerun)."""
        text = self._module_text()
        base = text.split("class GbrainUpgradeConformanceTestCase", 1)[1]
        base = base.split("class GbrainUpgradeConformanceRuntimeTests", 1)[0]
        self.assertIn("self._seed_tagged_note()", base)
        self.assertIn("self._seed_welcome_wikilink()", base)
        self.assertIn("tags: [conformance]", base)
        self.assertIn("[[projects/atlas]]", base)

    def test_candidate_operations_matrix_and_report_coverage(self) -> None:
        """Every candidate operation is reported with an actual result, and
        every core conformance operation is either rerun on the candidate,
        recorded as an allowed exclusion, or covered by a dedicated upgrade
        scenario (reindex) / baseline setup (baseline_*)."""
        text = self._module_text()
        report = text.split("def _write_report", 1)[1]
        report = report.split("def test_baseline_to_candidate_upgrade_conformance", 1)[0]
        self.assertIn('"candidate_operations":', report)
        self.assertIn('"candidate_exclusions":', report)
        # Every candidate operation key is written by a rerun scenario: the
        # mixin must set the same keys the report carries.
        scenario_text = (
            REPO_ROOT / "tests" / "runtime" / "gbrain_conformance_scenarios.py"
        ).read_text(encoding="utf-8")
        for op in CANDIDATE_OPERATIONS:
            self.assertIn(f'self._matrix["{op}"]', scenario_text)
        # Completeness: rerun + allowed exclusions + dedicated reindex
        # coverage + baseline setup == the full core conformance matrix.
        covered = (
            set(CANDIDATE_OPERATIONS)
            | set(CANDIDATE_EXCLUSIONS)
            | {"reindex"}
            | {op for op in CONFORMANCE_MATRIX if op.startswith("baseline_")}
        )
        self.assertEqual(set(CONFORMANCE_MATRIX), covered)


class _ScriptedProbeRuntime:
    """Minimal stand-in for ``GbrainConformanceRuntime`` used ONLY by the
    host-side #125 probe tests: returns a scripted ``CommandEvidence`` per
    probe step, so ``_probe_issue_125_git_move`` hard-contract semantics
    are exercised without Docker. Repeatable steps (refresh/get_new/
    get_old/search) map their second occurrence to a ``2``-suffixed key
    so the second-refresh postconditions can be scripted independently."""

    def __init__(self, steps: dict[str, tuple[int, str]]) -> None:
        self.steps = steps
        self.calls: list[tuple[str, ...]] = []
        self._counts: dict[str, int] = {}

    def run_as_hermes(
        self, *command: str, check: bool = True, timeout: int = 180,
    ) -> CommandEvidence:
        self.calls.append(command)
        rc, stdout = self.steps[self._step(command)]
        return CommandEvidence(
            command=list(command),
            returncode=rc,
            stdout=stdout,
            stderr="",
            elapsed_seconds=0.0,
        )

    def _step(self, command: tuple[str, ...]) -> str:
        key = self._base_step(command)
        if key in ("get_new", "get_old", "search", "refresh"):
            self._counts[key] = self._counts.get(key, 0) + 1
            if self._counts[key] > 1:
                return key + "2"
        return key

    @staticmethod
    def _base_step(command: tuple[str, ...]) -> str:
        if command[0] == "gbrain":
            if command[1] == "capture":
                return "capture"
            if command[1] == "get":
                return "get_new" if command[2] == GIT_MOVE_NEW_SLUG else "get_old"
            if command[1] == "search":
                return "search"
        if command[0] == "sh":
            return "mv" if "git mv" in command[2] else "commit"
        if command[0] == "test":
            return "file"
        if command[0] == "josemar-gbrain":
            return "refresh"
        raise AssertionError(f"unscripted probe command: {command!r}")


class GbrainIssue125ClassificationTests(unittest.TestCase):
    """Host-side semantics for the #127 issue-#125 oracle (PR #129 MAJOR:
    ``fixed`` requires new-slug get + unique-token search with the old slug
    no longer live; ``present`` is the recorded both-slugs-missing signature;
    anything else that still fails is ``changed_failure_mode``). W3: the
    hard contract consults this oracle and fails on every non-``fixed``
    outcome after valid setup."""

    def test_fixed_requires_new_get_plus_token_search_and_old_not_live(self) -> None:
        self.assertEqual(
            _classify_issue_125_git_move(
                moved_file_exists=True,
                new_resolves=True,
                old_resolves=False,
                token_search_resolves=True,
            ),
            "fixed",
        )

    def test_fixed_not_reached_without_token_search(self) -> None:
        """New-slug get alone is NOT fixed: the unique body token search
        must resolve as well."""
        self.assertEqual(
            _classify_issue_125_git_move(
                moved_file_exists=True,
                new_resolves=True,
                old_resolves=False,
                token_search_resolves=False,
            ),
            "changed_failure_mode",
        )

    def test_fixed_not_reached_while_old_slug_still_live(self) -> None:
        self.assertEqual(
            _classify_issue_125_git_move(
                moved_file_exists=True,
                new_resolves=True,
                old_resolves=True,
                token_search_resolves=True,
            ),
            "changed_failure_mode",
        )

    def test_present_requires_moved_file_with_no_resolution_at_all(self) -> None:
        """Issue #125's recorded failure mode: the moved file exists but
        neither slug resolves nor the unique token search."""
        self.assertEqual(
            _classify_issue_125_git_move(
                moved_file_exists=True,
                new_resolves=False,
                old_resolves=False,
                token_search_resolves=False,
            ),
            "present",
        )

    def test_present_not_reached_when_anything_resolves(self) -> None:
        for kwargs in (
            {"new_resolves": True, "old_resolves": False, "token_search_resolves": False},
            {"new_resolves": False, "old_resolves": True, "token_search_resolves": False},
            {"new_resolves": False, "old_resolves": False, "token_search_resolves": True},
        ):
            self.assertEqual(
                _classify_issue_125_git_move(moved_file_exists=True, **kwargs),
                "changed_failure_mode",
            )

    def test_changed_failure_mode_covers_remaining_failures(self) -> None:
        # Old slug still live (stale index) differs from the #125 signature.
        self.assertEqual(
            _classify_issue_125_git_move(
                moved_file_exists=True,
                new_resolves=False,
                old_resolves=True,
                token_search_resolves=False,
            ),
            "changed_failure_mode",
        )
        # New slug resolves but is not searchable: retrieval still broken.
        self.assertEqual(
            _classify_issue_125_git_move(
                moved_file_exists=True,
                new_resolves=True,
                old_resolves=False,
                token_search_resolves=False,
            ),
            "changed_failure_mode",
        )


class GbrainIssue125ProbeSetupTests(unittest.TestCase):
    """Host-side hard-contract semantics for the #125 probe: ``inconclusive``
    ONLY for genuine unestablished setup conditions (preconditions/
    infrastructure), while any observed regression after valid setup must
    raise AssertionError — it is never recorded as ``present``/
    ``changed_failure_mode``."""

    @staticmethod
    def _probe(steps: dict[str, tuple[int, str]]) -> tuple[str, list[tuple[str, ...]]]:
        runtime = _ScriptedProbeRuntime(steps)
        case = GbrainUpgradeConformanceRuntimeTests.__new__(
            GbrainUpgradeConformanceRuntimeTests
        )
        case.runtime = runtime  # type: ignore[assignment]
        case._evidence = []
        classification = case._probe_issue_125_git_move()
        return classification, runtime.calls

    @staticmethod
    def _steps_all_ok() -> dict[str, tuple[int, str]]:
        """Every step succeeds and every probe stays ``fixed``, including
        the second-refresh postconditions."""
        return {
            "capture": (0, "captured"),
            "commit": (0, ""),
            "mv": (0, ""),
            "file": (0, ""),
            "refresh": (0, "refreshed"),
            "get_new": (0, GIT_MOVE_TOKEN + " new page"),
            "get_old": (1, "not found"),
            "search": (0, GIT_MOVE_NEW_SLUG + " " + GIT_MOVE_TOKEN),
            "refresh2": (0, "refreshed"),
            "get_new2": (0, GIT_MOVE_TOKEN + " new page"),
            "get_old2": (1, "not found"),
            "search2": (0, GIT_MOVE_NEW_SLUG + " " + GIT_MOVE_TOKEN),
        }

    def test_inconclusive_when_capture_fails(self) -> None:
        steps = self._steps_all_ok()
        steps["capture"] = (1, "")
        classification, calls = self._probe(steps)
        self.assertEqual(classification, "inconclusive")
        self.assertEqual(len(calls), 1)

    def test_inconclusive_when_initial_commit_fails(self) -> None:
        steps = self._steps_all_ok()
        steps["commit"] = (1, "")
        classification, calls = self._probe(steps)
        self.assertEqual(classification, "inconclusive")
        self.assertEqual(len(calls), 2)

    def test_inconclusive_when_git_mv_fails(self) -> None:
        steps = self._steps_all_ok()
        steps["mv"] = (1, "")
        classification, _ = self._probe(steps)
        self.assertEqual(classification, "inconclusive")

    def test_inconclusive_when_moved_file_missing(self) -> None:
        steps = self._steps_all_ok()
        steps["file"] = (1, "")
        classification, _ = self._probe(steps)
        self.assertEqual(classification, "inconclusive")

    def test_first_refresh_failure_hard_fails(self) -> None:
        """The FIRST refresh is NOT construction: after capture/commit/
        ``git mv``/file-existence succeeded, a failing refresh is an
        OBSERVED REGRESSION and must raise AssertionError — never
        ``inconclusive``."""
        steps = self._steps_all_ok()
        steps["refresh"] = (1, "")
        with self.assertRaisesRegex(AssertionError, "FIRST refresh failed"):
            self._probe(steps)

    def test_hard_contract_passes_when_fixed(self) -> None:
        """Valid setup + fixed first probe + fixed re-probe after the
        second refresh: the probe returns ``fixed`` (the only non-
        inconclusive success)."""
        classification, calls = self._probe(self._steps_all_ok())
        self.assertEqual(classification, "fixed")
        # 4 construction steps (capture/commit/mv/file) + first refresh (1)
        # + first probe (3) + second refresh (1) + re-probe (3): the first
        # refresh and the second-refresh postconditions are part of the
        # hard contract.
        self.assertEqual(len(calls), 12)

    def test_present_signature_hard_fails(self) -> None:
        """The recorded #125 signature (moved file exists, neither slug nor
        the token search resolves) is an OBSERVED REGRESSION: the probe
        must raise, never return ``present``."""
        steps = self._steps_all_ok()
        steps["get_new"] = (1, "")
        steps["get_old"] = (1, "")
        steps["search"] = (0, "")
        with self.assertRaisesRegex(AssertionError, "regression"):
            self._probe(steps)

    def test_old_slug_still_live_hard_fails(self) -> None:
        """The old slug still serving the moved page (stale state) must
        raise, never return ``changed_failure_mode``."""
        steps = self._steps_all_ok()
        steps["get_old"] = (0, GIT_MOVE_TOKEN + " stale")
        with self.assertRaisesRegex(AssertionError, "regression"):
            self._probe(steps)

    def test_destination_get_failure_hard_fails(self) -> None:
        """New-slug get failing after valid setup is a regression."""
        steps = self._steps_all_ok()
        steps["get_new"] = (1, "")
        with self.assertRaisesRegex(AssertionError, "regression"):
            self._probe(steps)

    def test_destination_search_failure_hard_fails(self) -> None:
        """Unique-token search failing after valid setup is a regression."""
        steps = self._steps_all_ok()
        steps["search"] = (1, "")
        with self.assertRaisesRegex(AssertionError, "regression"):
            self._probe(steps)

    def test_second_refresh_changes_fixed_state_hard_fails(self) -> None:
        """A second refresh that changes/breaks the fixed state (here: the
        new slug stops resolving) is a regression: the re-probe must stay
        ``fixed``."""
        steps = self._steps_all_ok()
        steps["get_new2"] = (1, "")
        with self.assertRaisesRegex(AssertionError, "regression"):
            self._probe(steps)

    def test_second_refresh_failure_hard_fails(self) -> None:
        """A failing second refresh after a fixed first probe is an
        observed breakage, not an unestablished setup condition."""
        steps = self._steps_all_ok()
        steps["refresh2"] = (1, "boom")
        with self.assertRaisesRegex(AssertionError, "regression"):
            self._probe(steps)


if __name__ == "__main__":
    unittest.main()