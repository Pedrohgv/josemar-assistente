"""Focused tests for workspace-sync command-allowlist state gating (issue #151).

These tests exercise the workspace-sync ingress/egress validation for the
state-owned Hermes runtime command-allowlist sidecars and the value-free
migration-completion marker:

- Every present sidecar (``hermes/command-allowlist/default.json`` plus
  ``hermes/command-allowlist/profiles/*.json``) is validated with the
  canonical helper validator (``validate_command_allowlist_state_from_text``,
  imported from ``josemar_skill_state`` — schema rules are never duplicated)
  BEFORE local staging, BEFORE every push (committed HEAD), and BEFORE any
  remote merge/acceptance.
- Every profile sidecar must carry a CANONICAL profile stem (the exact W1
  helper ``_PROFILE_ID_RE`` contract): noncanonical filenames are
  rejected value-free at every ingress — the manifest wildcard enumerates
  the family, it never authorizes noncanonical filenames.
- The migration marker (``hermes/command-allowlist/migration-v1.json``,
  exactly ``{"version":1,"legacy_runtime_import_complete":true}``) is
  validated at the same three boundaries and is MONOTONIC, history-aware:
  initial first-time addition is valid; working-copy deletion is rejected
  iff HEAD contains the marker; a manually committed deletion is blocked
  before push when reachable history previously contained the marker; a
  remote candidate deletion is rejected when the remote history previously
  contained it; a remote history that never contained the marker is valid
  and the merge must retain a local first-time marker. Normal sidecar
  deletion remains valid.
- Absence (sidecars; marker with markerless history) is always valid and
  needs no helper.
- A missing/unusable helper with present state fails closed.
- Error output never contains allowlist values or marker/allowlist values.
- Manifest/gitignore/template ownership stays narrow: the exact marker,
  ``default.json``, and the sanctioned ``profiles/*.json`` wildcard; only
  the profiles entry is a wildcard; broader forms, ``config.yaml``,
  arbitrary json, and lock/temp files stay rejected/ignored. The template
  ships ONLY the value-free marker — no sidecar values.

The canonical marker validator
(``validate_command_allowlist_migration_marker_from_text``) is W1's
public API in ``scripts/josemar_skill_state.py``; these tests always run
the sync tool against that real helper — schema rules are never
duplicated or stubbed here.

All tests use local bare remotes and temp git workspaces; no network. All
command values used here are generic synthetic samples, never real user
patterns.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.runtime.workspace_sync_fixture import (
    GitEnvIsolation,
    REMOTE_GIT_EMAIL,
    WorkspaceRepo,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT_PATH = REPO_ROOT / "scripts" / "workspace_sync.py"
HELPER_PATH = REPO_ROOT / "scripts" / "josemar_skill_state.py"
TEMPLATE_DIR = REPO_ROOT / "templates" / "agent-state-template"
TEMPLATE_MANIFEST = TEMPLATE_DIR / ".sync-manifest"
TEMPLATE_GITIGNORE = TEMPLATE_DIR / ".gitignore"
TEMPLATE_MARKER = TEMPLATE_DIR / "hermes" / "command-allowlist" / "migration-v1.json"

FAMILY_DIR = "hermes/command-allowlist"
FAMILY_DEFAULT = "hermes/command-allowlist/default.json"
FAMILY_PROFILES = "hermes/command-allowlist/profiles"
FAMILY_MARKER = "hermes/command-allowlist/migration-v1.json"

# Exact deny-by-default unignore chain required for the family (parents
# first, then the exact admitted shapes: marker, default, profiles glob).
FAMILY_GITIGNORE_CHAIN = (
    "!hermes/",
    "!hermes/command-allowlist/",
    "!hermes/command-allowlist/default.json",
    "!hermes/command-allowlist/migration-v1.json",
    "!hermes/command-allowlist/profiles/",
    "!hermes/command-allowlist/profiles/*.json",
)

# Exact value-free migration marker document (plan v2).
MARKER_TEXT = '{"version":1,"legacy_runtime_import_complete":true}'

# Generic synthetic allowlist values (never real user patterns).
SAMPLE_ITEMS = ["sample-cmd-alpha", "sample-cmd-beta"]

# Invalid sidecar contents (missing key / malformed JSON / empty file).
INVALID_MISSING_KEY = '{"version":1}'
INVALID_MALFORMED = "{not json"
INVALID_EMPTY = ""

# Malformed marker variants (wrong shape/value/type — all rejected).
INVALID_MARKERS = (
    "{not json",
    '{"version":1}',
    '{"version":1,"legacy_runtime_import_complete":false}',
    '{"version":2,"legacy_runtime_import_complete":true}',
    '{"version":1,"legacy_runtime_import_complete":true,"extra":1}',
)


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "josemar_skill_state_ws_allowlist", HELPER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_sync_module():
    spec = importlib.util.spec_from_file_location(
        "workspace_sync_ws_allowlist", SYNC_SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPER = _load_helper()

# Canonical one-line sidecar text produced by the canonical serializer.
VALID_SIDECAR_TEXT = HELPER.serialize_command_allowlist_state(
    {"version": 1, "command_allowlist": SAMPLE_ITEMS}
)


# ---------------------------------------------------------------------------
# Base test class: WorkspaceRepo fixture + script runners
# ---------------------------------------------------------------------------


class _CommandAllowlistSyncTest(GitEnvIsolation, unittest.TestCase):
    """Base: WorkspaceRepo fixture, temp-dir cleanup, script runners.

    Runs the sync tool against the REAL canonical helper
    (``scripts/josemar_skill_state.py``), so sidecar and marker
    validation always exercise genuine W1 code.
    """

    def setUp(self) -> None:
        self._isolate_git_environment()
        self._extra_temp_dirs: list[tempfile.TemporaryDirectory] = []
        self.repo = WorkspaceRepo()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.repo.cleanup()
        for td in self._extra_temp_dirs:
            td.cleanup()

    def _mk_temp_dir(self) -> str:
        td = tempfile.TemporaryDirectory(prefix="ws-allowlist-")
        self._extra_temp_dirs.append(td)
        return td.name

    # -- sidecar/marker writers --

    def _sidecar_path(self, profile: str | None = None) -> Path:
        if profile in (None, "default"):
            return Path(self.repo.workspace) / FAMILY_DEFAULT
        return Path(self.repo.workspace) / FAMILY_PROFILES / f"{profile}.json"

    def _write_valid_sidecar(
        self, items: list[str] | None = None, profile: str | None = None
    ) -> Path:
        path = self._sidecar_path(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            HELPER.serialize_command_allowlist_state(
                {"version": 1, "command_allowlist": items or SAMPLE_ITEMS}
            ),
            encoding="utf-8",
        )
        return path

    def _write_raw_sidecar(self, text: str, profile: str | None = None) -> Path:
        path = self._sidecar_path(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _marker_path(self) -> Path:
        return Path(self.repo.workspace) / FAMILY_MARKER

    def _write_marker(self, text: str = MARKER_TEXT) -> Path:
        path = self._marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return path

    # -- ownership helpers (manifest + gitignore chain) --

    def _allow_family_in_workspace(self) -> None:
        """Append the exact unignore chain and the exact manifest entries
        (sidecars + marker), mirroring the shipped template contract."""
        gitignore = Path(self.repo.workspace) / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8")
        lines = existing.rstrip("\n").splitlines()
        for rule in FAMILY_GITIGNORE_CHAIN:
            if rule not in lines:
                lines.append(rule)
        gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest = Path(self.repo.workspace) / ".sync-manifest"
        with manifest.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{FAMILY_DEFAULT}\n{FAMILY_PROFILES}/*.json\n{FAMILY_MARKER}\n"
            )

    # -- runners --

    def _sync_env(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.repo.workspace)
        env["WORKSPACE_STATE_REPO"] = str(self.repo.remote)
        env["WORKSPACE_GIT_BRANCH"] = "main"
        env["WORKSPACE_SYNC_ON_START"] = "true"
        env["JOSEMAR_SKILL_STATE"] = str(HELPER_PATH)
        env.update(overrides)
        return env

    def _run_tool(
        self, payload: dict[str, Any], **env_overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SYNC_SCRIPT_PATH)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=self._sync_env(**env_overrides),
            check=False,
        )

    def _run_lifecycle(
        self,
        mode: str,
        *,
        workspace: str | None = None,
        remote: str | None = None,
        **env_overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        env = self._sync_env(**env_overrides)
        if workspace is not None:
            env["WORKSPACE_DIR"] = workspace
        if remote is not None:
            env["WORKSPACE_STATE_REPO"] = remote
        return subprocess.run(
            [sys.executable, str(SYNC_SCRIPT_PATH), mode],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    # -- queries --

    def _tracked_files(self) -> set[str]:
        return self.repo.tracked_files()

    def _init_bare_remote_with_initial_push(self) -> None:
        """Push the initial fixture state so later pushes have a target."""
        self.repo.push_to_remote()

    def _commit_invalid_head_sidecar(self, profile: str | None = None) -> None:
        """Corrupt the committed sidecar in HEAD via direct git (bypassing
        workspace-sync validation, simulating a commit by another path)."""
        path = self._sidecar_path(profile)
        path.write_text(INVALID_MISSING_KEY, encoding="utf-8")
        rel = path.relative_to(self.repo.workspace).as_posix()
        self.repo.git(["add", "-f", rel])
        self.repo.git(["commit", "--amend", "--no-edit"])

    def _advance_remote_deleting(self, relative_path: str) -> None:
        """Advance the bare remote with an independent commit that deletes
        ``relative_path`` (simulates a remote-authored deletion)."""
        clone_dir = self._mk_temp_dir()
        _run = subprocess.run
        _run(["git", "clone", "-q", self.repo.remote, clone_dir], check=True,
             capture_output=True, text=True)
        # Configure clone-local identity before committing: GitEnvIsolation
        # strips global GIT_* identity and CI has no global identity, so the
        # commit would otherwise fail. Mirror the fixture convention.
        _run(["git", "-C", clone_dir, "config", "user.email", REMOTE_GIT_EMAIL],
             check=True, capture_output=True, text=True)
        _run(["git", "-C", clone_dir, "config", "user.name", "Remote Author"],
             check=True, capture_output=True, text=True)
        _run(["git", "-C", clone_dir, "rm", "-q", relative_path], check=True,
             capture_output=True, text=True)
        _run(["git", "-C", clone_dir, "commit", "-qm", "remote deletes marker"],
             check=True, capture_output=True, text=True)
        _run(["git", "-C", clone_dir, "push", "-q", "origin", "HEAD:main"],
             check=True, capture_output=True, text=True)

    def _head_has(self, rel: str) -> bool:
        proc = self.repo.git_check(["cat-file", "-e", f"HEAD:{rel}"])
        return proc.returncode == 0


# ---------------------------------------------------------------------------
# Template ownership contract (no git required)
# ---------------------------------------------------------------------------


class TemplateOwnershipContractTests(unittest.TestCase):
    """Narrow template ownership: exact manifest entries, exact gitignore
    chain, and a template that ships ONLY the value-free migration marker
    (no sidecar values)."""

    def test_template_ships_exact_marker_only(self) -> None:
        """The template family directory carries exactly the migration
        marker with the exact value-free document — no sidecar values."""
        self.assertTrue(
            TEMPLATE_MARKER.exists(),
            "template must ship hermes/command-allowlist/migration-v1.json "
            "with the exact value-free marker document",
        )
        self.assertEqual(
            MARKER_TEXT + "\n", TEMPLATE_MARKER.read_text(encoding="utf-8")
        )
        family = TEMPLATE_DIR / "hermes" / "command-allowlist"
        self.assertEqual(
            {"migration-v1.json"},
            {entry.name for entry in family.iterdir()},
            "template must ship only the marker — no default.json, no "
            "profiles/, no allowlist values",
        )

    def test_template_manifest_family_entries_exact(self) -> None:
        """Manifest carries exactly the marker, the default entry, and the
        sanctioned profiles wildcard."""
        lines = [
            line.strip()
            for line in TEMPLATE_MANIFEST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertIn(FAMILY_MARKER, lines)
        self.assertIn(FAMILY_DEFAULT, lines)
        self.assertIn(f"{FAMILY_PROFILES}/*.json", lines)
        for broader in (
            "hermes/command-allowlist/**",
            "hermes/command-allowlist/*",
            "hermes/command-allowlist/*.json",
            "hermes/command-allowlist/profiles/*",
            "hermes/*.json",
            "hermes/**",
        ):
            self.assertNotIn(broader, lines)

    def test_template_gitignore_chain_exact(self) -> None:
        """Gitignore un-ignores exactly the family sidecar shapes (parents
        first), each exactly once, with no broader form."""
        lines = [
            line.strip()
            for line in TEMPLATE_GITIGNORE.read_text(encoding="utf-8").splitlines()
        ]
        for rule in FAMILY_GITIGNORE_CHAIN:
            self.assertIn(rule, lines)
            self.assertEqual(
                1,
                lines.count(rule),
                f"gitignore rule must appear exactly once: {rule}",
            )
        for broader in (
            "!hermes/command-allowlist/**",
            "!hermes/command-allowlist/*",
            "!hermes/command-allowlist/profiles/*",
            "!hermes/**",
        ):
            self.assertNotIn(broader, lines)

    def test_template_gitignore_stays_deny_by_default(self) -> None:
        """The first effective (non-comment) rule must remain ``*``."""
        lines = [
            line.strip()
            for line in TEMPLATE_GITIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual("*", lines[0])


# ---------------------------------------------------------------------------
# Sanctioned wildcard set is exact
# ---------------------------------------------------------------------------


class SanctionedWildcardExactTests(unittest.TestCase):
    """workspace_sync.ALLOWED_WILDCARD_PATHSPECS must contain exactly the
    three intentional template wildcard forms."""

    def test_allowed_wildcard_set_exact(self) -> None:
        mod = _load_sync_module()
        self.assertEqual(
            frozenset(
                {
                    "avatars/*",
                    "hermes/skill-toggles/profiles/*.json",
                    "hermes/command-allowlist/profiles/*.json",
                }
            ),
            mod.ALLOWED_WILDCARD_PATHSPECS,
        )


# ---------------------------------------------------------------------------
# Local staging validation (before staging/commit)
# ---------------------------------------------------------------------------


class LocalStagingValidationTests(_CommandAllowlistSyncTest):
    """Present sidecars are validated before anything is staged."""

    def test_absent_sidecars_commit_succeeds(self) -> None:
        """Absence of the whole family is valid and needs no helper."""
        result = self._run_tool({"action": "commit", "message": "no sidecars"})
        self.assertEqual(0, result.returncode, result.stderr)

    def test_valid_default_and_multiple_profile_sidecars_committed(self) -> None:
        """Valid default + multiple canonical profile sidecars commit."""
        self._allow_family_in_workspace()
        self._write_valid_sidecar()
        self._write_valid_sidecar(["sample-cmd-one"], profile="alpha")
        self._write_valid_sidecar(["sample-cmd-two"], profile="beta")

        result = self._run_tool({"action": "commit", "message": "allowlists"})

        self.assertEqual(0, result.returncode, result.stderr)
        tracked = self._tracked_files()
        self.assertIn(FAMILY_DEFAULT, tracked)
        self.assertIn(f"{FAMILY_PROFILES}/alpha.json", tracked)
        self.assertIn(f"{FAMILY_PROFILES}/beta.json", tracked)

    def test_invalid_default_sidecar_rejected_before_stage(self) -> None:
        """Invalid local sidecar fails commit nonzero with nothing staged."""
        self._write_raw_sidecar(INVALID_MISSING_KEY)

        result = self._run_tool({"action": "commit", "message": "bad sidecar"})

        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "command-allowlist sidecar validation failed", result.stderr
        )
        self.assertNotIn(FAMILY_DEFAULT, self._tracked_files())
        staged = self.repo.git_check(["diff", "--cached", "--name-only"])
        self.assertNotIn(FAMILY_DEFAULT, staged.stdout)

    def test_malformed_json_sidecar_rejected(self) -> None:
        self._write_raw_sidecar(INVALID_MALFORMED)
        result = self._run_tool({"action": "commit", "message": "bad sidecar"})
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "command-allowlist sidecar validation failed", result.stderr
        )

    def test_empty_sidecar_file_rejected(self) -> None:
        """An empty sidecar file is malformed (absence semantics are
        file-level, so an empty file must fail closed)."""
        self._write_raw_sidecar(INVALID_EMPTY)
        result = self._run_tool({"action": "commit", "message": "empty sidecar"})
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "command-allowlist sidecar validation failed", result.stderr
        )

    def test_invalid_profile_sidecar_among_valid_rejected(self) -> None:
        """One invalid sidecar among several present ones fails the commit."""
        self._allow_family_in_workspace()
        self._write_valid_sidecar()
        self._write_valid_sidecar(["sample-cmd-one"], profile="alpha")
        self._write_raw_sidecar(INVALID_MISSING_KEY, profile="beta")

        result = self._run_tool({"action": "commit", "message": "mixed"})

        self.assertNotEqual(0, result.returncode)
        self.assertIn("beta.json", result.stderr)
        tracked = self._tracked_files()
        self.assertNotIn(FAMILY_DEFAULT, tracked)
        self.assertNotIn(f"{FAMILY_PROFILES}/alpha.json", tracked)
        self.assertNotIn(f"{FAMILY_PROFILES}/beta.json", tracked)

    def test_local_noncanonical_profile_name_rejected_with_valid_content(
        self,
    ) -> None:
        """A noncanonical profile filename is rejected even when its
        content is a perfectly valid sidecar — the wildcard enumerates
        canonical profiles, it never authorizes noncanonical names."""
        self._write_raw_sidecar(VALID_SIDECAR_TEXT, profile="Upper")

        result = self._run_tool({"action": "commit", "message": "bad name"})

        self.assertNotEqual(0, result.returncode)
        self.assertIn("noncanonical", result.stderr)
        self.assertNotIn(f"{FAMILY_PROFILES}/Upper.json", self._tracked_files())
        staged = self.repo.git_check(["diff", "--cached", "--name-only"])
        self.assertNotIn(f"{FAMILY_PROFILES}/Upper.json", staged.stdout)

    def test_local_noncanonical_profile_name_rejected_value_free(self) -> None:
        """The noncanonical-filename rejection happens on the name alone:
        file content is never read or echoed."""
        content_marker = "sample-cmd-content-never-read"
        self._write_raw_sidecar(
            json.dumps(
                {"version": 1, "command_allowlist": [content_marker]}
            ),
            profile="Odd.Name",
        )

        result = self._run_tool({"action": "commit", "message": "bad name"})

        self.assertNotEqual(0, result.returncode)
        self.assertIn("noncanonical", result.stderr)
        self.assertNotIn(content_marker, result.stdout + result.stderr)

    def test_canonical_profile_stem_boundary_accepted(self) -> None:
        """Canonical stems (lowercase alnum + ``_``/``-``, digit-leading)
        pass the name gate and commit like any valid sidecar."""
        self._allow_family_in_workspace()
        self._write_valid_sidecar(["sample-cmd-one"], profile="team-lead")
        self._write_valid_sidecar(["sample-cmd-two"], profile="p2")

        result = self._run_tool({"action": "commit", "message": "canonical"})

        self.assertEqual(0, result.returncode, result.stderr)
        tracked = self._tracked_files()
        self.assertIn(f"{FAMILY_PROFILES}/team-lead.json", tracked)
        self.assertIn(f"{FAMILY_PROFILES}/p2.json", tracked)

    def test_error_output_contains_no_allowlist_values(self) -> None:
        """Errors identify path + structural violation only — never entries."""
        secret_marker = "sample-cmd-secret-marker"
        self._write_raw_sidecar(
            json.dumps(
                {"version": 1, "command_allowlist": [secret_marker, 3]}
            )
        )

        result = self._run_tool({"action": "commit", "message": "leak check"})

        self.assertNotEqual(0, result.returncode)
        combined = result.stdout + result.stderr
        self.assertIn("command-allowlist sidecar validation failed", combined)
        self.assertNotIn(secret_marker, combined)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["success"])
        self.assertNotIn(secret_marker, doc["error"])

    def test_validator_unavailable_fail_closed_when_state_present(self) -> None:
        """Present sidecar + unavailable helper fails closed (nonzero)."""
        self._write_valid_sidecar()

        result = self._run_tool(
            {"action": "commit", "message": "helper missing"},
            JOSEMAR_SKILL_STATE="/nonexistent/helper.py",
        )

        self.assertNotEqual(0, result.returncode)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["success"])
        self.assertIn("unavailable", doc["error"].lower())
        self.assertNotIn(FAMILY_DEFAULT, self._tracked_files())

    def test_validator_unavailable_absent_state_still_valid(self) -> None:
        """Absent state + unavailable helper remains valid (no validation
        is required when there is nothing to validate)."""
        result = self._run_tool(
            {"action": "commit", "message": "no sidecars"},
            JOSEMAR_SKILL_STATE="/nonexistent/helper.py",
        )
        self.assertEqual(0, result.returncode, result.stderr)


# ---------------------------------------------------------------------------
# HEAD validation before every push
# ---------------------------------------------------------------------------


class HeadPushValidationTests(_CommandAllowlistSyncTest):
    """Committed HEAD sidecar state is validated before every push."""

    def setUp(self) -> None:
        super().setUp()
        self._init_bare_remote_with_initial_push()

    def test_invalid_head_sidecar_blocks_push(self) -> None:
        """A sidecar committed by another path (direct git) with invalid
        content must fail the push closed."""
        self._allow_family_in_workspace()
        self._write_valid_sidecar()
        result = self._run_tool({"action": "commit", "message": "valid"})
        self.assertEqual(0, result.returncode, result.stderr)

        # Corrupt the committed state behind workspace-sync's back.
        self._commit_invalid_head_sidecar()

        result = self._run_tool({"action": "push"})
        self.assertNotEqual(0, result.returncode)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["success"])
        self.assertIn(
            "command-allowlist sidecar validation failed", result.stdout
        )
        # The invalid commit must not have reached the remote.
        proc = self.repo.remote_show_file(FAMILY_DEFAULT)
        self.assertNotEqual(0, proc.returncode)

    def test_head_noncanonical_profile_name_blocks_push(self) -> None:
        """A sidecar committed by another path (direct git) under a
        noncanonical profile filename must fail the push closed, even
        with valid sidecar content."""
        self._allow_family_in_workspace()
        self._write_valid_sidecar()
        result = self._run_tool({"action": "commit", "message": "valid"})
        self.assertEqual(0, result.returncode, result.stderr)

        # Add a noncanonical-named file to HEAD behind workspace-sync's
        # back (valid content — the name alone must reject).
        bad = self._sidecar_path("Bad-Name")
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(VALID_SIDECAR_TEXT + "\n", encoding="utf-8")
        bad_rel = bad.relative_to(self.repo.workspace).as_posix()
        self.repo.git(["add", "-f", bad_rel])
        self.repo.git(["commit", "--amend", "--no-edit"])

        result = self._run_tool({"action": "push"})
        self.assertNotEqual(0, result.returncode)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["success"])
        self.assertIn("noncanonical", doc["error"])
        # Nothing reached the remote.
        proc = self.repo.remote_show_file(FAMILY_DEFAULT)
        self.assertNotEqual(0, proc.returncode)
        proc = self.repo.remote_show_file(f"{FAMILY_PROFILES}/Bad-Name.json")
        self.assertNotEqual(0, proc.returncode)

    def test_valid_head_sidecar_push_succeeds(self) -> None:
        self._allow_family_in_workspace()
        self._write_valid_sidecar()
        result = self._run_tool({"action": "commit", "message": "valid"})
        self.assertEqual(0, result.returncode, result.stderr)

        result = self._run_tool({"action": "push"})

        self.assertEqual(0, result.returncode, result.stderr)
        self.repo.assert_remote_tracks_file(FAMILY_DEFAULT, VALID_SIDECAR_TEXT)

    def test_head_without_sidecars_push_succeeds(self) -> None:
        result = self._run_tool({"action": "push"})
        self.assertEqual(0, result.returncode, result.stderr)


# ---------------------------------------------------------------------------
# Remote candidate validation before merge/acceptance
# ---------------------------------------------------------------------------


class RemoteAcceptanceValidationTests(_CommandAllowlistSyncTest):
    """Remote sidecar state is validated before any merge/acceptance."""

    def _point_origin_at(self, remote: str) -> None:
        self.repo.git(["remote", "set-url", "origin", remote])
        self.repo.git(["fetch", "origin"])

    def test_invalid_remote_sidecar_rejected_before_merge(self) -> None:
        """A remote carrying an invalid sidecar fails startup closed and
        the file is never materialized into the workspace."""
        bad_remote = WorkspaceRepo.build_bare_remote_with_state(
            self._extra_temp_dirs,
            initial_commit_subject="bad allowlist remote",
            extra_tracked={FAMILY_DEFAULT: INVALID_MISSING_KEY},
        )
        self._point_origin_at(bad_remote)

        result = self._run_lifecycle("startup")

        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "command-allowlist sidecar validation failed", result.stderr
        )
        self.assertFalse((Path(self.repo.workspace) / FAMILY_DEFAULT).exists())

    def test_remote_noncanonical_profile_name_rejected_before_merge(self) -> None:
        """A remote carrying a noncanonical-named profile sidecar fails
        startup closed (value-free — content is never read or echoed) and
        nothing is materialized. Covers single-level and deeper paths."""
        for rel in (
            f"{FAMILY_PROFILES}/Odd.json",
            f"{FAMILY_PROFILES}/sub/deep.json",
        ):
            with self.subTest(rel=rel):
                bad_remote = WorkspaceRepo.build_bare_remote_with_state(
                    self._extra_temp_dirs,
                    initial_commit_subject=f"noncanonical remote {rel}",
                    extra_tracked={rel: VALID_SIDECAR_TEXT + "\n"},
                )
                self._point_origin_at(bad_remote)

                result = self._run_lifecycle("startup")

                self.assertNotEqual(0, result.returncode)
                self.assertIn("noncanonical", result.stderr)
                self.assertNotIn(
                    "sample-cmd-alpha", result.stderr, "content must not be echoed"
                )
                self.assertFalse(
                    (Path(self.repo.workspace) / rel).exists(),
                    "remote content must not be materialized",
                )
                # Reset workspace state between subtests.
                self.repo.git(["remote", "set-url", "origin", self.repo.remote])
                self.repo.git(["fetch", "origin"])

    def test_valid_remote_profile_sidecars_accepted(self) -> None:
        """A remote with valid default + multiple profile sidecars clones
        and restores cleanly."""
        good_remote = WorkspaceRepo.build_bare_remote_with_state(
            self._extra_temp_dirs,
            initial_commit_subject="good allowlist remote",
            extra_tracked={
                FAMILY_DEFAULT: VALID_SIDECAR_TEXT + "\n",
                f"{FAMILY_PROFILES}/alpha.json": HELPER.serialize_command_allowlist_state(
                    {"version": 1, "command_allowlist": ["sample-cmd-one"]}
                )
                + "\n",
                f"{FAMILY_PROFILES}/beta.json": HELPER.serialize_command_allowlist_state(
                    {"version": 1, "command_allowlist": ["sample-cmd-two"]}
                )
                + "\n",
            },
        )
        empty_workspace = self._mk_temp_dir()

        result = self._run_lifecycle(
            "startup", workspace=empty_workspace, remote=good_remote
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            VALID_SIDECAR_TEXT + "\n",
            (Path(empty_workspace) / FAMILY_DEFAULT).read_text(encoding="utf-8"),
        )
        self.assertTrue(
            (Path(empty_workspace) / FAMILY_PROFILES / "alpha.json").exists()
        )
        self.assertTrue(
            (Path(empty_workspace) / FAMILY_PROFILES / "beta.json").exists()
        )

    def test_remote_sidecar_deletion_merges_cleanly(self) -> None:
        """Deleting a tracked sidecar commits, pushes, and removes it from
        the remote (deletion is valid; absence needs no helper)."""
        self.repo.add_tracked_file(FAMILY_DEFAULT, VALID_SIDECAR_TEXT + "\n")
        self.repo.commit_all("add sidecar")
        self.repo.push_to_remote()
        self.repo.assert_remote_tracks_file(FAMILY_DEFAULT, VALID_SIDECAR_TEXT + "\n")

        self._sidecar_path().unlink()
        result = self._run_tool({"action": "sync", "message": "delete sidecar"})

        self.assertEqual(0, result.returncode, result.stderr)
        proc = self.repo.remote_show_file(FAMILY_DEFAULT)
        self.assertNotEqual(
            0, proc.returncode, "remote must no longer track the deleted sidecar"
        )

    def test_remote_without_sidecars_is_valid(self) -> None:
        """A remote that never carried sidecars passes acceptance."""
        result = self._run_lifecycle("startup")
        self.assertEqual(0, result.returncode, result.stderr)

    def test_old_remote_history_without_marker_is_valid_and_merge_retains_local_marker(
        self,
    ) -> None:
        """A remote whose reachable history never contained the marker is
        valid; merging it must retain the local first-time marker, and the
        merged result pushes."""
        # Local first-time marker commit (unpushed; remote has no marker).
        self._allow_family_in_workspace()
        self._write_marker()
        result = self._run_tool({"action": "commit", "message": "first marker"})
        self.assertEqual(0, result.returncode, result.stderr)

        # Remote advances independently — its history never had the marker.
        self.repo._advance_remote_with_file("notes/remote.md", "from-remote\n")
        self.repo.git(["fetch", "origin"])

        result = self._run_lifecycle("periodic")
        self.assertEqual(0, result.returncode, result.stderr)

        # The local first-time marker survived the remote-wins merge.
        self.assertTrue(self._marker_path().exists())
        self.assertEqual(
            MARKER_TEXT + "\n", self._marker_path().read_text(encoding="utf-8")
        )
        self.assertTrue(self._head_has(FAMILY_MARKER))
        # Periodic pushed the merged result: remote now tracks both.
        self.repo.assert_remote_tracks_file(FAMILY_MARKER, MARKER_TEXT + "\n")
        self.repo.assert_remote_tracks_file("notes/remote.md", "from-remote\n")


# ---------------------------------------------------------------------------
# Migration marker (plan v2): content validation + monotonicity
# ---------------------------------------------------------------------------


class MarkerStagingValidationTests(_CommandAllowlistSyncTest):
    """Marker content validation and working-copy monotonicity at staging."""

    def test_first_time_marker_addition_commits_and_pushes(self) -> None:
        """Initial first-time marker addition is valid end to end."""
        self._init_bare_remote_with_initial_push()
        self._allow_family_in_workspace()
        self._write_marker()

        result = self._run_tool({"action": "commit", "message": "first marker"})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(FAMILY_MARKER, self._tracked_files())

        result = self._run_tool({"action": "push"})
        self.assertEqual(0, result.returncode, result.stderr)
        self.repo.assert_remote_tracks_file(FAMILY_MARKER, MARKER_TEXT + "\n")

    def test_marker_absence_is_valid(self) -> None:
        """A manifest-admitted marker that is absent (and never committed)
        is valid — absence needs no marker validator."""
        self._allow_family_in_workspace()
        result = self._run_tool({"action": "commit", "message": "no marker"})
        self.assertEqual(0, result.returncode, result.stderr)

    def test_malformed_marker_rejected_before_stage(self) -> None:
        """Every malformed marker shape fails staging closed, value-free,
        with nothing staged."""
        for bad in INVALID_MARKERS:
            with self.subTest(bad=bad):
                self._write_marker(bad)
                result = self._run_tool(
                    {"action": "commit", "message": "bad marker"}
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn(
                    "migration marker validation failed", result.stderr
                )
                staged = self.repo.git_check(["diff", "--cached", "--name-only"])
                self.assertNotIn(FAMILY_MARKER, staged.stdout)
                self.assertNotIn(FAMILY_MARKER, self._tracked_files())
                self._marker_path().unlink()

    def test_marker_error_values_never_appear(self) -> None:
        """Marker validation errors are structural only — a wrong-typed
        value never appears in stdout/stderr or the error JSON."""
        secret_value = "secret-marker-value-xyz"
        self._write_marker(
            json.dumps(
                {"version": 1, "legacy_runtime_import_complete": secret_value}
            )
        )

        result = self._run_tool({"action": "commit", "message": "leak check"})

        self.assertNotEqual(0, result.returncode)
        combined = result.stdout + result.stderr
        self.assertIn("migration marker validation failed", combined)
        self.assertNotIn(secret_value, combined)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["success"])
        self.assertNotIn(secret_value, doc["error"])

    def test_marker_validator_unavailable_fails_closed_when_present(self) -> None:
        """A present marker with an unavailable helper fails closed —
        validation is never skipped when the marker is present."""
        self._write_marker()
        result = self._run_tool(
            {"action": "commit", "message": "no validator"},
            JOSEMAR_SKILL_STATE="/nonexistent/helper.py",
        )
        self.assertNotEqual(0, result.returncode)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["success"])
        self.assertIn("unavailable", doc["error"].lower())

    def test_working_copy_marker_deletion_rejected_iff_head_has_marker(
        self,
    ) -> None:
        """Working-copy deletion is rejected iff HEAD contains the marker:
        after the marker is committed, deleting the file must fail
        staging; with a markerless HEAD the same absence is valid."""
        self._init_bare_remote_with_initial_push()
        self._allow_family_in_workspace()
        self._write_marker()
        result = self._run_tool({"action": "commit", "message": "marker"})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self._head_has(FAMILY_MARKER))

        self._marker_path().unlink()
        result = self._run_tool({"action": "commit", "message": "delete marker"})
        self.assertNotEqual(0, result.returncode)
        self.assertIn("marker deletion rejected", result.stderr)
        # HEAD still carries the marker (nothing was committed).
        self.assertTrue(self._head_has(FAMILY_MARKER))


class MarkerMonotonicityBoundaryTests(_CommandAllowlistSyncTest):
    """History-aware marker monotonicity at the push and remote-merge
    boundaries."""

    def setUp(self) -> None:
        super().setUp()
        self._init_bare_remote_with_initial_push()
        self._allow_family_in_workspace()
        self._write_marker()
        result = self._run_tool({"action": "commit", "message": "marker"})
        self.assertEqual(0, result.returncode, result.stderr)
        result = self._run_tool({"action": "push"})
        self.assertEqual(0, result.returncode, result.stderr)

    def test_manually_committed_marker_deletion_blocked_before_push(self) -> None:
        """A deletion committed by another path (direct git) must not push
        when reachable history previously contained the marker; the remote
        keeps the marker."""
        self._marker_path().unlink()
        self.repo.git(["add", "-A", "--", FAMILY_MARKER])
        self.repo.git(["commit", "-qm", "manually remove marker"])
        self.assertFalse(self._head_has(FAMILY_MARKER))

        result = self._run_tool({"action": "push"})

        self.assertNotEqual(0, result.returncode)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["success"])
        self.assertIn("marker deletion rejected", doc["error"])
        # The remote still tracks the marker (push was blocked).
        self.repo.assert_remote_tracks_file(FAMILY_MARKER, MARKER_TEXT + "\n")

    def test_head_marker_content_validated_before_push(self) -> None:
        """A malformed marker committed by another path must fail the push
        closed (HEAD content gate)."""
        self._write_marker('{"version":1}')
        self.repo.git(["add", "-f", FAMILY_MARKER])
        self.repo.git(["commit", "--amend", "--no-edit"])

        result = self._run_tool({"action": "push"})

        self.assertNotEqual(0, result.returncode)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["success"])
        self.assertIn("migration marker validation failed", doc["error"])

    def test_remote_malformed_marker_rejected_before_merge(self) -> None:
        """A remote candidate carrying a malformed marker fails startup
        closed before any merge; the local marker is unchanged and no
        remote content is merged."""
        bad_remote = WorkspaceRepo.build_bare_remote_with_state(
            self._extra_temp_dirs,
            initial_commit_subject="bad marker remote",
            extra_tracked={FAMILY_MARKER: '{"version":1}'},
        )
        self.repo.git(["remote", "set-url", "origin", bad_remote])
        self.repo.git(["fetch", "origin"])

        result = self._run_lifecycle("startup")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("migration marker validation failed", result.stderr)
        # No merge occurred: the local marker is exactly as committed.
        self.assertEqual(
            MARKER_TEXT + "\n", self._marker_path().read_text(encoding="utf-8")
        )

    def test_remote_valid_marker_accepted_on_clone(self) -> None:
        """A remote carrying the exact marker clones and restores it."""
        good_remote = WorkspaceRepo.build_bare_remote_with_state(
            self._extra_temp_dirs,
            initial_commit_subject="good marker remote",
            extra_tracked={FAMILY_MARKER: MARKER_TEXT + "\n"},
        )
        empty_workspace = self._mk_temp_dir()

        result = self._run_lifecycle(
            "startup", workspace=empty_workspace, remote=good_remote
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            MARKER_TEXT + "\n",
            (Path(empty_workspace) / FAMILY_MARKER).read_text(encoding="utf-8"),
        )

    def test_remote_marker_deletion_rejected_before_merge(self) -> None:
        """A remote candidate that removes the marker when the remote
        history previously contained it must not merge; the local marker
        is retained untouched."""
        self._advance_remote_deleting(FAMILY_MARKER)
        self.repo.git(["fetch", "origin"])

        result = self._run_lifecycle("periodic")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("marker deletion rejected", result.stderr)
        # Local marker retained; no merge occurred.
        self.assertTrue(self._marker_path().exists())
        self.assertTrue(self._head_has(FAMILY_MARKER))

    def test_remote_deletion_commit_author_email_is_remote(self) -> None:
        """The remote-authored deletion commit must carry the fixture's
        REMOTE_GIT_EMAIL author, so a host-global git identity cannot mask
        a regression in the remote-advance helper."""
        self._advance_remote_deleting(FAMILY_MARKER)
        self.repo.git(["fetch", "origin"])

        proc = self.repo.git_check(
            ["log", "-1", "--format=%ae", "origin/main"]
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(REMOTE_GIT_EMAIL, proc.stdout.strip())

    def test_sidecar_deletion_still_allowed_while_marker_present(self) -> None:
        """Normal allowlist-sidecar deletion is unaffected by the marker:
        only the marker is monotonic."""
        self._write_valid_sidecar()
        result = self._run_tool({"action": "commit", "message": "sidecar"})
        self.assertEqual(0, result.returncode, result.stderr)
        result = self._run_tool({"action": "push"})
        self.assertEqual(0, result.returncode, result.stderr)

        self._sidecar_path().unlink()
        result = self._run_tool({"action": "sync", "message": "delete sidecar"})
        self.assertEqual(0, result.returncode, result.stderr)

        proc = self.repo.remote_show_file(FAMILY_DEFAULT)
        self.assertNotEqual(0, proc.returncode, "sidecar deletion must propagate")
        # The marker remains present locally and on the remote.
        self.assertTrue(self._marker_path().exists())
        self.repo.assert_remote_tracks_file(FAMILY_MARKER, MARKER_TEXT + "\n")


# ---------------------------------------------------------------------------
# Manifest wildcard policy: narrow accepted, broader denied
# ---------------------------------------------------------------------------


class WildcardPolicyTests(_CommandAllowlistSyncTest):
    """Only the exact sanctioned wildcard is accepted; broader forms and
    arbitrary family files stay rejected/ignored."""

    def test_exact_profile_wildcard_accepted(self) -> None:
        """The exact sanctioned wildcard stages canonical profile sidecars
        and nothing else (non-json siblings stay untracked)."""
        self.repo.set_manifest(
            f"skills/.gitkeep\n{FAMILY_PROFILES}/*.json\n"
        )
        gitignore = Path(self.repo.workspace) / ".gitignore"
        lines = gitignore.read_text(encoding="utf-8").rstrip("\n").splitlines()
        lines.extend(
            [
                "!hermes/",
                "!hermes/command-allowlist/",
                "!hermes/command-allowlist/profiles/",
                "!hermes/command-allowlist/profiles/*.json",
            ]
        )
        gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._write_valid_sidecar(["sample-cmd-one"], profile="alpha")
        notes = Path(self.repo.workspace) / FAMILY_PROFILES / "notes.txt"
        notes.write_text("not a sidecar\n", encoding="utf-8")

        result = self._run_tool({"action": "commit", "message": "wildcard"})

        self.assertEqual(0, result.returncode, result.stderr)
        tracked = self._tracked_files()
        self.assertIn(f"{FAMILY_PROFILES}/alpha.json", tracked)
        self.assertNotIn(f"{FAMILY_PROFILES}/notes.txt", tracked)

    def test_broader_wildcards_denied(self) -> None:
        """Every broader form is rejected as a disallowed wildcard."""
        for path in (
            "hermes/command-allowlist/**",
            "hermes/command-allowlist/*",
            "hermes/command-allowlist/*.json",
            "hermes/command-allowlist/profiles/*",
            "hermes/**/*.json",
            "hermes/*",
        ):
            with self.subTest(path=path):
                self.repo.set_manifest(f"skills/.gitkeep\n{path}\n")
                result = self._run_tool(
                    {"action": "commit", "message": "broad"}
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn("disallowed wildcard", result.stderr)

    def test_arbitrary_family_json_stays_ignored(self) -> None:
        """An explicit arbitrary json path inside the family is ignored by
        the deny-by-default gitignore (only the exact shapes are allowed)."""
        self.repo.set_manifest(f"skills/.gitkeep\n{FAMILY_DIR}/extra.json\n")
        for rule in FAMILY_GITIGNORE_CHAIN:
            gitignore = Path(self.repo.workspace) / ".gitignore"
            lines = gitignore.read_text(encoding="utf-8").rstrip("\n").splitlines()
            if rule not in lines:
                lines.append(rule)
            gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
        extra = Path(self.repo.workspace) / FAMILY_DIR / "extra.json"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text('{"arbitrary": true}\n', encoding="utf-8")

        result = self._run_tool({"action": "commit", "message": "arbitrary"})

        self.assertNotEqual(0, result.returncode)
        self.assertIn("ignored by .gitignore", result.stderr)
        self.assertNotIn(f"{FAMILY_DIR}/extra.json", self._tracked_files())


if __name__ == "__main__":
    unittest.main()
