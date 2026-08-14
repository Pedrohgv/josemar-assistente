"""Opt-in built-image runtime test for the real gbrain TaskNotes lifecycle."""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_SCRIPT = REPO_ROOT / "tests" / "tasknotes_mcp" / "real_gbrain_e2e.py"
IMAGE = os.environ.get("TASKNOTES_TEST_IMAGE", "josemar-assistente-hermes:latest")

# Fixed deployment contract (issue #110): the vault, gbrain state, and the
# shared lock live at pinned paths inside the container. The harness
# provisions a fresh disposable host directory, mounts it there, and owns its
# removal after the container exits (the e2e never deletes anything).
CONTAINER_DATA_DIR = "/opt/data"

# Hard upper bound for uid/gid sanity validation (2**32 - 2); 0 (root) is
# always rejected because the MCP and the harness refuse root execution.
MAX_RUNTIME_ID = (1 << 32) - 2


def _validated_runtime_id(name: str, default: int) -> int:
    """Caller-provided runtime ids are only honored after validation: a
    positive integer within the uid/gid range, never 0 (root)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not 1 <= value <= MAX_RUNTIME_ID:
        raise ValueError(
            f"{name} must be a valid non-root uid/gid (1..{MAX_RUNTIME_ID}), got {value}"
        )
    return value


def _hermes_runtime_ids() -> tuple[int, int]:
    """The ids the Hermes runtime is configured to run as (docker-compose
    default 10000:10000)."""
    return (
        _validated_runtime_id("HERMES_UID", 10000),
        _validated_runtime_id("HERMES_GID", 10000),
    )


def _chown_data_dir(data_dir: Path, uid: int, gid: int) -> None:
    """Give the Hermes runtime user ownership of the disposable /opt/data
    mount. The image's default user is root, but the test container must run
    as the runtime user, so ownership is fixed by a short root helper run of
    the same image (the bind mount is fresh and empty; nothing else is
    touched)."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{data_dir}:{CONTAINER_DATA_DIR}",
            "--entrypoint",
            "/bin/sh",
            IMAGE,
            "-c",
            f"chown -R {uid}:{gid} {CONTAINER_DATA_DIR}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "failed to chown disposable /opt/data mount\n"
            f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
        )


