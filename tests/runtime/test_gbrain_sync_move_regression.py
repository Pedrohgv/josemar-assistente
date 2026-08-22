"""Issue #125 W1: committed unchanged ``git mv`` + ``josemar-gbrain refresh``
characterization gate (branch fix/125-gbrain-vault-renames).

W1 is the dedicated, deterministic characterization of issue #125 against the
CURRENT pinned gbrain (Dockerfile ``GBRAIN_REF`` = 69aea15e…, gbrain
v0.46.26.0) on the EXISTING Docker/PGLite conformance harness
(``GbrainConformanceRuntime`` + disposable Compose project + synthetic vault).
It characterizes BOTH recorded origin paths through the SAME normal
``josemar-gbrain refresh``:

  - Case A ``sync-originated``: the page is created as a plain vault file
    committed by the hermes runtime user, indexed ONLY through
    ``josemar-gbrain refresh`` (the sync path), then moved.
  - Case B ``capture-originated``: the page is created through the public
    ``gbrain capture`` write-through (file + PGLite row), committed, then
    moved.

Both cases perform a committed UNCHANGED ``git mv`` (content proven
byte-identical by sha256 before/after) and run the normal operator
``josemar-gbrain refresh`` (shared TaskNotes lock, hermes runtime user). The
probe surfaces are the public ``gbrain get`` (old slug, new slug) and the
unique body-token ``gbrain search`` — the surfaces issue #125 records as
unreachable after the move.

Evidence contract (preserved RAW on every failure, never collapsed):
  - version/ref: ``gbrain status --json`` version, ``/opt/gbrain/.git/HEAD``,
    and the Dockerfile ``GBRAIN_REF`` baseline pin
  - pre/post commits: ``git log --oneline -2`` before and after the move
  - git classification: ``git diff --name-status -M HEAD~1 HEAD`` (rename
    detection; the unchanged move must classify ``R100``)
  - moved existence: ``test -f`` new path present / old path absent
  - content unchanged: sha256 of the file before vs. after the move
  - refresh stdout/stderr/json: full ``CommandEvidence`` for every
    ``josemar-gbrain refresh`` invocation
  - old/new ``gbrain get`` and unique-token ``gbrain search``: full raw
    stdout/stderr for every probe
  - second refresh postconditions: a second identical refresh + re-probe of
    all three surfaces, recorded and classified separately
  - supported metadata: ``gbrain status --json``, ``gbrain sources list
    --json``, ``gbrain doctor --json`` when available

The whole run is written to
``dump_folder/gbrain-conformance/gbrain-sync-move-regression.json``
(command/result metadata only, never environment dumps) in a ``finally``, so
raw failure state survives any hard assertion.

Gate semantics (W1): while issue #125 is open this gate FAILS — the runtime
test hard-asserts the ``fixed`` classification for BOTH cases and raises with
the full raw evidence dump (report path + per-surface outputs + git
classification + refresh evidence + pipeline location). The classification
oracle recognizes ``fixed`` and the recorded ``present`` signature; every
other outcome is its exact ``_failure_signature`` rather than a generic
catch-all label. ``inconclusive`` only when the harness cannot establish the
scenario (a precondition failure), which is also a hard failure with evidence.
When the owning fix lands, this gate turns green unchanged.

Safety model (root AGENTS.md issue #110 non-negotiables + issue #127):
  - disposable Compose project with disposable agent-state/credentials
    mounts, repo ``.env`` bypassed, test-isolation overlay always last
  - every in-container command runs as the hermes runtime user
    (``run_as_hermes``), never root
  - gbrain writes go through the supported public ``gbrain`` surface; the
    only operator path used is the normal ``josemar-gbrain refresh`` (shared
    lock, canonical wrapper)
  - canonical schema/template seeding (``seed_source_state``) and synthetic
    vault committed as hermes — no production data, no SQL mutation
  - unconditional final cleanup ``down -v --remove-orphans``

Runtime execution is gated strictly on ``RUN_DOCKER_TESTS=1`` AND
``RUN_GBRAIN_SYNC_MOVE_REGRESSION=1`` and skips when the docker CLI is
absent. Fast host-side gate/structure/classification tests in this module
always run and need no Docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from .gbrain_conformance_scenarios import PINNED_GBRAIN_VERSION
from .gbrain_conformance_support import (
    CONFORMANCE_EMPTY_ENV_KEYS,
    CommandEvidence,
    GbrainConformanceRuntime,
    conformance_report_dir,
    write_report,
)
from .helpers import REPO_ROOT, docker_available


# ---------------------------------------------------------------------------
# Deterministic W1 facts (both cases run inside ONE disposable runtime)
# ---------------------------------------------------------------------------

CASE_A_TAG = "case_a"
CASE_B_TAG = "case_b"

# Case A: sync-originated page (file committed, indexed only via refresh).
CASE_A_OLD_SLUG = "inbox/sync-originated-a"
CASE_A_NEW_SLUG = "notes/sync-originated-a"
CASE_A_TOKEN = "conformance-sync-move-token-a"

# Case B: capture-originated page (public write-through, committed).
CASE_B_OLD_SLUG = "inbox/capture-originated-b"
CASE_B_NEW_SLUG = "notes/capture-originated-b"
CASE_B_TOKEN = "conformance-sync-move-token-b"

def _sync_move_regression_enabled() -> bool:
    """Strict opt-in gate: RUN_DOCKER_TESTS=1 AND
    RUN_GBRAIN_SYNC_MOVE_REGRESSION=1 AND a docker CLI is available."""
    return (
        os.getenv("RUN_DOCKER_TESTS") == "1"
        and os.getenv("RUN_GBRAIN_SYNC_MOVE_REGRESSION") == "1"
        and docker_available()
    )


# ---------------------------------------------------------------------------
# Pure W1 oracles (host-side testable, no Docker)
# ---------------------------------------------------------------------------


def _classify_git_mv_probe(
    *,
    moved_file_exists: bool,
    new_resolves: bool,
    old_resolves: bool,
    token_search_resolves: bool,
) -> str:
    """Issue #125 W1 classification oracle:

      - ``fixed``: the new slug resolves, the unique body token search
        resolves, and the old slug no longer serves the moved page.
      - ``present``: the moved file still exists but neither the old nor
        the new slug resolves and the unique token search does not resolve
        either — issue #125's recorded failure mode.
      - every other outcome: the exact ``_failure_signature`` (for example,
        ``old_slug_still_live`` or ``new_get_ok_token_search_missing``),
        never a generic catch-all classification.
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
    return _failure_signature(
        moved_file_exists=moved_file_exists,
        new_resolves=new_resolves,
        old_resolves=old_resolves,
        token_search_resolves=token_search_resolves,
    )


def _failure_signature(
    *,
    moved_file_exists: bool,
    new_resolves: bool,
    old_resolves: bool,
    token_search_resolves: bool,
) -> str:
    """Specific decomposition of a NON-fixed probe outcome — the W1 gate
    never reports a generic label. Returns a deterministic ``+``-joined set
    of concrete observed symptoms (``"none"`` when the probe is fixed).

    Rules are ordered and mutually exclusive per symptom so the signature is
    stable for a given evidence vector:
      - ``moved_file_missing``: the moved file no longer exists on disk
      - ``old_slug_still_live``: the old slug still serves the page
      - ``new_get_ok_token_search_missing``: new slug get works but the
        unique-token search does not resolve the page
      - ``stale_duplicate``: BOTH slugs resolve and the token search
        resolves (duplicated stale index entry)
      - ``token_search_only``: neither slug resolves but the token search
        still finds the page
      - ``no_resolution_at_all``: neither slug nor the token search resolve
        (the recorded #125 signature when the file still exists)
    """
    if new_resolves and token_search_resolves and not old_resolves:
        return "none"
    if new_resolves and token_search_resolves and old_resolves:
        return "stale_duplicate"
    parts: list[str] = []
    if not moved_file_exists:
        parts.append("moved_file_missing")
    if old_resolves:
        parts.append("old_slug_still_live")
    if new_resolves and not token_search_resolves:
        parts.append("new_get_ok_token_search_missing")
    if not new_resolves and not old_resolves and token_search_resolves:
        parts.append("token_search_only")
    if not new_resolves and not old_resolves and not token_search_resolves:
        parts.append("no_resolution_at_all")
    return "+".join(parts) or "unknown"


def _pipeline_location(
    *,
    moved_file_exists: bool,
    new_resolves: bool,
    old_resolves: bool,
    token_search_resolves: bool,
) -> str:
    """Where in the supported retrieval surface the failure sits, stated
    from the public evidence alone (no SQL, no internals). Used to point the
    W1 reviewer at the failing stage of the refresh pipeline."""
    if new_resolves and token_search_resolves and not old_resolves:
        return "no failure (fixed)"
    if not moved_file_exists:
        return "sync/extract lost the moved file entirely (file missing after refresh)"
    if old_resolves:
        return "stale index entry: old slug still resolves after refresh"
    if not new_resolves and not token_search_resolves:
        return "refresh did not re-route the page: new-slug get AND token search both miss"
    if new_resolves and not token_search_resolves:
        return "slug resolution updated but search index stale: token search misses"
    if token_search_resolves and not new_resolves:
        return "search index updated but slug resolution stale: new-slug get misses"
    return "partial/stale index state (see signature)"


