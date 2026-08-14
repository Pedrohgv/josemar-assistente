"""Unit/contract tests for the Phase-2 recovery DOWNLOAD step
(scripts/vault-recovery-recover.sh) using the fake rclone fixture.

Covers: crypt remote validation, list-remote inventory validation (invalid
names fail closed; markerless/invalid-marker/unbound committed dirs are
invisible; indeterminate remote READY/manifest read failures fail the whole
listing closed — never hidden as markerless), download of a committed
generation with FULL validation before the RECOVERY_READY handoff is
written (including the remote READY-marker pre-check bound to the manifest
BEFORE any payload transfer; an indeterminate remote read failure is
refused explicitly, not reported as markerless), tampered/partial downloads
never producing a sentinel, traversal rejection before any rclone
interaction, and stale-handoff cleanup between downloads.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

try:
    from .phase2_helpers import (
        FakeRcloneFixture,
        RECOVER_SCRIPT,
        make_generation,
        recover_env_for,
        seed_remote_committed,
        seed_remote_committed_id,
    )
except ImportError:  # discover -s tests/vault_recovery imports top-level
    from phase2_helpers import (  # type: ignore
        FakeRcloneFixture,
        RECOVER_SCRIPT,
        make_generation,
        recover_env_for,
        seed_remote_committed,
        seed_remote_committed_id,
    )


def _transfer_cmds(log_entries: list) -> list:
    return [e["cmd"] for e in log_entries if e["cmd"] in ("copy", "move", "purge", "lsjson")]


class RecoverDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-recover-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gen_id, self.staging = make_generation(self.tmp)
        self.recovery = self.tmp / "recovery"
        self.fixture = FakeRcloneFixture(self.tmp)

    def _run(self, *args, **over):
        import subprocess as _sp
        return _sp.run(
            ["/bin/sh", str(RECOVER_SCRIPT), *args],
            env=recover_env_for(self.fixture, self.recovery, **over),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _seed_committed(self) -> None:
        seed_remote_committed(self.fixture, self.gen_id, self.staging)

    # ------------------------------------------------------------------
    # Remote validation
    # ------------------------------------------------------------------

    def test_requires_crypt_remote(self) -> None:
        self.fixture._write_config({
            "vault-crypt": {"type": "drive", "client_id": "x", "client_secret": "y"}
        })
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("not rclone type 'crypt'", proc.stderr)
        self.assertEqual(_transfer_cmds(self.fixture.log_entries()), [])
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_rejects_filename_encryption_off(self) -> None:
        """Metadata-encryption standard: `off` filename encryption would
        leak plaintext file names in the ciphertext metadata; recovery
        refuses the download before any payload transfer."""
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": "pw",
                            "filename_encryption": "off"}
        })
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("filename_encryption", proc.stderr)
        self.assertIn("standard", proc.stderr)
        self.assertEqual(_transfer_cmds(self.fixture.log_entries()), [])
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_rejects_filename_encryption_obfuscate(self) -> None:
        """`obfuscate` is reversible obfuscation, not encryption: refused
        for recovery the same way as for upload."""
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": "pw",
                            "filename_encryption": "obfuscate"}
        })
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("filename_encryption", proc.stderr)
        self.assertEqual(_transfer_cmds(self.fixture.log_entries()), [])

    def test_rejects_directory_name_encryption_false(self) -> None:
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": "pw",
                            "directory_name_encryption": "false"}
        })
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("directory_name_encryption", proc.stderr)
        self.assertEqual(_transfer_cmds(self.fixture.log_entries()), [])

    def test_accepts_absent_encryption_keys_and_standard(self) -> None:
        """Absent keys mean the rclone defaults (standard/true) and an
        explicit standard+true config is accepted: the full download path
        still works end to end."""
        self._seed_committed()
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.recovery / "RECOVERY_READY").exists())
        # Explicit secure standard also accepted (fresh fixture).
        fixture2 = FakeRcloneFixture(self.tmp / "fx2", {
            "vault-crypt": {"type": "crypt", "remote": "local:/underlying",
                            "password": "obfuscated",
                            "filename_encryption": "standard",
                            "directory_name_encryption": "true"}
        })
        seed_remote_committed(fixture2, self.gen_id, self.staging)
        proc = fixture2.run(
            RECOVER_SCRIPT, ["download", self.gen_id],
            **recover_env_for(fixture2, self.tmp / "recovery2"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.tmp / "recovery2" / "RECOVERY_READY").exists())

    def test_requires_nonempty_underlying(self) -> None:
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "", "password": "pw"}
        })
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("EMPTY underlying remote", proc.stderr)

    def test_requires_nonempty_password(self) -> None:
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": ""}
        })
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("EMPTY password", proc.stderr)

    # ------------------------------------------------------------------
    # Generation-id validation happens BEFORE any rclone interaction
    # ------------------------------------------------------------------

    def test_invalid_generation_id_rejected_before_rclone(self) -> None:
        proc = self._run("download", "../evil")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("Invalid generation id", proc.stderr)
        self.assertEqual(self.fixture.log_entries(), [])
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_missing_generation_id_rejected(self) -> None:
        proc = self._run("download")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.fixture.log_entries(), [])

    # ------------------------------------------------------------------
    # Happy path: download -> FULL validation -> RECOVERY_READY
    # ------------------------------------------------------------------

    def test_download_committed_generation_writes_handoff(self) -> None:
        self._seed_committed()
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bundle = self.recovery / self.gen_id
        self.assertTrue((bundle / "READY").exists())
        self.assertTrue((bundle / "manifest.json").exists())
        self.assertTrue((bundle / "vault" / "notes" / "hello.md").exists())
        ready = (self.recovery / "RECOVERY_READY").read_text("utf-8").splitlines()
        self.assertEqual(ready[0], self.gen_id)
        import hashlib
        expected = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
        self.assertEqual(ready[1], expected)
        # Only the committed namespace is touched: the READY-marker + manifest
        # binding checks (cat), then one download copy. The startup `config
        # show` crypt validation is expected too.
        self.assertEqual(
            [c for c in self.fixture.log_commands() if c not in ("config", "cat")],
            ["copy"],
        )

    def test_stale_handoff_cleared_before_new_download(self) -> None:
        # A previous partial download + stale sentinel must be wiped.
        stale = self.recovery / "20260101T000000000000Z-deadbeef"
        stale.mkdir(parents=True)
        (stale / "junk").write_text("x", encoding="utf-8")
        (self.recovery / "RECOVERY_READY").write_text(
            "20260101T000000000000Z-deadbeef\nstale-sha\n", encoding="utf-8"
        )
        self._seed_committed()
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(stale.exists(), "stale bundle must be cleared")
        self.assertEqual(
            (self.recovery / "RECOVERY_READY").read_text("utf-8").splitlines()[0],
            self.gen_id,
        )

    # ------------------------------------------------------------------
    # Tampered/partial downloads never produce a sentinel
    # ------------------------------------------------------------------

    def test_tampered_download_never_writes_sentinel(self) -> None:
        self._seed_committed()
        proc = self._run("download", self.gen_id, **{"FAKE_RCLONE_TAMPER_AFTER_COPY_TO": str(self.recovery)})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("failed validation", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_partial_download_never_writes_sentinel(self) -> None:
        self._seed_committed()
        proc = self._run("download", self.gen_id, **{"FAKE_RCLONE_PARTIAL_COPY_TO": str(self.recovery)})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_copy_failure_never_writes_sentinel(self) -> None:
        self._seed_committed()
        proc = self._run("download", self.gen_id, **{"FAKE_RCLONE_FAIL_CMDS": "copy"})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("download of", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_uncommitted_generation_not_recoverable(self) -> None:
        # A generation that only exists under uncommitted/ must NOT be
        # downloadable (only committed generations are recoverable).
        uncommitted = self.fixture.remote_dir("Josemar", "vault-recovery", "uncommitted")
        shutil.copytree(self.staging / self.gen_id, uncommitted / self.gen_id)
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_markerless_committed_not_recoverable(self) -> None:
        """A committed dir WITHOUT a READY marker (interrupted commit) is
        refused by the READY-marker pre-check BEFORE any payload transfer."""
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        target = committed / self.gen_id
        shutil.copytree(self.staging / self.gen_id, target)
        (target / "READY").unlink()
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("no valid READY marker bound to the manifest", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())
        self.assertNotIn(
            "copy", [e["cmd"] for e in self.fixture.log_entries()],
            "no payload transfer may start for a markerless committed dir",
        )

    def test_download_ready_cat_failure_fails_closed_not_markerless(self) -> None:
        """A FAILED remote READY read (rclone transport error, not a
        confirmed not-found) leaves the marker state UNKNOWN: download is
        refused with an explicit error and NO payload transfer starts —
        it must never be reported as merely markerless (that would hide a
        possibly-valid snapshot behind a transient remote problem)."""
        self._seed_committed()
        proc = self._run("download", self.gen_id, **{"FAKE_RCLONE_FAIL_CAT_SUBSTR": "READY"})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("marker state UNKNOWN", proc.stderr)
        self.assertIn("refusing download", proc.stderr)
        self.assertNotIn("no valid READY marker bound to the manifest", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())
        self.assertNotIn(
            "copy", [e["cmd"] for e in self.fixture.log_entries()],
            "no payload transfer may start when the marker state is UNKNOWN",
        )

    def test_download_manifest_cat_failure_fails_closed_not_markerless(self) -> None:
        """A FAILED remote manifest read after a valid READY read is also
        indeterminate: download is refused with an explicit error, no
        payload transfer starts, and the failure is not reported as a
        markerless/unbound generation."""
        self._seed_committed()
        proc = self._run("download", self.gen_id, **{"FAKE_RCLONE_FAIL_CAT_SUBSTR": "manifest.json"})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("marker state UNKNOWN", proc.stderr)
        self.assertIn("refusing download", proc.stderr)
        self.assertNotIn("no valid READY marker bound to the manifest", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())
        self.assertNotIn(
            "copy", [e["cmd"] for e in self.fixture.log_entries()],
            "no payload transfer may start when the marker state is UNKNOWN",
        )

    def test_invalid_marker_committed_not_recoverable(self) -> None:
        """A committed dir whose READY content does not match its name is
        not a published snapshot and is refused up front."""
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        target = committed / self.gen_id
        shutil.copytree(self.staging / self.gen_id, target)
        (target / "READY").write_text("20260101T000000000000Z-ffffffff\n")
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("no valid READY marker bound to the manifest", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())
        self.assertNotIn(
            "copy", [e["cmd"] for e in self.fixture.log_entries()],
            "no payload transfer may start for an invalid-marker committed dir",
        )

    def test_manifest_generation_mismatch_never_writes_sentinel(self) -> None:
        self._seed_committed()
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed", self.gen_id)
        manifest_path = committed / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["generation_id"] = "20260101T000000000000Z-ffffffff"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        # The remote READY-marker pre-check refuses the unbound generation
        # BEFORE any payload transfer.
        self.assertIn("no valid READY marker bound to the manifest", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_malformed_remote_manifest_is_confirmed_invalid_marker(self) -> None:
        """Strict JSON schema validation (council fix): a remote manifest
        that is NOT well-formed JSON (while its generation_id stays
        grep-visible) is a CONFIRMED invalid marker — the download is
        refused BEFORE any payload transfer, exactly like a missing or
        unbound READY (it must never be handed off as recoverable)."""
        self._seed_committed()
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed", self.gen_id)
        manifest_path = committed / "manifest.json"
        valid = manifest_path.read_text("utf-8")
        broken = (
            '{"schema_version": 1, "generation_id": "' + self.gen_id + '",\n'
            + valid.split('"trees"')[0]
            + '"trees": {".gbrain": {"entries_digest": "' + "0" * 64 + '",\n'
            + '"unclosed-string-prefix\n'
        )
        manifest_path.write_text(broken, encoding="utf-8")
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("no valid READY marker bound to the manifest", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())
        self.assertNotIn(
            "copy", [e["cmd"] for e in self.fixture.log_entries()],
            "no payload transfer may start when the remote manifest is invalid",
        )


class RecoverListRemoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-recover-list-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gen_id, self.staging = make_generation(self.tmp)
        self.recovery = self.tmp / "recovery"
        self.fixture = FakeRcloneFixture(self.tmp)

    def _run(self, *args, **over):
        import subprocess as _sp
        return _sp.run(
            ["/bin/sh", str(RECOVER_SCRIPT), *args],
            env=recover_env_for(self.fixture, self.recovery, **over),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_list_remote_empty(self) -> None:
        proc = self._run("list-remote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("No committed remote generations", proc.stdout)

    def test_list_remote_sorted_newest_first(self) -> None:
        older = "20260101T000000000000Z-aaaa0001"
        newer = "20260102T000000000000Z-aaaa0002"
        # Both seeds carry a READY marker bound to their own id (the READY
        # protocol requires marker == dir name == manifest generation_id).
        seed_remote_committed_id(self.fixture, self.staging, self.gen_id, older)
        seed_remote_committed_id(self.fixture, self.staging, self.gen_id, newer)
        proc = self._run("list-remote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [
            line for line in proc.stdout.splitlines()
            if not line.startswith("[vault-recovery-recover]")
        ]
        self.assertEqual(lines, [newer, older], lines)

    def test_list_remote_markerless_invisible(self) -> None:
        """A committed dir WITHOUT a READY marker (interrupted commit) is
        invisible: never listed, never fails the listing."""
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        markerless = "20260101T000000000000Z-bbbb0001"
        target = committed / markerless
        shutil.copytree(self.staging / self.gen_id, target)
        (target / "READY").unlink()
        proc = self._run("list-remote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not listed", proc.stdout)
        lines = [
            line for line in proc.stdout.splitlines()
            if not line.startswith("[vault-recovery-recover]")
        ]
        self.assertEqual(lines, [self.gen_id], lines)

    def test_list_remote_invalid_marker_invisible(self) -> None:
        """A committed dir whose READY content does not match its name is
        invisible (no valid marker bound to the manifest)."""
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        bad = "20260101T000000000000Z-bbbb0002"
        target = committed / bad
        shutil.copytree(self.staging / self.gen_id, target)
        (target / "READY").write_text("20260101T000000000000Z-ffffffff\n")
        proc = self._run("list-remote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [
            line for line in proc.stdout.splitlines()
            if not line.startswith("[vault-recovery-recover]")
        ]
        self.assertEqual(lines, [self.gen_id], lines)

    def test_list_remote_manifest_binding_mismatch_invisible(self) -> None:
        """A committed dir whose READY names the dir but whose manifest
        generation_id does not bind is invisible."""
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        bad = "20260101T000000000000Z-bbbb0003"
        target = committed / bad
        shutil.copytree(self.staging / self.gen_id, target)
        (target / "READY").write_text(f"{bad}\n")
        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["generation_id"] = "20260101T000000000000Z-ffffffff"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        proc = self._run("list-remote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [
            line for line in proc.stdout.splitlines()
            if not line.startswith("[vault-recovery-recover]")
        ]
        self.assertEqual(lines, [self.gen_id], lines)

    def test_list_remote_invalid_inventory_fails_closed(self) -> None:
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        shutil.copytree(self.staging / self.gen_id, committed / self.gen_id)
        (committed / "not-a-generation").mkdir()
        proc = self._run("list-remote")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("invalid name", proc.stderr)
        listing = [
            line for line in proc.stdout.splitlines()
            if not line.startswith("[vault-recovery-recover]")
        ]
        self.assertEqual(listing, [], "no inventory printed for a suspect listing")

    def test_list_remote_failure_fails_closed(self) -> None:
        """A FAILED inventory listing must never be reported as 'no
        committed generations' (false negative on backup existence): it
        fails closed with an error."""
        proc = self._run("list-remote", **{"FAKE_RCLONE_FAIL_CMDS": "lsjson"})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("inventory listing FAILED", proc.stderr)
        self.assertNotIn("No committed remote generations", proc.stdout)

    def test_list_remote_zero_byte_inventory_fails_closed(self) -> None:
        """A ZERO-BYTE successful lsjson response is a PROTOCOL failure,
        never an empty inventory: a successful rclone lsjson always emits
        at least a valid JSON array (`[]` for an empty namespace). The
        listing fails closed (exit 2) instead of reporting 'no committed
        generations' — a false negative on backup existence."""
        proc = self._run("list-remote", **{"FAKE_RCLONE_LSJSON_ZERO_BYTES": "1"})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("ZERO-BYTE", proc.stderr)
        self.assertNotIn("No committed remote generations", proc.stdout)

    def test_list_remote_empty_json_array_is_valid_empty_inventory(self) -> None:
        """An empty committed namespace emits the valid JSON array `[]`:
        that IS a clean empty inventory and reports 'no committed
        generations' with exit 0 — the opposite of a zero-byte protocol
        failure."""
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        committed.mkdir(parents=True)  # exists but empty -> lsjson prints []
        proc = self._run("list-remote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("No committed remote generations", proc.stdout)

    def test_list_remote_ready_read_failure_fails_closed_not_markerless(self) -> None:
        """A FAILED remote READY read (rclone transport error, not a
        confirmed not-found) during the marker sweep must FAIL the whole
        listing: a possibly-valid generation is never hidden as if it were
        markerless (that would be a false negative on backup existence)."""
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        proc = self._run("list-remote", **{"FAKE_RCLONE_FAIL_CAT_SUBSTR": "READY"})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("marker state UNKNOWN", proc.stderr)
        self.assertIn("refusing to list", proc.stderr)
        listing = [
            line for line in proc.stdout.splitlines()
            if not line.startswith("[vault-recovery-recover]")
        ]
        self.assertEqual(
            listing, [],
            "a failed marker read must not print a partial/empty inventory",
        )
        self.assertNotIn("No committed remote generations", proc.stdout)


class RecoverSchemaBoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-recover-bound-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gen_id, self.staging = make_generation(self.tmp)
        self.recovery = self.tmp / "recovery"
        self.fixture = FakeRcloneFixture(self.tmp)

    def _run(self, *args, **over):
        import subprocess as _sp
        return _sp.run(
            ["/bin/sh", str(RECOVER_SCRIPT), *args],
            env=recover_env_for(self.fixture, self.recovery, **over),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _seed_committed(self) -> None:
        seed_remote_committed(self.fixture, self.gen_id, self.staging)

    def test_download_refuses_unknown_manifest_schema(self) -> None:
        self._seed_committed()
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed", self.gen_id)
        manifest_path = committed / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["schema_version"] = 99
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("schema_version", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_download_refuses_oversized_ready_sentinel(self) -> None:
        self._seed_committed()
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed", self.gen_id)
        with open(committed / "READY", "a", encoding="utf-8") as fh:
            fh.write("x" * 5000)
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        # The bounded first-line read makes the oversized sentinel fail the
        # remote READY-marker pre-check (content != generation id).
        self.assertIn("no valid READY marker bound to the manifest", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())


if __name__ == "__main__":
    unittest.main()
