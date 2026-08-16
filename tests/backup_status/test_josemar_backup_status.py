"""Adversarial unit/contract tests for scripts/josemar-backup-status.py.

Covers, per the Oracle-approved direct local reader contract:

  * exact argv (`[vault|mnemosyne] [list|latest]`) and bounded stable usage
    failures;
  * identity enforcement (root denied, euid must equal the system `hermes`
    uid, HERMES_UID must be a nonzero decimal matching it and NEVER
    authorizes alone, missing system user fails closed);
  * env path poisoning (staging roots are fixed constants; no env var can
    redirect the reader);
  * no-follow descriptor-relative traversal (symlink/special/race-ish
    rejection at the root, generation dirs, marker files, and the latest
    pointer);
  * malformed/oversized/binding-invalid READY and manifest inputs (per-
    snapshot observations, never crashes);
  * lane READY rules and digest binding (vault exact id+newline; mnemosyne
    id+newline+64-lowercase-hex+newline matching manifest artifact.sha256);
  * caps and determinism (depth/dirs/files/bytes/snapshots truncation with
    `truncated: true`, stable sorted output, byte-exact repeatability);
  * strict no-leak failure JSON (fixed schema/codes/messages, no
    traceback/path/raw exception text, empty stderr, stable exit codes);
  * no subprocess/shell/network/Docker/rclone/PGLite/locks in the module;
  * Dockerfile.hermes install wiring.

The production staging roots and caps are module constants; every test
exercises the public keyword seams (staging_root, module-constant patches)
so no production path is touched. CLI-level tests run the real script in a
subprocess with identity/staging stubbed in a wrapper (the process euid can
never be the hermes uid in a dev/CI environment).
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
MODULE_PATH = SCRIPTS_DIR / "josemar-backup-status.py"
DOCKERFILE = REPO_ROOT / "Dockerfile.hermes"

# Two fixed test generation ids (lexically sortable; GEN_ID_NEWER > GEN_ID).
GEN_ID = "20260802T012247123456Z-a1b2c3d4"
GEN_ID_NEWER = "20260802T012247123457Z-b2c3d4e5"
GEN_ID_OLDER = "20260801T000000000000Z-00000000"
BAD_GEN_ID = "20260802T012247123456Z-../../etc/passwd"


def _import_module():
    """Import the reader from source (repo convention: no package install,
    no reliance on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "josemar_backup_status", str(MODULE_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _import_module()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_vault_gen(
    root: Path,
    gen_id: str,
    *,
    ready_content: Optional[str] = None,
    manifest: Optional[dict] = None,
    extra_files: Optional[dict] = None,
) -> dict:
    """Build a vault-recovery-style generation. Returns fixture facts used to
    compute expected counts (ready bytes, manifest text, extra file spec)."""
    if ready_content is None:
        ready_content = f"{gen_id}\n"
    if manifest is None:
        manifest = {"schema_version": 1, "generation_id": gen_id}
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True)
    if extra_files is None:
        extra_files = {}
    d = root / gen_id
    (d / "vault").mkdir(parents=True)
    (d / ".gbrain").mkdir()
    (d / "vault" / "note.md").write_text("hello", encoding="utf-8")
    (d / ".gbrain" / "state.bin").write_bytes(b"x" * 10)
    for rel, content in extra_files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            p.mkdir(parents=True, exist_ok=True)
        elif isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    (d / "manifest.json").write_text(manifest_text, encoding="utf-8")
    (d / "READY").write_text(ready_content, encoding="utf-8")
    return {"ready_bytes": len(ready_content.encode("utf-8")), "manifest_bytes": len(manifest_text)}


def _vault_gen_count(facts: dict, extra_file_bytes: int = 0) -> tuple:
    """(files, bytes) for a default vault fixture: note.md(5) + state.bin(10)
    + manifest.json + READY."""
    return (4 + 0, 5 + 10 + facts["manifest_bytes"] + facts["ready_bytes"] + extra_file_bytes)


def _write_mnemosyne_gen(
    root: Path,
    gen_id: str,
    *,
    artifact_bytes: bytes = b"\x1f\x8b" + b"payload" * 2,
    manifest: Optional[dict] = None,
    ready_content: Optional[str] = None,
) -> dict:
    """Build a mnemosyne-style generation (artifact + manifest + READY
    binding). When ``ready_content`` is None it is derived from the manifest's
    artifact sha256 (or the artifact's real sha256 when no manifest is given).
    Returns fixture facts for expected-count computation."""
    if manifest is None:
        sha = hashlib.sha256(artifact_bytes).hexdigest()
        manifest = {
            "generation_id": gen_id,
            "artifact": {"name": "mnemosyne.db.gz", "sha256": sha},
        }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True)
    if ready_content is None:
        artifact_block = manifest.get("artifact")
        if isinstance(artifact_block, dict) and isinstance(artifact_block.get("sha256"), str):
            sha = artifact_block["sha256"]
        else:
            sha = hashlib.sha256(artifact_bytes).hexdigest()
        ready_content = f"{gen_id}\n{sha}\n"
    d = root / gen_id
    d.mkdir(parents=True)
    (d / "mnemosyne.db.gz").write_bytes(artifact_bytes)
    (d / "manifest.json").write_text(manifest_text, encoding="utf-8")
    (d / "READY").write_text(ready_content, encoding="utf-8")
    return {
        "artifact_bytes": len(artifact_bytes),
        "manifest_bytes": len(manifest_text),
        "ready_bytes": len(ready_content.encode("utf-8")),
    }


def _mnemosyne_gen_count(facts: dict) -> tuple:
    return (3, facts["artifact_bytes"] + facts["manifest_bytes"] + facts["ready_bytes"])


def _expected_snapshot(
    gen_id: str, ready: bool, manifest: bool, files: int, bytes_total: int
) -> dict:
    return {
        "generation_id": gen_id,
        "timestamp": (
            f"{gen_id[:4]}-{gen_id[4:6]}-{gen_id[6:8]}T"
            f"{gen_id[9:11]}:{gen_id[11:13]}:{gen_id[13:15]}.{gen_id[15:21]}Z"
        ),
        "local_ready_manifest_observation": {"ready": ready, "manifest": manifest},
        "total_regular_file_count": files,
        "total_regular_file_bytes": bytes_total,
    }


def _expected_result(lane: str, operation: str, snapshots: list, truncated: bool = False) -> dict:
    return {
        "schema_version": 1,
        "lane": lane,
        "operation": operation,
        "scope": "local_staging",
        "remote_status": "unknown_operator_only",
        "truncated": truncated,
        "snapshots": snapshots,
    }


def _expected_failure(code: str, lane=None, operation=None) -> dict:
    return {
        "schema_version": 1,
        "lane": lane,
        "operation": operation,
        "scope": "local_staging",
        "remote_status": "unknown_operator_only",
        "truncated": False,
        "ok": False,
        "error": {"code": code, "message": m.FAILURE_MESSAGES[code]},
    }