@functools.lru_cache(maxsize=1)
def _e2e_module():
    """Load the e2e as a module for host-side proof simulation. Module-level
    imports are stdlib only (the MCP client is imported lazily inside the
    lifecycle), so this is safe on hosts without the image venv."""
    spec = importlib.util.spec_from_file_location("tasknotes_real_gbrain_e2e", E2E_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    os.environ.get("RUN_DOCKER_TESTS") == "1",
    "set RUN_DOCKER_TESTS=1 to run Docker runtime tests",
)
class TaskNotesDockerRuntimeTests(unittest.TestCase):
    def _run_e2e_container(
        self,
        data_dir: Path,
        uid: int,
        gid: int,
        *,
        as_runtime_user: bool,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess:
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
        ]
        if as_runtime_user:
            command += ["--user", f"{uid}:{gid}"]
        command += [
            "--entrypoint",
            "/opt/hermes/.venv/bin/python3",
            "-e",
            f"HOME={CONTAINER_DATA_DIR}",
            "-e",
            f"TASKNOTES_E2E_UID={uid}",
            "-e",
            f"TASKNOTES_E2E_GID={gid}",
            "-e",
            "TELEGRAM_BOT_TOKEN=",
            "-e",
            "PRIMARY_TELEGRAM_ID=",
            "-e",
            "TELEGRAM_ALLOWED_USERS=",
            "-e",
            "TELEGRAM_HOME_CHANNEL=",
            "-e",
            "GATEWAY_ALLOWED_USERS=",
            "-e",
            "ZAI_API_KEY=",
            "-e",
            "GLM_API_KEY=",
            "-e",
            "DEEPSEEK_API_KEY=",
            "-e",
            "OLLAMA_API_KEY=",
            "-e",
            "TAVILY_API_KEY=",
            "-v",
            f"{E2E_SCRIPT}:/tmp/real_gbrain_e2e.py:ro",
            "-v",
            f"{data_dir}:{CONTAINER_DATA_DIR}",
            IMAGE,
            "/tmp/real_gbrain_e2e.py",
        ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def test_real_gbrain_mcp_lifecycle_in_built_image(self) -> None:
        uid, gid = _hermes_runtime_ids()
        data_dir = Path(tempfile.mkdtemp(prefix="tasknotes-runtime-"))
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
        _chown_data_dir(data_dir, uid, gid)
        result = self._run_e2e_container(data_dir, uid, gid, as_runtime_user=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("real-gbrain MCP lifecycle: PASS", result.stdout)

    def test_root_container_run_is_rejected(self) -> None:
        """Regression: dropping --user makes the image default root; the e2e
        must refuse via its exact-identity proof instead of running the
        lifecycle as root."""
        uid, gid = _hermes_runtime_ids()
        data_dir = Path(tempfile.mkdtemp(prefix="tasknotes-runtime-"))
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
        _chown_data_dir(data_dir, uid, gid)
        result = self._run_e2e_container(data_dir, uid, gid, as_runtime_user=False)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("refuses", result.stdout + result.stderr)
        self.assertIn("identity mismatch", result.stdout + result.stderr)


class TaskNotesDockerHarnessContractTests(unittest.TestCase):
    """Static and host-side guards (always run, no Docker) so wrong-identity,
    host-execution, missing-container-evidence, or path-contract drift in the
    opt-in runtime harness cannot silently recur when RUN_DOCKER_TESTS is
    unset."""

    @staticmethod
    def _harness_text() -> str:
        return (REPO_ROOT / "tests" / "tasknotes_mcp" / "test_docker_runtime.py").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _e2e_text() -> str:
        return (REPO_ROOT / "tests" / "tasknotes_mcp" / "real_gbrain_e2e.py").read_text(
            encoding="utf-8"
        )

    def test_harness_runs_container_as_runtime_user_not_root(self) -> None:
        text = self._harness_text()
        self.assertIn('"--user"', text)
        self.assertIn('f"{uid}:{gid}"', text)
        # The expected identity is passed to the e2e so it can validate the
        # actual runtime identity exactly (not merely non-root).
        self.assertIn('f"TASKNOTES_E2E_UID={uid}"', text)
        self.assertIn('f"TASKNOTES_E2E_GID={gid}"', text)
        self.assertIn("as_runtime_user=True", text)
        # Caller-provided ids are validated before use.
        self.assertIn("_validated_runtime_id", text)
        self.assertIn("1 <= value <= MAX_RUNTIME_ID", text)

    def test_harness_mounts_disposable_data_and_owns_removal(self) -> None:
        text = self._harness_text()
        self.assertIn('CONTAINER_DATA_DIR = "/opt/data"', text)
        self.assertIn('f"{data_dir}:{CONTAINER_DATA_DIR}"', text)
        # The host fixture owns the fresh temporary directory and removes it
        # after container exit; the e2e must never delete /opt/data trees.
        self.assertIn("self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)", text)

    def test_e2e_proves_docker_harness_and_exact_identity(self) -> None:
        text = self._e2e_text()
        self.assertIn("tasknotes runtime harness refuses to run", text)
        self.assertIn('HARNESS_INTERPRETER = "/opt/hermes/.venv/bin/python3"', text)
        self.assertIn('HARNESS_SCRIPT_MOUNT = Path("/tmp/real_gbrain_e2e.py")', text)
        # Docker-native evidence, never env-driven.
        self.assertIn('"/.dockerenv"', text)
        self.assertIn("_has_docker_native_evidence", text)
        # Read-only script mount proof (mount table or safe write attempt).
        self.assertIn("_harness_script_is_readonly", text)
        self.assertIn("is not read-only", text)
        # Disposable mount + freshness + explicit pre-mutation recheck.
        self.assertIn("os.path.ismount", text)
        self.assertIn("not a fresh disposable mount", text)
        self.assertIn("_recheck_mount_safety", text)
        # Exact identity, not merely non-root.
        self.assertIn("os.geteuid() != expected_uid", text)
        self.assertIn("identity mismatch", text)
        self.assertIn('VAULT = Path("/opt/data/obsidian")', text)
        self.assertIn('GBRAIN_HOME = Path("/opt/data")', text)
        self.assertIn('LOCK_DIR = Path("/opt/data/.locks")', text)

    def test_e2e_has_no_host_bypasses_no_overrides_no_recursive_deletion(self) -> None:
        """No caller-controlled escape hatch may exist: no host-execution
        permission flag, no native gbrain executable override, and no
        recursive deletion of /opt/data trees (cleanup is owned by the host
        fixture)."""
        text = self._e2e_text()
        self.assertIn('GBRAIN_NATIVE = "/opt/josemar/libexec/gbrain-native"', text)
        self.assertNotIn("REAL_GBRAIN_E2E_ALLOW_HOST_DATA", text)
        self.assertNotIn("REAL_GBRAIN_E2E_NATIVE_BIN", text)
        self.assertNotIn('"TASKNOTES_LOCK_DIR"', text)
        self.assertNotIn("TASKNOTES_E2E_IN_CONTAINER", text)
        self.assertNotIn("rmtree", text)
        self.assertNotIn("shutil", text)

    def test_e2e_refuses_host_execution(self) -> None:
        """Behavioral regression: running the e2e directly on the host must
        be refused before it touches anything."""
        result = subprocess.run(
            [sys.executable, str(E2E_SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("refuses", result.stdout + result.stderr)

    def _simulated_harness_ctx(self, data_dir: Path, script_mount: Path) -> contextlib.ExitStack:
        """Simulate a host state that satisfies the interpreter, script-path,
        mountpoint, and freshness checks — so the proof can only pass if the
        remaining Docker-native/read-only evidence also holds."""
        e2e = _e2e_module()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(e2e, "HARNESS_INTERPRETER", sys.executable))
        stack.enter_context(mock.patch.object(e2e, "__file__", str(script_mount)))
        stack.enter_context(mock.patch.object(e2e, "HARNESS_SCRIPT_MOUNT", script_mount))
        stack.enter_context(mock.patch.object(e2e, "GBRAIN_HOME", data_dir))
        stack.enter_context(mock.patch.object(e2e, "_is_disposable_mount", return_value=True))
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {"TASKNOTES_E2E_UID": "10000", "TASKNOTES_E2E_GID": "10000"},
            )
        )
        return stack

    def test_e2e_refuses_simulated_host_without_docker_evidence(self) -> None:
        """A host state that satisfies mount/path checks but lacks
        Docker-native evidence must be refused before any mutation."""
        e2e = _e2e_module()
        with tempfile.TemporaryDirectory(prefix="tasknotes-sim-") as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            with tempfile.TemporaryDirectory(prefix="tasknotes-sim-mnt-") as mnt:
                script_mount = Path(mnt) / "real_gbrain_e2e.py"
                script_mount.write_text("simulated harness mount\n", encoding="utf-8")
                with self._simulated_harness_ctx(data_dir, script_mount) as stack:
                    stack.enter_context(
                        mock.patch.object(e2e, "_has_docker_native_evidence", return_value=False)
                    )
                    with self.assertRaisesRegex(RuntimeError, "Docker-native"):
                        e2e.main()
            # Refusal happened before any mutation or cleanup.
            self.assertEqual([], list(data_dir.iterdir()))

    def test_e2e_refuses_simulated_host_with_writable_script(self) -> None:
        """A host state that satisfies mount/path checks and Docker evidence
        but whose harness script is not read-only must be refused before any
        mutation."""
        e2e = _e2e_module()
        with tempfile.TemporaryDirectory(prefix="tasknotes-sim-") as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            with tempfile.TemporaryDirectory(prefix="tasknotes-sim-mnt-") as mnt:
                script_mount = Path(mnt) / "real_gbrain_e2e.py"
                script_mount.write_text("simulated harness mount\n", encoding="utf-8")
                with self._simulated_harness_ctx(data_dir, script_mount) as stack:
                    stack.enter_context(
                        mock.patch.object(e2e, "_has_docker_native_evidence", return_value=True)
                    )
                    with self.assertRaisesRegex(RuntimeError, "read-only"):
                        e2e.main()
            # Refusal happened before any mutation or cleanup.
            self.assertEqual([], list(data_dir.iterdir()))

    def test_e2e_cleanup_is_absent_and_host_fixture_owns_removal(self) -> None:
        """The e2e must never delete /opt/data trees; the outer fixture owns
        the disposable directory. Verified behaviorally: the loaded e2e has
        no cleanup routine that could touch the simulated data dir."""
        e2e = _e2e_module()
        self.assertFalse(hasattr(e2e, "_remove_disposable_state"))
        self.assertFalse(hasattr(e2e, "_cleanup"))


if __name__ == "__main__":
    unittest.main()
