"""Focused tests for the verify-restore recovery destination boundary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "mnemosyne-backup-restore.sh"


class RestoreBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.recovery = self.tmp / "recovery"
        self.recovery.mkdir()
        self.artifact = self.recovery / "mnemosyne.db.gz"
        self.artifact.write_bytes(b"artifact")
        digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        (self.recovery / "manifest.json").write_text(
            json.dumps({"generation_id": "gen-1", "artifact": {"sha256": digest}})
        )
        (self.recovery / "RECOVERY_READY").write_text(f"gen-1\n{digest}\n")
        self.core = self.tmp / "core.py"
        self.core.write_text(
            "import shutil, sys\n"
            "assert sys.argv[1] == 'verify-restore'\n"
            "shutil.copyfile(sys.argv[2], sys.argv[3])\n"
        )

    def tearDown(self) -> None:
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.tmp.rmdir()

    def run_restore(self, destination: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(MNEMOSYNE_BACKUP_PYTHON=sys.executable, MNEMOSYNE_BACKUP_CORE=str(self.core))
        return subprocess.run(
            ["sh", str(SCRIPT), "verify-restore", str(self.recovery), str(destination)],
            env=env, capture_output=True, text=True,
        )

    def test_allows_destination_inside_recovery_directory(self) -> None:
        destination = self.recovery / "nested" / "verified.db"
        destination.parent.mkdir()
        result = self.run_restore(destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(destination.exists())

    def test_rejects_parent_traversal_without_creating_outside_path(self) -> None:
        destination = self.recovery / "nested" / ".." / ".." / "escaped.db"
        result = self.run_restore(destination)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.tmp / "escaped.db").exists())

    def test_rejects_symlink_escape_without_writing_through_symlink(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.recovery / "link").symlink_to(outside, target_is_directory=True)
        result = self.run_restore(self.recovery / "link" / "escaped.db")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((outside / "escaped.db").exists())


if __name__ == "__main__":
    unittest.main()
