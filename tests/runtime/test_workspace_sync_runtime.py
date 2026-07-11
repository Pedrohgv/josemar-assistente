from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_SYNC_SCRIPT = REPO_ROOT / "scripts" / "workspace-sync.sh"
CRON_WRAPPER_SCRIPT = REPO_ROOT / "scripts" / "hermes-workspace-sync-cron.sh"
TEST_GIT_EMAIL = "test" + "@example.invalid"


class WorkspaceSyncRuntimeTests(unittest.TestCase):
    """Fast, isolated runtime tests for scripts/workspace-sync.sh and the cron wrapper.

    These tests use temp git workspaces and a local bare remote; no Docker or
    network access is required.
    """

    def setUp(self) -> None:
        self._isolate_git_environment()
        self._temp_dirs: list[tempfile.TemporaryDirectory] = []
        self.workspace = self._mk_dir()
        self.remote = self._mk_dir() + ".git"
        self._init_bare_remote_and_workspace()

    def tearDown(self) -> None:
        for td in self._temp_dirs:
            td.cleanup()

    # ------------------------------------------------------------------
    # workspace-sync.sh protected runtime path
    # ------------------------------------------------------------------

    def test_workspace_sync_script_protects_gbrain_runtime_path(self) -> None:
        # The workspace-sync.sh script must reject .gbrain in .sync-manifest
        # so PGLite DB/config/marker are never versioned.
        (Path(self.workspace) / ".sync-manifest").write_text(".gbrain\n", encoding="utf-8")

        result = self._run_workspace_sync_script()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("protected runtime path", result.stderr)

    # ------------------------------------------------------------------
    # workspace-sync.sh initial clone path
    # ------------------------------------------------------------------

    def test_initial_clone_restores_remote_state_without_bootstrap_commit(self) -> None:
        memory_content = "# user memory\nremote-authored\n"
        initial_commit_subject = "remote initial state"
        clone_remote = self._build_bare_remote_with_state(
            initial_commit_subject=initial_commit_subject,
            extra_tracked={"memories/MEMORY.md": memory_content},
        )
        empty_workspace = self._mk_dir()

        result = self._run_workspace_sync_script_with(
            workspace=empty_workspace,
            remote=clone_remote,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((Path(empty_workspace) / ".git").exists(), "workspace should be a git repo after clone")
        restored = (Path(empty_workspace) / "memories" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertEqual(memory_content, restored, "manifest-tracked file should be restored from remote")
        self.assertTrue((Path(empty_workspace) / ".sync-manifest").exists())
        self.assertTrue((Path(empty_workspace) / "skills" / ".gitkeep").exists())
        log = self._git_log_oneline_for(empty_workspace, limit=5)
        self.assertIn(initial_commit_subject, log)
        self.assertNotIn("Auto-commit", log)
        self.assertNotIn("Auto-sync", log)

    # ------------------------------------------------------------------
    # cron wrapper exit propagation
    # ------------------------------------------------------------------

    def test_cron_wrapper_propagates_success(self) -> None:
        fake = self._make_fake_sync(exit_code=0, stdout="FAKE_OK", stderr="")
        wrapper = self._patched_cron_wrapper(fake)

        result = self._run_cron_wrapper(wrapper)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_cron_wrapper_propagates_failure(self) -> None:
        fake = self._make_fake_sync(exit_code=7, stdout="FAKE_FAIL", stderr="err_marker")
        wrapper = self._patched_cron_wrapper(fake)

        result = self._run_cron_wrapper(wrapper)

        self.assertNotEqual(0, result.returncode, "wrapper must propagate nonzero exit")
        combined = result.stdout + result.stderr
        self.assertIn("FAKE_FAIL", combined)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _isolate_git_environment(self) -> None:
        self._saved_git_env = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("GIT_")}
        self.addCleanup(self._restore_git_environment)

    def _restore_git_environment(self) -> None:
        for key in list(os.environ):
            if key.startswith("GIT_"):
                os.environ.pop(key)
        os.environ.update(self._saved_git_env)

    def _mk_dir(self) -> str:
        td = tempfile.TemporaryDirectory(prefix="ws-runtime-")
        self._temp_dirs.append(td)
        return td.name

    def _init_bare_remote_and_workspace(self) -> None:
        # Bare remote
        subprocess.run(
            ["git", "init", "-q", "--bare", self.remote],
            check=True,
            capture_output=True,
            text=True,
        )
        # Workspace repo on main with initial state
        subprocess.run(["git", "init", "-q", self.workspace], check=True, capture_output=True, text=True)
        self._git(["config", "user.email", TEST_GIT_EMAIL])
        self._git(["config", "user.name", "Test User"])
        self._git(["checkout", "-q", "-B", "main"])
        ws = Path(self.workspace)
        (ws / "skills").mkdir(exist_ok=True)
        (ws / "skills" / ".gitkeep").touch()
        (ws / ".gitignore").write_text(
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
        (ws / ".sync-manifest").write_text("skills/.gitkeep\n", encoding="utf-8")
        self._git(["add", ".gitignore", ".sync-manifest", "skills/.gitkeep"])
        self._git(["commit", "-qm", "initial state"])
        self._git(["remote", "add", "origin", self.remote])
        self._git(["push", "-q", "-u", "origin", "main"])

    def _run_workspace_sync_script(self) -> subprocess.CompletedProcess[str]:
        return self._run_workspace_sync_script_with(workspace=self.workspace, remote=self.remote)

    def _run_workspace_sync_script_with(
        self,
        *,
        workspace: str,
        remote: str,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "WORKSPACE_DIR": workspace,
                "WORKSPACE_STATE_REPO": remote,
                "WORKSPACE_GIT_BRANCH": "main",
                "WORKSPACE_SYNC_ON_START": "true",
            }
        )
        return subprocess.run(
            ["bash", str(WORKSPACE_SYNC_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _build_bare_remote_with_state(
        self,
        *,
        initial_commit_subject: str,
        extra_tracked: dict[str, str] | None = None,
    ) -> str:
        """Build a local bare remote on ``main`` with committed state files.

        Authors the state in a temp source repo (with .gitignore allow rules for
        any extra tracked paths), pushes to a freshly created bare remote, and
        returns the bare remote path. The remote ``main`` ref contains the
        standard state files plus any ``extra_tracked`` entries.
        """
        source = self._mk_dir()
        subprocess.run(["git", "init", "-q", source], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", source, "config", "user.email", TEST_GIT_EMAIL],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", source, "config", "user.name", "Test User"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "-C", source, "checkout", "-q", "-B", "main"], check=True, capture_output=True, text=True)

        src = Path(source)
        (src / "skills").mkdir(exist_ok=True)
        (src / "skills" / ".gitkeep").touch()

        gitignore_lines = [
            "*",
            "!.gitignore",
            "!.sync-manifest",
            "!skills/",
            "!skills/.gitkeep",
        ]
        manifest_lines = ["skills/.gitkeep"]

        if extra_tracked:
            for relative_path, content in extra_tracked.items():
                target = src / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                # Un-ignore each parent directory then the file itself.
                parts = relative_path.split("/")
                for i in range(1, len(parts)):
                    gitignore_lines.append(f"!{'/'.join(parts[:i])}/")
                gitignore_lines.append(f"!{relative_path}")
                manifest_lines.append(relative_path)

        (src / ".gitignore").write_text("\n".join(gitignore_lines) + "\n", encoding="utf-8")
        (src / ".sync-manifest").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

        add_args = [".gitignore", ".sync-manifest", "skills/.gitkeep"]
        if extra_tracked:
            add_args.extend(extra_tracked.keys())
        subprocess.run(
            ["git", "-C", source, "add", *add_args],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", source, "commit", "-qm", initial_commit_subject],
            check=True,
            capture_output=True,
            text=True,
        )

        bare = self._mk_dir() + ".git"
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", source, "remote", "add", "origin", bare],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", source, "push", "-q", "-u", "origin", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        return bare

    def _git_log_oneline_for(self, repo: str, *, limit: int) -> str:
        proc = subprocess.run(
            ["git", "-C", repo, "log", "--oneline", f"-n{limit}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def _git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", self.workspace, *args],
            capture_output=True,
            text=True,
            check=True,
        )

    def _git_ls_files(self) -> set[str]:
        proc = subprocess.run(
            ["git", "-C", self.workspace, "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        )
        return set(proc.stdout.splitlines())

    def _git_log_oneline(self, *, limit: int) -> str:
        proc = subprocess.run(
            ["git", "-C", self.workspace, "log", "--oneline", f"-n{limit}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def _git_rev_parse_head(self) -> str:
        proc = subprocess.run(
            ["git", "-C", self.workspace, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def _make_fake_sync(self, *, exit_code: int, stdout: str, stderr: str) -> Path:
        bin_dir = Path(self._mk_dir())
        fake = bin_dir / "fake-sync.sh"
        fake.write_text(
            "#!/bin/sh\n"
            f"echo '{stdout}'\n"
            f"echo '{stderr}' >&2\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    def _patched_cron_wrapper(self, fake_sync: Path) -> Path:
        """Return a copy of the cron wrapper with the hardcoded sync path replaced.

        The production wrapper hardcodes ``/usr/local/bin/workspace-sync.sh`` which
        is not writable in the test environment, so we substitute the fake script
        path while preserving the rest of the wrapper logic verbatim.
        """
        src = CRON_WRAPPER_SCRIPT.read_text(encoding="utf-8")
        patched = src.replace("/usr/local/bin/workspace-sync.sh", str(fake_sync))
        self.assertNotEqual(src, patched, "cron wrapper does not reference expected hardcoded path")
        out_dir = Path(self._mk_dir())
        wrapper = out_dir / "cron-wrapper.sh"
        wrapper.write_text(patched, encoding="utf-8")
        wrapper.chmod(0o755)
        return wrapper

    def _run_cron_wrapper(self, wrapper: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["TMPDIR"] = str(Path(wrapper).parent)
        return subprocess.run(
            ["bash", str(wrapper)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