@contextlib.contextmanager
def _run_main(argv):
    """Call m.main(argv) capturing stdout; yields (exit_code, stdout_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = m.main(argv)
    yield code, buf.getvalue()


@contextlib.contextmanager
def _identity(euid, system_uid, hermes_uid_env=None, hermes_exists=True):
    """Mock the identity boundary: os.geteuid + pwd.getpwnam + HERMES_UID."""
    old_env = os.environ.pop("HERMES_UID", None)

    def fake_pwnam(name):
        if name == "hermes" and hermes_exists:
            return type("P", (), {"pw_uid": system_uid})()
        raise KeyError(name)

    try:
        if hermes_uid_env is not None:
            os.environ["HERMES_UID"] = hermes_uid_env
        with mock.patch.object(os, "geteuid", return_value=euid), mock.patch.object(
            pwd, "getpwnam", side_effect=fake_pwnam
        ):
            yield
    finally:
        if old_env is None:
            os.environ.pop("HERMES_UID", None)
        else:
            os.environ["HERMES_UID"] = old_env


@contextlib.contextmanager
def _staging(tmpdir: Path, lane: str = "vault"):
    """Point the fixed staging constants at a temp dir for the given lane."""
    attr = "VAULT_STAGING_ROOT" if lane == "vault" else "MNEMOSYNE_STAGING_ROOT"
    with mock.patch.object(m, attr, str(tmpdir)):
        yield


class _FakeStat:
    """Minimal stat_result stand-in for swap simulation (st_dev/st_ino/
    st_mode are all the reader consults)."""

    def __init__(self, st_dev, st_ino, st_mode):
        self.st_dev = st_dev
        self.st_ino = st_ino
        self.st_mode = st_mode


@contextlib.contextmanager
def _swap_stat_patch(name_hit: str):
    """Simulate a same-type TOCTOU swap: os.stat reports a DIFFERENT inode
    for the entry ``name_hit`` than the real object the subsequent open
    returns, so the inode/device identity verification must reject it."""
    real_stat = os.stat

    def fake_stat(name, dir_fd=None, follow_symlinks=True):
        st = real_stat(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if name == name_hit:
            return _FakeStat(st.st_dev, st.st_ino + 1, st.st_mode)
        return st

    with mock.patch.object(os, "stat", side_effect=fake_stat):
        yield


@contextlib.contextmanager
def _payload_swap_open_patch(name_hit: str, swap_fn):
    """Simulate a race in the ordinary payload-file counting path: when the
    reader performs the O_NOFOLLOW|O_NONBLOCK descriptor open of
    ``name_hit``, the entry is FIRST replaced by ``swap_fn``'s object, so
    the open sees a different object than the lstat baseline (same-type
    swap or regular-to-special) and must be rejected."""
    real_open = os.open

    def fake_open(name, flags, dir_fd=None, **kwargs):
        if name == name_hit and dir_fd is not None:
            swap_fn()
        return real_open(name, flags, dir_fd=dir_fd, **kwargs)

    with mock.patch.object(os, "open", side_effect=fake_open):
        yield


# ---------------------------------------------------------------------------
# CLI args: exact argv only, stable bounded usage failures
# ---------------------------------------------------------------------------


class CliArgsTests(unittest.TestCase):
    def _usage_cases(self):
        return [
            [],
            ["vault"],
            ["mnemosyne"],
            ["list"],
            ["latest"],
            ["vault", "list", "extra"],
            ["vault", "latest", "extra", "more"],
            ["x", "list"],
            ["vault", "x"],
            ["VAULT", "list"],
            ["vault", "LIST"],
            ["--help"],
            ["-h"],
            ["--", "vault", "list"],
            [BAD_GEN_ID, "list"],
            ["vault", BAD_GEN_ID],
            ["", ""],
        ]

    def test_bad_argv_returns_stable_usage_failure(self):
        for argv in self._usage_cases():
            with self.subTest(argv=argv):
                with _run_main(argv) as (code, out):
                    self.assertEqual(code, m.FAILURE_EXIT_CODES["usage"])
                    self.assertEqual(json.loads(out), _expected_failure("usage"))

    def test_usage_failure_is_deterministic(self):
        for argv in self._usage_cases():
            with self.subTest(argv=argv):
                with _run_main(argv) as (_, out1), _run_main(argv) as (_, out2):
                    self.assertEqual(out1, out2)

    def test_usage_failure_output_shape_is_bounded_and_fixed(self):
        with _run_main(["vault"]) as (code, out):
            obj = json.loads(out)
            self.assertEqual(list(obj.keys()), [
                "schema_version", "lane", "operation", "scope",
                "remote_status", "truncated", "ok", "error",
            ])
            self.assertEqual(list(obj["error"].keys()), ["code", "message"])
            self.assertIsNone(obj["lane"])
            self.assertIsNone(obj["operation"])
            self.assertEqual(obj["error"]["message"], m.FAILURE_MESSAGES["usage"])

    def test_good_argv_does_not_fail_usage(self):
        # With identity and staging satisfied, a good argv succeeds: usage
        # must not be hit for the exact two-argument form.
        with _identity(euid=4242, system_uid=4242):
            with _staging(Path(tempfile.mkdtemp(prefix="bs-args-"))):
                with _run_main(["vault", "list"]) as (code, out):
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(out)["snapshots"], [])
                    self.assertNotIn("error", json.loads(out))

    def test_main_rejects_non_list_argv(self):
        with _run_main("vault list") as (code, out):
            self.assertEqual(code, m.FAILURE_EXIT_CODES["usage"])


# ---------------------------------------------------------------------------
# Identity: root denied; euid must equal system hermes uid; env never
# authorizes alone
# ---------------------------------------------------------------------------


class IdentityTests(unittest.TestCase):
    def test_root_denied_even_with_matching_env(self):
        with _identity(euid=0, system_uid=4242, hermes_uid_env="4242"):
            with self.assertRaises(m._ReaderError) as ctx:
                m.check_identity()
            self.assertEqual(ctx.exception.code, "identity")

    def test_root_denied_without_env(self):
        with _identity(euid=0, system_uid=4242):
            with self.assertRaises(m._ReaderError):
                m.check_identity()

    def test_missing_system_hermes_user_fails_closed(self):
        # Even a "matching" env cannot authorize without the system user.
        with _identity(euid=4242, system_uid=4242, hermes_uid_env="4242", hermes_exists=False):
            with self.assertRaises(m._ReaderError) as ctx:
                m.check_identity()
            self.assertEqual(ctx.exception.code, "identity")

    def test_euid_mismatch_denied(self):
        with _identity(euid=9999, system_uid=4242):
            with self.assertRaises(m._ReaderError):
                m.check_identity()

    def test_euid_matching_allowed_without_env(self):
        with _identity(euid=4242, system_uid=4242):
            m.check_identity()  # must not raise

    def test_euid_matching_allowed_with_matching_env(self):
        with _identity(euid=4242, system_uid=4242, hermes_uid_env="4242"):
            m.check_identity()

    def test_env_never_authorizes_alone(self):
        # HERMES_UID equals the EUID but not the system hermes uid: denied.
        with _identity(euid=1000, system_uid=4242, hermes_uid_env="1000"):
            with self.assertRaises(m._ReaderError) as ctx:
                m.check_identity()
            self.assertEqual(ctx.exception.code, "identity")

    def test_env_mismatch_denied(self):
        with _identity(euid=4242, system_uid=4242, hermes_uid_env="10000"):
            with self.assertRaises(m._ReaderError):
                m.check_identity()

    def test_env_zero_denied(self):
        with _identity(euid=4242, system_uid=4242, hermes_uid_env="0"):
            with self.assertRaises(m._ReaderError):
                m.check_identity()

    def test_env_non_decimal_denied(self):
        for bad in ("abc", "-1", " 4242", "4242 ", "0x10", "42.0", "4,242", "٤٢٤٢", "+4242", "nan", "inf"):
            with self.subTest(hermes_uid=bad):
                with _identity(euid=4242, system_uid=4242, hermes_uid_env=bad):
                    with self.assertRaises(m._ReaderError) as ctx:
                        m.check_identity()
                    self.assertEqual(ctx.exception.code, "identity")

    def test_env_leading_zeros_allowed_when_value_matches(self):
        with _identity(euid=4242, system_uid=4242, hermes_uid_env="04242"):
            m.check_identity()

    def test_env_huge_number_denied(self):
        with _identity(euid=4242, system_uid=4242, hermes_uid_env="9" * 40):
            with self.assertRaises(m._ReaderError):
                m.check_identity()

    def test_system_hermes_uid_zero_fails_closed(self):
        with _identity(euid=0, system_uid=0):
            with self.assertRaises(m._ReaderError):
                m.check_identity()

    def test_identity_failure_emits_bounded_json_from_main(self):
        with _identity(euid=9999, system_uid=4242):
            with _staging(Path(tempfile.mkdtemp(prefix="bs-id-"))):
                with _run_main(["vault", "list"]) as (code, out):
                    self.assertEqual(code, m.FAILURE_EXIT_CODES["identity"])
                    self.assertEqual(json.loads(out), _expected_failure("identity", "vault", "list"))


# ---------------------------------------------------------------------------
# Env path poisoning: staging roots are fixed constants, never env-driven
# ---------------------------------------------------------------------------


class EnvPoisonTests(unittest.TestCase):
    POISON = {
        "VAULT_RECOVERY_STAGING_DIR": "/tmp/poison-vault",
        "MNEMOSYNE_BACKUP_STAGING_DIR": "/tmp/poison-mnemosyne",
        "MNEMOSYNE_DATA_DIR": "/tmp/poison-data",
        "HERMES_UID": "12345",
        "PYTHONPATH": "/tmp/poison-pythonpath",
        "TMPDIR": "/tmp/poison-tmp",
        "PATH": "/tmp/poison-path",
    }

    def test_read_status_ignores_poisoned_env(self):
        with tempfile.TemporaryDirectory(prefix="bs-poison-") as tmp:
            facts = _write_vault_gen(Path(tmp), GEN_ID)
            files, bytes_total = _vault_gen_count(facts)
            with mock.patch.dict(os.environ, self.POISON):
                result = m.read_status("vault", "list", staging_root=tmp)
            self.assertEqual(
                result,
                _expected_result(
                    "vault", "list",
                    [_expected_snapshot(GEN_ID, True, True, files, bytes_total)],
                ),
            )

    def test_main_uses_fixed_constants_not_env(self):
        with tempfile.TemporaryDirectory(prefix="bs-poison-") as tmp:
            _write_vault_gen(Path(tmp), GEN_ID)
            with mock.patch.dict(os.environ, self.POISON):
                with _identity(euid=4242, system_uid=4242):
                    with _staging(Path(tmp)):
                        with _run_main(["vault", "list"]) as (code, out):
                            self.assertEqual(code, 0)
                            self.assertEqual(json.loads(out)["snapshots"][0]["generation_id"], GEN_ID)

    def test_env_cannot_redirect_missing_lane(self):
        # Even with poisoned env, an unpatched constant resolves to the real
        # fixed /opt path, which is missing locally -> empty success, never
        # the poison path content.
        with mock.patch.dict(os.environ, self.POISON):
            with _identity(euid=4242, system_uid=4242):
                with mock.patch.object(m, "VAULT_STAGING_ROOT", "/nonexistent/bs-root"):
                    with _run_main(["vault", "list"]) as (code, out):
                        self.assertEqual(code, 0)
                        self.assertEqual(json.loads(out), _expected_result("vault", "list", []))


# ---------------------------------------------------------------------------
# No-follow descriptor-relative traversal: symlink/special/race-ish rejection
# ---------------------------------------------------------------------------


class NoFollowRejectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bs-nofollow-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _assert_rejected(self, lane="vault"):
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status(lane, "list", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_staging_root_is_symlink(self):
        real = self.root / "real"
        real.mkdir()
        (self.root / "link").symlink_to(real)
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "list", staging_root=str(self.root / "link"))
        self.assertEqual(ctx.exception.code, "staging")

    def test_staging_root_is_regular_file(self):
        f = self.root / "file"
        f.write_text("x")
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "list", staging_root=str(f))
        self.assertEqual(ctx.exception.code, "staging")

    def test_missing_root_is_empty_success(self):
        result = m.read_status("vault", "list", staging_root=str(self.root / "missing"))
        self.assertEqual(result, _expected_result("vault", "list", []))

    def test_empty_root_is_empty_success(self):
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(result, _expected_result("vault", "list", []))

    def test_generation_named_symlink_rejected(self):
        (self.root / GEN_ID).symlink_to(self.root)
        self._assert_rejected()

    def test_generation_named_regular_file_rejected(self):
        (self.root / GEN_ID).write_text("x")
        self._assert_rejected()

    def test_generation_named_fifo_rejected(self):
        os.mkfifo(self.root / GEN_ID)
        self._assert_rejected()

    def test_generation_named_socket_rejected(self):
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        s.bind(str(self.root / GEN_ID))
        self._assert_rejected()

    def test_symlink_inside_generation_rejected(self):
        d = self.root / GEN_ID
        d.mkdir()
        (d / "vault").symlink_to(self.root)
        self._assert_rejected()

    def test_nested_symlink_rejected(self):
        d = self.root / GEN_ID
        (d / "vault" / "deep").mkdir(parents=True)
        (d / "vault" / "deep" / "link").symlink_to("/etc")
        self._assert_rejected()

    def test_fifo_inside_generation_rejected(self):
        d = self.root / GEN_ID
        d.mkdir()
        os.mkfifo(d / "fifo")
        self._assert_rejected()

    def test_socket_inside_generation_rejected(self):
        d = self.root / GEN_ID
        d.mkdir()
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        s.bind(str(d / "sock"))
        self._assert_rejected()

    def test_ready_as_directory_rejected(self):
        d = self.root / GEN_ID
        d.mkdir()
        (d / "READY").mkdir()
        self._assert_rejected()

    def test_ready_as_symlink_rejected(self):
        d = self.root / GEN_ID
        d.mkdir()
        (d / "READY").symlink_to("/etc/passwd")
        self._assert_rejected()

    def test_manifest_as_symlink_rejected(self):
        d = self.root / GEN_ID
        d.mkdir()
        (d / "manifest.json").symlink_to("/etc/passwd")
        self._assert_rejected()

    def test_latest_pointer_symlink_rejected(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / "latest").symlink_to("manifest.json")
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_latest_pointer_directory_rejected(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / "latest").mkdir()
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_latest_dangling_pointer_invalid(self):
        (self.root / "latest").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "invalid")

    def test_latest_malformed_pointer_invalid(self):
        _write_vault_gen(self.root, GEN_ID)
        for content in ("", "nope\n", f"{GEN_ID}", f"{GEN_ID}\n\n", "x" * 300, f"{BAD_GEN_ID}\n"):
            with self.subTest(content=content[:20]):
                (self.root / "latest").write_text(content, encoding="utf-8")
                with self.assertRaises(m._ReaderError) as ctx:
                    m.read_status("vault", "latest", staging_root=str(self.root))
                self.assertEqual(ctx.exception.code, "invalid")

    def test_latest_pointer_oversized_invalid(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / "latest").write_bytes(b"a" * 300)
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "invalid")

    def test_latest_pointer_non_ascii_invalid(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / "latest").write_bytes(b"\xff\xfe\n")
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "invalid")

    def test_latest_pointer_fifo_rejected_immediately(self):
        # A FIFO at the `latest` position must be rejected WITHOUT blocking
        # (an O_RDONLY open of a FIFO with no writer would hang forever).
        _write_vault_gen(self.root, GEN_ID)
        os.mkfifo(self.root / "latest")
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_open_regular_fifo_rejected_immediately(self):
        # The open-time path (race window: entry lstatted as regular, then
        # swapped for a FIFO) must reject immediately via O_NONBLOCK instead
        # of blocking on the FIFO open.
        os.mkfifo(self.root / "fifo")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(m._ReaderError) as ctx:
            m._read_bounded("fifo", fd, 10)
        self.assertEqual(ctx.exception.code, "rejected")
        with self.assertRaises(m._ReaderError) as ctx:
            m._open_regular_no_follow("fifo", fd)
        self.assertEqual(ctx.exception.code, "rejected")

    def test_staging_root_intermediate_symlink_rejected(self):
        # O_NOFOLLOW on the final component alone would miss a symlinked
        # intermediate: the anchored descent must reject it.
        real = self.root / "real"
        real.mkdir()
        (real / "staging").mkdir()
        (self.root / "link").symlink_to(real)
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status(
                "vault", "list", staging_root=str(self.root / "link" / "staging")
            )
        self.assertEqual(ctx.exception.code, "staging")

    def test_staging_root_intermediate_swap_rejected(self):
        # Same-type TOCTOU: an intermediate component lstat-verified as a
        # directory whose inode differs from the opened object (simulated by
        # reporting a different inode at lstat time) must fail closed.
        (self.root / "staging").mkdir()
        with _swap_stat_patch("staging"):
            with self.assertRaises(m._ReaderError) as ctx:
                m.read_status("vault", "list", staging_root=str(self.root / "staging"))
        self.assertEqual(ctx.exception.code, "staging")

    def test_generation_dir_same_type_swap_rejected(self):
        # A generation dir replaced by another directory (same type, different
        # inode) between lstat and open must be rejected, not silently read.
        _write_vault_gen(self.root, GEN_ID)
        with _swap_stat_patch(GEN_ID):
            with self.assertRaises(m._ReaderError) as ctx:
                m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_latest_gen_dir_same_type_swap_rejected(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / "latest").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with _swap_stat_patch(GEN_ID):
            with self.assertRaises(m._ReaderError) as ctx:
                m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_latest_pointer_same_type_swap_rejected(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / "latest").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with _swap_stat_patch("latest"):
            with self.assertRaises(m._ReaderError) as ctx:
                m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_open_dir_identity_mismatch_rejected(self):
        (self.root / "sub").mkdir()
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(m._ReaderError) as ctx:
            m._open_dir_no_follow("sub", dir_fd=fd, expected=(0, 0))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_open_regular_identity_mismatch_rejected(self):
        (self.root / "reg").write_text("x")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(m._ReaderError) as ctx:
            m._open_regular_no_follow("reg", fd, expected=(0, 0))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_payload_same_type_swap_rejected(self):
        # Ordinary payload file replaced by ANOTHER regular file (different
        # inode) between lstat and open: the descriptor open + identity
        # verification must reject it, never count the swapped-in object.
        # The replacement is created BEFORE the original is unlinked and then
        # renamed over it, so both inodes coexist and cannot collide (plain
        # unlink+create could reuse the original inode).
        payload = self.root / GEN_ID / "vault" / "payload.bin"
        _write_vault_gen(self.root, GEN_ID, extra_files={"vault/payload.bin": "original"})

        def swap_to_other_file():
            replacement = self.root / "replacement.bin"
            replacement.write_text("swapped", encoding="utf-8")
            payload.unlink()
            os.replace(replacement, payload)

        with _payload_swap_open_patch("payload.bin", swap_to_other_file):
            with self.assertRaises(m._ReaderError) as ctx:
                m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_payload_regular_to_fifo_swap_rejected(self):
        # Ordinary payload file replaced by a FIFO between lstat and open:
        # the O_NONBLOCK open must not hang and the fstat type check must
        # reject it immediately.
        payload = self.root / GEN_ID / "vault" / "payload.bin"
        _write_vault_gen(self.root, GEN_ID, extra_files={"vault/payload.bin": "original"})

        def swap_to_fifo():
            payload.unlink()
            os.mkfifo(payload)

        with _payload_swap_open_patch("payload.bin", swap_to_fifo):
            with self.assertRaises(m._ReaderError) as ctx:
                m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_payload_regular_to_symlink_swap_rejected(self):
        payload = self.root / GEN_ID / "vault" / "payload.bin"
        _write_vault_gen(self.root, GEN_ID, extra_files={"vault/payload.bin": "original"})

        def swap_to_symlink():
            payload.unlink()
            payload.symlink_to("/etc/passwd")

        with _payload_swap_open_patch("payload.bin", swap_to_symlink):
            with self.assertRaises(m._ReaderError) as ctx:
                m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "rejected")

    def test_payload_files_are_descriptor_verified(self):
        # Every ordinary regular payload file (not just READY/manifest) must
        # go through the descriptor-relative nofollow/nonblock open +
        # identity verification before being counted.
        _write_vault_gen(
            self.root, GEN_ID,
            extra_files={"vault/payload.bin": "x", "deep/sub/data.bin": "y"},
        )
        opened = []
        real_open = m._open_regular_no_follow

        def recording(name, dir_fd, expected=None):
            opened.append(name)
            return real_open(name, dir_fd, expected=expected)

        with mock.patch.object(m, "_open_regular_no_follow", side_effect=recording):
            result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertFalse(result["truncated"])
        for name in ("READY", "manifest.json", "payload.bin", "data.bin", "note.md", "state.bin"):
            self.assertIn(name, opened)

    def test_non_generation_entries_never_opened(self):
        # Symlinks/specials with non-generation names at the root are outside
        # the data model: they must be ignored, never followed, never fatal.
        _write_vault_gen(self.root, GEN_ID)
        (self.root / "latest").symlink_to(GEN_ID)  # named `latest`, not a gen id
        (self.root / "latest.manifest.json").symlink_to(GEN_ID)
        os.mkfifo(self.root / "fifo")
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(len(result["snapshots"]), 1)
        self.assertEqual(result["snapshots"][0]["generation_id"], GEN_ID)

    def test_open_time_type_change_rejected(self):
        # Race-ish: the O_NOFOLLOW open path itself must reject a symlink at
        # the final component (ELOOP) even without a prior lstat.
        (self.root / "link").symlink_to("/etc")
        with self.assertRaises(m._ReaderError) as ctx:
            m._open_dir_no_follow("link", dir_fd=os.open(self.root, os.O_RDONLY | os.O_DIRECTORY))
        self.assertEqual(ctx.exception.code, "rejected")
        (self.root / "link").unlink()
        (self.root / "reg").write_text("x")
        (self.root / "link2").symlink_to("reg")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(m._ReaderError) as ctx:
            m._read_bounded("link2", fd, 10)
        self.assertEqual(ctx.exception.code, "rejected")


# ---------------------------------------------------------------------------
# Malformed / oversized / binding-invalid READY and manifest inputs
# ---------------------------------------------------------------------------


class InvalidInputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bs-input-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _vault_snapshot(self, gen_id=GEN_ID, ready_content=None, manifest=None, extra=None):
        facts = _write_vault_gen(
            self.root, gen_id,
            ready_content=ready_content, manifest=manifest, extra_files=extra,
        )
        return self._read_vault_snapshot(), facts

    def _read_vault_snapshot(self, gen_id=GEN_ID):
        """Read the single vault snapshot WITHOUT rewriting the fixture."""
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(len(result["snapshots"]), 1)
        self.assertEqual(result["snapshots"][0]["generation_id"], gen_id)
        return result["snapshots"][0]

    def test_malformed_manifest_json_manifest_false(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / GEN_ID / "manifest.json").write_text("{not json", encoding="utf-8")
        snap = self._read_vault_snapshot()
        self.assertFalse(snap["local_ready_manifest_observation"]["manifest"])
        self.assertTrue(snap["local_ready_manifest_observation"]["ready"])  # vault READY unaffected by manifest

    def test_manifest_not_an_object_manifest_false(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / GEN_ID / "manifest.json").write_text("[1,2,3]", encoding="utf-8")
        snap = self._read_vault_snapshot()
        self.assertFalse(snap["local_ready_manifest_observation"]["manifest"])

    def test_manifest_generation_id_mismatch_manifest_false(self):
        snap, _ = self._vault_snapshot(manifest={"generation_id": GEN_ID_NEWER})
        self.assertFalse(snap["local_ready_manifest_observation"]["manifest"])
        self.assertTrue(snap["local_ready_manifest_observation"]["ready"])

    def test_manifest_deeply_nested_json_manifest_false(self):
        _write_vault_gen(self.root, GEN_ID)
        bomb = "[" * 200000 + "]" * 200000
        (self.root / GEN_ID / "manifest.json").write_text(bomb, encoding="utf-8")
        snap = self._read_vault_snapshot()
        self.assertFalse(snap["local_ready_manifest_observation"]["manifest"])

    def test_manifest_oversized_manifest_false(self):
        pad = {"generation_id": GEN_ID, "pad": "x" * (m.MAX_MANIFEST_BYTES + 10)}
        snap, _ = self._vault_snapshot(manifest=pad)
        self.assertFalse(snap["local_ready_manifest_observation"]["manifest"])
        self.assertTrue(snap["local_ready_manifest_observation"]["ready"])  # vault READY is independent of the manifest

    def test_vault_ready_variants(self):
        for content in (
            None,  # missing READY
            GEN_ID,  # no newline
            f"{GEN_ID}\n\n",
            f"{GEN_ID}\njunk\n",
            f"{GEN_ID_OLDER}\n",  # wrong id
            f"{GEN_ID} \n",  # trailing space
            f" {GEN_ID}\n",  # leading space
            "\n",
            "",
        ):
            with self.subTest(ready=content):
                _write_vault_gen(self.root, GEN_ID)
                ready_path = self.root / GEN_ID / "READY"
                if content is None:
                    ready_path.unlink()
                else:
                    ready_path.write_text(content, encoding="utf-8")
                snap = self._read_vault_snapshot()
                self.assertFalse(snap["local_ready_manifest_observation"]["ready"])
                self.assertTrue(snap["local_ready_manifest_observation"]["manifest"])
                shutil.rmtree(self.root / GEN_ID, ignore_errors=True)

    def test_vault_ready_oversized_ready_false(self):
        _write_vault_gen(self.root, GEN_ID, ready_content=f"{GEN_ID}\n" + "x" * (m.MAX_READY_BYTES + 10))
        snap = self._read_vault_snapshot()
        self.assertFalse(snap["local_ready_manifest_observation"]["ready"])

    def test_vault_ready_exact_is_true(self):
        snap, _ = self._vault_snapshot()
        self.assertTrue(snap["local_ready_manifest_observation"]["ready"])
        self.assertTrue(snap["local_ready_manifest_observation"]["manifest"])

    def test_mnemosyne_ready_binding(self):
        artifact = b"\x1f\x8b" + b"payload" * 3
        good_sha = hashlib.sha256(artifact).hexdigest()
        good_manifest = {
            "generation_id": GEN_ID,
            "artifact": {"name": "mnemosyne.db.gz", "sha256": good_sha},
        }
        cases = [
            # (manifest, ready_content, expect_ready, expect_manifest)
            (None, None, True, True),  # auto-consistent
            ({"generation_id": GEN_ID, "artifact": {"name": "mnemosyne.db.gz", "sha256": "0" * 64}}, None, True, True),
            (good_manifest, f"{GEN_ID}\n{'0' * 64}\n", False, True),  # digest mismatch
            (good_manifest, f"{GEN_ID}\n{good_sha.upper()}\n", False, True),  # uppercase != lowercase hex
            (good_manifest, f"{GEN_ID}\n{'z' * 64}\n", False, True),  # not hex
            (good_manifest, f"{GEN_ID}\n{good_sha[:63]}g\n", False, True),  # 'g' not hex
            ({"generation_id": GEN_ID_NEWER, "artifact": {"name": "mnemosyne.db.gz", "sha256": good_sha}}, f"{GEN_ID}\n{good_sha}\n", False, False),  # manifest id mismatch
            ({"generation_id": GEN_ID, "artifact": {"name": "x", "sha256": 123}}, f"{GEN_ID}\n{good_sha}\n", False, True),  # sha not a string
            ({"generation_id": GEN_ID, "artifact": "notanobject"}, f"{GEN_ID}\n{good_sha}\n", False, True),
            ({"generation_id": GEN_ID}, f"{GEN_ID}\n{good_sha}\n", False, True),  # no artifact block
            ({"generation_id": GEN_ID, "artifact": {"name": "mnemosyne.db.gz", "sha256": good_sha[:63]}}, f"{GEN_ID}\n{good_sha}\n", False, True),  # truncated sha
            (good_manifest, GEN_ID, False, True),  # no trailing newline
            (good_manifest, f"{GEN_ID}\n{good_sha}", False, True),  # missing final newline
            (good_manifest, f"{GEN_ID}\n{good_sha}\n\n", False, True),  # extra newline
            (good_manifest, f"{GEN_ID}\n", False, True),  # missing sha line
            (None, f"{GEN_ID}\n{'0' * 64}\n", False, True),  # READY sha vs real artifact sha mismatch
            (good_manifest, f"{GEN_ID}\n{good_sha}\n" + "y" * 300, False, True),  # oversized READY
        ]
        for idx, (manifest, ready_content, expect_ready, expect_manifest) in enumerate(cases):
            with self.subTest(case=idx):
                try:
                    _write_mnemosyne_gen(
                        self.root, GEN_ID, artifact_bytes=artifact,
                        manifest=manifest, ready_content=ready_content,
                    )
                    result = m.read_status("mnemosyne", "list", staging_root=str(self.root))
                    snap = result["snapshots"][0]
                    self.assertEqual(snap["local_ready_manifest_observation"]["ready"], expect_ready)
                    self.assertEqual(snap["local_ready_manifest_observation"]["manifest"], expect_manifest)
                finally:
                    shutil.rmtree(self.root / GEN_ID, ignore_errors=True)

    def test_mnemosyne_ready_missing_manifest_false(self):
        d = self.root / GEN_ID
        d.mkdir()
        (d / "mnemosyne.db.gz").write_bytes(b"data")
        sha = hashlib.sha256(b"data").hexdigest()
        (d / "READY").write_text(f"{GEN_ID}\n{sha}\n", encoding="utf-8")
        result = m.read_status("mnemosyne", "list", staging_root=str(self.root))
        snap = result["snapshots"][0]
        self.assertFalse(snap["local_ready_manifest_observation"]["ready"])
        self.assertFalse(snap["local_ready_manifest_observation"]["manifest"])

    def test_mnemosyne_ready_oversized_false(self):
        artifact = b"data"
        sha = hashlib.sha256(artifact).hexdigest()
        _write_mnemosyne_gen(
            self.root, GEN_ID,
            artifact_bytes=artifact,
            ready_content=f"{GEN_ID}\n{sha}\n" + "y" * (m.MAX_READY_BYTES + 10),
        )
        result = m.read_status("mnemosyne", "list", staging_root=str(self.root))
        self.assertFalse(result["snapshots"][0]["local_ready_manifest_observation"]["ready"])

    def test_mnemosyne_ready_non_ascii_false(self):
        # Non-ASCII sha line must be an observation (ready false), never a
        # crash and never an internal-error leak.
        _write_mnemosyne_gen(
            self.root, GEN_ID,
            ready_content=f"{GEN_ID}\n{chr(0x1F600) * 64}\n",
        )
        result = m.read_status("mnemosyne", "list", staging_root=str(self.root))
        snap = result["snapshots"][0]
        self.assertFalse(snap["local_ready_manifest_observation"]["ready"])
        self.assertTrue(snap["local_ready_manifest_observation"]["manifest"])


# ---------------------------------------------------------------------------
# Lane READY rules + digest: happy paths and exactness
# ---------------------------------------------------------------------------


class LaneReadyRulesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bs-ready-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_vault_reads_generation_fully(self):
        facts = _write_vault_gen(self.root, GEN_ID)
        files, bytes_total = _vault_gen_count(facts)
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(
            result,
            _expected_result("vault", "list", [_expected_snapshot(GEN_ID, True, True, files, bytes_total)]),
        )

    def test_mnemosyne_reads_generation_fully(self):
        facts = _write_mnemosyne_gen(self.root, GEN_ID)
        files, bytes_total = _mnemosyne_gen_count(facts)
        result = m.read_status("mnemosyne", "list", staging_root=str(self.root))
        self.assertEqual(
            result,
            _expected_result("mnemosyne", "list", [_expected_snapshot(GEN_ID, True, True, files, bytes_total)]),
        )

    def test_snapshots_sorted_newest_first(self):
        _write_vault_gen(self.root, GEN_ID)
        _write_vault_gen(self.root, GEN_ID_OLDER)
        _write_vault_gen(self.root, GEN_ID_NEWER)
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(
            [s["generation_id"] for s in result["snapshots"]],
            [GEN_ID_NEWER, GEN_ID, GEN_ID_OLDER],
        )
        self.assertFalse(result["truncated"])

    def test_latest_returns_pointed_snapshot(self):
        facts = _write_vault_gen(self.root, GEN_ID)
        _write_vault_gen(self.root, GEN_ID_NEWER)
        (self.root / "latest").write_text(f"{GEN_ID}\n", encoding="utf-8")
        files, bytes_total = _vault_gen_count(facts)
        result = m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(
            result,
            _expected_result("vault", "latest", [_expected_snapshot(GEN_ID, True, True, files, bytes_total)]),
        )

    def test_latest_missing_pointer_empty_success(self):
        result = m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(result, _expected_result("vault", "latest", []))

    def test_latest_reports_invalid_ready_as_observation(self):
        _write_vault_gen(self.root, GEN_ID, ready_content="junk")
        (self.root / "latest").write_text(f"{GEN_ID}\n", encoding="utf-8")
        result = m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(len(result["snapshots"]), 1)
        self.assertFalse(result["snapshots"][0]["local_ready_manifest_observation"]["ready"])

    def test_mnemosyne_latest_binds_digest(self):
        facts = _write_mnemosyne_gen(self.root, GEN_ID)
        (self.root / "latest").write_text(f"{GEN_ID}\n", encoding="utf-8")
        files, bytes_total = _mnemosyne_gen_count(facts)
        result = m.read_status("mnemosyne", "latest", staging_root=str(self.root))
        self.assertEqual(
            result,
            _expected_result("mnemosyne", "latest", [_expected_snapshot(GEN_ID, True, True, files, bytes_total)]),
        )

    def test_timestamp_derived_from_id(self):
        snap = _expected_snapshot(GEN_ID, True, True, 0, 0)
        self.assertEqual(snap["timestamp"], "2026-08-02T01:22:47.123456Z")
        self.assertEqual(m._timestamp_from_id(GEN_ID), "2026-08-02T01:22:47.123456Z")

    def test_is_valid_generation_id_rejects_poison(self):
        for bad in (
            "", "x" * 31, GEN_ID[:-1], GEN_ID + "0",
            "20260802T012247123456Z-a1b2c3d4/..", "/etc/passwd",
            "2026-13-99T99:99:99.999999Z-00000000", "20260802t012247123456z-a1b2c3d4",
            "20260802T012247123456Z-A1B2C3D4", GEN_ID.replace("-", ":"),
        ):
            self.assertFalse(m.is_valid_generation_id(bad))
        self.assertTrue(m.is_valid_generation_id(GEN_ID))

    def test_is_valid_generation_id_rejects_impossible_calendar(self):
        # Shape-valid ids whose embedded timestamp is NOT a real UTC
        # calendar/time value must be rejected before any timestamp claim.
        impossible = (
            "20261301T000000000000Z-00000000",  # month 13
            "20260001T000000000000Z-00000000",  # month 00
            "20260832T000000000000Z-00000000",  # day 32
            "20260800T000000000000Z-00000000",  # day 00
            "20260802T240000000000Z-00000000",  # hour 24
            "20260802T006000000000Z-00000000",  # minute 60
            "20260802T000060000000Z-00000000",  # second 60
            "20260230T000000000000Z-00000000",  # Feb 30
            "20230431T000000000000Z-00000000",  # Apr 31
            "20230229T000000000000Z-00000000",  # Feb 29 non-leap
            "19000229T000000000000Z-00000000",  # 1900 not divisible by 400
            "21000229T000000000000Z-00000000",  # 2100 not divisible by 400
            "00000101T000000000000Z-00000000",  # year 0000 outside datetime range
        )
        for bad in impossible:
            with self.subTest(gen_id=bad):
                self.assertFalse(m.is_valid_generation_id(bad))

    def test_is_valid_generation_id_accepts_real_calendar(self):
        valid = (
            GEN_ID,
            "20240229T000000000000Z-00000000",  # leap day
            "20000229T235959999999Z-00000000",  # 2000 divisible by 400
            "19991231T235959000000Z-00000000",  # century boundary
            "20260801T000000000000Z-00000000",
            "99991231T235959999999Z-00000000",  # datetime max
            "20230228T000000000000Z-00000000",  # non-leap Feb 28
        )
        for good in valid:
            with self.subTest(gen_id=good):
                self.assertTrue(m.is_valid_generation_id(good))

    def test_impossible_date_dirs_excluded_from_list(self):
        # A generation directory with an impossible embedded date is NOT a
        # valid candidate: excluded from the snapshot list, never read.
        _write_vault_gen(self.root, GEN_ID)
        _write_vault_gen(self.root, "20261301T000000000000Z-00000000")
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual([s["generation_id"] for s in result["snapshots"]], [GEN_ID])

    def test_impossible_date_latest_pointer_invalid(self):
        _write_vault_gen(self.root, GEN_ID)
        (self.root / "latest").write_text("20261301T000000000000Z-00000000\n", encoding="utf-8")
        with self.assertRaises(m._ReaderError) as ctx:
            m.read_status("vault", "latest", staging_root=str(self.root))
        self.assertEqual(ctx.exception.code, "invalid")

    def test_leap_day_generation_reads_fully(self):
        leap = "20240229T000000000000Z-00000000"
        facts = _write_vault_gen(self.root, leap)
        files, bytes_total = _vault_gen_count(facts)
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(
            result,
            _expected_result(
                "vault", "list",
                [_expected_snapshot(leap, True, True, files, bytes_total)],
            ),
        )
        self.assertEqual(result["snapshots"][0]["timestamp"], "2024-02-29T00:00:00.000000Z")


# ---------------------------------------------------------------------------
# Caps and determinism
# ---------------------------------------------------------------------------


class CapsAndDeterminismTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bs-caps-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_depth_cap_truncates(self):
        d = self.root / GEN_ID
        (d / "vault" / "a" / "b" / "c").mkdir(parents=True)
        (d / "vault" / "a" / "b" / "c" / "f").write_text("x")
        (d / "READY").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with mock.patch.object(m, "MAX_DEPTH", 2):
            result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["snapshots"]), 1)
        self.assertEqual(result["snapshots"][0]["generation_id"], GEN_ID)

    def test_files_cap_truncates(self):
        d = self.root / GEN_ID
        (d / "vault").mkdir(parents=True)
        for i in range(5):
            (d / "vault" / f"f{i}").write_text("x")
        (d / "READY").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with mock.patch.object(m, "MAX_FILES", 3):
            result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertTrue(result["truncated"])

    def test_dirs_cap_truncates(self):
        d = self.root / GEN_ID
        for sub in ("a", "b", "c"):
            (d / "vault" / sub).mkdir(parents=True)
        (d / "READY").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with mock.patch.object(m, "MAX_DIRS", 2):
            result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertTrue(result["truncated"])

    def test_bytes_cap_truncates_with_sparse_file(self):
        # Default cap (512 GiB) exceeded by a 1 TiB sparse file: no disk cost.
        d = self.root / GEN_ID
        d.mkdir()
        with open(d / "big", "wb") as fh:
            fh.truncate(1 << 40)
        (d / "READY").write_text(f"{GEN_ID}\n", encoding="utf-8")
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertTrue(result["truncated"])

    def test_bytes_cap_small_patch_truncates(self):
        d = self.root / GEN_ID
        d.mkdir()
        (d / "f").write_bytes(b"x" * 20)
        (d / "READY").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with mock.patch.object(m, "MAX_BYTES", 10):
            result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertTrue(result["truncated"])

    def test_snapshots_cap_truncates_and_keeps_newest(self):
        ids = [GEN_ID_OLDER, GEN_ID, GEN_ID_NEWER, "20260803T000000000000Z-11111111"]
        for gen_id in ids:
            _write_vault_gen(self.root, gen_id)
        with mock.patch.object(m, "MAX_SNAPSHOTS", 2):
            result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertTrue(result["truncated"])
        self.assertEqual([s["generation_id"] for s in result["snapshots"]], sorted(ids, reverse=True)[:2])

    def test_dir_entry_cap_truncates_promptly_and_deterministically(self):
        # A directory with far more entries than the cap (including many
        # invalid names) must terminate promptly: enumeration is buffered in
        # bounded slices and stopped at the cap, never materialized whole.
        d = self.root / GEN_ID
        d.mkdir()
        for i in range(60):
            (d / f"junk{i:04d}").write_text("x")
        (d / "manifest.json").write_text(
            json.dumps({"generation_id": GEN_ID}), encoding="utf-8"
        )
        (d / "READY").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with mock.patch.object(m, "MAX_DIR_ENTRIES", 5):
            r1 = m.read_status("vault", "list", staging_root=str(self.root))
            r2 = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertTrue(r1["truncated"])
        self.assertEqual(
            json.dumps(r1, separators=(",", ":")),
            json.dumps(r2, separators=(",", ":")),
        )

    def test_root_entry_cap_truncates_with_many_invalid_names(self):
        # The staging root itself is enumerated in bounded slices too: many
        # invalid entries cannot force unbounded materialization, and invalid
        # entries (including symlinks) are never opened or followed.
        _write_vault_gen(self.root, GEN_ID)
        for i in range(40):
            (self.root / f"junk{i:04d}").write_text("x")
        for i in range(40, 45):
            (self.root / f"junk{i:04d}").symlink_to("/etc/passwd")
        with mock.patch.object(m, "MAX_DIR_ENTRIES", 8):
            result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertTrue(result["truncated"])
        # No error: the buffered slice is processed, invalid names skipped
        # without opening, and the overflow only flags truncation.
        self.assertLessEqual(len(result["snapshots"]), 1)

    def test_entry_cap_with_default_bounds_normal_dirs(self):
        # Sanity: the default per-directory cap is far above any realistic
        # staging directory, so a normal generation is not truncated by the
        # enumeration cap alone.
        facts = _write_vault_gen(self.root, GEN_ID)
        files, bytes_total = _vault_gen_count(facts)
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertFalse(result["truncated"])
        self.assertEqual(
            result["snapshots"][0]["total_regular_file_count"], files
        )
        self.assertEqual(
            result["snapshots"][0]["total_regular_file_bytes"], bytes_total
        )

    def test_oversized_marker_is_observation_not_truncation(self):
        _write_vault_gen(self.root, GEN_ID, ready_content=f"{GEN_ID}\n" + "x" * (m.MAX_READY_BYTES + 10))
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertFalse(result["truncated"])
        self.assertFalse(result["snapshots"][0]["local_ready_manifest_observation"]["ready"])

    def test_determinism_byte_exact(self):
        _write_vault_gen(self.root, GEN_ID)
        _write_vault_gen(self.root, GEN_ID_NEWER)
        _write_mnemosyne_gen(self.root, GEN_ID_OLDER)
        expected = {
            "vault": m.read_status("vault", "list", staging_root=str(self.root)),
            "mnemosyne": m.read_status("mnemosyne", "list", staging_root=str(self.root)),
        }
        for lane in ("vault", "mnemosyne"):
            with self.subTest(lane=lane):
                again = m.read_status(lane, "list", staging_root=str(self.root))
                self.assertEqual(json.dumps(again, separators=(",", ":")), json.dumps(expected[lane], separators=(",", ":")))

    def test_determinism_under_truncation(self):
        d = self.root / GEN_ID
        (d / "vault" / "a" / "b" / "c").mkdir(parents=True)
        (d / "vault" / "a" / "b" / "c" / "f").write_text("x")
        (d / "READY").write_text(f"{GEN_ID}\n", encoding="utf-8")
        with mock.patch.object(m, "MAX_DEPTH", 2):
            r1 = m.read_status("vault", "list", staging_root=str(self.root))
            r2 = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(json.dumps(r1, separators=(",", ":")), json.dumps(r2, separators=(",", ":")))

    def test_output_schema_fixed_key_order(self):
        _write_vault_gen(self.root, GEN_ID)
        result = m.read_status("vault", "list", staging_root=str(self.root))
        self.assertEqual(list(result.keys()), [
            "schema_version", "lane", "operation", "scope",
            "remote_status", "truncated", "snapshots",
        ])
        self.assertEqual(list(result["snapshots"][0].keys()), [
            "generation_id", "timestamp", "local_ready_manifest_observation",
            "total_regular_file_count", "total_regular_file_bytes",
        ])
        self.assertEqual(list(result["snapshots"][0]["local_ready_manifest_observation"].keys()), ["ready", "manifest"])

    def test_read_status_rejects_unknown_lane(self):
        with self.assertRaises(ValueError):
            m.read_status("bogus", "list", staging_root=str(self.root))
        with self.assertRaises(ValueError):
            m.read_status("vault", "bogus", staging_root=str(self.root))


# ---------------------------------------------------------------------------
# Strict no-leak failure JSON
# ---------------------------------------------------------------------------


class FailureOutputTests(unittest.TestCase):
    def test_internal_error_never_leaks_exception_text(self):
        with tempfile.TemporaryDirectory(prefix="bs-internal-") as tmp:
            secret = f"secret-path-{tmp}/leak"
            with mock.patch.object(
                m, "_open_staging_root", side_effect=RuntimeError(secret)
            ):
                with _identity(euid=4242, system_uid=4242):
                    with _run_main(["vault", "list"]) as (code, out):
                        self.assertEqual(code, m.FAILURE_EXIT_CODES["internal"])
                        self.assertEqual(json.loads(out), _expected_failure("internal", "vault", "list"))
                        self.assertNotIn(secret, out)
                        self.assertNotIn("Traceback", out)

    def test_reader_error_messages_are_fixed(self):
        for code, message in m.FAILURE_MESSAGES.items():
            self.assertIn(code, m.FAILURE_EXIT_CODES)
            self.assertNotIn("/", message)  # no path can appear in a fixed message
            self.assertNotIn("{", message)  # no formatting placeholders

    def test_failure_json_is_bounded_for_every_code(self):
        for code in m.FAILURE_EXIT_CODES:
            with self.subTest(code=code):
                failure = m._failure(code, "vault", "list")
                text = json.dumps(failure, separators=(",", ":"))
                self.assertLess(len(text), 1024)
                self.assertEqual(json.loads(text), _expected_failure(code, "vault", "list"))


# ---------------------------------------------------------------------------
# No subprocess / shell / network / Docker / rclone / PGLite / locks
# ---------------------------------------------------------------------------


class NoSubprocessTests(unittest.TestCase):
    ALLOWED_IMPORTS = {"__future__", "datetime", "json", "os", "pwd", "re", "stat", "sys", "typing"}

    def test_imports_are_stdlib_allowlist_only(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
        self.assertLessEqual(imports, self.ALLOWED_IMPORTS)

    def test_no_shell_or_network_calls_in_source(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("system", "popen", "call", "run"):
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    self.fail(f"os.{node.attr} used in module source")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
                self.fail("dynamic __import__ used in module source")
        # Token scan over the CODE only (the module docstring is allowed to
        # name the banned facilities it refuses to use). Strip the leading
        # docstring by source lines (AST-docstring text does not round-trip
        # through the file's escape sequences).
        first = tree.body[0]
        code_only = source
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines = source.splitlines(keepends=True)
            del lines[first.lineno - 1 : first.end_lineno]
            code_only = "".join(lines)
        for forbidden in ("subprocess", "socket.", "sqlite3", "fcntl", "flock", "rclone"):
            self.assertNotIn(forbidden, code_only)

    def test_fresh_interpreter_imports_no_forbidden_modules(self):
        probe = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('josemar_backup_status', %r)\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "print('subprocess' in sys.modules, 'socket' in sys.modules, "
            "'sqlite3' in sys.modules, 'fcntl' in sys.modules)\n"
            % str(MODULE_PATH)
        )
        proc = subprocess.run(
            [sys.executable, "-I", "-c", probe],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False False False False")
        self.assertEqual(proc.stderr, "")


# ---------------------------------------------------------------------------
# Docker install wiring
# ---------------------------------------------------------------------------


class DockerInstallTests(unittest.TestCase):
    def test_dockerfile_copies_and_installs_script(self):
        self.assertTrue(MODULE_PATH.exists())
        text = DOCKERFILE.read_text(encoding="utf-8")
        # 1. COPY into /opt/josemar/scripts
        self.assertIn(
            "COPY scripts/josemar-backup-status.py /opt/josemar/scripts/josemar-backup-status.py",
            text,
        )
        # 2. chmod +x (executable install)
        self.assertIn("/opt/josemar/scripts/josemar-backup-status.py \\", text)
        # 3. compileall validation of the installed copy
        self.assertIn("/opt/josemar/scripts/josemar-backup-status.py", text)
        # 4. bare PATH name via symlink (canonical copy stays in scripts/)
        self.assertIn(
            "ln -s /opt/josemar/scripts/josemar-backup-status.py "
            "/usr/local/bin/josemar-backup-status",
            text,
        )
        # COPY, chmod, compileall, and symlink: exactly one occurrence each.
        self.assertEqual(text.count("/opt/josemar/scripts/josemar-backup-status.py"), 4)

    def test_dockerfile_bakes_backup_operations_skill(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        # Repo-owned skill baked into the image like every other repo skill.
        self.assertIn(
            "COPY skills-factory/backup-operations /opt/josemar/skills/backup-operations",
            text,
        )
        # Other repo-owned skills remain baked in.
        for skill in ("aux-ml", "workspace-sync", "gbrain", "tasknotes", "browser-control"):
            with self.subTest(skill=skill):
                self.assertIn(
                    f"COPY skills-factory/{skill} /opt/josemar/skills/{skill}",
                    text,
                )


# ---------------------------------------------------------------------------
# Real CLI end-to-end (subprocess with identity/staging stubbed via a
# wrapper; the script itself never spawns anything)
# ---------------------------------------------------------------------------


def _real_hermes_uid():
    try:
        return pwd.getpwnam("hermes").pw_uid
    except KeyError:
        return None


CLI_WRAPPER = (
    "import importlib.util, os, pwd, sys\n"
    "spec = importlib.util.spec_from_file_location('josemar_backup_status', os.environ['BS_MODULE'])\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(m)\n"
    "m.VAULT_STAGING_ROOT = os.environ['BS_VAULT']\n"
    "m.MNEMOSYNE_STAGING_ROOT = os.environ['BS_MNEMOSYNE']\n"
    "os.geteuid = lambda: 4242\n"
    "class _P: pw_uid = 4242\n"
    "pwd.getpwnam = lambda name: _P()\n"
    "sys.exit(m.main(sys.argv[1:]))\n"
)


def _run_cli(args, env=None, wrapper=True):
    if wrapper:
        cmd = [sys.executable, "-I", "-c", CLI_WRAPPER] + args
    else:
        cmd = [sys.executable, "-I", str(MODULE_PATH)] + args
    full_env = {"PATH": os.environ.get("PATH", "")}
    if env:
        full_env.update(env)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=full_env)
    return proc


class CliSubprocessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bs-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name) / "vault-staging"
        self.mnemosyne = Path(self._tmp.name) / "mnemosyne-staging"
        self.vault.mkdir()
        self.mnemosyne.mkdir()

    def _cli_env(self, extra=None):
        env = {
            "BS_MODULE": str(MODULE_PATH),
            "BS_VAULT": str(self.vault),
            "BS_MNEMOSYNE": str(self.mnemosyne),
        }
        if extra:
            env.update(extra)
        return env

    def test_cli_usage_failure_bounded_json_no_stderr(self):
        for args in ([], ["vault"], ["vault", "list", "x"], ["bogus", "list"], ["vault", "bogus"], ["--help"]):
            with self.subTest(args=args):
                proc = _run_cli(args, wrapper=False)
                self.assertEqual(proc.returncode, m.FAILURE_EXIT_CODES["usage"])
                self.assertEqual(proc.stderr, "")
                self.assertEqual(json.loads(proc.stdout), _expected_failure("usage"))

    def test_cli_identity_failure_bounded_json_no_stderr(self):
        if _real_hermes_uid() == os.geteuid():
            self.skipTest("running as the system hermes user; identity cannot fail")
        proc = _run_cli(["vault", "list"], wrapper=False)
        self.assertEqual(proc.returncode, m.FAILURE_EXIT_CODES["identity"])
        self.assertEqual(proc.stderr, "")
        obj = json.loads(proc.stdout)
        self.assertEqual(obj, _expected_failure("identity", "vault", "list"))
        self.assertNotIn("Traceback", proc.stdout)
        self.assertNotIn("/opt/", proc.stdout)
        self.assertNotIn(str(MODULE_PATH), proc.stdout)

    def test_cli_env_mismatch_identity_failure(self):
        proc = _run_cli(["vault", "list"], wrapper=False, env={"HERMES_UID": "99999"})
        self.assertEqual(proc.returncode, m.FAILURE_EXIT_CODES["identity"])
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "identity")

    def test_cli_vault_list_happy_path(self):
        facts = _write_vault_gen(self.vault, GEN_ID)
        _write_vault_gen(self.vault, GEN_ID_NEWER)
        files_newer, bytes_newer = _vault_gen_count(facts)
        files, bytes_total = _vault_gen_count(facts)
        proc = _run_cli(["vault", "list"], env=self._cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stderr, "")
        obj = json.loads(proc.stdout)
        self.assertEqual(
            obj,
            _expected_result("vault", "list", [
                _expected_snapshot(GEN_ID_NEWER, True, True, files_newer, bytes_newer),
                _expected_snapshot(GEN_ID, True, True, files, bytes_total),
            ]),
        )

    def test_cli_mnemosyne_latest_happy_path(self):
        facts = _write_mnemosyne_gen(self.mnemosyne, GEN_ID)
        (self.mnemosyne / "latest").write_text(f"{GEN_ID}\n", encoding="utf-8")
        files, bytes_total = _mnemosyne_gen_count(facts)
        proc = _run_cli(["mnemosyne", "latest"], env=self._cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(
            json.loads(proc.stdout),
            _expected_result("mnemosyne", "latest", [
                _expected_snapshot(GEN_ID, True, True, files, bytes_total),
            ]),
        )

    def test_cli_mnemosyne_digest_mismatch_failure_is_observation(self):
        _write_mnemosyne_gen(self.mnemosyne, GEN_ID, ready_content=f"{GEN_ID}\n{'0' * 64}\n")
        (self.mnemosyne / "latest").write_text(f"{GEN_ID}\n", encoding="utf-8")
        proc = _run_cli(["mnemosyne", "latest"], env=self._cli_env())
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(json.loads(proc.stdout)["snapshots"][0]["local_ready_manifest_observation"]["ready"])

    def test_cli_empty_lane_success(self):
        proc = _run_cli(["vault", "list"], env=self._cli_env())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), _expected_result("vault", "list", []))
        proc = _run_cli(["vault", "latest"], env=self._cli_env())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), _expected_result("vault", "latest", []))

    def test_cli_poisoned_env_cannot_redirect(self):
        # Poison every path-like env var: the CLI must still read the fixed
        # (wrapper-patched) constants and report the real fixture.
        facts = _write_vault_gen(self.vault, GEN_ID)
        files, bytes_total = _vault_gen_count(facts)
        poison = {
            "VAULT_RECOVERY_STAGING_DIR": "/tmp/poison-vault",
            "MNEMOSYNE_BACKUP_STAGING_DIR": "/tmp/poison-mnemosyne",
            "MNEMOSYNE_DATA_DIR": "/tmp/poison-data",
            "PYTHONPATH": "/tmp/poison-pythonpath",
            "TMPDIR": "/tmp/poison-tmp",
            "HOME": "/tmp/poison-home",
            "HERMES_UID": "4242",
        }
        proc = _run_cli(["vault", "list"], env=self._cli_env(extra=poison))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(
            json.loads(proc.stdout),
            _expected_result("vault", "list", [
                _expected_snapshot(GEN_ID, True, True, files, bytes_total),
            ]),
        )

    def test_cli_rejected_symlink_bounded_failure(self):
        (self.vault / GEN_ID).symlink_to(self.vault)
        proc = _run_cli(["vault", "list"], env=self._cli_env())
        self.assertEqual(proc.returncode, m.FAILURE_EXIT_CODES["rejected"])
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), _expected_failure("rejected", "vault", "list"))
        self.assertNotIn("Traceback", proc.stdout)
        self.assertNotIn(str(self.vault), proc.stdout)

    def test_cli_latest_fifo_rejected_bounded_failure_no_hang(self):
        # A FIFO at the `latest` position must fail promptly with the fixed
        # bounded failure JSON (an O_RDONLY FIFO open would hang forever).
        os.mkfifo(self.vault / "latest")
        proc = _run_cli(["vault", "latest"], env=self._cli_env())
        self.assertEqual(proc.returncode, m.FAILURE_EXIT_CODES["rejected"])
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), _expected_failure("rejected", "vault", "latest"))
        self.assertNotIn("Traceback", proc.stdout)
        self.assertNotIn(str(self.vault), proc.stdout)

    def test_cli_missing_staging_success(self):
        env = self._cli_env()
        env["BS_VAULT"] = str(self.vault / "absent")
        proc = _run_cli(["vault", "list"], env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), _expected_result("vault", "list", []))

    def test_cli_output_is_deterministic(self):
        _write_vault_gen(self.vault, GEN_ID)
        _write_vault_gen(self.vault, GEN_ID_NEWER)
        env = self._cli_env()
        p1 = _run_cli(["vault", "list"], env=env)
        p2 = _run_cli(["vault", "list"], env=env)
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p1.stdout, p2.stdout)


if __name__ == "__main__":
    unittest.main()
