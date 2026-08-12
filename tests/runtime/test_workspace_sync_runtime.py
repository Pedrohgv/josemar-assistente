from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_SYNC_SCRIPT = REPO_ROOT / "scripts" / "workspace_sync.py"
CRON_WRAPPER_SCRIPT = REPO_ROOT / "scripts" / "hermes-workspace-sync-cron.sh"
GBRAIN_REFRESH_CRON_SCRIPT = REPO_ROOT / "scripts" / "hermes-gbrain-refresh-cron.sh"
TASKNOTES_LOCK_RUNNER = REPO_ROOT / "scripts" / "tasknotes_lock_run.py"
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
        # The workspace-sync.sh script must reject .gbrain/config.json
        # in .sync-manifest so the PGLite config is never versioned.
        # Protection was narrowed from the whole .gbrain/ directory to
        # specific files (.gbrain/config.json, .gbrain/brain.pglite, etc.)
        # so that .gbrain/schema-packs/ can be state-owned.
        (Path(self.workspace) / ".sync-manifest").write_text(".gbrain/config.json\n", encoding="utf-8")

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

    def test_gbrain_refresh_cron_wrapper_propagates_success(self) -> None:
        fake = self._make_fake_sync(exit_code=0, stdout="GBRAIN_OK", stderr="")
        wrapper = self._patched_cron_wrapper(
            fake,
            source=GBRAIN_REFRESH_CRON_SCRIPT,
            hardcoded_path="/usr/local/bin/josemar-gbrain",
        )

        result = self._run_cron_wrapper(wrapper)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_gbrain_refresh_cron_wrapper_propagates_failure(self) -> None:
        fake = self._make_fake_sync(exit_code=9, stdout="GBRAIN_FAIL", stderr="refresh_err")
        wrapper = self._patched_cron_wrapper(
            fake,
            source=GBRAIN_REFRESH_CRON_SCRIPT,
            hardcoded_path="/usr/local/bin/josemar-gbrain",
        )

        result = self._run_cron_wrapper(wrapper)

        self.assertNotEqual(0, result.returncode, "gbrain refresh wrapper must propagate nonzero exit")
        combined = result.stdout + result.stderr
        self.assertIn("GBRAIN_FAIL", combined)

    def test_gbrain_refresh_cron_skips_when_tasknotes_lock_is_busy(self) -> None:
        fake = self._make_fake_sync(exit_code=0, stdout="MUST_NOT_RUN", stderr="")
        wrapper = self._patched_cron_wrapper(
            fake,
            source=GBRAIN_REFRESH_CRON_SCRIPT,
            hardcoded_path="/usr/local/bin/josemar-gbrain",
        )
        lock_path = Path(wrapper).parent / "tasknotes.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self._run_cron_wrapper(
                wrapper, extra_env={"TASKNOTES_LOCK_PATH": str(lock_path)}
            )
        finally:
            os.close(fd)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("refresh skipped", result.stdout)
        self.assertNotIn("MUST_NOT_RUN", result.stdout + result.stderr)

    def test_gbrain_refresh_cron_refuses_to_run_as_root(self) -> None:
        """The cron must enforce the hermes runtime identity before touching
        the lock: a root UID (simulated via a fake id binary) refuses with a
        clear message instead of relying on base-image behavior."""
        fake_id = Path(self._mk_dir()) / "id"
        fake_id.write_text("#!/bin/sh\necho 0\n", encoding="utf-8")
        fake_id.chmod(0o755)
        src = GBRAIN_REFRESH_CRON_SCRIPT.read_text(encoding="utf-8")
        patched = src.replace("/usr/bin/id", str(fake_id))
        self.assertNotEqual(src, patched)
        out_dir = Path(self._mk_dir())
        wrapper = out_dir / "cron-root.sh"
        wrapper.write_text(patched, encoding="utf-8")
        wrapper.chmod(0o755)
        result = self._run_cron_wrapper(wrapper)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refuses to run as root", result.stderr)

    def test_gbrain_refresh_cron_fails_closed_when_uid_unknown(self) -> None:
        """A failing/garbage id lookup must refuse (fail closed), never
        proceed as if non-root."""
        fake_id = Path(self._mk_dir()) / "id"
        fake_id.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_id.chmod(0o755)
        src = GBRAIN_REFRESH_CRON_SCRIPT.read_text(encoding="utf-8")
        patched = src.replace("/usr/bin/id", str(fake_id))
        out_dir = Path(self._mk_dir())
        wrapper = out_dir / "cron-noid.sh"
        wrapper.write_text(patched, encoding="utf-8")
        wrapper.chmod(0o755)
        result = self._run_cron_wrapper(wrapper)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("could not determine the effective UID", result.stderr)

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
            [sys.executable, str(WORKSPACE_SYNC_SCRIPT), "startup"],
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

    def _patched_cron_wrapper(
        self,
        fake_sync: Path,
        *,
        source: Path = CRON_WRAPPER_SCRIPT,
        hardcoded_path: str = "/usr/local/bin/workspace-sync periodic",
    ) -> Path:
        """Return a copy of the cron wrapper with the hardcoded sync path replaced.

        The production wrapper hardcodes binary paths which
        are not writable in the test environment, so we substitute the fake script
        path while preserving the rest of the wrapper logic verbatim. The gbrain
        refresh cron also hardcodes the fixed image interpreter
        (/opt/hermes/.venv/bin/python3, absent locally), the lock runner path,
        and the lock path; the copy substitutes local equivalents so the
        fixture runs without touching the production script (which keeps no
        environment seam for any of them).
        """
        src = source.read_text(encoding="utf-8")
        patched = src.replace(hardcoded_path, str(fake_sync))
        patched = patched.replace("/opt/hermes/.venv/bin/python3", sys.executable)
        patched = patched.replace(
            "/opt/josemar/scripts/tasknotes_lock_run.py", str(TASKNOTES_LOCK_RUNNER)
        )
        out_dir = Path(self._mk_dir())
        lock_path = out_dir / "tasknotes.lock"
        patched = patched.replace(
            'lock_path="/opt/data/.locks/tasknotes.lock"', f'lock_path="{lock_path}"'
        )
        self.assertNotEqual(src, patched, "cron wrapper does not reference expected hardcoded path")
        wrapper = out_dir / "cron-wrapper.sh"
        wrapper.write_text(patched, encoding="utf-8")
        wrapper.chmod(0o755)
        return wrapper

    def _run_cron_wrapper(
        self, wrapper: Path, *, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["TMPDIR"] = str(Path(wrapper).parent)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(wrapper)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
