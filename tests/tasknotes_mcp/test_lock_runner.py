"""Runtime tests for the shared TaskNotes/gbrain lock runner."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "tasknotes_lock_run.py"


class LockRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="tasknotes-lock-")
        self.root = Path(self.tempdir.name)
        self.lock_path = self.root / "locks" / "tasknotes.lock"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_shebang_uses_fixed_isolated_interpreter(self) -> None:
        """The runner shebang must not resolve python through PATH (env):
        the fixed image interpreter in isolated mode keeps hostile
        PYTHONPATH/sitecustomize from running code before the flock."""
        first = RUNNER.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, "#!/opt/hermes/.venv/bin/python3 -I")
        self.assertNotIn("/usr/bin/env", first)

    def _run(self, command: list[str], *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--lock-path",
                str(self.lock_path),
                *options,
                "--",
                *command,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def _spawn_holder(self, hold: float):
        """Spawn a process that flocks the lock file and holds it for `hold`s."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        code = (
            "import fcntl, os, time\n"
            f"fd = os.open({str(self.lock_path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            f"time.sleep({hold})\n"
        )
        return subprocess.Popen([sys.executable, "-c", code])

    def _grandchild_sleeper_code(self, pid_file: Path) -> str:
        """Child that ignores SIGTERM and spawns a long-sleeping grandchild,
        recording the grandchild pid so tests can assert group cleanup."""
        return (
            "import os, signal, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"Path({str(pid_file)!r}).write_text(str(proc.pid))\n"
            "time.sleep(60)\n"
        )

    def _assert_process_gone(self, pid: int) -> None:
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_propagates_child_success_and_failure(self) -> None:
        success = self._run([sys.executable, "-c", "raise SystemExit(0)"])
        failure = self._run([sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(failure.returncode, 7, failure.stderr)

    def test_nonblocking_busy_returns_distinct_status_without_running_child(self) -> None:
        self.lock_path.parent.mkdir(parents=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = self.root / "child-ran"
            result = self._run(
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                "--nonblocking",
            )
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("lock busy", result.stderr)
        self.assertFalse(marker.exists())

    def test_timeout_kills_and_reaps_child(self) -> None:
        result = self._run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            "--timeout",
            "0.2",
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("timed out", result.stderr)

    def test_symlink_lock_is_rejected(self) -> None:
        self.lock_path.parent.mkdir(parents=True)
        outside = self.root / "outside.lock"
        outside.write_text("", encoding="utf-8")
        self.lock_path.symlink_to(outside)
        result = self._run([sys.executable, "-c", "pass"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot open lock safely", result.stderr)

    # --- bounded lock acquisition ---

    def test_lock_timeout_waits_then_acquires(self) -> None:
        """Bounded acquisition must wait for the lock to free up, not fail
        immediately like --nonblocking."""
        holder = self._spawn_holder(hold=0.4)
        start = time.monotonic()
        try:
            result = self._run(
                [sys.executable, "-c", "raise SystemExit(0)"],
                "--lock-timeout",
                "10",
            )
            elapsed = time.monotonic() - start
        finally:
            holder.wait(timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(
            elapsed, 0.25, "runner must wait for the lock holder, not fail instantly"
        )

    def test_lock_timeout_expires_busy(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        start = time.monotonic()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self._run(
                [sys.executable, "-c", "pass"],
                "--lock-timeout",
                "0.3",
            )
            elapsed = time.monotonic() - start
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("timed out", result.stderr)
        self.assertGreaterEqual(elapsed, 0.2)

    def test_nonblocking_and_lock_timeout_are_mutually_exclusive(self) -> None:
        result = self._run(
            [sys.executable, "-c", "pass"],
            "--nonblocking",
            "--lock-timeout",
            "5",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("mutually exclusive", result.stderr)

    def test_rejects_non_finite_timeouts_and_grace(self) -> None:
        """NaN/inf/-inf must be rejected for every duration knob while 0
        (unbounded) and finite positive values stay accepted."""
        for option, bad in (
            ("--timeout", "nan"),
            ("--timeout", "inf"),
            ("--timeout", "-inf"),
            ("--lock-timeout", "nan"),
            ("--lock-timeout", "inf"),
            ("--lock-timeout", "-inf"),
            ("--kill-grace", "nan"),
            ("--kill-grace", "inf"),
            ("--kill-grace", "-inf"),
        ):
            with self.subTest(option=option, bad=bad):
                # equals form: a bare "-inf" would look like an option to
                # argparse; with the equals form it is parsed as the value.
                result = self._run(
                    [sys.executable, "-c", "raise SystemExit(0)"],
                    f"{option}={bad}",
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("finite", result.stderr)

    # --- group cleanup on timeout and signal ---

    def test_timeout_terminates_whole_group_after_grace(self) -> None:
        pid_file = self.root / "grandchild.pid"
        result = self._run(
            [sys.executable, "-c", self._grandchild_sleeper_code(pid_file)],
            "--timeout",
            "0.3",
            "--kill-grace",
            "0.2",
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertTrue(pid_file.exists(), "child must have started")
        self._assert_process_gone(int(pid_file.read_text()))

    def test_sigterm_terminates_whole_group_and_exits_128_plus(self) -> None:
        pid_file = self.root / "grandchild.pid"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                "--lock-path",
                str(self.lock_path),
                "--kill-grace",
                "0.2",
                "--",
                sys.executable,
                "-c",
                self._grandchild_sleeper_code(pid_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(), "child must have started")
        # The child (python startup + grandchild spawn) writes the pid file far
        # later than the parent installs its signal handlers, but sleep briefly
        # to make the ordering deterministic before signaling the parent.
        time.sleep(0.2)
        os.kill(proc.pid, signal.SIGTERM)
        _, stderr = proc.communicate(timeout=10)
        self.assertEqual(
            proc.returncode, 128 + signal.SIGTERM, stderr.decode()
        )
        self._assert_process_gone(int(pid_file.read_text()))

    # --- inherited lock fd for downstream wrappers ---

    def test_child_env_carries_valid_inherited_lock_fd(self) -> None:
        """The child must inherit the actual lock fd and be told its number
        (TASKNOTES_LOCK_FD) so wrappers can verify they run under this lock
        without any forgeable env boolean (issue #110). The child validates
        the fd itself: it must refer to the lock file and its own open file
        description must hold the flock."""
        env = os.environ.copy()
        env["LOCK_PATH_TEST"] = str(self.lock_path)
        child = (
            "import os\n"
            "fd = int(os.environ['TASKNOTES_LOCK_FD'])\n"
            "print(os.path.samefile(f'/proc/self/fd/{fd}', "
            "os.environ['LOCK_PATH_TEST']))\n"
            "info = open(f'/proc/self/fdinfo/{fd}').read()\n"
            "print(any(l.startswith('lock:') and 'FLOCK' in l "
            "for l in info.splitlines()))\n"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--lock-path",
                str(self.lock_path),
                "--",
                sys.executable,
                "-c",
                child,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True\nTrue")

    # --- runner re-entry with an already-held inherited lock ---

    def test_runner_accepts_validated_inherited_lock_without_reacquiring(self) -> None:
        """The wrapper re-enters through the runner after acquiring the flock
        itself (exec preserves the OFD). The runner must validate the
        inherited fd, skip its own acquisition (its own fresh fd would
        conflict), and manage the chain: the child must see the SAME fd
        number still holding the flock."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = os.environ.copy()
        env["TASKNOTES_LOCK_FD"] = str(lock_fd)
        env["LOCK_PATH_TEST"] = str(self.lock_path)
        child = (
            "import os\n"
            "fd = int(os.environ['TASKNOTES_LOCK_FD'])\n"
            "print(os.path.samefile(f'/proc/self/fd/{fd}', "
            "os.environ['LOCK_PATH_TEST']))\n"
            "info = open(f'/proc/self/fdinfo/{fd}').read()\n"
            "print(any('FLOCK' in l and 'WRITE' in l for l in info.splitlines()))\n"
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--lock-path",
                    str(self.lock_path),
                    "--timeout",
                    "10",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=env,
                pass_fds=[lock_fd],
            )
        finally:
            os.close(lock_fd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True\nTrue")

    def test_runner_rejects_forged_inherited_fd_and_acquires_itself(self) -> None:
        """An inherited fd that does NOT actually hold an exclusive flock on
        the exact lock file (here: a shared lock on a different file) must be
        ignored; the runner falls back to its own safe acquisition."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        other = self.root / "other.lock"
        other.touch()
        forged = os.open(other, os.O_RDWR)
        fcntl.flock(forged, fcntl.LOCK_SH | fcntl.LOCK_NB)
        env = os.environ.copy()
        env["TASKNOTES_LOCK_FD"] = str(forged)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--lock-path",
                    str(self.lock_path),
                    "--nonblocking",
                    "--",
                    sys.executable,
                    "-c",
                    "raise SystemExit(0)",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=env,
                pass_fds=[forged],
            )
        finally:
            os.close(forged)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The runner must have acquired the real lock itself: verify the lock
        # is free after the run.
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    # --- child signal-death mapping ---

    def test_child_dies_by_own_sigterm_mapped_to_128_plus(self) -> None:
        """An independent child death by signal must be reported as 128+N,
        like a shell."""
        result = self._run(
            [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]
        )
        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)

    def test_child_dies_by_sigkill_mapped_to_128_plus(self) -> None:
        result = self._run(
            [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
        )
        self.assertEqual(result.returncode, 128 + signal.SIGKILL, result.stderr)

    # --- handler-before-spawn / whole group behavior ---

    def test_signal_at_spawn_is_handled_not_default(self) -> None:
        """The child's very first action signals the runner (SIGHUP). With
        the block/install/unblock pattern the handler must already be in
        place: the signal stays pending until the mask is restored, so the
        whole group is cleaned up and the runner exits 128+SIGHUP instead of
        dying by the default disposition and orphaning the group."""
        pid_file = self.root / "grandchild.pid"
        child = (
            "import os, signal, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "proc = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'])\n"
            f"Path({str(pid_file)!r}).write_text(str(proc.pid))\n"
            "os.kill(os.getppid(), signal.SIGHUP)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                "--lock-path",
                str(self.lock_path),
                "--kill-grace",
                "0.2",
                "--",
                sys.executable,
                "-c",
                child,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 128 + signal.SIGHUP, stderr.decode())
        self.assertTrue(pid_file.exists(), "child must have started")
        self._assert_process_gone(int(pid_file.read_text()))

    def test_sigquit_maps_to_128_plus_and_cleans_group(self) -> None:
        pid_file = self.root / "grandchild.pid"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                "--lock-path",
                str(self.lock_path),
                "--kill-grace",
                "0.2",
                "--",
                sys.executable,
                "-c",
                self._grandchild_sleeper_code(pid_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(), "child must have started")
        time.sleep(0.2)
        os.kill(proc.pid, signal.SIGQUIT)
        _, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 128 + signal.SIGQUIT, stderr.decode())
        self._assert_process_gone(int(pid_file.read_text()))

    def test_lock_released_only_after_group_reaped(self) -> None:
        """After a timeout the runner must exit only once the whole group is
        gone, and the flock must be acquirable immediately after exit (no
        orphaned lock holder)."""
        pid_file = self.root / "grandchild.pid"
        result = self._run(
            [sys.executable, "-c", self._grandchild_sleeper_code(pid_file)],
            "--timeout",
            "0.3",
            "--kill-grace",
            "0.2",
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertTrue(pid_file.exists(), "child must have started")
        self._assert_process_gone(int(pid_file.read_text()))
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
