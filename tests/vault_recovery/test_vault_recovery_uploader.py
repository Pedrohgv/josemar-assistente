"""Unit/contract tests for the Phase-2 encrypted uploader
(scripts/vault-recovery-uploader.sh) using the fake rclone fixture.

Covers: crypt remote validation (type + non-empty underlying + password),
full local manifest/hashes/tree validation BEFORE upload (tampered files,
entries-index digest mismatch, READY/manifest mismatches), upload to the
uncommitted namespace, REMOTE DECRYPTED verification BEFORE commit, commit
of the payload, verification of the COMMITTED payload BEFORE the READY
publication (remote verification ordering), READY-last visibility, local
acknowledgement ONLY after verification+commit, partial inbound objects
never committed, traversal rejection, idempotent no-op on acked
generations, the retry-after-READY protocol (a READY-visible committed
generation is re-validated and acknowledged or failed — never overwritten;
an invalid marker is re-committed), and retention pruning that counts and
prunes ONLY committed READY-valid generations while preserving incomplete
dirs (which never evict valid generations) — pruning only after a valid
remote inventory (invalid inventory or unacknowledged generations -> no
prune). Indeterminate remote READY/manifest read failures (fake rclone
transport-error injections) are NEVER treated as markerless: the upload
aborts before any remote mutation and the prune is skipped entirely.
Also covers the FULL staged backlog reconciliation (foreground): every
staged generation not acknowledged in the local ledger is uploaded and
acknowledged oldest first (not just the `latest` pointer), acked ones
are skipped, an unrecognized staged directory or an upload failure
aborts the run before further uploads (the next run resumes at the same
oldest unacknowledged generation), and a READY-visible committed newest
generation is re-validated and acknowledged without mutation while older
staged generations are still pending.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

try:
    from .phase2_helpers import (
        FakeRcloneFixture,
        UPLOADER_SCRIPT,
        make_generation,
        seed_remote_committed,
        uploader_env_for,
    )
except ImportError:  # discover -s tests/vault_recovery imports top-level
    from phase2_helpers import (  # type: ignore
        FakeRcloneFixture,
        UPLOADER_SCRIPT,
        make_generation,
        seed_remote_committed,
        uploader_env_for,
    )

# Oldest-first seed ids: lexically sortable timestamps + hex suffixes. Each
# id must be exactly 31 chars to pass the strict generation-id validation
# (the prune path validates the whole inventory before touching anything).
SEED_IDS = [
    f"202601{i:02d}T000000000000Z-a{i:07d}" for i in range(1, 21)
]


def _seed_committed_generation(gen_dir: Path, gen_id: str, target: Path) -> None:
    """Copy a valid generation dir and rewrite its gen id (manifest + READY)."""
    shutil.copytree(gen_dir, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["generation_id"] = gen_id
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (target / "READY").write_text(f"{gen_id}\n")


def _no_transfer_cmds(log_entries: list) -> list:
    """Transfer/prune rclone calls in a log (the startup `config show` crypt
    validation is expected and excluded). The retention inventory listing
    (`lsjson`) counts as a prune-path call."""
    return [e["cmd"] for e in log_entries if e["cmd"] in ("copy", "move", "purge", "lsjson")]


def _acked(
    gen_id: str,
    staging: Path,
    remote: str = "vault-crypt",
    path: str = "Josemar/vault-recovery",
) -> str:
    """The DIGEST-BOUND ledger line acknowledging `gen_id` under a remote
    identity (`gen-id TAB remote-name TAB remote-path TAB manifest-sha256
    TAB ready-sha256`; see ack binding). The digests are the sha256 of the
    generation's manifest.json and READY bytes — the fake rclone round-trips
    bytes exactly, so the staged bytes equal the verified remote payload
    bytes the uploader records."""
    gen_dir = staging / gen_id
    manifest_sha = hashlib.sha256((gen_dir / "manifest.json").read_bytes()).hexdigest()
    ready_sha = hashlib.sha256((gen_dir / "READY").read_bytes()).hexdigest()
    return f"{gen_id}\t{remote}\t{path}\t{manifest_sha}\t{ready_sha}"


def _broken_manifest(gen_id: str, valid_text: str) -> str:
    """Malformed JSON that keeps the schema_version/generation_id/
    entries_digest fields grep-visible (the strict-validation gap the
    well-formedness validator closes): the document must fail a real JSON
    parse while every field the shell greps for is still findable."""
    return (
        '{"schema_version": 1, "generation_id": "' + gen_id + '",\n'
        + valid_text.split('"trees"')[0]
        + '"trees": {".gbrain": {"entries_digest": "' + "0" * 64 + '",\n'
        + '"unclosed-string-prefix\n'
    )


class UploaderBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-uploader-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gen_id, self.staging = make_generation(self.tmp)
        self.state = self.tmp / "state"
        self.fixture = FakeRcloneFixture(self.tmp)

    def _run_impl(self, over):
        import subprocess as _sp
        return _sp.run(
            ["/bin/sh", str(UPLOADER_SCRIPT)],
            env=uploader_env_for(self.fixture, self.staging, self.state, **over),
            capture_output=True,
            text=True,
            timeout=120,
        )

    # ------------------------------------------------------------------
    # Crypt remote validation
    # ------------------------------------------------------------------

    def test_requires_crypt_type(self) -> None:
        self.fixture._write_config({
            "vault-crypt": {
                "type": "drive",
                "client_id": "x",
                "client_secret": "y",
            }
        })
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("not rclone type 'crypt'", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_requires_nonempty_underlying_remote(self) -> None:
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "", "password": "pw"}
        })
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("EMPTY underlying remote", proc.stderr)

    def test_requires_nonempty_password(self) -> None:
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": ""}
        })
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("EMPTY password", proc.stderr)

    def test_requires_remote_name(self) -> None:
        proc = self._run_impl({"VAULT_RECOVERY_RCLONE_REMOTE": ""})
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_rejects_filename_encryption_off(self) -> None:
        """Metadata-encryption standard: `off` filename encryption would
        leak every plaintext file name in the ciphertext metadata; the
        uploader must refuse BEFORE any transfer."""
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": "pw",
                            "filename_encryption": "off"}
        })
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("filename_encryption", proc.stderr)
        self.assertIn("standard", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_rejects_filename_encryption_obfuscate(self) -> None:
        """`obfuscate` is reversible-obfuscation, not encryption: plaintext
        names are recoverable from the ciphertext metadata; refused."""
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": "pw",
                            "filename_encryption": "obfuscate"}
        })
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("filename_encryption", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_rejects_directory_name_encryption_false(self) -> None:
        """Directory names must be encrypted: `false` leaks the remote
        namespace layout (generation dirs, tree names) in plaintext."""
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": "pw",
                            "directory_name_encryption": "false"}
        })
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("directory_name_encryption", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_accepts_explicit_standard_encryption(self) -> None:
        """The documented secure standard (filename_encryption=standard +
        directory_name_encryption=true, explicitly written) is accepted."""
        self.fixture._write_config({
            "vault-crypt": {"type": "crypt", "remote": "local:/x", "password": "pw",
                            "filename_encryption": "standard",
                            "directory_name_encryption": "true"}
        })
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_absent_encryption_keys_use_rclone_defaults(self) -> None:
        """Absent filename_encryption/directory_name_encryption mean the
        rclone defaults (`standard`/`true`) — the common existing configs
        keep working (covered implicitly by every other test; asserted
        explicitly here through a successful full upload)."""
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    # ------------------------------------------------------------------
    # Full local validation BEFORE upload
    # ------------------------------------------------------------------

    def test_tampered_file_never_uploaded(self) -> None:
        target = self.staging / self.gen_id / "vault" / "notes" / "hello.md"
        target.write_text("TAMPERED\n", encoding="utf-8")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("sha256 mismatch", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])
        self.assertFalse(
            self.fixture.remote_dir("Josemar", "vault-recovery", "committed").exists()
        )

    def test_entries_digest_mismatch_never_uploaded(self) -> None:
        entries = self.staging / self.gen_id / "vault.entries.txt"
        entries.write_text(entries.read_text("utf-8") + "file\t644\t1\t" + "0" * 64 + "\textra\n")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("entries index digest mismatch", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_extra_entry_never_uploaded(self) -> None:
        extra = self.staging / self.gen_id / "vault" / "extra.md"
        extra.write_text("sneaky\n", encoding="utf-8")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        # The exact-count check (disk != entries) fires before the per-entry
        # diff; both fail closed with nothing uploaded.
        self.assertIn("entry count mismatch", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_missing_entry_never_uploaded(self) -> None:
        (self.staging / self.gen_id / "vault" / "notes" / "hello.md").unlink()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("entry count mismatch", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_symlink_never_uploaded(self) -> None:
        link = self.staging / self.gen_id / "vault" / "evil-link"
        link.symlink_to("/etc/passwd")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("symlink", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_ready_mismatch_never_uploaded(self) -> None:
        (self.staging / self.gen_id / "READY").write_text("20260101T000000000000Z-ffffffff\n")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("READY generation mismatch", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_manifest_generation_mismatch_never_uploaded(self) -> None:
        manifest_path = self.staging / self.gen_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["generation_id"] = "20260101T000000000000Z-ffffffff"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("manifest generation_id mismatch", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_malformed_manifest_json_never_uploaded(self) -> None:
        """Strict JSON schema validation (council fix): a manifest that is
        NOT well-formed JSON is rejected even when its required fields stay
        grep-visible — such a document could never be restored by the
        Python core, so it must never become READY or be acknowledged."""
        manifest_path = self.staging / self.gen_id / "manifest.json"
        manifest_path.write_text(
            _broken_manifest(self.gen_id, manifest_path.read_text("utf-8")),
            encoding="utf-8",
        )
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("not well-formed JSON", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])
        self.assertFalse(
            self.fixture.remote_dir("Josemar", "vault-recovery", "committed").exists()
        )

    def test_malformed_remote_manifest_is_invalid_marker_and_recommits(self) -> None:
        """The REMOTE manifest read is also strict: a committed namespace
        whose manifest is malformed JSON (grep-visible generation id) is a
        CONFIRMED invalid marker — never a published snapshot. The uploader
        does NOT acknowledge it as-is: the retry-after-READY path is not
        taken, the staged generation is re-uploaded and the broken remote
        payload is REPLACED by the fully validated content before the ack."""
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        target = committed / self.gen_id
        shutil.copytree(self.staging / self.gen_id, target)
        (target / "READY").write_text(f"{self.gen_id}\n", encoding="utf-8")
        target_manifest = target / "manifest.json"
        target_manifest.write_text(
            _broken_manifest(self.gen_id, target_manifest.read_text("utf-8")),
            encoding="utf-8",
        )
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The broken payload was NOT treated as a valid published snapshot
        # (the READY-visible retry path logs this exact message; it must be
        # absent), and the committed manifest is now the validated staged
        # one, not the malformed document.
        self.assertNotIn("already committed with a valid READY marker", proc.stdout)
        self.assertEqual(
            target_manifest.read_bytes(),
            (self.staging / self.gen_id / "manifest.json").read_bytes(),
        )
        self.assertIn(
            _acked(self.gen_id, self.staging),
            (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines(),
        )

    def test_missing_entries_index_not_uploadable(self) -> None:
        (self.staging / self.gen_id / ".gbrain.entries.txt").unlink()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("no entries index", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    # ------------------------------------------------------------------
    # Traversal / pointer validation
    # ------------------------------------------------------------------

    def test_traversal_latest_pointer_rejected(self) -> None:
        (self.staging / "latest").write_text("../evil\n")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Invalid latest pointer", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_invalid_generation_dir_name_rejected(self) -> None:
        bad = self.staging / "not-a-generation"
        bad.mkdir()
        (bad / "READY").write_text("not-a-generation\n")
        (self.staging / "latest").write_text("not-a-generation\n")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Invalid latest pointer", proc.stderr)

    # ------------------------------------------------------------------
    # Happy path: upload -> remote verify -> commit -> ack
    # ------------------------------------------------------------------

    def test_upload_verifies_remote_before_commit_and_acks_after(self) -> None:
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        uncommitted = self.fixture.remote_dir("Josemar", "vault-recovery", "uncommitted")
        self.assertTrue((committed / self.gen_id / "READY").exists())
        self.assertTrue((committed / self.gen_id / "manifest.json").exists())
        self.assertTrue((committed / self.gen_id / "vault" / "notes" / "hello.md").exists())
        # Ordering: upload to uncommitted, verify the remote decrypted
        # content, commit the payload, verify the COMMITTED payload, and
        # ONLY THEN publish READY (the last move). The local ack is written
        # after all of them. The startup `config show` and the retention
        # `lsjson` inventory are excluded.
        cmds = [c for c in self.fixture.log_commands() if c in ("copy", "move")]
        self.assertEqual(cmds, ["copy", "copy", "move", "copy", "move"], cmds)
        entries = self.fixture.log_entries()
        # The committed-payload verification copy strictly precedes the
        # READY publication move: the payload is verified before the marker
        # that makes it visible/recoverable.
        verify_idx = [i for i, e in enumerate(entries)
                      if e["cmd"] == "copy" and e["args"][1].startswith("vault-crypt:")][-1]
        ready_move_idx = [i for i, e in enumerate(entries)
                          if e["cmd"] == "move" and e["args"][1].endswith("/READY")][-1]
        self.assertLess(verify_idx, ready_move_idx,
                        "the committed payload must be verified BEFORE the READY publication")
        # READY-last remote commit visibility: the FIRST commit move excludes
        # the READY sentinel and the LAST move publishes exactly that
        # sentinel, so a partial commit never leaves READY in the committed
        # namespace.
        moves = [e for e in entries if e["cmd"] == "move"]
        self.assertEqual(len(moves), 2, moves)
        # Logged args carry the command name at index 0; args[1] is the source.
        self.assertIn("--exclude", moves[0]["args"])
        self.assertIn("/READY", moves[0]["args"])
        self.assertNotIn("/READY", moves[0]["args"][1], "first move target is the committed gen dir")
        self.assertTrue(moves[-1]["args"][1].endswith("/READY"), moves[-1])
        # rclone keeps the emptied source dirs when --create-empty-src-dirs
        # is used, so uncommitted/<gen> may hold ONLY empty directories —
        # never a file and never the READY sentinel.
        leftover = uncommitted / self.gen_id
        self.assertTrue(leftover.exists())
        self.assertFalse((leftover / "READY").exists())
        self.assertEqual(
            [p for p in leftover.rglob("*") if p.is_file()], [],
            "the commit moves must remove every file from the uncommitted generation",
        )
        ledger = self.state / "uploaded-generations.jsonl"
        self.assertTrue(ledger.exists())
        self.assertIn(_acked(self.gen_id, self.staging), ledger.read_text("utf-8").splitlines())
        self.assertEqual(
            (self.state / "last-uploaded-generation").read_text("utf-8").strip(),
            self.gen_id,
        )

    def test_partial_remote_never_committed(self) -> None:
        proc = self._run_impl({"FAKE_RCLONE_PARTIAL_COPY_TO": "uncommitted"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        self.assertFalse(committed.exists(), "partial inbound object must never commit")
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        # The partial object stays in the uncommitted namespace only.
        self.assertTrue(
            (self.fixture.remote_dir("Josemar", "vault-recovery", "uncommitted") / self.gen_id).exists()
        )
        self.assertNotIn("move", self.fixture.log_commands())

    def test_remote_tamper_never_commits(self) -> None:
        proc = self._run_impl({"FAKE_RCLONE_TAMPER_AFTER_COPY_TO": "uncommitted"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("remote decrypted content validation failed", proc.stderr)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        self.assertFalse(committed.exists())
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertNotIn("move", self.fixture.log_commands())

    def test_verify_download_failure_never_commits(self) -> None:
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CMDS": "copy"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        self.assertFalse(committed.exists())
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())

    def test_commit_failure_never_acks(self) -> None:
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CMDS": "move"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertFalse((self.state / "last-uploaded-generation").exists())

    def test_interrupted_commit_never_visible_as_ready(self) -> None:
        """READY-last remote visibility: when the final READY publication
        move fails after the rest of the generation was committed, the
        committed namespace must hold the generation WITHOUT a READY
        sentinel (not a visible/recoverable snapshot), and nothing may be
        acknowledged locally."""
        proc = self._run_impl({"FAKE_RCLONE_FAIL_MOVE_SRC_SUBSTR": "READY"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("READY sentinel not published", proc.stderr)
        committed_gen = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "committed", self.gen_id
        )
        self.assertTrue(committed_gen.exists(), "the generation itself was committed")
        self.assertTrue((committed_gen / "manifest.json").exists())
        self.assertTrue((committed_gen / "vault" / "notes" / "hello.md").exists())
        self.assertFalse(
            (committed_gen / "READY").exists(),
            "a partial commit must never leave READY in the committed namespace",
        )
        # Not acknowledged: the local pointer/ledger must not claim it.
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertFalse((self.state / "last-uploaded-generation").exists())
        # The sentinel is still in the uncommitted namespace, untouched, as
        # the only remaining file (rclone keeps emptied source dirs).
        leftover = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "uncommitted", self.gen_id
        )
        self.assertTrue((leftover / "READY").exists())
        self.assertEqual(
            [p for p in leftover.rglob("*") if p.is_file()], [leftover / "READY"],
            "only the READY sentinel may remain in the uncommitted generation",
        )

    def test_interrupted_commit_rerun_is_idempotent(self) -> None:
        """A generation whose commit died between the content move and the
        READY publication is re-committed idempotently by the next run and
        only then acknowledged."""
        first = self._run_impl({"FAKE_RCLONE_FAIL_MOVE_SRC_SUBSTR": "READY"})
        self.assertEqual(first.returncode, 1, first.stderr)
        self.fixture.log.unlink(missing_ok=True)
        second = self._run_impl({})
        self.assertEqual(second.returncode, 0, second.stderr)
        committed_gen = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "committed", self.gen_id
        )
        self.assertTrue((committed_gen / "READY").exists())
        self.assertTrue((committed_gen / "manifest.json").exists())
        leftover = self.fixture.remote_dir("Josemar", "vault-recovery", "uncommitted") / self.gen_id
        self.assertFalse(
            (leftover / "READY").exists(),
            "the completed commit must publish READY and remove it from uncommitted",
        )
        self.assertEqual(
            [p for p in leftover.rglob("*") if p.is_file()], [],
            "the completed commit must remove every file from the uncommitted generation",
        )
        self.assertIn(self.gen_id, (self.state / "uploaded-generations.jsonl").read_text("utf-8"))
        self.assertEqual(
            (self.state / "last-uploaded-generation").read_text("utf-8").strip(),
            self.gen_id,
        )

    def test_noop_when_latest_already_acked(self) -> None:
        first = self._run_impl({})
        self.assertEqual(first.returncode, 0, first.stderr)
        first_calls = len(self.fixture.log_entries())
        # _run_impl bypasses fixture.run, so the fake-rclone log accumulates
        # across runs; clear it so only the second run is asserted.
        self.fixture.log.unlink(missing_ok=True)
        second = self._run_impl({})
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already uploaded; no-op", second.stdout)
        self.assertEqual(
            _no_transfer_cmds(self.fixture.log_entries()), [], "no rclone transfers on no-op"
        )

    # ------------------------------------------------------------------
    # Retry after READY is visible (crash between READY publication and the
    # local ack): the published payload is NEVER mutated — validate it and
    # acknowledge, or fail. No upload, no commit move, no overwrite.
    # ------------------------------------------------------------------

    def _remote_targeting_entries(self) -> list:
        """Copy/move/purge invocations that would MUTATE the remote
        namespace: copy/move with a remote DESTINATION, or purge of a
        remote path. (The fake log records [cmd, src, dst, ...] for
        copy/move and [cmd, path] for purge; args[0] repeats the cmd.)"""
        out = []
        for e in self.fixture.log_entries():
            if e["cmd"] == "purge":
                if "vault-crypt:" in e["args"][1]:
                    out.append(e)
            elif e["cmd"] in ("copy", "move"):
                if "vault-crypt:" in e["args"][2]:
                    out.append(e)
        return out

    def test_retry_after_ready_visible_acks_without_overwrite(self) -> None:
        """A committed generation that already carries a valid READY marker
        (e.g. a previous run crashed after READY publication but before the
        local ack) is re-validated and acknowledged WITHOUT any remote
        mutation: no upload, no commit move, no overwrite of the published
        payload."""
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "committed", self.gen_id
        )
        before = (committed / "vault" / "notes" / "hello.md").read_bytes()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("valid READY marker", proc.stdout)
        self.assertIn("no overwrite", proc.stdout)
        self.assertEqual(
            self._remote_targeting_entries(), [],
            "a READY-visible retry must not upload, move, or purge anything",
        )
        self.assertEqual(
            (committed / "vault" / "notes" / "hello.md").read_bytes(), before,
            "the published payload must be byte-identical after the retry",
        )
        # Acknowledged: ledger + last-uploaded pointer now claim the gen.
        ledger = self.state / "uploaded-generations.jsonl"
        self.assertTrue(ledger.exists())
        self.assertIn(_acked(self.gen_id, self.staging), ledger.read_text("utf-8").splitlines())
        self.assertEqual(
            (self.state / "last-uploaded-generation").read_text("utf-8").strip(),
            self.gen_id,
        )

    def test_retry_after_ready_visible_invalid_payload_fails_without_overwrite(self) -> None:
        """A valid READY marker with a CORRUPT committed payload must fail
        the retry — and must NEVER be silently overwritten: the published
        payload stays byte-identical and no ack is written."""
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "committed", self.gen_id
        )
        payload = committed / "vault" / "notes" / "hello.md"
        payload.write_text("TAMPERED-REMOTE\n", encoding="utf-8")
        before = payload.read_bytes()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("refusing to overwrite", proc.stderr)
        self.assertEqual(self._remote_targeting_entries(), [])
        self.assertEqual(payload.read_bytes(), before,
                         "the corrupted published payload must never be overwritten")
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertFalse((self.state / "last-uploaded-generation").exists())

    def test_committed_verify_failure_never_publishes_ready(self) -> None:
        """Remote verification ordering: when the verification of the
        COMMITTED payload fails (3rd copy), the READY sentinel must NOT be
        published — the payload is verified before the marker that makes it
        visible, and nothing is acknowledged."""
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CMD_AFTER": "copy:3"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("READY NOT published", proc.stderr)
        committed_gen = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "committed", self.gen_id
        )
        self.assertTrue(committed_gen.exists(), "the payload was committed")
        self.assertTrue((committed_gen / "manifest.json").exists())
        self.assertFalse(
            (committed_gen / "READY").exists(),
            "READY must never be published before the committed payload verified",
        )
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertFalse((self.state / "last-uploaded-generation").exists())

    def test_retry_invalid_ready_marker_recommits(self) -> None:
        """An INVALID READY marker in the committed namespace is not a
        published snapshot: the normal commit flow re-runs and replaces it
        with a valid, manifest-bound marker (only a VALID marker makes the
        payload immutable)."""
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        target = committed / self.gen_id
        shutil.copytree(self.staging / self.gen_id, target)
        (target / "READY").write_text("20260101T000000000000Z-ffffffff\n")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            (committed / self.gen_id / "READY").read_text("utf-8").strip(),
            self.gen_id,
            "the re-commit must publish a valid READY marker bound to the manifest",
        )
        self.assertTrue((committed / self.gen_id / "manifest.json").exists())
        self.assertIn(_acked(self.gen_id, self.staging),
                      (self.state / "uploaded-generations.jsonl").read_text("utf-8"))

    # ------------------------------------------------------------------
    # Indeterminate remote READY/manifest read failures (rclone
    # transport/auth/backend error) are NEVER treated as markerless: the
    # upload aborts BEFORE any remote mutation — a possibly-published
    # payload must never be re-uploaded/re-committed over.
    # ------------------------------------------------------------------

    def test_ready_cat_failure_aborts_before_mutation(self) -> None:
        """A FAILED remote READY read (not a confirmed not-found) leaves
        the marker state UNKNOWN: the uploader must abort BEFORE any remote
        mutation instead of treating the generation as markerless and
        re-uploading over a possibly-published payload."""
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "committed", self.gen_id
        )
        before = (committed / "vault" / "notes" / "hello.md").read_bytes()
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CAT_SUBSTR": "READY"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("marker state UNKNOWN", proc.stderr)
        self.assertIn("aborting BEFORE any remote mutation", proc.stderr)
        # No rclone transfer or prune command at all after the startup
        # config check: the abort happens before the retry validation
        # download, the upload, and the retention prune.
        transfers = [e["cmd"] for e in self.fixture.log_entries()
                     if e["cmd"] in ("copy", "move", "purge", "lsd")]
        self.assertEqual(transfers, [], transfers)
        self.assertEqual(
            (committed / "vault" / "notes" / "hello.md").read_bytes(), before,
            "the possibly-published payload must stay byte-identical",
        )
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertFalse((self.state / "last-uploaded-generation").exists())

    def test_manifest_cat_failure_aborts_before_mutation(self) -> None:
        """A FAILED remote manifest read after a valid READY read (rclone
        error, not a confirmed not-found) is also indeterminate: the
        uploader aborts BEFORE any remote mutation instead of treating the
        marker as unbound and re-committing over a possibly-published
        payload."""
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "committed", self.gen_id
        )
        before = (committed / "vault" / "notes" / "hello.md").read_bytes()
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CAT_SUBSTR": "manifest.json"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("marker state UNKNOWN", proc.stderr)
        self.assertIn("aborting BEFORE any remote mutation", proc.stderr)
        transfers = [e["cmd"] for e in self.fixture.log_entries()
                     if e["cmd"] in ("copy", "move", "purge", "lsd")]
        self.assertEqual(transfers, [], transfers)
        self.assertEqual(
            (committed / "vault" / "notes" / "hello.md").read_bytes(), before,
            "the possibly-published payload must stay byte-identical",
        )
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertFalse((self.state / "last-uploaded-generation").exists())

    # ------------------------------------------------------------------
    # Staged backlog reconciliation (foreground): every staged generation
    # NOT acknowledged in the local ledger is uploaded and acknowledged,
    # oldest first — not just the `latest` pointer. Fail closed on any
    # unrecognized staged entry or upload failure (abort before further
    # uploads; the next run resumes at the same oldest unacknowledged
    # generation).
    # ------------------------------------------------------------------

    def _seed_local_backlog(self) -> list[str]:
        """Seed three older unacknowledged FULL generations in staging and
        return their ids (older than the fresh `self.gen_id`)."""
        older = [f"202601{i:02d}T000000000000Z-b{i:07d}" for i in range(1, 4)]
        self._seed_local(older)
        return older

    def _uncommitted_uploads(self) -> list[str]:
        """Remote destinations of the upload copies (the first copy of
        each generation, into the uncommitted namespace), in log order."""
        return [e["args"][2] for e in self.fixture.log_entries()
                if e["cmd"] == "copy" and "/uncommitted/" in e["args"][2]]

    def test_backlog_uploads_all_unacknowledged_oldest_first(self) -> None:
        """The uploader reconciles the FULL staged backlog, not just the
        `latest` pointer: every staged generation absent from the local
        ledger is uploaded and acknowledged, oldest first (lexical order
        == chronological order for generation ids)."""
        older = self._seed_local_backlog()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reconciling staged backlog: 4 unacknowledged", proc.stdout)
        self.assertEqual(self._committed_names(), sorted(older + [self.gen_id]))
        # Oldest-first upload order: the upload copies into the uncommitted
        # namespace appear in chronological order (copy args: [cmd, src, dst]).
        self.assertEqual(
            self._uncommitted_uploads(),
            [f"vault-crypt:Josemar/vault-recovery/uncommitted/{g}"
             for g in older + [self.gen_id]],
        )
        # Every committed generation is READY-bound to its manifest.
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        for g in older + [self.gen_id]:
            self.assertEqual((committed / g / "READY").read_text("utf-8").strip(), g)
        # Every generation is acknowledged; the pointer ends at the newest.
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        for g in older + [self.gen_id]:
            self.assertIn(_acked(g, self.staging), ledger)
        self.assertEqual(
            (self.state / "last-uploaded-generation").read_text("utf-8").strip(),
            self.gen_id,
        )

    def test_backlog_skips_ledger_acked_generations(self) -> None:
        """Generations already acknowledged in the local ledger are
        skipped by the reconciliation (no re-upload, no re-validation);
        only the unacknowledged staged generations are processed. A
        DIGEST-BOUND ack is honored only while the CURRENT remote holds a
        committed payload matching the recorded manifest/READY digests, so
        the acked generations are seeded on the remote too (the confirmed
        state a completed upload leaves behind)."""
        older = self._seed_local_backlog()
        self._seed_ledger([older[0], older[2]])
        self._seed_remote_committed([older[0], older[2]])
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reconciling staged backlog: 2 unacknowledged", proc.stdout)
        self.assertEqual(
            self._uncommitted_uploads(),
            [f"vault-crypt:Josemar/vault-recovery/uncommitted/{g}"
             for g in [older[1], self.gen_id]],
        )
        # The seeded acked generations stay on the remote (that is what made
        # their acks CONFIRMED); only the unacknowledged ones were uploaded.
        self.assertEqual(
            self._committed_names(), sorted(older + [self.gen_id])
        )
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(ledger, [_acked(older[0], self.staging), _acked(older[2], self.staging), _acked(older[1], self.staging), _acked(self.gen_id, self.staging)])

    def test_backlog_indeterminate_ack_read_aborts_before_mutation(self) -> None:
        """Foreground reconciliation after a cancelled/failed uploader task:
        an INDETERMINATE remote READY read (rclone transport error, not a
        confirmed not-found) while confirming an acknowledged generation is
        NEVER treated as a confirmed mismatch. The run ABORTS before any
        remote mutation — a possibly-published payload must never be
        re-uploaded over because its ack could not be re-validated."""
        older = self._seed_local_backlog()
        self._seed_ledger([older[0]])
        self._seed_remote_committed([older[0]])
        committed = self.fixture.remote_dir(
            "Josemar", "vault-recovery", "committed", older[0]
        )
        before = (committed / "vault" / "notes" / "hello.md").read_bytes()
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CAT_SUBSTR": "READY"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("marker state UNKNOWN", proc.stderr)
        self.assertIn("aborting backlog reconciliation", proc.stderr)
        # No transfer or prune command at all: the abort happens during the
        # ack revalidation, before any upload/commit/purge.
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])
        self.assertEqual(
            (committed / "vault" / "notes" / "hello.md").read_bytes(), before,
            "the possibly-published payload must stay byte-identical",
        )

    def test_backlog_confirmed_missing_remote_payload_reuploads(self) -> None:
        """Foreground reconciliation after a remote WIPE: a digest-bound ack
        whose remote committed payload is CONFIRMED absent (rclone
        file-not-found, not a read failure) is a confirmed mismatch — the
        staged generation is re-uploaded and re-acknowledged, never aborted
        (a confirmed-missing payload is NOT an indeterminate state)."""
        older = self._seed_local_backlog()
        self._seed_ledger([older[0]])
        # The ack claims a committed payload that is NOT there (remote wiped).
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no longer matches the remote payload", proc.stdout)
        self.assertNotIn("marker state UNKNOWN", proc.stderr)
        self.assertIn("Reconciling staged backlog: 4 unacknowledged", proc.stdout)
        self.assertEqual(self._committed_names(), sorted([older[0], older[1], older[2], self.gen_id]))
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        # The stale digest-bound line stays as history; older[0] is
        # re-uploaded (confirmed mismatch) and re-acknowledged under the
        # current remote, along with the rest of the backlog.
        self.assertEqual(
            ledger,
            [_acked(older[0], self.staging), _acked(older[0], self.staging),
             _acked(older[1], self.staging), _acked(older[2], self.staging),
             _acked(self.gen_id, self.staging)],
        )

    def test_backlog_aborts_on_invalid_staged_name_before_any_upload(self) -> None:
        """A staging root containing a directory that is not a strict
        generation id (e.g. a crashed export's leftover, or a tampered
        entry) cannot be fully accounted for: the reconciliation ABORTS
        before ANY upload — nothing is transferred, nothing is committed,
        nothing is acknowledged (fail closed)."""
        self._seed_local_backlog()
        (self.staging / "not-a-generation").mkdir()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Invalid directory name in staging", proc.stderr)
        self.assertIn("aborting backlog reconciliation", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])
        self.assertEqual(self._committed_names(), [])
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertFalse((self.state / "last-uploaded-generation").exists())

    def test_backlog_failure_aborts_and_rerun_completes(self) -> None:
        """A failure while uploading the backlog aborts the run BEFORE
        the remaining generations (oldest first): nothing after the
        failure is uploaded or acknowledged in that run, and the next run
        resumes from the same oldest unacknowledged generation
        (incremental catch-up)."""
        older = self._seed_local_backlog()
        # copy #1: upload of the oldest, #2: its remote verify, #3: its
        # committed verify, #4: the SECOND generation's upload -> fails.
        first = self._run_impl({"FAKE_RCLONE_FAIL_CMD_AFTER": "copy:4"})
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertIn("ABORTED", first.stderr)
        self.assertEqual(self._committed_names(), [older[0]])
        self.assertEqual(
            (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines(),
            [_acked(older[0], self.staging)],
        )
        # The untouched backlog stays staged.
        for g in older + [self.gen_id]:
            self.assertTrue((self.staging / g).is_dir(), g)
        # Next run: acked ones are skipped, the rest is completed.
        self.fixture.log.unlink(missing_ok=True)
        second = self._run_impl({})
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Reconciling staged backlog: 3 unacknowledged", second.stdout)
        self.assertEqual(self._committed_names(), sorted(older + [self.gen_id]))
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(ledger, [_acked(older[0], self.staging), _acked(older[1], self.staging), _acked(older[2], self.staging), _acked(self.gen_id, self.staging)])

    def test_backlog_retry_after_ready_not_mutated_when_older_pending(self) -> None:
        """Mixed backlog: the newest generation is already committed with
        a valid READY marker (crash between READY publication and the
        local ack) while OLDER staged generations are still
        unacknowledged. The older ones are uploaded normally; the
        READY-visible newest is re-validated and acknowledged WITHOUT any
        mutation — no upload, no commit move, no overwrite of the
        published payload."""
        older = self._seed_local_backlog()
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        payload = committed / self.gen_id / "vault" / "notes" / "hello.md"
        before = payload.read_bytes()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("valid READY marker", proc.stdout)
        self.assertIn("no overwrite", proc.stdout)
        self.assertEqual(self._committed_names(), sorted(older + [self.gen_id]))
        # No copy/move/purge ever TARGETED the committed newest generation
        # (the re-validation download only reads it).
        writes = [
            e["args"][2] for e in self.fixture.log_entries()
            if e["cmd"] in ("copy", "move") and self.gen_id in e["args"][2]
        ] + [
            e["args"][1] for e in self.fixture.log_entries()
            if e["cmd"] == "purge" and self.gen_id in e["args"][1]
        ]
        self.assertEqual(writes, [], writes)
        self.assertEqual(payload.read_bytes(), before)
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(ledger, [_acked(older[0], self.staging), _acked(older[1], self.staging), _acked(older[2], self.staging), _acked(self.gen_id, self.staging)])

    # ------------------------------------------------------------------
    # Retention: exactly 14 committed generations, prune only after a
    # valid remote inventory
    # ------------------------------------------------------------------

    def _seed_committed(self, ids: list[str], ledger_ids: list[str]) -> None:
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        gen_dir = self.staging / self.gen_id
        for gen_id in ids:
            _seed_committed_generation(gen_dir, gen_id, committed / gen_id)
        if ledger_ids:
            self.state.mkdir(parents=True, exist_ok=True)
            # Digest-bound acks: the manifest/READY sha256 of the REMOTE
            # committed payloads (a retention prune honors an ack only while
            # the current remote still holds a payload matching the
            # ledger-bound digests).
            lines = []
            for gen_id in ledger_ids:
                target = committed / gen_id
                manifest_sha = hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest()
                ready_sha = hashlib.sha256((target / "READY").read_bytes()).hexdigest()
                lines.append(
                    f"{gen_id}\tvault-crypt\tJosemar/vault-recovery\t{manifest_sha}\t{ready_sha}"
                )
            (self.state / "uploaded-generations.jsonl").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )

    def _committed_names(self) -> list[str]:
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        if not committed.exists():
            return []
        return sorted(e.name for e in committed.iterdir() if e.is_dir())

    def test_retention_keeps_exactly_14(self) -> None:
        self._seed_committed(SEED_IDS, SEED_IDS)
        proc = self._run_impl({})  # uploads the newest gen, then prunes
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = self._committed_names()
        self.assertEqual(len(names), 14, names)
        newest = sorted(SEED_IDS + [self.gen_id])[-14:]
        self.assertEqual(names, sorted(newest))
        # Exactly the 7 oldest were purged from the committed namespace.
        purges = [
            e for e in self.fixture.log_entries()
            if e["cmd"] == "purge" and "/committed/" in e["args"][1]
        ]
        self.assertEqual(len(purges), 7, purges)

    def test_no_prune_when_inventory_invalid(self) -> None:
        self._seed_committed(SEED_IDS, SEED_IDS)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        (committed / "not-a-generation").mkdir()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("invalid name", proc.stderr)
        self.assertEqual(
            [e for e in self.fixture.log_entries() if e["cmd"] == "purge"], []
        )
        # 20 seeded + the uploaded generation + the invalid inventory dir.
        self.assertEqual(len(self._committed_names()), 22)

    def test_no_prune_when_inventory_listing_fails(self) -> None:
        """A FAILED remote inventory listing is not a clean listing: prune
        is skipped (safety over convenience) but the failure is VISIBLE in
        the logs instead of being silently treated as an empty namespace."""
        self._seed_committed(SEED_IDS, SEED_IDS)
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CMDS": "lsjson"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("inventory listing FAILED", proc.stderr)
        self.assertEqual(
            [e for e in self.fixture.log_entries() if e["cmd"] == "purge"], []
        )
        # The upload itself still committed + acknowledged.
        self.assertEqual(
            (self.state / "last-uploaded-generation").read_text("utf-8").strip(),
            self.gen_id,
        )
        self.assertIn(self.gen_id, (self.state / "uploaded-generations.jsonl").read_text("utf-8"))

    def test_zero_byte_inventory_response_fails_closed(self) -> None:
        """A ZERO-BYTE successful lsjson response is a PROTOCOL failure,
        never an empty inventory: a successful rclone lsjson always emits
        at least a valid JSON array (`[]` for an empty namespace). Prune is
        skipped with a visible error (fail closed), never silently treated
        as "nothing to prune"."""
        self._seed_committed(SEED_IDS, SEED_IDS)
        proc = self._run_impl({"FAKE_RCLONE_LSJSON_ZERO_BYTES": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ZERO-BYTE", proc.stderr)
        self.assertEqual(
            [e for e in self.fixture.log_entries() if e["cmd"] == "purge"], []
        )
        # 20 seeded + the uploaded generation; nothing pruned.
        self.assertEqual(len(self._committed_names()), 21)
        # The upload itself still committed + acknowledged before the
        # (skipped) prune ran.
        self.assertEqual(
            (self.state / "last-uploaded-generation").read_text("utf-8").strip(),
            self.gen_id,
        )

    def test_no_prune_when_ready_read_fails(self) -> None:
        """An INDETERMINATE remote READY/manifest read failure during the
        prune's validity sweep (rclone error, not a confirmed not-found) is
        never treated as markerless: the ENTIRE prune is skipped with a
        visible error, even though the upload itself succeeded. A prune
        computed over partially-unknown marker state could evict
        generations a healthy read would have kept."""
        self._seed_committed(SEED_IDS, SEED_IDS)
        # cat #1 is the upload's own READY check for the new generation
        # (confirmed missing -> normal upload proceeds); cat #2 is the
        # first READY check of the prune sweep (the newest generation) and
        # fails with an indeterminate rclone error.
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CMD_AFTER": "cat:2"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skipping the ENTIRE prune", proc.stderr)
        self.assertIn("marker state UNKNOWN", proc.stderr)
        self.assertEqual(
            [e for e in self.fixture.log_entries() if e["cmd"] == "purge"], []
        )
        # Nothing pruned: 20 seeded + the uploaded generation.
        self.assertEqual(len(self._committed_names()), 21)
        # The upload still committed + acknowledged before the prune ran.
        self.assertEqual(
            (self.state / "last-uploaded-generation").read_text("utf-8").strip(),
            self.gen_id,
        )
        self.assertIn(self.gen_id, (self.state / "uploaded-generations.jsonl").read_text("utf-8"))

    def test_oversized_latest_pointer_fails_closed_before_rclone(self) -> None:
        """An oversized (abnormal) latest pointer is refused before any
        remote interaction: the bounded read makes it fail the strict
        generation-id validation instead of being processed unbounded."""
        (self.staging / "latest").write_text(
            self.gen_id + "\n" + "x" * 5000, encoding="utf-8"
        )
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Invalid latest pointer", proc.stderr)
        self.assertEqual(self.fixture.log_entries(), [])

    def test_no_prune_of_unacknowledged_generations(self) -> None:
        self._seed_committed(SEED_IDS, [])
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NOT pruning", proc.stdout)
        self.assertEqual(
            [e for e in self.fixture.log_entries() if e["cmd"] == "purge"], []
        )
        self.assertEqual(len(self._committed_names()), 21)

    def test_retention_under_limit_no_prune(self) -> None:
        self._seed_committed(SEED_IDS[:5], SEED_IDS[:5])
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [e for e in self.fixture.log_entries() if e["cmd"] == "purge"], []
        )
        self.assertEqual(len(self._committed_names()), 6)

    def test_retention_counts_and_prunes_only_ready_valid(self) -> None:
        """Retention counts and prunes ONLY committed generations carrying a
        valid READY marker bound to the manifest. Incomplete committed dirs
        (interrupted commits: markerless, invalid marker, empty, manifest
        binding mismatch) are PRESERVED — they never count toward retention,
        are never pruned, and never evict valid generations."""
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        gen_dir = self.staging / self.gen_id
        for gen_id in SEED_IDS:
            _seed_committed_generation(gen_dir, gen_id, committed / gen_id)
        # Acknowledge the valid seeds with DIGEST-BOUND entries (pruning
        # requires a ledger match bound to the current remote identity AND
        # to the committed payload digests right now; the incomplete dirs
        # must never get one).
        self.state.mkdir(parents=True, exist_ok=True)
        lines = []
        for gen_id in SEED_IDS:
            target = committed / gen_id
            manifest_sha = hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest()
            ready_sha = hashlib.sha256((target / "READY").read_bytes()).hexdigest()
            lines.append(
                f"{gen_id}\tvault-crypt\tJosemar/vault-recovery\t{manifest_sha}\t{ready_sha}"
            )
        (self.state / "uploaded-generations.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        incomplete = [
            "20260121T000000000000Z-a0000021",  # markerless (no READY)
            "20260122T000000000000Z-a0000022",  # invalid READY content
            "20260123T000000000000Z-a0000023",  # empty dir
            "20260124T000000000000Z-a0000024",  # manifest binding mismatch
        ]
        for gen_id in incomplete:
            target = committed / gen_id
            shutil.copytree(gen_dir, target)
        (committed / incomplete[0] / "READY").unlink()
        (committed / incomplete[1] / "READY").write_text(
            "20260101T000000000000Z-ffffffff\n", encoding="utf-8"
        )
        # Empty dir: remove every entry.
        for child in (committed / incomplete[2]).iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        # Binding mismatch: READY names the dir but the manifest does not.
        (committed / incomplete[3] / "READY").write_text(
            f"{incomplete[3]}\n", encoding="utf-8"
        )
        manifest_path = committed / incomplete[3] / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["generation_id"] = "20260101T000000000000Z-ffffffff"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

        proc = self._run_impl({})  # uploads the newest gen, then prunes
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = self._committed_names()
        # 14 newest VALID generations kept (21 valid: 20 seeded + new one)
        # plus the 4 preserved incomplete dirs.
        expected_kept = sorted(SEED_IDS[7:] + [self.gen_id] + incomplete)
        self.assertEqual(names, expected_kept, names)
        # Exactly the 7 oldest VALID generations were purged — never an
        # incomplete dir, and never more valid generations than the window.
        purged = [
            e["args"][1].rsplit("/", 1)[-1]
            for e in self.fixture.log_entries()
            if e["cmd"] == "purge" and "/committed/" in e["args"][1]
        ]
        self.assertEqual(sorted(purged), SEED_IDS[:7], purged)
        self.assertIn("Preserving incomplete", proc.stdout)
        # A second run is a no-op for the acknowledged generation and must
        # not evict the incomplete dirs either.
        self.fixture.log.unlink(missing_ok=True)
        second = self._run_impl({})
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self._committed_names(), expected_kept)

    # ------------------------------------------------------------------
    # LOCAL staged retention (ack-based): after a remote-acknowledged
    # upload, prune the local staging beyond the newest
    # VAULT_RECOVERY_LOCAL_RETENTION FULL generations — only acked ones.
    # ------------------------------------------------------------------

    def _seed_local(self, ids: list[str]) -> None:
        """Seed older FULL generation dirs directly in the staging tree."""
        for gen_id in ids:
            _seed_committed_generation(
                self.staging / self.gen_id, gen_id, self.staging / gen_id
            )

    def _seed_remote_committed(self, ids: list[str]) -> None:
        """Copy staged generations into the fake remote's COMMITTED
        namespace (what the uploader would have produced). Used to make a
        ledger acknowledgement CONFIRMED: the digest-bound ack is honored
        only while the CURRENT remote holds a committed payload matching
        the recorded manifest/READY digests."""
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        for gen_id in ids:
            shutil.copytree(self.staging / gen_id, committed / gen_id)

    def _ledger_line(
        self,
        gen_id: str,
        remote: str = "vault-crypt",
        path: str = "Josemar/vault-recovery",
    ) -> str:
        """The digest-bound ledger line for a staged generation (manifest/
        READY sha256 of the staged bytes; the fake rclone round-trips bytes
        exactly, so these equal the remote payload digests)."""
        gen_dir = self.staging / gen_id
        if (gen_dir / "manifest.json").is_file():
            manifest_sha = hashlib.sha256((gen_dir / "manifest.json").read_bytes()).hexdigest()
        else:
            manifest_sha = "0" * 64
        if (gen_dir / "READY").is_file():
            ready_sha = hashlib.sha256((gen_dir / "READY").read_bytes()).hexdigest()
        else:
            ready_sha = "0" * 64
        return f"{gen_id}\t{remote}\t{path}\t{manifest_sha}\t{ready_sha}"

    def _seed_ledger(self, ids: list[str], remote: str = "vault-crypt", path: str = "Josemar/vault-recovery") -> None:
        """Seed the ack ledger with DIGEST-BOUND entries (gen-id TAB
        remote-name TAB remote-path TAB manifest-sha256 TAB ready-sha256)
        bound to a remote identity. A bare generation id is NOT an
        acknowledgement for the current remote. The digests are the sha256
        of the staged manifest/READY bytes (equal to the remote payload
        digests when the same generation was copied to the committed
        namespace); an absent staged dir yields zero digests (the binding
        still fails the `ledger_has` prefix match on other tests)."""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "uploaded-generations.jsonl").write_text(
            "".join(self._ledger_line(g, remote, path) + "\n" for g in ids),
            encoding="utf-8",
        )

    def _local_gen_names(self) -> list[str]:
        return sorted(
            e.name for e in self.staging.iterdir()
            if e.is_dir()
            and e.name != ".vault-recovery-install"
            and len(e.name) == 31
            and e.name[:8].isdigit()
            and "-" in e.name
        )

    def test_local_retention_prunes_acked_old_generations(self) -> None:
        """After the upload is acknowledged, local staging generations
        beyond the newest LOCAL_RETENTION that ARE acked are pruned; the
        newest (the just-uploaded one) and `latest` are always kept."""
        self._seed_local(SEED_IDS[:3])
        self._seed_ledger(SEED_IDS[:3])
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Local retention: pruning staged generation", proc.stdout)
        self.assertEqual(self._local_gen_names(), [self.gen_id])
        self.assertTrue((self.staging / "latest").exists())

    # ------------------------------------------------------------------
    # ACK BINDING (council fix): every ledger acknowledgement is bound to
    # the remote identity (name + path) it was recorded against. An ack
    # for a DIFFERENT remote (rotation, re-pointed path) or a legacy bare
    # generation id is NOT an acknowledgement for the current remote —
    # backlog skip, the dangling-pointer check, and both retention passes
    # only honor bound acks, so remote rotation can never let retention
    # delete generations the current remote has not committed.
    # ------------------------------------------------------------------

    def test_ack_bound_to_other_remote_not_honored_for_backlog_skip(self) -> None:
        """A ledger acknowledging the staged generations under a DIFFERENT
        remote name is not honored: the backlog treats them as
        unacknowledged and re-uploads them to the CURRENT remote (oldest
        first), then re-acknowledges them under the current identity."""
        older = self._seed_local_backlog()
        self._seed_ledger(older, remote="other-crypt")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reconciling staged backlog: 4 unacknowledged", proc.stdout)
        self.assertEqual(self._committed_names(), sorted(older + [self.gen_id]))
        # The ledger is append-only history: the old-remote lines remain,
        # and every generation now ALSO carries a CURRENT-remote binding
        # (the re-upload re-acknowledged it under the current identity).
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        for g in older + [self.gen_id]:
            self.assertIn(_acked(g, self.staging), ledger)

    def test_ack_bound_to_other_path_not_honored_for_backlog_skip(self) -> None:
        """Same binding rule for the remote PATH: an ack recorded against a
        different namespace (e.g. `Other/vault-recovery`) does not count
        for the current `Josemar/vault-recovery` remote."""
        older = self._seed_local_backlog()
        self._seed_ledger(older, path="Other/vault-recovery")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reconciling staged backlog: 4 unacknowledged", proc.stdout)
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        for g in older + [self.gen_id]:
            self.assertIn(_acked(g, self.staging), ledger)

    def test_legacy_bare_ledger_lines_not_acknowledgements(self) -> None:
        """A pre-binding ledger (bare generation ids) is not an
        acknowledgement for the current remote: the generations are
        re-uploaded (or re-validated via the READY protocol) and
        re-acknowledged in the bound format. The bound re-ack happens
        BEFORE any retention, so nothing is ever pruned on an unbound
        ack."""
        older = self._seed_local_backlog()
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "uploaded-generations.jsonl").write_text(
            "".join(f"{g}\n" for g in older), encoding="utf-8"
        )
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reconciling staged backlog: 4 unacknowledged", proc.stdout)
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        # The legacy bare lines stay as history; the FOUR NEW lines are
        # the current-remote DIGEST-BOUND re-acknowledgements (gen-id TAB
        # remote-name TAB remote-path TAB manifest-sha256 TAB ready-sha256).
        self.assertEqual(len(ledger), 7)
        for line in ledger[3:]:
            self.assertEqual(len(line.split("\t")), 5, line)
            gen, remote, path, manifest_sha, ready_sha = line.split("\t")
            self.assertEqual((gen, remote, path),
                             (gen, "vault-crypt", "Josemar/vault-recovery"), line)
            self.assertRegex(manifest_sha, r"^[0-9a-f]{64}$", line)
            self.assertRegex(ready_sha, r"^[0-9a-f]{64}$", line)
        for line in ledger[:3]:
            self.assertEqual(len(line.split("\t")), 1, line)

    def test_dangling_latest_not_satisfied_by_other_remote_ack(self) -> None:
        """The dangling-`latest` fail-closed check honors ONLY bound acks:
        a latest pointer whose generation is acked under a DIFFERENT remote
        (remote rotation) is still treated as dangling and ABORTS the run
        — the staged generation is absent and the current remote has not
        acknowledged it."""
        (self.staging / "latest").write_text(f"{SEED_IDS[0]}\n", encoding="utf-8")
        self._seed_ledger([SEED_IDS[0]], remote="other-crypt")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Generation dir not found for latest pointer", proc.stderr)

    def test_stale_identity_ack_missing_latest_fails_closed(self) -> None:
        """A `latest` pointer whose generation is NOT staged cannot be
        re-uploaded, so an IDENTITY-ONLY ledger entry (legacy 3-field line,
        no digests) must never turn it into a no-op success: with the
        remote wiped (no committed payload matching the ack) the
        acknowledgement is UNCONFIRMED and the run FAILS CLOSED before any
        remote mutation or prune — no false success, and no prune without
        revalidation of the current remote payload."""
        # Other committed generations carry valid digest-bound acks so the
        # run would have candidates to prune if it wrongly succeeded.
        self._seed_committed(SEED_IDS[:5], SEED_IDS[:5])
        # latest points at a generation that is NOT staged; its only ledger
        # record is a stale legacy identity-only line (the remote was
        # wiped: no committed payload exists for it).
        latest = SEED_IDS[10]
        (self.staging / "latest").write_text(f"{latest}\n", encoding="utf-8")
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / "uploaded-generations.jsonl", "a", encoding="utf-8") as fh:
            fh.write(f"{latest}\tvault-crypt\tJosemar/vault-recovery\n")
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("not staged", proc.stderr)
        self.assertIn("NOT confirmed", proc.stderr)
        self.assertNotIn("no-op", proc.stdout)
        # No upload, no prune: the seeded committed generations stay
        # untouched (nothing is pruned without revalidation).
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])
        self.assertEqual(self._committed_names(), sorted(SEED_IDS[:5]))
        self.assertFalse((self.state / "last-uploaded-generation").exists())

    def test_latest_missing_with_indeterminate_ack_read_aborts(self) -> None:
        """The dangling-`latest` check also honors the tri-state contract:
        an INDETERMINATE remote READY read (rclone transport error, not a
        confirmed not-found) while validating the latest generation's ack
        is UNKNOWN — the run ABORTS instead of reporting success or
        pruning (fail closed, NOT treated as confirmed)."""
        latest = SEED_IDS[0]
        (self.staging / "latest").write_text(f"{latest}\n", encoding="utf-8")
        # A digest-bound ledger line (zero digests: no staged copy exists;
        # the read failure decides before any digest comparison).
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "uploaded-generations.jsonl").write_text(
            f"{latest}\tvault-crypt\tJosemar/vault-recovery\t{'0' * 64}\t{'0' * 64}\n",
            encoding="utf-8",
        )
        proc = self._run_impl({"FAKE_RCLONE_FAIL_CAT_SUBSTR": "READY"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("marker state UNKNOWN", proc.stderr)
        self.assertIn("aborting", proc.stderr)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])
        self.assertFalse((self.state / "last-uploaded-generation").exists())

    def test_latest_missing_with_confirmed_ack_is_honest_noop(self) -> None:
        """The strengthened dangling-`latest` check keeps the legitimate
        no-op: a latest generation that is NOT staged (e.g. locally pruned
        after acknowledgement) but whose DIGEST-BOUND ack is CONFIRMED
        against the CURRENT remote payload is a real prior upload — the
        run reports the no-op success without re-uploading."""
        latest = SEED_IDS[0]
        (self.staging / "latest").write_text(f"{latest}\n", encoding="utf-8")
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        # The remote holds the committed payloads; the ledger binds their
        # exact manifest/READY digests (no staged copy needed for `latest`).
        shutil.copytree(self.staging / self.gen_id, committed / latest)
        (committed / latest / "manifest.json").write_text(
            json.dumps({**json.loads((committed / latest / "manifest.json").read_text("utf-8")),
                        "generation_id": latest}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (committed / latest / "READY").write_text(f"{latest}\n", encoding="utf-8")
        shutil.copytree(self.staging / self.gen_id, committed / self.gen_id)
        lines = []
        for gen_id in (latest, self.gen_id):
            target = committed / gen_id
            manifest_sha = hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest()
            ready_sha = hashlib.sha256((target / "READY").read_bytes()).hexdigest()
            lines.append(
                f"{gen_id}\tvault-crypt\tJosemar/vault-recovery\t{manifest_sha}\t{ready_sha}"
            )
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "uploaded-generations.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8",
        )
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already uploaded; no-op", proc.stdout)
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])

    def test_remote_rotation_revalidates_committed_payload_before_rebinding(self) -> None:
        """Remote rotation with an already-committed READY-visible payload:
        the ack is bound to the OLD remote; the current remote already
        holds the generation (READY-visible, bound to its manifest). The
        retry-after-READY protocol re-validates the committed payload
        WITHOUT mutation and re-acknowledges it under the CURRENT remote
        identity — no upload, no overwrite."""
        older = self._seed_local_backlog()
        self._seed_ledger(older, remote="old-crypt")
        # The CURRENT remote already committed the newest generation.
        seed_remote_committed(self.fixture, self.gen_id, self.staging)
        committed = self.fixture.remote_dir("Josemar", "vault-recovery", "committed")
        payload = committed / self.gen_id / "vault" / "notes" / "hello.md"
        before = payload.read_bytes()
        proc = self._run_impl({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The rotation case: older generations re-uploaded to the current
        # remote, the READY-visible newest re-validated without mutation.
        self.assertEqual(self._committed_names(), sorted(older + [self.gen_id]))
        self.assertEqual(payload.read_bytes(), before)
        # History (old-remote lines) is retained; the newest four lines
        # carry the CURRENT-remote binding (3 older re-uploads + 1
        # READY-visible re-acknowledgement).
        ledger = (self.state / "uploaded-generations.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(len(ledger), 7)
        for line in ledger[3:]:
            self.assertIn("\tvault-crypt\tJosemar/vault-recovery", line)
        for g in older + [self.gen_id]:
            self.assertIn(_acked(g, self.staging), ledger)

    def test_local_retention_keeps_unacked_generations(self) -> None:
        """A generation the remote has NOT acknowledged is never pruned
        locally, even beyond the keep window (safety over convenience).
        With backlog reconciliation the unacknowledged generations are
        uploaded (and thus acked) FIRST, oldest first — when an upload
        fails, the run aborts and the still-unacknowledged generations
        stay staged and untouched."""
        self._seed_local(SEED_IDS[:3])
        # copy #1: first upload, #2: first remote verify, #3: first
        # committed verify, #4: SECOND generation's upload -> fails, the
        # run aborts before any further upload or prune.
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1",
                               "FAKE_RCLONE_FAIL_CMD_AFTER": "copy:4"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ABORTED", proc.stderr)
        self.assertIn("NOT pruning staged generation", proc.stdout)
        # Only the acknowledged first generation may be pruned locally;
        # the unacknowledged ones were never pruned.
        self.assertEqual(
            self._local_gen_names(), sorted(SEED_IDS[1:3] + [self.gen_id])
        )

    def test_local_retention_skips_entire_prune_on_invalid_name(self) -> None:
        """An invalid directory name in the staging root ABORTS the whole
        backlog reconciliation (fail closed) before any upload and before
        any prune: nothing is deleted, nothing is uploaded, the error is
        visible. (The prune-level guard remains as defense in depth.)"""
        self._seed_local(SEED_IDS[:3])
        self._seed_ledger(SEED_IDS[:3])
        (self.staging / "not-a-generation").mkdir()
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Invalid directory name in staging", proc.stderr)
        self.assertIn("aborting backlog reconciliation", proc.stderr)
        # Nothing was deleted and nothing was uploaded: all valid
        # generations AND the invalid dir itself remain, and the remote
        # committed namespace was never touched.
        self.assertEqual(
            self._local_gen_names(), sorted(SEED_IDS[:3] + [self.gen_id])
        )
        self.assertTrue((self.staging / "not-a-generation").is_dir())
        self.assertEqual(_no_transfer_cmds(self.fixture.log_entries()), [])
        self.assertFalse(
            self.fixture.remote_dir("Josemar", "vault-recovery", "committed").exists()
        )

    def test_local_retention_skips_entire_prune_on_missing_ready(self) -> None:
        """An incomplete local generation (no READY) SKIPS the ENTIRE local
        prune: a partially-published generation is never deleted and never
        prunes its neighbors."""
        self._seed_local(SEED_IDS[:3])
        self._seed_ledger(SEED_IDS[:3])
        # The acks must be CONFIRMED against the current remote (digest-bound
        # ledger + matching committed payload) so the backlog SKIPS the
        # seeded generations and the run reaches the local prune instead of
        # re-uploading (and failing on) the invalid candidate.
        self._seed_remote_committed(SEED_IDS[:3])
        (self.staging / SEED_IDS[0] / "READY").unlink()
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("missing READY", proc.stderr)
        self.assertIn("skipping the ENTIRE local prune", proc.stderr)
        self.assertEqual(
            self._local_gen_names(), sorted(SEED_IDS[:3] + [self.gen_id])
        )

    def test_local_retention_skips_entire_prune_on_ready_content_mismatch(self) -> None:
        """FULL validation before deletion (council fix): a READY sentinel
        that EXISTS but does not bind to the generation id (content !=
        generation id) SKIPS the ENTIRE local prune. The old existence-only
        check would have accepted this entry and pruned valid state."""
        self._seed_local(SEED_IDS[:3])
        self._seed_ledger(SEED_IDS[:3])
        # Confirmed acks (digest-bound ledger + matching committed payload):
        # the backlog skips the seeded generations, the local prune decides.
        self._seed_remote_committed(SEED_IDS[:3])
        (self.staging / SEED_IDS[0] / "READY").write_text(
            "20260101T000000000000Z-ffffffff\n", encoding="utf-8"
        )
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("READY generation mismatch", proc.stderr)
        self.assertIn("skipping the ENTIRE local prune", proc.stderr)
        self.assertEqual(
            self._local_gen_names(), sorted(SEED_IDS[:3] + [self.gen_id])
        )
        self.assertEqual(
            [e for e in self.fixture.log_entries() if e["cmd"] == "purge"], []
        )

    def test_local_retention_skips_entire_prune_on_manifest_mismatch(self) -> None:
        """FULL validation before deletion (council fix): a generation whose
        manifest generation_id does not bind to the directory name SKIPS the
        ENTIRE local prune — valid old state is never removed while any
        staged state is suspect."""
        self._seed_local(SEED_IDS[:3])
        self._seed_ledger(SEED_IDS[:3])
        # Confirmed acks (digest-bound ledger + matching committed payload):
        # the backlog skips the seeded generations, the local prune decides.
        self._seed_remote_committed(SEED_IDS[:3])
        manifest_path = self.staging / SEED_IDS[0] / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["generation_id"] = "20260101T000000000000Z-ffffffff"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("manifest generation_id mismatch", proc.stderr)
        self.assertIn("skipping the ENTIRE local prune", proc.stderr)
        self.assertEqual(
            self._local_gen_names(), sorted(SEED_IDS[:3] + [self.gen_id])
        )

    def test_local_retention_skips_entire_prune_on_tampered_tree(self) -> None:
        """FULL validation before deletion (council fix): a generation whose
        TREE content no longer matches its manifest-bound entries index
        (tampered/missing file, hash mismatch) SKIPS the ENTIRE local prune
        — nothing is deleted, including the valid generations that were
        within prune range."""
        self._seed_local(SEED_IDS[:3])
        self._seed_ledger(SEED_IDS[:3])
        # Confirmed acks (digest-bound ledger + matching committed payload):
        # the backlog skips the seeded generations, the local prune decides.
        self._seed_remote_committed(SEED_IDS[:3])
        (self.staging / SEED_IDS[0] / "vault" / "notes" / "hello.md").write_text(
            "TAMPERED\n", encoding="utf-8"
        )
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sha256 mismatch", proc.stderr)
        self.assertIn("skipping the ENTIRE local prune", proc.stderr)
        self.assertEqual(
            self._local_gen_names(), sorted(SEED_IDS[:3] + [self.gen_id])
        )
        # Even acked generations in prune range were NOT removed.
        for gen_id in SEED_IDS[:3]:
            self.assertTrue((self.staging / gen_id).is_dir(), gen_id)

    def test_local_retention_full_validation_still_prunes_valid(self) -> None:
        """FULL validation does not break the normal path: fully valid acked
        generations beyond the window are still pruned, only the newest is
        kept."""
        self._seed_local(SEED_IDS[:3])
        self._seed_ledger(SEED_IDS[:3])
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Local retention: pruning staged generation", proc.stdout)
        self.assertEqual(self._local_gen_names(), [self.gen_id])

    def test_local_retention_never_prunes_artifacts(self) -> None:
        """`latest` and non-generation artifacts in the staging root are
        never pruned (only full generation dirs are candidates)."""
        self._seed_local(SEED_IDS[:3])
        self._seed_ledger(SEED_IDS[:3])
        (self.staging / "notes.txt").write_text("keep me\n", encoding="utf-8")
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.staging / "latest").exists())
        self.assertEqual((self.staging / "notes.txt").read_text("utf-8"), "keep me\n")

    def test_local_retention_rejects_invalid_config(self) -> None:
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "0"})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("VAULT_RECOVERY_LOCAL_RETENTION must be at least 1", proc.stderr)
        proc = self._run_impl({"VAULT_RECOVERY_LOCAL_RETENTION": "abc"})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("Invalid VAULT_RECOVERY_LOCAL_RETENTION", proc.stderr)


if __name__ == "__main__":
    unittest.main()
