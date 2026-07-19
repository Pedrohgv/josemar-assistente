"""Runtime tests for the shared TaskNotes/gbrain lock runner."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
