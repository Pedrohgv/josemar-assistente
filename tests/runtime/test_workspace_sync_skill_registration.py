from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_SYNC_SKILL = REPO_ROOT / "skills-factory" / "workspace-sync" / "workspace-sync"
TEMPLATE_GITIGNORE = REPO_ROOT / "templates" / "agent-state-template" / ".gitignore"
TEST_GIT_EMAIL = "test" + "@example.invalid"
REMOTE_GIT_EMAIL = "remote" + "@example.invalid"


class WorkspaceSyncSkillRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._isolate_git_environment()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self._extra_temp_dirs: list[tempfile.TemporaryDirectory] = []
        self._write_old_style_state_files()
        self._run_git("init", "-q")
        self._run_git("config", "user.email", TEST_GIT_EMAIL)
        self._run_git("config", "user.name", "Test User")
        self._run_git("add", ".gitignore", ".sync-manifest", "skills/.gitkeep")
        self._run_git("commit", "-qm", "initial state")

    def tearDown(self) -> None:
        for td in self._extra_temp_dirs:
            td.cleanup()
        self.temp_dir.cleanup()

    def _isolate_git_environment(self) -> None:
        self._saved_git_env = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("GIT_")}
        self.addCleanup(self._restore_git_environment)

    def _restore_git_environment(self) -> None:
        for key in list(os.environ):
            if key.startswith("GIT_"):
                os.environ.pop(key)
        os.environ.update(self._saved_git_env)

    def test_commit_auto_registers_user_skill_files(self) -> None:
        skill_dir = self.workspace / "skills" / "auto-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Auto Skill\n", encoding="utf-8")
        (skill_dir / "auto-skill").write_text("#!/bin/sh\ntrue\n", encoding="utf-8")

        result = self._run_workspace_sync({"action": "commit", "message": "register skill"})

        self.assertTrue(result["success"])
        manifest = (self.workspace / ".sync-manifest").read_text(encoding="utf-8")
        gitignore = (self.workspace / ".gitignore").read_text(encoding="utf-8")
        tracked_files = self._tracked_files()

        self.assertIn("skills/auto-skill/SKILL.md", manifest)
        self.assertIn("skills/auto-skill/auto-skill", manifest)
        self.assertNotIn("skills/*", manifest)
        self.assertNotIn("skills/**", manifest)
        self.assertIn("!skills/**", gitignore)
        self.assertIn("skills/auto-skill/SKILL.md", tracked_files)
        self.assertIn("skills/auto-skill/auto-skill", tracked_files)

    def test_commit_skips_vault_gateway_and_non_skill_dirs(self) -> None:
        vault_gateway_dir = self.workspace / "skills" / "vault-gateway"
        vault_gateway_dir.mkdir(parents=True)
        (vault_gateway_dir / "SKILL.md").write_text("# Override\n", encoding="utf-8")
        scratch_dir = self.workspace / "skills" / "scratch"
        scratch_dir.mkdir(parents=True)
        (scratch_dir / "notes.txt").write_text("not a skill\n", encoding="utf-8")

        result = self._run_workspace_sync({"action": "commit", "message": "skip unsafe skills"})

        self.assertEqual("No changes to commit", result["message"])
        manifest = (self.workspace / ".sync-manifest").read_text(encoding="utf-8")
        tracked_files = self._tracked_files()

        self.assertNotIn("skills/vault-gateway/SKILL.md", manifest)
        self.assertNotIn("skills/scratch/notes.txt", manifest)
        self.assertNotIn("skills/vault-gateway/SKILL.md", tracked_files)
        self.assertNotIn("skills/scratch/notes.txt", tracked_files)

    def test_commit_registration_is_idempotent(self) -> None:
        skill_dir = self.workspace / "skills" / "auto-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Auto Skill\n", encoding="utf-8")

        self._run_workspace_sync({"action": "commit", "message": "register skill"})
        self._run_workspace_sync({"action": "commit", "message": "register skill again"})

        manifest_lines = (self.workspace / ".sync-manifest").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, manifest_lines.count("skills/auto-skill/SKILL.md"))

    def test_commit_registration_adds_gitignore_allow_once(self) -> None:
        (self.workspace / ".gitignore").write_text(
            (self.workspace / ".gitignore").read_text(encoding="utf-8") + "!skills/**\n",
            encoding="utf-8",
        )
        skill_dir = self.workspace / "skills" / "auto-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Auto Skill\n", encoding="utf-8")

        self._run_workspace_sync({"action": "commit", "message": "register skill"})
        self._run_workspace_sync({"action": "commit", "message": "register skill again"})

        gitignore_lines = (self.workspace / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, gitignore_lines.count("!skills/**"))

    def test_commit_auto_registers_nested_skill_files(self) -> None:
        skill_dir = self.workspace / "skills" / "nested-skill"
        helper_path = skill_dir / "lib" / "helper.py"
        helper_path.parent.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Nested Skill\n", encoding="utf-8")
        helper_path.write_text("print('ok')\n", encoding="utf-8")

        result = self._run_workspace_sync({"action": "commit", "message": "register nested skill"})

        self.assertTrue(result["success"])
        manifest = (self.workspace / ".sync-manifest").read_text(encoding="utf-8")
        tracked_files = self._tracked_files()
        self.assertIn("skills/nested-skill/lib/helper.py", manifest)
        self.assertIn("skills/nested-skill/lib/helper.py", tracked_files)

    def test_manifest_rejects_protected_runtime_paths(self) -> None:
        for path in ["config.yaml", "credentials/token.json", ".env"]:
            with self.subTest(path=path):
                (self.workspace / ".sync-manifest").write_text(f"{path}\n", encoding="utf-8")

                process = self._run_workspace_sync_raw({"action": "commit", "message": "bad manifest"})

                self.assertNotEqual(0, process.returncode)
                self.assertIn("protected runtime path", process.stderr)

    def test_manifest_rejects_unsafe_pathspecs(self) -> None:
        for path in ["../escape.md", "/etc/passwd", "foo/../bar", ":bad"]:
            with self.subTest(path=path):
                (self.workspace / ".sync-manifest").write_text(f"{path}\n", encoding="utf-8")

                process = self._run_workspace_sync_raw({"action": "commit", "message": "bad manifest"})

                self.assertNotEqual(0, process.returncode)
                self.assertIn("unsafe pathspec", process.stderr)

    def test_manifest_rejects_ignored_explicit_path(self) -> None:
        (self.workspace / ".gitignore").write_text(
            "\n".join(["*", "!.gitignore", "!.sync-manifest", "*.tmp", ""]),
            encoding="utf-8",
        )
        (self.workspace / ".sync-manifest").write_text("foo.tmp\n", encoding="utf-8")
        (self.workspace / "foo.tmp").write_text("ignored\n", encoding="utf-8")

        process = self._run_workspace_sync_raw({"action": "commit", "message": "bad manifest"})

        self.assertNotEqual(0, process.returncode)
        self.assertIn("ignored by .gitignore", process.stderr)

    def test_template_gitignore_allows_explicit_skill_paths(self) -> None:
        (self.workspace / ".gitignore").write_text(TEMPLATE_GITIGNORE.read_text(encoding="utf-8"), encoding="utf-8")

        result = self._run_git("check-ignore", "skills/example/SKILL.md", check=False)

        self.assertNotEqual(0, result.returncode)

    def test_manifest_rejects_skill_wildcards(self) -> None:
        (self.workspace / ".sync-manifest").write_text("skills/**\n", encoding="utf-8")

        process = self._run_workspace_sync_raw({"action": "commit", "message": "bad manifest"})

        self.assertNotEqual(0, process.returncode)
        self.assertIn("must use explicit skills paths", process.stderr)

    def test_status_reports_manifest_and_auth_state(self) -> None:
        home_dir = self.workspace / "home"
        home_dir.mkdir()

        result = self._run_workspace_sync({"action": "status"}, extra_env={"HOME": str(home_dir)})
        tracked_patterns = cast(list[str], result["tracked_patterns"])

        self.assertTrue(result["success"])
        self.assertEqual("status", result["action"])
        self.assertIn("skills/.gitkeep", tracked_patterns)
        self.assertFalse(result["auth_configured"])

    def test_sync_no_changes_returns_without_remote(self) -> None:
        result = self._run_workspace_sync({"action": "sync", "message": "sync no changes"})

        self.assertTrue(result["success"])
        self.assertEqual("sync", result["action"])
        self.assertEqual("No changes to sync", result["message"])

    # ------------------------------------------------------------------
    # slash-command parsing / action-source
    # ------------------------------------------------------------------

    def test_slash_command_status_runs_status_action(self) -> None:
        home_dir = self.workspace / "home"
        home_dir.mkdir()

        result = self._run_workspace_sync(
            {"commandName": "workspace-sync", "command": "/workspace-sync status"},
            extra_env={"HOME": str(home_dir)},
        )

        self.assertTrue(result["success"])
        self.assertEqual("status", result["action"])
        self.assertIn("skills/.gitkeep", cast(list[str], result["tracked_patterns"]))

    def test_slash_command_log_returns_one_commit(self) -> None:
        result = self._run_workspace_sync(
            {"commandName": "workspace-sync", "command": "/workspace-sync:log 1"},
        )

        self.assertTrue(result["success"])
        self.assertEqual("log", result["action"])
        commits = cast(list[str], result["commits"])
        self.assertEqual(1, len(commits))
        self.assertIn("initial state", commits[0])

    def test_slash_command_default_action_is_sync(self) -> None:
        result = self._run_workspace_sync({"commandName": "workspace-sync"})

        self.assertTrue(result["success"])
        self.assertEqual("sync", result["action"])
        self.assertEqual("No changes to sync", result["message"])

    # ------------------------------------------------------------------
    # local-remote action smoke tests (temp bare remote, no network)
    # ------------------------------------------------------------------

    def test_push_action_pushes_committed_change_to_remote(self) -> None:
        self._configure_bare_remote()
        self._add_tracked_manifest_file("notes/keep.md", "remote-bound\n")
        self._run_workspace_sync({"action": "commit", "message": "add tracked file"})

        result = self._run_workspace_sync({"action": "push"})

        self.assertTrue(result["success"])
        self.assertEqual("push", result["action"])
        self._assert_remote_tracks_file("notes/keep.md", "remote-bound\n")

    def test_pull_action_fast_forwards_to_remote_commit(self) -> None:
        self._configure_bare_remote()
        # Push current local state so the remote branch exists.
        self._run_git("push", "-q", "origin", self._current_branch())
        # Advance remote with an independent commit on a manifest-tracked file.
        remote_content = "from-remote\n"
        self._advance_remote_with_file("notes/keep.md", remote_content)

        result = self._run_workspace_sync({"action": "pull"})

        self.assertTrue(result["success"])
        self.assertEqual("pull", result["action"])
        self.assertEqual(remote_content, (self.workspace / "notes" / "keep.md").read_text(encoding="utf-8"))

    def test_sync_action_commits_and_pushes_to_remote(self) -> None:
        self._configure_bare_remote()
        self._add_tracked_manifest_file("notes/keep.md", "synced\n")
        # Push initial state so the remote branch exists before sync.
        self._run_git("push", "-q", "origin", self._current_branch())

        result = self._run_workspace_sync({"action": "sync", "message": "sync tracked file"})

        self.assertTrue(result["success"])
        self.assertEqual("sync", result["action"])
        self.assertTrue(result["push"])
        self._assert_remote_tracks_file("notes/keep.md", "synced\n")

    def _current_branch(self) -> str:
        proc = self._run_git("branch", "--show-current")
        return proc.stdout.strip()

    def _configure_bare_remote(self) -> None:
        """Create a local bare remote and wire it as ``origin`` for the workspace."""
        remote_dir = tempfile.TemporaryDirectory(prefix="ws-bare-remote-")
        self._extra_temp_dirs.append(remote_dir)
        self.remote_path = remote_dir.name
        subprocess.run(
            ["git", "init", "-q", "--bare", self.remote_path],
            check=True,
            capture_output=True,
            text=True,
        )
        self._run_git("remote", "add", "origin", self.remote_path)

    def _add_tracked_manifest_file(self, relative_path: str, content: str) -> None:
        """Create a manifest-tracked file and append it to .sync-manifest.

        The wrapper's manifest validator rejects paths ignored by .gitignore.
        The default test .gitignore uses ``*`` with narrow allowlist, so we add
        an explicit ``!`` allow rule for the file's directory to keep it
        trackable. The file is left unstaged; the wrapper's commit/sync actions
        handle staging via the manifest.
        """
        target = self.workspace / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        gitignore = self.workspace / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8")
        # ``*`` ignores everything; to un-ignore a nested file we must first
        # un-ignore each parent directory, then the file itself.
        parts = relative_path.split("/")
        allow_rules: list[str] = []
        for i in range(1, len(parts)):
            allow_rules.append(f"!{'/'.join(parts[:i])}/")
        allow_rules.append(f"!{relative_path}")
        new_lines = existing.rstrip("\n").splitlines()
        for rule in allow_rules:
            if rule not in new_lines:
                new_lines.append(rule)
        gitignore.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        with (self.workspace / ".sync-manifest").open("a", encoding="utf-8") as fh:
            fh.write(f"{relative_path}\n")

    def _advance_remote_with_file(self, relative_path: str, content: str) -> None:
        """Advance the bare remote with a new commit on ``main``.

        Uses a temporary clone to author the commit, then pushes it back to the
        bare remote so ``origin/main`` moves ahead of the local workspace.
        """
        clone_dir = tempfile.TemporaryDirectory(prefix="ws-remote-clone-")
        self._extra_temp_dirs.append(clone_dir)
        clone_path = clone_dir.name
        subprocess.run(
            ["git", "clone", "-q", self.remote_path, clone_path],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", clone_path, "config", "user.email", REMOTE_GIT_EMAIL],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", clone_path, "config", "user.name", "Remote Author"],
            check=True,
            capture_output=True,
            text=True,
        )
        target = Path(clone_path) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        # The cloned .gitignore uses ``*``; force-add to bypass it.
        subprocess.run(
            ["git", "-C", clone_path, "add", "-f", relative_path],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", clone_path, "commit", "-qm", "remote advance"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", clone_path, "push", "-q", "origin", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )

    def _assert_remote_tracks_file(self, relative_path: str, expected_content: str) -> None:
        """Assert the bare remote HEAD contains the file with expected content.

        Uses ``HEAD`` rather than a hardcoded branch ref because the wrapper
        pushes ``HEAD:<current-branch>`` and the test workspace's current
        branch follows the local git default (``main`` or ``master``).
        """
        proc = subprocess.run(
            ["git", "-C", self.remote_path, "show", f"HEAD:{relative_path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, proc.returncode, f"remote missing {relative_path}: {proc.stderr}")
        self.assertEqual(expected_content, proc.stdout)

    def _write_old_style_state_files(self) -> None:
        (self.workspace / "skills").mkdir()
        (self.workspace / "skills" / ".gitkeep").touch()
        (self.workspace / ".gitignore").write_text(
            "\n".join(
                [
                    "*",
                    "!.gitignore",
                    "!.sync-manifest",
                    "!skills/",
                    "!skills/.gitkeep",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (self.workspace / ".sync-manifest").write_text("skills/.gitkeep\n", encoding="utf-8")

    def _run_workspace_sync(self, payload: dict[str, str], extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        process = self._run_workspace_sync_raw(payload, extra_env=extra_env)
        self.assertEqual(process.returncode, 0, process.stderr)
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError:
            return json.loads(process.stdout.splitlines()[-1])

    def _run_workspace_sync_raw(
        self,
        payload: dict[str, str],
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(WORKSPACE_SYNC_SKILL)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.workspace), *args],
            text=True,
            capture_output=True,
            check=check,
        )

    def _tracked_files(self) -> set[str]:
        process = self._run_git("ls-files")
        return set(process.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