# ---------------------------------------------------------------------------
# Docker-gated base setup (single disposable runtime, both cases)
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    _sync_move_regression_enabled(),
    "set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_SYNC_MOVE_REGRESSION=1 with a docker CLI",
)
class GbrainSyncMoveRegressionTestCase(unittest.TestCase):
    """Shared base setup: disposable Compose project + synthetic vault +
    operator activation + version/ref evidence. Final teardown is
    unconditional ``down -v --remove-orphans``."""

    def setUp(self) -> None:
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {
            CASE_A_TAG: "harness_failed",
            CASE_B_TAG: "harness_failed",
        }
        self._gbrain_version: str | None = None
        self._gbrain_source_ref: str | None = None
        self._report_path: Path | None = None

        self.runtime = GbrainConformanceRuntime()
        # Pre-start source-state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # Baseline Hermes-only build/start at the committed Dockerfile pin.
        self.runtime.up("hermes", timeout=900)
        # Wait for the exact hermes-writable surface before any exec probe.
        self.runtime.wait_until_hermes_writable(timeout=120)
        # Isolation safety checks: empty credentials + disabled owned jobs.
        self._evidence.append(self._assert_no_credentials())
        self._evidence.append(self.runtime.assert_owned_jobs_disabled())
        # Synthetic vault init committed as the hermes runtime user.
        self._evidence.append(self.runtime.init_synthetic_vault())
        # Operator activation (canonical path; hard harness precondition).
        ev = self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "reindex")
        # Version/ref evidence (issue #125 W1 evidence contract).
        ev = self.runtime.run_as_hermes("gbrain", "status", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self._gbrain_version = json.loads(ev.stdout).get("version")
        self.assertEqual(
            self._gbrain_version,
            PINNED_GBRAIN_VERSION,
            "W1 gate runs against the pinned gbrain; a version mismatch is a "
            "harness/upgrade anomaly",
        )
        ev = self.runtime.run_as_hermes("cat", "/opt/gbrain/.git/HEAD")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self._gbrain_source_ref = ev.stdout.strip()
        self.assertRegex(self._gbrain_source_ref, r"^[0-9a-f]{40}$")
        self.assertEqual(
            self._gbrain_source_ref,
            self.runtime.baseline_gbrain_ref(),
            "W1 must exercise the exact committed Dockerfile GBRAIN_REF",
        )

    def tearDown(self) -> None:
        # Unconditional final cleanup: down -v --remove-orphans.
        self.runtime.cleanup()

    # --- safety helpers ---------------------------------------------------

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

    # --- evidence helper --------------------------------------------------

    def _ev(
        self,
        *command: str,
        timeout: int = 120,
    ) -> CommandEvidence:
        """Run an in-container command as the hermes runtime user and append
        its complete evidence BEFORE any assertion, so raw failure state is
        always preserved in the report."""
        ev = self.runtime.run_as_hermes(*command, check=False, timeout=timeout)
        self._evidence.append(ev)
        return ev

    # --- report -----------------------------------------------------------

    def _write_report(self, name: str = "gbrain-sync-move-regression") -> None:
        """Persist the W1 report: baseline ref, gbrain version/source ref,
        per-case classification matrix, and complete raw evidence (argv, rc,
        stdout, stderr, elapsed — never environment dumps). The W2 safety
        contract test writes its own report under a distinct name so the W1
        characterization evidence is never clobbered."""
        metadata: dict[str, object] = {
            "baseline_ref": self.runtime.baseline_gbrain_ref(),
            "pinned_gbrain_version": PINNED_GBRAIN_VERSION,
            "gbrain_version": self._gbrain_version,
            "gbrain_source_ref": self._gbrain_source_ref,
            "matrix": dict(self._matrix),
            "cases": {
                CASE_A_TAG: {
                    "origin": "sync-originated",
                    "old_slug": CASE_A_OLD_SLUG,
                    "new_slug": CASE_A_NEW_SLUG,
                    "token": CASE_A_TOKEN,
                },
                CASE_B_TAG: {
                    "origin": "capture-originated",
                    "old_slug": CASE_B_OLD_SLUG,
                    "new_slug": CASE_B_NEW_SLUG,
                    "token": CASE_B_TOKEN,
                },
            },
        }
        results: dict[str, dict[str, object]] = {}
        for case in getattr(self, "_case_facts", {}).values():
            results[case["tag"]] = {
                "classification": case["classification"],
                "signature": case["signature"],
                "classification_after_second_refresh": case["classification2"],
                "signature_after_second_refresh": case["signature2"],
                "git_rename_status": case["git_rename_status"],
                "content_unchanged": case["content_unchanged"],
                "moved_file_exists": case["moved_file_exists"],
                "old_file_absent": case["old_file_absent"],
                "pipeline_location": case["pipeline_location"],
            }
        metadata["results"] = results
        w2_matrix = getattr(self, "_w2_matrix", None)
        if w2_matrix is not None:
            metadata["w2_safety_matrix"] = dict(w2_matrix)
        evidence = list(self._evidence) + list(getattr(self, "_w2_evidence", []))
        self._report_path = write_report(
            conformance_report_dir(),
            name,
            evidence,
            metadata=metadata,
        )


class GbrainSyncMoveRegressionRuntimeTests(GbrainSyncMoveRegressionTestCase):
    """W1 runtime characterization (Docker-gated via the base class): both
    origin cases, raw evidence preserved, hard ``fixed`` gate while issue
    #125 is open."""

    def test_git_mv_characterization_both_origins(self) -> None:
        self._case_facts: dict[str, dict] = {}
        harness_failures: list[str] = []
        try:
            try:
                self._case_facts[CASE_A_TAG] = self._run_case(
                    CASE_A_TAG, sync_originated=True
                )
            except AssertionError as exc:
                harness_failures.append(f"case a harness precondition: {exc}")
            try:
                self._case_facts[CASE_B_TAG] = self._run_case(
                    CASE_B_TAG, sync_originated=False
                )
            except AssertionError as exc:
                harness_failures.append(f"case b harness precondition: {exc}")
        finally:
            self._write_report()
        # The W1 gate: while #125 is open the classification is not fixed and
        # the assertion fails with the raw evidence dump. Both cases must be
        # characterized (each exception message carries its own raw state).
        for case in self._case_facts.values():
            self._matrix[case["tag"]] = case["classification"]
            self.assertEqual(
                case["classification"],
                "fixed",
                self._case_failure_message(case),
            )
        if harness_failures:
            raise AssertionError(
                "W1 harness could not establish one or both cases (evidence "
                f"preserved in {self._report_path}): "
                + " | ".join(harness_failures)
            )

    # ------------------------------------------------------------------
    # Per-case evidence pipeline
    # ------------------------------------------------------------------

    def _run_case(self, tag: str, *, sync_originated: bool) -> dict:
        """Characterize ONE origin case end-to-end and return its facts dict.

        Hard assertions are limited to harness preconditions (create,
        commit, pre-move indexing proof, git mv, file existence, unchanged
        content, refresh success, supported metadata surfaces). The probe
        outcomes themselves are soft: they are recorded raw and classified.
        Every command's complete evidence is appended before any assertion.
        """
        old_slug, new_slug, token = self._case_slugs(tag)
        facts: dict = {
            "tag": tag,
            "origin": "sync-originated" if sync_originated else "capture-originated",
            "old_slug": old_slug,
            "new_slug": new_slug,
            "token": token,
        }

        # --- 1. Create the page through the origin's supported path --------
        if sync_originated:
            create = self._ev(
                "sh", "-lc",
                "set -eu\n"
                "cd /opt/data/obsidian\n"
                "mkdir -p inbox\n"
                "cat > " + old_slug + ".md <<'MD'\n"
                "# Sync-Originated Move Probe\n"
                "\n"
                "Unique body token: " + token + ".\n"
                "MD\n"
                "git add " + old_slug + ".md\n"
                "git commit -qm 'w1 sync-originated probe create'\n",
            )
            self.assertEqual(create.returncode, 0, create.stderr)
            facts["create"] = create
            # Index the committed file through the normal sync path ONLY.
            index_refresh = self._ev(
                "josemar-gbrain", "refresh", timeout=300,
            )
            self.assertEqual(index_refresh.returncode, 0, index_refresh.stderr)
            self.assertIs(
                json.loads(index_refresh.stdout).get("success"), True,
            )
            facts["index_refresh"] = index_refresh
        else:
            create = self._ev(
                "gbrain", "capture",
                "Capture-originated move probe with unique token " + token + ".",
                "--slug", old_slug, "--json",
            )
            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertIs(json.loads(create.stdout).get("written"), True)
            facts["create"] = create
            commit = self._ev(
                "sh", "-lc",
                "set -eu\n"
                "cd /opt/data/obsidian\n"
                "git add -A\n"
                "git commit -qm 'w1 capture-originated probe commit'\n",
            )
            self.assertEqual(commit.returncode, 0, commit.stderr)
            facts["create_commit"] = commit

        # --- 2. Pre-move: the page must resolve at the OLD slug -----------
        pre_move_get = self._ev("gbrain", "get", old_slug)
        self.assertEqual(pre_move_get.returncode, 0, pre_move_get.stderr)
        self.assertIn(token, pre_move_get.stdout, pre_move_get.stdout)
        facts["pre_move_get"] = pre_move_get

        # --- 3. Pre-move git facts + content hash -------------------------
        facts["pre_move_git_log"] = self._ev(
            "sh", "-lc",
            "set -eu; cd /opt/data/obsidian; git log --oneline -2",
        )
        self.assertEqual(facts["pre_move_git_log"].returncode, 0)
        facts["pre_move_git_status"] = self._ev(
            "sh", "-lc",
            "set -eu; cd /opt/data/obsidian; git diff --name-status -M HEAD~1 HEAD",
        )
        self.assertEqual(facts["pre_move_git_status"].returncode, 0)
        facts["pre_move_hash"] = self._file_sha256(old_slug)

        # --- 4. Committed UNCHANGED git mv --------------------------------
        mv = self._ev(
            "sh", "-lc",
            "set -eu\n"
            "cd /opt/data/obsidian\n"
            "git mv " + old_slug + ".md " + new_slug + ".md\n"
            "git commit -qm 'w1 unchanged git mv probe'\n",
        )
        self.assertEqual(mv.returncode, 0, mv.stderr)
        facts["mv"] = mv

        # --- 5. Moved existence + unchanged content (hard) ----------------
        new_exists = self._ev("test", "-f", "/opt/data/obsidian/" + new_slug + ".md")
        self.assertEqual(new_exists.returncode, 0, "moved file must exist")
        facts["moved_file_exists"] = True
        old_absent = self._ev(
            "sh", "-lc", "test ! -f /opt/data/obsidian/" + old_slug + ".md",
        )
        self.assertEqual(old_absent.returncode, 0, "old path must be gone")
        facts["old_file_absent"] = True
        facts["post_move_hash"] = self._file_sha256(new_slug)
        facts["content_unchanged"] = (
            facts["pre_move_hash"].stdout.strip()
            == facts["post_move_hash"].stdout.strip()
        )
        self.assertTrue(
            facts["content_unchanged"],
            "W1 requires an UNCHANGED move: pre-move sha256 "
            f"{facts['pre_move_hash'].stdout.strip()!r} != post-move "
            f"{facts['post_move_hash'].stdout.strip()!r}",
        )

        # --- 6. Post-move git facts: git's own rename classification ------
        facts["post_move_git_log"] = self._ev(
            "sh", "-lc",
            "set -eu; cd /opt/data/obsidian; git log --oneline -2",
        )
        self.assertEqual(facts["post_move_git_log"].returncode, 0)
        facts["post_move_git_status"] = self._ev(
            "sh", "-lc",
            "set -eu; cd /opt/data/obsidian; git diff --name-status -M HEAD~1 HEAD",
        )
        self.assertEqual(facts["post_move_git_status"].returncode, 0)
        status_lines = [
            line for line in facts["post_move_git_status"].stdout.splitlines()
            if line.strip()
        ]
        self.assertTrue(status_lines, "git diff must report the move")
        facts["git_rename_status"] = status_lines[0].split("\t")[0]
        self.assertTrue(
            facts["git_rename_status"].startswith("R"),
            "git must classify the unchanged move as a rename, got "
            f"{facts['git_rename_status']!r}",
        )

        # --- 7. Normal operator refresh #1 (hard success) -----------------
        facts["refresh1"] = self._ev("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(facts["refresh1"].returncode, 0, facts["refresh1"].stderr)
        self.assertIs(json.loads(facts["refresh1"].stdout).get("success"), True)

        # --- 8. Probe: old/new get + unique-token search (soft) -----------
        facts["get_new"] = self._ev("gbrain", "get", new_slug)
        facts["get_old"] = self._ev("gbrain", "get", old_slug)
        facts["search"] = self._ev(
            "gbrain", "search", token, "--limit", "5",
        )
        self._classify(facts)

        # --- 9. Second refresh postconditions -----------------------------
        facts["refresh2"] = self._ev("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(facts["refresh2"].returncode, 0, facts["refresh2"].stderr)
        self.assertIs(json.loads(facts["refresh2"].stdout).get("success"), True)
        facts["get_new2"] = self._ev("gbrain", "get", new_slug)
        facts["get_old2"] = self._ev("gbrain", "get", old_slug)
        facts["search2"] = self._ev(
            "gbrain", "search", token, "--limit", "5",
        )
        self._classify(facts, second=True)

        # --- 10. Supported metadata surfaces (hard: they must work) -------
        facts["sources"] = self._ev("gbrain", "sources", "list", "--json")
        self.assertEqual(facts["sources"].returncode, 0, facts["sources"].stderr)
        facts["doctor"] = self._ev("gbrain", "doctor", "--json", timeout=120)
        self.assertEqual(facts["doctor"].returncode, 0, facts["doctor"].stderr)

        self._matrix[tag] = facts["classification"]
        return facts

    @staticmethod
    def _case_slugs(tag: str) -> tuple[str, str, str]:
        if tag == CASE_A_TAG:
            return CASE_A_OLD_SLUG, CASE_A_NEW_SLUG, CASE_A_TOKEN
        if tag == CASE_B_TAG:
            return CASE_B_OLD_SLUG, CASE_B_NEW_SLUG, CASE_B_TOKEN
        raise AssertionError(f"unknown case tag: {tag}")

    def _file_sha256(self, slug: str) -> CommandEvidence:
        """sha256 of the vault file at ``slug`` (as hermes; content-equality
        proof for the unchanged move)."""
        return self._ev(
            "sh", "-lc",
            "python3 -c 'import hashlib,sys;"
            'print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())\' '
            "/opt/data/obsidian/" + slug + ".md",
        )

    def _classify(self, facts: dict, *, second: bool = False) -> None:
        """Compute classification + specific signature + pipeline location
        from the probe evidence. ``second=True`` classifies the post-second-
        refresh re-probe (postconditions)."""
        suffix = "2" if second else ""
        new_ev = facts["get_new" + suffix]
        old_ev = facts["get_old" + suffix]
        search_ev = facts["search" + suffix]
        new_resolves = new_ev.returncode == 0 and facts["token"] in new_ev.stdout
        old_resolves = old_ev.returncode == 0 and facts["token"] in old_ev.stdout
        token_search_resolves = (
            search_ev.returncode == 0 and facts["token"] in search_ev.stdout
        )
        moved = facts["moved_file_exists"]
        classification = _classify_git_mv_probe(
            moved_file_exists=moved,
            new_resolves=new_resolves,
            old_resolves=old_resolves,
            token_search_resolves=token_search_resolves,
        )
        signature = _failure_signature(
            moved_file_exists=moved,
            new_resolves=new_resolves,
            old_resolves=old_resolves,
            token_search_resolves=token_search_resolves,
        )
        key = "classification" if not second else "classification2"
        sig_key = "signature" if not second else "signature2"
        loc_key = "pipeline_location" if not second else "pipeline_location2"
        facts[key] = classification
        facts[sig_key] = signature
        facts[loc_key] = _pipeline_location(
            moved_file_exists=moved,
            new_resolves=new_resolves,
            old_resolves=old_resolves,
            token_search_resolves=token_search_resolves,
        )

    # ------------------------------------------------------------------
    # Raw failure-state dump (never a generic label)
    # ------------------------------------------------------------------

    def _case_failure_message(self, facts: dict) -> str:
        def fmt(ev: CommandEvidence) -> str:
            return (
                f"rc={ev.returncode} stdout={ev.stdout!r} stderr={ev.stderr!r}"
            )

        return (
            "W1 issue #125 gate: case "
            f"{facts['tag']} ({facts['origin']}) classification="
            f"{facts['classification']!r} signature={facts['signature']!r}\n"
            f"  second-refresh postconditions: classification="
            f"{facts.get('classification2', 'n/a')!r} signature="
            f"{facts.get('signature2', 'n/a')!r}\n"
            f"  pipeline location: {facts['pipeline_location']}\n"
            f"  git rename classification: {facts['git_rename_status']!r} "
            f"(content unchanged: {facts['content_unchanged']})\n"
            f"  moved file exists: {facts['moved_file_exists']}; "
            f"old file absent: {facts['old_file_absent']}\n"
            f"  pre-move git log: {facts['pre_move_git_log'].stdout!r}\n"
            f"  pre-move status: {facts['pre_move_git_status'].stdout!r}\n"
            f"  post-move git log: {facts['post_move_git_log'].stdout!r}\n"
            f"  post-move status: {facts['post_move_git_status'].stdout!r}\n"
            f"  refresh #1: {fmt(facts['refresh1'])}\n"
            f"  refresh #2: {fmt(facts['refresh2'])}\n"
            f"  get new: {fmt(facts['get_new'])}\n"
            f"  get old: {fmt(facts['get_old'])}\n"
            f"  search token: {fmt(facts['search'])}\n"
            f"  get new (post refresh2): {fmt(facts['get_new2'])}\n"
            f"  get old (post refresh2): {fmt(facts['get_old2'])}\n"
            f"  search token (post refresh2): {fmt(facts['search2'])}\n"
            f"  full raw evidence (report): {self._report_path}\n"
        )

    # ------------------------------------------------------------------
    # W2 two-part safety contracts (maintainer-approved design, issue #125)
    # ------------------------------------------------------------------

    def test_identity_and_stale_pass_safety_contracts(self) -> None:
        """Mechanical proofs for the W2 two-part patch on the disposable
        runtime (synthetic isolated state only). Scenarios run sequentially
        in one runtime; every scenario runs to completion, failures are
        collected, and the report is written before the first failure is
        re-raised."""
        self._w2_evidence: list[CommandEvidence] = []
        self._w2_matrix: dict[str, str] = {}
        failures: list[str] = []

        def run(name: str, fn) -> None:
            self._w2_matrix[name] = "fail"
            try:
                fn()
                self._w2_matrix[name] = "pass"
            except AssertionError as exc:
                failures.append(f"{name}: {exc}")

        try:
            run("identity_established", self._s2_identity_established)
            run("never_overwrite_non_null", self._s2_never_overwrite_non_null)
            run("write_through_disabled_null_never_swept", self._s2_disabled_write_through_null)
            run("write_through_failed_null_never_swept", self._s2_failed_write_through_null)
            run("never_committed_retained", self._s2_never_committed_retained)
            run("import_failure_preserves_old_no_advance", self._s2_import_failure_blocks_sweep)
            run("delete_failure_blocks_bookmark_no_autoskip", self._s2_delete_failure_blocks_bookmark)
            run("rename_follow_failure_blocks_and_retries", self._s2_rename_follow_failure_blocks_and_retries)
            run("unexpected_prep_failure_blocks_and_retries", self._s2_unexpected_prep_failure_blocks_and_retries)
            run("committed_deletion_reconciles", self._s2_committed_deletion)
            run("repeated_refresh_no_duplicate", self._s2_repeated_refresh_no_duplicate)
            run("mass_valve_trips", self._s2_mass_valve)
            run("no_mass_escape_in_refresh_env", self._s2_mass_escape_hatch_absent)
        finally:
            self._write_report(name="gbrain-sync-move-regression-w2")
        if failures:
            raise AssertionError(
                "W2 safety-contract scenario failures (evidence in "
                f"{self._report_path}): " + " | ".join(failures)
            )

    # --- W2 helpers --------------------------------------------------------

    def _w2(self, *command: str, timeout: int = 180) -> CommandEvidence:
        ev = self.runtime.run_as_hermes(*command, check=False, timeout=timeout)
        self._w2_evidence.append(ev)
        return ev

    def _pglite_query(self, sql: str, params: list) -> list[dict]:
        """Run a query against the DISPOSABLE runtime's PGLite via the
        in-container bun + @electric-sql/pglite. SYNTHETIC isolated state
        only (read-back evidence, plus synthetic fault-injection DDL on the
        disposable brain — the delete-failure trigger, the F1 rename-follow
        trigger, and the F2 pages.source_path column rename — each
        dropped/restored in the same scenario); never production data,
        never the production paths. SQL
        must use $N placeholders for ALL literals so the generated bun code
        contains no single quotes (shell-safe)."""
        import json as _json
        import shlex as _shlex
        code = (
            'import { PGlite } from "@electric-sql/pglite";'
            'const db = new PGlite("/opt/data/.gbrain/brain.pglite");'
            "const r = await db.query("
            + _shlex.quote(sql)
            + ", JSON.parse("
            + _shlex.quote(_json.dumps(params))
            + "));console.log(JSON.stringify(r.rows ?? []));await db.close();"
        )
        script = "cd /opt/gbrain && bun --silent -e " + _shlex.quote(code)
        ev = self._w2("sh", "-lc", script)
        self.assertEqual(ev.returncode, 0, ev.stderr + ev.stdout)
        return _json.loads(ev.stdout)

    def _w2_source_path(self, slug: str) -> str | None:
        rows = self._pglite_query(
            "SELECT source_path FROM pages WHERE source_id = $1 AND slug = $2 AND deleted_at IS NULL",
            ["default", slug],
        )
        return rows[0]["source_path"] if rows else None

    def _w2_native_config(self, key: str, value: str) -> CommandEvidence:
        """Set an operator-only config key via the native gbrain binary with
        the wrapper's canonical env (the public `gbrain` adapter refuses
        `config`; the wrapper itself uses this exact path). Synthetic state
        only."""
        return self._w2(
            "sh", "-lc",
            "export GBRAIN_HOME=/opt/data GBRAIN_BRAIN_REPO=/opt/data/obsidian "
            "GBRAIN_SKIP_STARTUP_HOOKS=1; "
            "/opt/josemar/libexec/gbrain-native config set " + key + " " + value,
        )

    def _last_commit(self) -> str:
        ev = self._w2("gbrain", "status", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        sources = json.loads(ev.stdout).get("sync", {}).get("sources", [])
        self.assertEqual(len(sources), 1, ev.stdout)
        return sources[0].get("last_commit", "")

    # --- W2 scenarios ------------------------------------------------------

    def _s2_identity_established(self) -> None:
        """Part 1: after a successful contained write-through, the live row
        gets the owning-source-relative vetted markdown target as
        source_path (identity established)."""
        slug = "inbox/id-page"
        ev = self._w2(
            "gbrain", "capture", "identity probe token conformance-id-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        self.assertEqual(
            self._w2_source_path(slug), "inbox/id-page.md",
            "identity must be established from the real write-through path",
        )

    def _s2_never_overwrite_non_null(self) -> None:
        """Part 1: identity is established ONLY while NULL; a non-NULL
        source_path is never overwritten — even when a later write-through
        writes a different file."""
        self._w2(
            "sh", "-lc",
            "cat > /opt/data/obsidian/notes/identity-file-x.md <<'MD'\n"
            "# Identity File X\n\nidentity-x-token\nMD\n",
        )
        ev = self._w2(
            "gbrain", "capture", "--file", "/opt/data/obsidian/notes/identity-file-x.md",
            "--slug", "inbox/id-file-page", "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        # Identity = the ACTUAL written in-vault file's relative path, not a
        # slug-derived fabrication.
        self.assertEqual(self._w2_source_path("inbox/id-file-page"), "notes/identity-file-x.md")
        self._w2(
            "sh", "-lc",
            "cat > /opt/data/obsidian/notes/identity-file-y.md <<'MD'\n"
            "# Identity File Y\n\nidentity-y-token\nMD\n",
        )
        ev = self._w2(
            "gbrain", "capture", "--file", "/opt/data/obsidian/notes/identity-file-y.md",
            "--slug", "inbox/id-file-page", "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        self.assertEqual(
            self._w2_source_path("inbox/id-file-page"), "notes/identity-file-x.md",
            "non-NULL identity must never be overwritten",
        )

    def _s2_disabled_write_through_null(self) -> None:
        """Part 1: a disabled/skipped write-through leaves source_path NULL
        (no fabricated identity), no file on disk, and the NULL row is never
        swept by the stale pass."""
        self._w2_native_config("sync.write_through", "false")
        slug = "inbox/dbonly-page"
        ev = self._w2(
            "gbrain", "capture", "dbonly probe token conformance-dbonly-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), False, "write-through must be skipped")
        absent = self._w2("test", "-f", "/opt/data/obsidian/inbox/dbonly-page.md")
        self.assertNotEqual(absent.returncode, 0, "no file may be written")
        self.assertIsNone(self._w2_source_path(slug), "identity must stay NULL")
        g = self._w2("gbrain", "get", slug)
        self.assertEqual(g.returncode, 0, g.stderr)
        self.assertIn("conformance-dbonly-token", g.stdout)
        # Re-enable write-through, commit a change so the stale pass runs.
        self._w2_native_config("sync.write_through", "true")
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && echo '# Noise s2' > notes/noise-s2.md "
            "&& git add notes/noise-s2.md && git commit -qm 's2 noise'",
        )
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIsNone(self._w2_source_path(slug), "NULL identity must never be swept")
        g = self._w2("gbrain", "get", slug)
        self.assertEqual(g.returncode, 0, g.stderr)

    def _s2_failed_write_through_null(self) -> None:
        """Part 1: a FAILED write-through (target blocked by a directory)
        leaves source_path NULL, retains the page, and is never swept."""
        slug = "inbox/blocked-write"
        self._w2("sh", "-lc", "mkdir -p /opt/data/obsidian/inbox/blocked-write.md")
        ev = self._w2(
            "gbrain", "capture", "blocked write token conformance-blocked-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), False, "write-through must fail")
        self.assertIsNone(self._w2_source_path(slug), "identity must stay NULL")
        g = self._w2("gbrain", "get", slug)
        self.assertEqual(g.returncode, 0, g.stderr)
        self.assertIn("conformance-blocked-token", g.stdout)
        self._w2("sh", "-lc", "rmdir /opt/data/obsidian/inbox/blocked-write.md")

    def _s2_never_committed_retained(self) -> None:
        """Part 2: a write-through file that disappears WITHOUT ever being
        committed is retained (the #2426 ever-committed proof refuses the
        delete) even though the stale pass runs."""
        slug = "inbox/nc-page"
        ev = self._w2(
            "gbrain", "capture", "never-committed token conformance-nc-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        self.assertEqual(self._w2_source_path(slug), "inbox/nc-page.md")
        self._w2("sh", "-lc", "rm -f /opt/data/obsidian/inbox/nc-page.md")
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && echo '# Noise nc' > notes/noise-nc.md "
            "&& git add notes/noise-nc.md && git commit -qm 's4 noise'",
        )
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        g = self._w2("gbrain", "get", slug)
        self.assertEqual(g.returncode, 0, "never-committed row must be retained: " + g.stderr)
        self.assertIn("conformance-nc-token", g.stdout)

    def _s2_import_failure_blocks_sweep(self) -> None:
        """Part 2: an import failure anywhere in the phase disables the
        stale pass AND blocks the bookmark; once the failure is fixed, the
        pass sweeps the stale row (destination success precedes old
        deletion)."""
        slug = "inbox/importfail-page"
        ev = self._w2(
            "gbrain", "capture", "import-fail token conformance-if-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git add inbox/importfail-page.md "
            "&& git commit -qm 's5 capture' "
            "&& git mv inbox/importfail-page.md notes/importfail-page.md "
            "&& git commit -qm 's5 move'",
        )
        before = self._last_commit()
        # Junk file: content-sanity hard block (Cloudflare pattern) fails the
        # import — the default disposition quarantines, so the operator path
        # opts into reject first (synthetic state).
        self._w2_native_config("content_sanity.junk_disposition", "reject")
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && cat > inbox/junk-s5.md <<'MD'\n"
            "# Attention Required! | Cloudflare\n\n"
            "Attention Required! | Cloudflare\n"
            "This website is using a security service to protect itself from online attacks.\n"
            "The service requires full JavaScript support in order to view this website.\n"
            "MD\n"
            "git add inbox/junk-s5.md && git commit -qm 's5 junk'",
        )
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertNotEqual(ev.returncode, 0, "sync must fail on the junk import")
        # Old row preserved: the stale pass must not run on a failed phase.
        g = self._w2("gbrain", "get", slug)
        self.assertEqual(g.returncode, 0, "old row must be preserved on import failure: " + g.stderr)
        self.assertIn("conformance-if-token", g.stdout)
        # Bookmark must not advance.
        after = self._last_commit()
        self.assertEqual(after, before, "last_commit must not advance on import failure")
        # Fix the failure; the next refresh sweeps the stale row.
        self._w2_native_config("content_sanity.junk_disposition", "quarantine")
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git rm -q inbox/junk-s5.md && git commit -qm 's5 junk rm'",
        )
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        g = self._w2("gbrain", "get", slug)
        self.assertNotEqual(g.returncode, 0, "old slug must be swept once the pass runs")
        g = self._w2("gbrain", "get", "notes/importfail-page")
        self.assertEqual(g.returncode, 0, g.stderr)
        self.assertIn("conformance-if-token", g.stdout)

    def _s2_delete_failure_blocks_bookmark(self) -> None:
        """Part 2: a stale-pass DELETE failure creates the non-skippable
        `<stale:…>` sentinel: the refresh hard-blocks (nonzero exit), the
        bookmark does NOT advance, and the old row is retained; once the
        blocker is removed, the next refresh sweeps the stale row and
        advances (destination-first convergence)."""
        slug = "inbox/delfail-page"
        ev = self._w2(
            "gbrain", "capture", "delete-fail token conformance-df-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        self.assertEqual(self._w2_source_path(slug), "inbox/delfail-page.md")
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git add inbox/delfail-page.md "
            "&& git commit -qm 's8 capture' "
            "&& git mv inbox/delfail-page.md notes/delfail-page.md "
            "&& git commit -qm 's8 move'",
        )
        before = self._last_commit()
        try:
            # Synthetic delete blocker (disposable PGLite only): a BEFORE
            # DELETE trigger on pages that raises, so the stale-pass delete
            # fails. Installed INSIDE the try so the finally always cleans up.
            self._pglite_query(
                "CREATE FUNCTION josemar_block_pages_delete() RETURNS trigger AS $$ "
                "BEGIN PERFORM 1/0; RETURN NEW; END $$ LANGUAGE plpgsql",
                [],
            )
            self._pglite_query(
                "CREATE TRIGGER josemar_block_pages_delete BEFORE DELETE ON pages "
                "FOR EACH ROW EXECUTE FUNCTION josemar_block_pages_delete()",
                [],
            )
            # First blocked run: delete failure → sentinel → hard block.
            ev = self._w2("josemar-gbrain", "refresh", timeout=300)
            self.assertNotEqual(ev.returncode, 0, "delete failure must block the refresh")
            self.assertEqual(
                self._last_commit(), before,
                "last_commit must not advance on delete failure",
            )
            # Second run with the blocker STILL installed: still blocked (the
            # sentinel is never auto-skipped across retries).
            ev = self._w2("josemar-gbrain", "refresh", timeout=300)
            self.assertNotEqual(ev.returncode, 0, "sentinel must not be auto-skipped")
            self.assertEqual(
                self._last_commit(), before,
                "last_commit must stay frozen while the blocker persists",
            )
            # Old row retained (delete failed).
            g = self._w2("gbrain", "get", slug)
            self.assertEqual(g.returncode, 0, g.stderr)
            self.assertIn("conformance-df-token", g.stdout)
        finally:
            self._pglite_query(
                "DROP TRIGGER IF EXISTS josemar_block_pages_delete ON pages", [],
            )
            self._pglite_query(
                "DROP FUNCTION IF EXISTS josemar_block_pages_delete", [],
            )
        # Blocker removed: next refresh sweeps the stale row and advances.
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        g = self._w2("gbrain", "get", slug)
        self.assertNotEqual(g.returncode, 0, "old slug must be swept once the delete succeeds")
        g = self._w2("gbrain", "get", "notes/delfail-page")
        self.assertEqual(g.returncode, 0, g.stderr)
        self.assertIn("conformance-df-token", g.stdout)

    def _s2_rename_follow_failure_blocks_and_retries(self) -> None:
        """F1 (merge-blocking finding): a rename source_path follow failure
        must integrate with the EXISTING rename convergence/checkpoint
        state — hard-block the bookmark, preserve last_commit, NOT bank the
        destination, NOT clear its own `<rename:…>` sentinel — and the next
        run must REPLAY the destination rename, retry the follow, converge
        and clear the sentinel.

        Fault injection: a synthetic BEFORE UPDATE trigger on pages (the
        established disposable-PGLite seam, same as the delete-failure
        scenario) raises ONLY when `source_path` changes — updateSlug
        (slug only) and the reimport's putPage (COALESCE-preserve leaves
        source_path unchanged) pass through untouched, so exactly the
        follow fails.

        Baseline: a normal refresh runs AFTER the capture commit and
        BEFORE the move + trigger. This pins last_commit AT the capture
        commit, so the move commit's diff is `capture..move` — the OLD
        path exists in the baseline tree and git reports a genuine rename
        (without the baseline, the diff would span capture+move, the old
        path would never appear in the range, and the sync would see only
        an add — the follow would never run, which is exactly the failure
        shape this scenario must not regress into)."""
        slug = "inbox/f1-page"
        new_slug = "notes/f1-page"
        ev = self._w2(
            "gbrain", "capture", "follow-fail token conformance-ff-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git add " + slug + ".md "
            "&& git commit -qm 'f1 capture'",
        )
        # Baseline: a normal refresh AFTER the capture commit pins
        # last_commit AT it (the pre-baseline bookmark predates the
        # capture, so a move without this refresh would hide the old path
        # from the diff range and the rename — and therefore the follow —
        # would never run).
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, "baseline refresh must establish the capture: " + ev.stderr)
        baseline = self._last_commit()
        # The move commit then diffs `capture..move` (old path present in
        # the baseline tree → git reports a genuine rename).
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git mv " + slug + ".md " + new_slug + ".md "
            "&& git commit -qm 'f1 move'",
        )
        # Install the fault AFTER the baseline (and the move): the follow
        # UPDATE is the only source_path-changing write the refresh will
        # attempt from here on — the baseline refresh's reimport preserves
        # source_path via COALESCE, so the trigger cannot misfire earlier.
        self._pglite_query(
            "CREATE FUNCTION josemar_block_source_path() RETURNS trigger AS $$ "
            "BEGIN PERFORM 1/0; RETURN NEW; END $$ LANGUAGE plpgsql",
            [],
        )
        self._pglite_query(
            "CREATE TRIGGER josemar_block_source_path BEFORE UPDATE ON pages "
            "FOR EACH ROW WHEN (NEW.source_path IS DISTINCT FROM OLD.source_path) "
            "EXECUTE FUNCTION josemar_block_source_path()",
            [],
        )
        try:
            before = self._last_commit()
            self.assertEqual(
                before, baseline,
                "the move commit must not be consumed before the faulted refresh",
            )
            # First run: the follow fails → non-skippable sentinel →
            # hard block, last_commit frozen, destination NOT banked.
            ev = self._w2("josemar-gbrain", "refresh", timeout=300)
            self.assertNotEqual(ev.returncode, 0, "follow failure must block the refresh")
            self.assertEqual(
                self._last_commit(), before,
                "last_commit must not advance while the follow is blocked",
            )
            # The destination must NOT be in the resume checkpoint.
            rows = self._pglite_query(
                "SELECT count(*) AS c FROM op_checkpoint_paths WHERE path = $1",
                [new_slug + ".md"],
            )
            self.assertEqual(rows[0]["c"], 0, "destination must not be banked/completed")
            # The sentinel must be recorded (open) in the failure ledger.
            ledger = self._w2("cat", "/opt/data/.gbrain/sync-failures.jsonl")
            self.assertEqual(ledger.returncode, 0, ledger.stderr)
            self.assertIn("<rename:" + new_slug + ".md>", ledger.stdout)
        finally:
            self._pglite_query("DROP TRIGGER IF EXISTS josemar_block_source_path ON pages", [])
            self._pglite_query("DROP FUNCTION IF EXISTS josemar_block_source_path", [])
        # Second run: the rename is REPLAYED (not banked), the follow
        # retries and converges, and the sentinel clears.
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertEqual(self._w2_source_path(new_slug), new_slug + ".md")
        g = self._w2("gbrain", "get", slug)
        self.assertNotEqual(g.returncode, 0, "old slug must be swept after convergence")
        g = self._w2("gbrain", "get", new_slug)
        self.assertEqual(g.returncode, 0, g.stderr)
        self.assertIn("conformance-ff-token", g.stdout)
        ledger = self._w2("cat", "/opt/data/.gbrain/sync-failures.jsonl")
        self.assertEqual(ledger.returncode, 0, ledger.stderr)
        self.assertNotIn("<rename:" + new_slug + ".md>", ledger.stdout)
        self.assertNotEqual(self._last_commit(), before, "bookmark must advance after convergence")

    def _s2_unexpected_prep_failure_blocks_and_retries(self) -> None:
        """F2 (merge-blocking finding): an UNEXPECTED stale-pass
        preparation/enumeration/planning failure (the outer catch) must add
        a dedicated non-skippable `<stale-prep:…>` sentinel through the
        shared failure ledger, hard-block the bookmark and preserve
        last_commit; a later clean run retries, converges and clears the
        sentinel. The intentional safe skips (git-history proof
        unavailable, mass valve, retention) are NOT errors and never enter
        the ledger.

        Fault injection: the stale pass's preparation SELECT is broken by
        temporarily renaming ONLY the `pages.source_path` COLUMN (not the
        table) — test-only direct PGLite manipulation on the isolated
        synthetic runtime (there is no viable public fault seam for the
        preparation step; the exact column is restored in the same
        scenario, unblocking the exact state). In this M-delta refresh the
        stale-pass rows SELECT (`SELECT slug, source_path FROM pages ...`,
        pinned sync.ts:2374-2376) is the ONLY `pages` query of the whole
        run — there are no renames (no T4 pre-resolve at pinned
        sync.ts:1733-1747, no updateSlug, no source_path follow), no
        deletes, and the modified file is silently skipped before any
        engine query — so the renamed column breaks exactly the outer
        catch and nothing else, preserving all other table data.

        The DELTA is a committed MODIFY of a tracked markdown file whose
        worktree parent directory is swapped, after the baseline, to an
        UNTRACKED SYMLINKED directory pointing outside the repo whose
        target does NOT contain the file: a baseline refresh pins
        last_commit at the source commit so the content change diffs as a
        plain M, and the modified loop's `!existsSync` short-circuit
        (importOnePath, pinned sync.ts:2049) SILENTLY skips the now
        unresolvable path — markCompleted + succeededPaths, NO failure
        entry, NO engine/page query — before the NAV-1 isPathSafe guard
        (pinned sync.ts:2071, which would RECORD a failure; that is
        exactly why the file must stay absent from the symlink target).
        The manifest stays non-empty, the import phase completes green,
        and ONLY the stale pass's rows SELECT fails on the renamed column
        — exactly the outer catch.

        A RENAME delta cannot isolate the outer catch: the rename lane's
        T4 pre-resolve reads `pages` at pinned sync.ts:1733-1747 BEFORE
        the destination isPathSafe check (pinned sync.ts:1826) and BEFORE
        the stale-pass outer catch — with the `pages` TABLE renamed, the
        rename source resolution breaks FIRST (best-effort-swallowed and
        the rename is marked converged), so the fault never lands in the
        stale pass and the ordinary phase is not green. The M delta with
        the file absent from the symlink target is the only shape where
        the modify loop refuses SILENTLY before any DB access. (A
        file→symlink rename cannot be used: git never pairs those as
        renames, so the delta would decompose into delete+add and the
        fail-closed delete/import loops would record ordinary failures —
        skipping the pass. The same is true of a malformed-bracket add,
        which is filtered out of the manifest entirely, making
        totalChanges 0 and advancing the sync past the commit without
        ever reaching the stale pass.)"""
        # Baseline: commit a tracked markdown file under a real `ext/`
        # dir, then refresh so last_commit pins at the source commit and
        # the subsequent content change diffs as a plain M.
        mod_file = "ext/f2-mod.md"
        ext_dir = "/tmp/f2-extdir"
        self._w2(
            "sh", "-lc",
            "set -eu; mkdir -p /opt/data/obsidian/ext && "
            "printf 'f2 prep modify probe v1\\n' > /opt/data/obsidian/"
            + mod_file + "; cd /opt/data/obsidian && git add " + mod_file + " "
            "&& git commit -qm 'f2 prep modify source'",
        )
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(
            ev.returncode, 0,
            "baseline refresh must establish the modify source: " + ev.stderr,
        )
        # The MODIFY: content change committed as a plain M (no renames,
        # no deletes). Then the WORKTREE `ext/` dir is swapped for a
        # symlink to an outside dir that does NOT contain the file: the
        # committed path is now unresolvable, so importOnePath's
        # `!existsSync` short-circuit silently skips it — markCompleted +
        # succeededPaths, no failure entry, no pages query (the NAV-1
        # isPathSafe guard at pinned sync.ts:2071 would record a failure,
        # which is exactly why the file must stay absent from the target).
        self._w2(
            "sh", "-lc",
            "set -eu; cd /opt/data/obsidian && "
            "printf 'f2 prep modify probe v2\\n' > " + mod_file + " && "
            "git add " + mod_file + " && git commit -qm 'f2 prep modify' && "
            "rm -rf ext && mkdir -p " + ext_dir + " && "
            "ln -s " + ext_dir + " ext",
        )
        # last_commit read-back from the sources table (robust under the
        # column fault; the public status surface also counts pages).
        def sources_last_commit() -> str:
            rows = self._pglite_query(
                "SELECT last_commit FROM sources WHERE id = $1", ["default"],
            )
            return rows[0]["last_commit"] if rows else ""

        before = sources_last_commit()
        # Install the fault: ONLY the `pages.source_path` column is
        # renamed, so the stale pass's rows SELECT (the only pages query
        # in this M-delta refresh) fails with "column source_path does not
        # exist" — every other column and all table data preserved.
        self._pglite_query(
            "ALTER TABLE pages RENAME COLUMN source_path TO source_path_f2_shadow",
            [],
        )
        try:
            ev = self._w2("josemar-gbrain", "refresh", timeout=300)
            self.assertNotEqual(ev.returncode, 0, "prep failure must block the refresh")
            # The exact outer-catch diagnostic must reach the operator
            # surface (the refresh wrapper embeds the native sync's merged
            # output in its failure envelope).
            self.assertIn(
                "[sync] incremental stale-file pass error (no rows deleted)",
                ev.stdout + ev.stderr,
                "the stale-pass outer-catch diagnostic must be surfaced:\n" + ev.stdout + ev.stderr,
            )
            self.assertIn(
                "source_path",
                ev.stdout + ev.stderr,
                "the diagnostic must name the renamed column:\n" + ev.stdout + ev.stderr,
            )
            self.assertEqual(
                sources_last_commit(), before,
                "last_commit must not advance while preparation is broken",
            )
            ledger = self._w2("cat", "/opt/data/.gbrain/sync-failures.jsonl")
            self.assertEqual(ledger.returncode, 0, ledger.stderr)
            self.assertIn("<stale-prep:default>", ledger.stdout)
        finally:
            self._pglite_query(
                "ALTER TABLE pages RENAME COLUMN source_path_f2_shadow TO source_path",
                [],
            )
        # Remove the fault: the next run retries, converges and clears the
        # sentinel (the pass runs to completion, so the success path clears
        # any previous `<stale-prep:…>` row).
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        ledger = self._w2("cat", "/opt/data/.gbrain/sync-failures.jsonl")
        self.assertEqual(ledger.returncode, 0, ledger.stderr)
        self.assertNotIn("<stale-prep:default>", ledger.stdout)
        self.assertNotEqual(sources_last_commit(), before, "bookmark must advance after recovery")

    def _s2_mass_valve(self) -> None:
        """Part 2: the #2828 mass-delete valve trips on a large same-window
        sweep and NO rows are deleted (and nothing in the refresh path can
        bypass it).

        The valve TRIP is proven, not just the survival: the operator
        wrapper captures sync output and discards it on success, so the
        scenario first runs the EXACT native sync command the wrapper
        invokes (same binary, same canonical env, same flags) and asserts
        the #2828 valve warning on its stderr — rows surviving alone would
        not distinguish "valve tripped" from "pass disabled". The normal
        operator refresh then stays green over the surviving rows."""
        n = 21
        for i in range(n):
            ev = self._w2(
                "gbrain", "capture", f"mass probe token conformance-mass-{i}",
                "--slug", f"inbox/mass-{i}", "--json",
            )
            self.assertEqual(ev.returncode, 0, ev.stderr)
            self.assertIs(json.loads(ev.stdout).get("written"), True)
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git add -A && git commit -qm 's6 mass captures'",
        )
        mv = " && ".join(
            f"git mv inbox/mass-{i}.md 'inbox/[mass-{i}].md'" for i in range(n)
        )
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && " + mv + " && git commit -qm 's6 mass move'",
        )
        # PROOF THE VALVE TRIPPED: run the exact native sync the wrapper
        # invokes (run_sync_extract_links: gbrain-native sync --no-embed
        # --yes --no-pull --json --repo <vault>) with the canonical wrapper
        # env, and assert the #2828 valve warning on its stderr. The
        # refresh wrapper captures this output and discards it on success,
        # so the direct invocation is the only surface that carries the
        # warning. Runs BEFORE the wrapper refresh: once the bookmark
        # advances, a later sync short-circuits `up_to_date` and the pass
        # never re-runs.
        sync = self._w2(
            "sh", "-lc",
            "export GBRAIN_HOME=/opt/data GBRAIN_BRAIN_REPO=/opt/data/obsidian "
            "GBRAIN_SKIP_STARTUP_HOOKS=1; "
            "/opt/josemar/libexec/gbrain-native sync --no-embed --yes --no-pull "
            "--json --repo /opt/data/obsidian",
            timeout=600,
        )
        self.assertEqual(sync.returncode, 0, "valve skip is not a failure: " + sync.stderr)
        self.assertIn(
            "incremental stale-file pass refused to delete",
            sync.stderr,
            "mass valve warning must appear on the native sync stderr:\n" + sync.stderr,
        )
        self.assertIn(
            "No pages were deleted",
            sync.stderr,
            "mass valve must state no pages were deleted:\n" + sync.stderr,
        )
        # Rows survived the native run (valve tripped, nothing deleted).
        rows = self._pglite_query(
            "SELECT count(*) AS c FROM pages WHERE source_id = $1 "
            "AND slug LIKE $2 AND deleted_at IS NULL",
            ["default", "inbox/mass-%"],
        )
        self.assertEqual(rows[0]["c"], n, "all mass rows must survive the valve")
        # The normal operator refresh stays green over the surviving rows.
        ev = self._w2("josemar-gbrain", "refresh", timeout=600)
        self.assertEqual(ev.returncode, 0, "valve skip is not a failure: " + ev.stderr)
        for i in (0, 10, 20):
            g = self._w2("gbrain", "get", f"inbox/mass-{i}")
            self.assertEqual(g.returncode, 0, f"mass valve must retain inbox/mass-{i}")
        rows2 = self._pglite_query(
            "SELECT count(*) AS c FROM pages WHERE source_id = $1 "
            "AND slug LIKE $2 AND deleted_at IS NULL",
            ["default", "inbox/mass-%"],
        )
        self.assertEqual(rows2[0]["c"], n, "all mass rows must survive the operator refresh")

    def _s2_committed_deletion(self) -> None:
        """Part 2: an ORDINARY committed deletion reconciles through the
        existing delete loop (the stamped identity makes the row findable)."""
        slug = "inbox/rm-page"
        ev = self._w2(
            "gbrain", "capture", "committed deletion token conformance-rm-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git add inbox/rm-page.md "
            "&& git commit -qm 's7 capture'",
        )
        # Advance the bookmark first so the later deletion is delta-visible
        # (an add+delete inside ONE window is invisible to the endpoint diff).
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git rm -q inbox/rm-page.md && git commit -qm 's7 delete'",
        )
        ev = self._w2("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        g = self._w2("gbrain", "get", slug)
        self.assertNotEqual(g.returncode, 0, "ordinary committed deletion must reconcile")
        rows = self._pglite_query(
            "SELECT count(*) AS c FROM pages WHERE source_id = $1 "
            "AND slug = $2 AND deleted_at IS NULL",
            ["default", slug],
        )
        self.assertEqual(rows[0]["c"], 0)

    def _s2_mass_escape_hatch_absent(self) -> None:
        """The automatic refresh path must not be able to set the mass
        reconcile escape hatch (env-only, never present in the runtime)."""
        ev = self._w2("sh", "-lc", "printenv GBRAIN_ALLOW_MASS_RECONCILE || echo ABSENT")
        self.assertIn("ABSENT", ev.stdout)

    def _s2_repeated_refresh_no_duplicate(self) -> None:
        """A same-window capture-originated move converges after the stale
        pass: old slug gone, new slug live, exactly ONE live row, single
        search hit, stable across a second refresh (no duplicate/orphan)."""
        slug = "inbox/repeat-page"
        ev = self._w2(
            "gbrain", "capture", "repeat token conformance-repeat-token",
            "--slug", slug, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIs(json.loads(ev.stdout).get("written"), True)
        self._w2(
            "sh", "-lc",
            "cd /opt/data/obsidian && git add inbox/repeat-page.md "
            "&& git commit -qm 's9 capture' "
            "&& git mv inbox/repeat-page.md notes/repeat-page.md "
            "&& git commit -qm 's9 move'",
        )
        for _ in range(2):
            ev = self._w2("josemar-gbrain", "refresh", timeout=300)
            self.assertEqual(ev.returncode, 0, ev.stderr)
        g = self._w2("gbrain", "get", slug)
        self.assertNotEqual(g.returncode, 0, "old slug must be gone after refresh")
        g = self._w2("gbrain", "get", "notes/repeat-page")
        self.assertEqual(g.returncode, 0, g.stderr)
        s = self._w2("gbrain", "search", "conformance-repeat-token", "--limit", "5")
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertEqual(s.stdout.count("notes/repeat-page"), 1, s.stdout)
        rows = self._pglite_query(
            "SELECT count(*) AS c FROM pages WHERE source_id = $1 "
            "AND slug LIKE $2 AND deleted_at IS NULL",
            ["default", "%repeat-page%"],
        )
        self.assertEqual(rows[0]["c"], 1, "exactly one live row for the moved page")


# ---------------------------------------------------------------------------
# Fast host-side guards (always run; no Docker)
# ---------------------------------------------------------------------------


class GbrainSyncMoveRegressionGateStructureTests(unittest.TestCase):
    """Fast structural guards proving the W1 gate is opt-in and uses ONLY
    the supported Josemar path (hermes runtime user, operator refresh,
    public gbrain probes, no SQL, no production state, unconditional
    cleanup)."""

    @staticmethod
    def _module_text() -> str:
        return (
            REPO_ROOT / "tests" / "runtime" / "test_gbrain_sync_move_regression.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _runtime_text() -> str:
        """Only the runtime/implementation portion of the module (docstring
        + oracles + base/runtime classes), excluding this structure-test
        class — so the guards cannot self-pollute with their own assertion
        strings."""
        return (
            REPO_ROOT / "tests" / "runtime" / "test_gbrain_sync_move_regression.py"
        ).read_text(encoding="utf-8").split(
            "class GbrainSyncMoveRegressionGateStructureTests", 1
        )[0]

    @staticmethod
    def _docker_available_patch(available: bool):
        """Patch this module's own ``docker_available`` reference (robust
        against double-import under ``discover -s tests``)."""
        return mock.patch.object(
            sys.modules[__name__], "docker_available", return_value=available
        )

    # --- opt-in gate ------------------------------------------------------

    def test_gate_requires_run_docker_tests(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RUN_DOCKER_TESTS": "", "RUN_GBRAIN_SYNC_MOVE_REGRESSION": "1"},
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_sync_move_regression_enabled())

    def test_gate_requires_run_gbrain_sync_move_regression(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_SYNC_MOVE_REGRESSION": ""},
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_sync_move_regression_enabled())

    def test_gate_requires_docker(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_SYNC_MOVE_REGRESSION": "1"},
        ):
            with self._docker_available_patch(False):
                self.assertFalse(_sync_move_regression_enabled())

    def test_gate_enabled_when_all_conditions_met(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_SYNC_MOVE_REGRESSION": "1"},
        ):
            with self._docker_available_patch(True):
                self.assertTrue(_sync_move_regression_enabled())

    def test_runtime_class_is_opt_in_and_gated(self) -> None:
        text = self._module_text()
        self.assertIn("RUN_DOCKER_TESTS", text)
        self.assertIn("RUN_GBRAIN_SYNC_MOVE_REGRESSION", text)
        self.assertIn("skipUnless", text)
        self.assertIn("docker_available()", text)

    # --- supported Josemar path ------------------------------------------

    def test_all_in_container_commands_run_as_hermes_never_root(self) -> None:
        """The gate must never run in-container work as root: every command
        goes through ``run_as_hermes``, and no root su/exec spelling exists
        in the runtime portion of the module."""
        text = self._runtime_text()
        self.assertIn("run_as_hermes", text)
        self.assertIn("self.runtime.run_as_hermes(*command, check=False", text)
        for forbidden in (
            '"su", "-", "root"',
            '"su", "root"',
            '"-u", "root"',
            "as_user=\"root\"",
        ):
            self.assertNotIn(forbidden, text)

    def test_operator_refresh_and_public_probes_used(self) -> None:
        """The supported Josemar path: normal ``josemar-gbrain refresh`` for
        reconciliation and the public ``gbrain`` surfaces for probes."""
        text = self._module_text()
        self.assertIn('"josemar-gbrain", "refresh", timeout=300', text)
        self.assertIn('"gbrain", "get", new_slug', text)
        self.assertIn('"gbrain", "get", old_slug', text)
        self.assertIn('"gbrain", "search", token, "--limit", "5"', text)

    def test_no_sql_mutation_and_disposable_runtime(self) -> None:
        """The production-facing flows must not mutate PGLite via SQL and
        must run on the disposable conformance runtime with canonical seeding
        and unconditional cleanup. The ONLY SQL in the module lives inside
        the dedicated ``_pglite_query`` helper (synthetic isolated state on
        the disposable runtime — never production data)."""
        text = self._runtime_text()
        # Everything before the synthetic-state helper must be SQL-free.
        pre_helper = text.split("def _pglite_query", 1)[0]
        for forbidden in ("psql", "INSERT INTO", "UPDATE ", "sqlite3", "pg_ctl"):
            self.assertNotIn(forbidden, pre_helper)
        # The helper exists and is documented as synthetic-only.
        self.assertIn("def _pglite_query", text)
        self.assertIn("SYNTHETIC isolated state", text)
        self.assertIn("never production", text)
        self.assertIn("GbrainConformanceRuntime()", text)
        self.assertIn("self.runtime.seed_source_state()", text)
        self.assertIn("self.runtime.init_synthetic_vault()", text)
        self.assertIn("self.runtime.cleanup()", text)
        self.assertIn("down -v --remove-orphans", text)

    def test_rename_follow_fault_scenario_is_real_injection(self) -> None:
        """F1 must be covered by REAL disposable fault injection (a
        source_path-change trigger on the synthetic PGLite), not structural
        text-only coverage: the scenario runs a BASELINE refresh after the
        capture commit (pinning last_commit so the move diffs as a rename),
        then installs the trigger, asserts the first refresh blocks with
        the bookmark frozen and the destination NOT banked, drops the
        trigger, and asserts the retry converges with the sentinel
        cleared."""
        text = self._runtime_text()
        self.assertIn("CREATE TRIGGER josemar_block_source_path BEFORE UPDATE ON pages", text)
        self.assertIn("WHEN (NEW.source_path IS DISTINCT FROM OLD.source_path)", text)
        self.assertIn("DROP TRIGGER IF EXISTS josemar_block_source_path ON pages", text)
        # The baseline refresh must precede the trigger install (and the
        # move), so the move diff is `capture..move` — the old path exists
        # in the baseline tree and git reports a genuine rename whose
        # follow actually runs.
        self.assertIn("baseline refresh must establish the capture", text)
        self.assertIn("the move commit must not be consumed before the faulted refresh", text)
        self.assertIn("last_commit must not advance while the follow is blocked", text)
        self.assertIn("destination must not be banked/completed", text)
        self.assertIn("op_checkpoint_paths", text)
        self.assertIn("<rename:" + '" + new_slug + ".md' + ">", text)
        # The retry run asserts convergence + sentinel clear.
        self.assertIn("old slug must be swept after convergence", text)
        self.assertIn("bookmark must advance after convergence", text)

    def test_unexpected_prep_failure_scenario_is_real_injection(self) -> None:
        """F2 must be covered by REAL disposable fault injection (a
        temporary RENAME OF ONLY the `pages.source_path` column breaks the
        stale-pass preparation SELECT), with the rationale for test-only
        direct PGLite manipulation documented, the exact column restored,
        and retry/convergence/sentinel-clear asserted. The delta is a
        committed MODIFY whose worktree parent is swapped to an outside
        symlink whose target does NOT contain the file, so the modify
        loop's existsSync short-circuit silently skips it before any
        engine/page query. NO claim survives that a rename destination
        guard occurs before DB access: the rename lane's T4 pre-resolve
        reads `pages` (pinned sync.ts:1733-1747) before its destination
        isPathSafe check (pinned sync.ts:1826) and before the stale-pass
        outer catch, so a rename delta with a broken pages schema could
        never isolate the outer catch."""
        text = self._runtime_text()
        self.assertIn("ALTER TABLE pages RENAME COLUMN source_path TO source_path_f2_shadow", text)
        self.assertIn("ALTER TABLE pages RENAME COLUMN source_path_f2_shadow TO source_path", text)
        self.assertIn("no viable", text)
        self.assertIn("public fault seam", text)
        # The delta must keep the manifest non-empty (a committed MODIFY
        # whose worktree path escapes through a symlinked dir, silently
        # refused by the modify loop's existsSync short-circuit BEFORE any
        # engine/page query), so the sync reaches the stale pass instead
        # of the no-syncable-changes early advance — the failure shape
        # this scenario must not regress into.
        self.assertIn("ext/f2-mod.md", text)
        self.assertIn("NAV-1", text)
        self.assertIn("<stale-prep:default>", text)
        self.assertIn("prep failure must block the refresh", text)
        self.assertIn("incremental stale-file pass error (no rows deleted)", text)
        self.assertIn("last_commit must not advance while preparation is broken", text)
        self.assertIn("bookmark must advance after recovery", text)
        # The safe skips stay non-erroring: the mass valve and
        # proof-unavailable paths never enter the ledger.
        self.assertIn("mass valve", text)
        self.assertIn("git-history proof", text)
        # The invalid table-rename mechanism is gone, and no claim remains
        # that a rename destination guard occurs before DB access.
        self.assertNotIn("ALTER TABLE pages RENAME TO pages_f2_shadow", text)
        self.assertNotIn("refuses the destination BEFORE any DB access", text)

    def test_mass_scenario_proves_valve_tripped(self) -> None:
        """The mass-reconcile scenario must PROVE the #2828 valve tripped
        (its warning asserted on the native sync stderr), not merely that
        rows survived — surviving rows alone would not distinguish "valve
        tripped" from "pass disabled"."""
        text = self._runtime_text()
        # The scenario runs the exact native sync the wrapper invokes, with
        # the canonical wrapper env, and asserts the valve warning.
        self.assertIn("gbrain-native sync --no-embed --yes --no-pull", text)
        self.assertIn("GBRAIN_HOME=/opt/data GBRAIN_BRAIN_REPO=/opt/data/obsidian", text)
        self.assertIn(
            '"incremental stale-file pass refused to delete"', text,
        )
        self.assertIn('"No pages were deleted"', text)
        # Rows surviving is asserted on BOTH the native run and the
        # operator refresh (valve tripped, nothing deleted, still green).
        self.assertIn("all mass rows must survive the valve", text)
        self.assertIn("all mass rows must survive the operator refresh", text)

    def test_cleanup_is_unconditional_in_teardown(self) -> None:
        text = self._module_text()
        base = text.split("class GbrainSyncMoveRegressionTestCase", 1)[1]
        base = base.split("class GbrainSyncMoveRegressionRuntimeTests", 1)[0]
        self.assertIn("def tearDown", base)
        self.assertIn("self.runtime.cleanup()", base)

    # --- A/B matrix -------------------------------------------------------

    def test_both_origin_cases_present(self) -> None:
        text = self._runtime_text()
        self.assertIn("CASE_A_OLD_SLUG", text)
        self.assertIn("CASE_A_NEW_SLUG", text)
        self.assertIn("CASE_A_TOKEN", text)
        self.assertIn("CASE_B_OLD_SLUG", text)
        self.assertIn("CASE_B_NEW_SLUG", text)
        self.assertIn("CASE_B_TOKEN", text)
        # Case A must be created as a committed vault FILE (sync-originated)
        # and Case B through the public capture write-through.
        self.assertIn("sync_originated", text)
        self.assertIn('"gbrain", "capture"', text)
        self.assertIn("git add -A", text)
        # Both cases run through the SAME normal operator refresh: the
        # runtime invokes it once for Case A's sync-originated indexing
        # (index_refresh) plus refresh #1 and refresh #2 for every case.
        self.assertGreaterEqual(
            text.count('"josemar-gbrain", "refresh", timeout=300'), 3,
            "refresh must run through the normal operator wrapper",
        )
        self.assertIn("facts[\"refresh1\"]", text)
        self.assertIn("facts[\"refresh2\"]", text)

    # --- evidence contract ------------------------------------------------

    def test_evidence_contract_preserved(self) -> None:
        """Every W1 evidence requirement must be collected by the case
        runner: version/ref, pre/post commits, git diff --name-status -M,
        moved existence, content hash, refresh stdout/stderr, old/new get,
        unique-token search, second-refresh postconditions, metadata."""
        text = self._runtime_text()
        for required in (
            '"/opt/gbrain/.git/HEAD"',
            '"gbrain", "status", "--json"',
            '"gbrain", "sources", "list", "--json"',
            '"gbrain", "doctor", "--json"',
            "git log --oneline -2",
            "git diff --name-status -M HEAD~1 HEAD",
            '"test", "-f", "/opt/data/obsidian/" + new_slug + ".md"',
            '"test ! -f /opt/data/obsidian/" + old_slug + ".md"',
            "hashlib.sha256",
            '"git mv " + old_slug + ".md " + new_slug + ".md',
            '"josemar-gbrain", "refresh", timeout=300',  # refresh #1 and #2
            '"gbrain", "get", new_slug',
            '"gbrain", "get", old_slug',
            '"gbrain", "search", token, "--limit", "5"',
            "get_new2",
            "get_old2",
            "search2",
        ):
            self.assertIn(required, text)

    def test_failure_classifications_are_specific(self) -> None:
        """The failure must never be reported as a generic label: the module
        carries the specific ``_failure_signature`` decomposition and the
        raw-state failure dump."""
        text = self._runtime_text()
        self.assertIn("def _failure_signature", text)
        self.assertIn('return "none"', text)
        self.assertIn("def _case_failure_message", text)
        self.assertIn("classification=", text)
        self.assertNotIn("changed" + "_failure_mode", text)
        for symptom in (
            "moved_file_missing",
            "old_slug_still_live",
            "new_get_ok_token_search_missing",
            "stale_duplicate",
            "token_search_only",
            "no_resolution_at_all",
        ):
            self.assertIn(f'"{symptom}"', text)

    # --- report -----------------------------------------------------------

    def test_report_written_with_full_raw_evidence(self) -> None:
        text = self._module_text()
        self.assertIn("write_report", text)
        self.assertIn("conformance_report_dir", text)
        self.assertIn('"gbrain-sync-move-regression"', text)
        report = text.split("def _write_report", 1)[1]
        report = report.split("class GbrainSyncMoveRegressionRuntimeTests", 1)[0]
        self.assertNotIn("os.environ", report)

    def test_report_persists_classification_and_results(self) -> None:
        text = self._module_text()
        report = text.split("def _write_report", 1)[1]
        report = report.split("class GbrainSyncMoveRegressionRuntimeTests", 1)[0]
        for key in (
            '"classification"',
            '"signature"',
            '"classification_after_second_refresh"',
            '"git_rename_status"',
            '"content_unchanged"',
            '"pipeline_location"',
            '"gbrain_version"',
            '"gbrain_source_ref"',
            '"matrix"',
        ):
            self.assertIn(key, report)


# ---------------------------------------------------------------------------
# Host-side oracle semantics (no Docker)
# ---------------------------------------------------------------------------


class GbrainSyncMoveClassificationTests(unittest.TestCase):
    """Semantics of the W1 oracles: ``_classify_git_mv_probe`` returns
    ``fixed`` / recorded ``present`` / exact symptom classifications, and
    ``_failure_signature`` decomposes the latter without a generic label."""

    @staticmethod
    def _classify(
        *,
        moved_file_exists: bool = True,
        new_resolves: bool = False,
        old_resolves: bool = False,
        token_search_resolves: bool = False,
    ) -> str:
        return _classify_git_mv_probe(
            moved_file_exists=moved_file_exists,
            new_resolves=new_resolves,
            old_resolves=old_resolves,
            token_search_resolves=token_search_resolves,
        )

    def test_fixed_requires_new_get_plus_token_search_and_old_not_live(self) -> None:
        self.assertEqual(
            self._classify(
                new_resolves=True, old_resolves=False, token_search_resolves=True
            ),
            "fixed",
        )

    def test_fixed_not_reached_without_token_search(self) -> None:
        self.assertEqual(
            self._classify(
                new_resolves=True, old_resolves=False, token_search_resolves=False
            ),
            "new_get_ok_token_search_missing",
        )

    def test_fixed_not_reached_while_old_slug_still_live(self) -> None:
        self.assertEqual(
            self._classify(
                new_resolves=True, old_resolves=True, token_search_resolves=True
            ),
            "stale_duplicate",
        )

    def test_present_requires_moved_file_with_no_resolution_at_all(self) -> None:
        """Issue #125's recorded failure mode: the moved file exists but
        neither slug resolves nor the unique token search."""
        self.assertEqual(
            self._classify(
                moved_file_exists=True,
                new_resolves=False,
                old_resolves=False,
                token_search_resolves=False,
            ),
            "present",
        )

    def test_present_not_reached_when_anything_resolves(self) -> None:
        for kwargs, expected in (
            (
                {"new_resolves": True, "old_resolves": False, "token_search_resolves": False},
                "new_get_ok_token_search_missing",
            ),
            (
                {"new_resolves": False, "old_resolves": True, "token_search_resolves": False},
                "old_slug_still_live",
            ),
            (
                {"new_resolves": False, "old_resolves": False, "token_search_resolves": True},
                "token_search_only",
            ),
        ):
            self.assertEqual(self._classify(**kwargs), expected)

    def test_signature_none_when_fixed(self) -> None:
        self.assertEqual(
            _failure_signature(
                moved_file_exists=True,
                new_resolves=True,
                old_resolves=False,
                token_search_resolves=True,
            ),
            "none",
        )

    def test_signature_decomposes_specific_symptoms(self) -> None:
        """Each non-fixed evidence vector yields its SPECIFIC symptom set,
        never a generic label."""
        self.assertEqual(
            _failure_signature(
                moved_file_exists=True,
                new_resolves=False,
                old_resolves=False,
                token_search_resolves=False,
            ),
            "no_resolution_at_all",
        )
        self.assertEqual(
            _failure_signature(
                moved_file_exists=True,
                new_resolves=False,
                old_resolves=True,
                token_search_resolves=False,
            ),
            "old_slug_still_live",
        )
        self.assertEqual(
            _failure_signature(
                moved_file_exists=True,
                new_resolves=True,
                old_resolves=False,
                token_search_resolves=False,
            ),
            "new_get_ok_token_search_missing",
        )
        self.assertEqual(
            _failure_signature(
                moved_file_exists=True,
                new_resolves=True,
                old_resolves=True,
                token_search_resolves=True,
            ),
            "stale_duplicate",
        )
        self.assertEqual(
            _failure_signature(
                moved_file_exists=False,
                new_resolves=False,
                old_resolves=False,
                token_search_resolves=False,
            ),
            "moved_file_missing+no_resolution_at_all",
        )


if __name__ == "__main__":
    unittest.main()
