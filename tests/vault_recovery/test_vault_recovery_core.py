"""Unit/contract tests for the vault-recovery export core (Phase 1).

Covers the exporter core logic WITHOUT Docker: generation ids, the no-follow
directory-fd/openat-style tree scan/copy (root/intermediate symlink and
component-race rejection, modes and empty dirs, root modes, read-only dirs,
scan-error fail-closed), fsync durability boundaries (file/dir fsync
failures never publish a generation; source file modes are applied before
the final content fsync; a failed first-generation `latest` publication
leaves no dangling pointer, while later publications restore the prior
pointer), convergence semantics (scan A ==
scan B == staged, bounded retry, fail without READY), Hermes-identity
enforcement (root and arbitrary uids rejected at the core CLI boundary), the
pinned doctor preflight contract, active-PGLite indicator rejection (scan
errors fail closed), shared-lock enforcement, the staging generation layout
(<gen>/vault, <gen>/.gbrain, manifest.json, READY, atomic latest), and the
manifest schema.

The production paths are module constants; every test exercises the public
keyword seams (gbrain_bin, lock_path, schema_pack_file) so no production
path is touched. The shared-lock requirement is tested with a REAL flock
held on a temp lock file (same validation the tasknotes lock runner relies
on: TASKNOTES_LOCK_FD + /proc/self/fdinfo FLOCK WRITE).
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _import_core():
    """Import the exporter core from source (repo convention: no package
    install required, no reliance on sys.path)."""
    core_path = Path(__file__).resolve().parents[2] / "scripts" / "vault_recovery_core.py"
    spec = importlib.util.spec_from_file_location("vault_recovery_core", str(core_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _import_core()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_tree(root: Path, spec: dict) -> None:
    """Build a tree from a {relpath: content|None(dir)|("mode", content)} spec."""
    for rel, value in spec.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if value is None:
            path.mkdir(parents=True, exist_ok=True)
            continue
        mode, content = value if isinstance(value, tuple) else (0o644, value)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)


def _fake_gbrain_bin(doctor_json: dict, exit_code: int = 0) -> str:
    """Create an executable fake gbrain binary printing ``doctor_json``."""
    script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"print(json.dumps({json.dumps(doctor_json)}))\n"
        f"sys.exit({exit_code})\n"
    )
    path = Path(tempfile.mkdtemp(prefix="vr-fake-gbrain-")) / "gbrain-native"
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)
    return str(path)


def _doctor_ok(**overrides) -> dict:
    report = {
        "schema_version": 2,
        "status": "healthy",
        "checks": [
            {"name": "connection", "status": "ok", "message": "Connected"},
            {"name": "jsonb_integrity", "status": "ok", "message": "ok"},
            {"name": "schema_version", "status": "ok", "message": "ok"},
            {"name": "pgvector", "status": "ok", "message": "Extension installed"},
            {"name": "queue_health", "status": "ok", "message": "Skipped (PGLite)"},
        ],
    }
    report.update(overrides)
    return report


class LockContext:
    """Hold a real exclusive flock on a temp lock file and export the fd."""

    def __init__(self, lock_path: Path, shared: bool = False) -> None:
        self.lock_path = lock_path
        self.shared = shared
        self.fd = None
        self._old_env = None

    def __enter__(self) -> "LockContext":
        self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX)
        self._old_env = os.environ.get("TASKNOTES_LOCK_FD")
        os.environ["TASKNOTES_LOCK_FD"] = str(self.fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._old_env is None:
            os.environ.pop("TASKNOTES_LOCK_FD", None)
        else:
            os.environ["TASKNOTES_LOCK_FD"] = self._old_env
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


def _sources(tmp: Path) -> tuple[Path, Path]:
    gbrain_dir = tmp / "source" / ".gbrain"
    vault_dir = tmp / "source" / "obsidian"
    _write_tree(
        gbrain_dir,
        {
            "config.json": (0o600, '{"search": {"mcp_keyword_only": true}}\n'),
            "base/PG_VERSION": (0o644, "16\n"),
            "empty-dir": None,
        },
    )
    _write_tree(
        vault_dir,
        {
            "notes/hello.md": (0o644, "# Hello\nmarker-1\n"),
            "notes/deep/nested.md": (0o600, "nested\n"),
            "empty/": None,
        },
    )
    return gbrain_dir, vault_dir


class VaultRecoveryCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Tests never touch production env state.
        self._old_lock_fd = os.environ.get("TASKNOTES_LOCK_FD")
        self.addCleanup(
            lambda: (os.environ.pop("TASKNOTES_LOCK_FD", None)
                     if self._old_lock_fd is None
                     else os.environ.__setitem__("TASKNOTES_LOCK_FD", self._old_lock_fd))
        )

    # ------------------------------------------------------------------
    # Generation ids
    # ------------------------------------------------------------------

    def test_generation_id_validation(self) -> None:
        self.assertTrue(core.is_valid_generation_id("20260802T012247123456Z-a1b2c3d4"))
        self.assertFalse(core.is_valid_generation_id(""))
        self.assertFalse(core.is_valid_generation_id("../20260802T012247123456Z-a1b2c3d4"))
        self.assertFalse(core.is_valid_generation_id("20260802T012247123456Z-a1b2c3d4/"))
        self.assertFalse(core.is_valid_generation_id("20260802T012247123456Z-a1b2c3d4x"))
        self.assertFalse(core.is_valid_generation_id("20260802T012247123456Z-zzzzzzzz"))
        self.assertFalse(core.is_valid_generation_id(123))

    def test_next_generation_id_is_valid(self) -> None:
        self.assertTrue(core.is_valid_generation_id(core._next_generation_id()))

    # ------------------------------------------------------------------
    # No-follow tree scan / copy
    # ------------------------------------------------------------------

    def test_scan_tree_records_modes_empty_dirs_and_hashes(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        records = core.scan_tree(gbrain_dir)
        by_path = {r["path"]: r for r in records}
        self.assertEqual(by_path["config.json"]["type"], "file")
        self.assertEqual(by_path["config.json"]["mode"], "0o600")
        self.assertEqual(by_path["config.json"]["sha256"], core._sha256_bytes(b'{"search": {"mcp_keyword_only": true}}\n'))
        self.assertEqual(by_path["empty-dir"]["type"], "dir")
        self.assertEqual(by_path["base"]["type"], "dir")
        # Deterministic ordering.
        self.assertEqual([r["path"] for r in records], sorted(r["path"] for r in records))

    def test_scan_tree_rejects_symlink(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        (gbrain_dir / "evil-link").symlink_to("config.json")
        with self.assertRaises(core.TreeScanError):
            core.scan_tree(gbrain_dir)

    def test_scan_tree_rejects_fifo(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        os.mkfifo(gbrain_dir / "pipe")
        with self.assertRaises(core.TreeScanError):
            core.scan_tree(gbrain_dir)

    def test_copy_tree_roundtrip_preserves_modes_and_empty_dirs(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        records = core.scan_tree(gbrain_dir)
        dst = self.tmp / "copy"
        core.copy_tree(gbrain_dir, records, dst)
        scanned = core.scan_tree(dst)
        self.assertEqual(records, scanned)
        self.assertTrue((dst / "empty-dir").is_dir())
        self.assertEqual(stat.S_IMODE((dst / "config.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((dst / "base" / "PG_VERSION").stat().st_mode), 0o644)

    def test_scan_digest_is_content_sensitive_and_deterministic(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        records_a = core.scan_tree(gbrain_dir)
        digest_a = core.scan_digest(records_a)
        self.assertEqual(digest_a, core.scan_digest(core.scan_tree(gbrain_dir)))
        (gbrain_dir / "config.json").write_text("changed\n", encoding="utf-8")
        self.assertNotEqual(digest_a, core.scan_digest(core.scan_tree(gbrain_dir)))

    # ------------------------------------------------------------------
    # Convergence semantics
    # ------------------------------------------------------------------

    def test_convergence_detects_change_during_copy_and_retries(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        real_copy = core.copy_tree
        marker = vault_dir / "notes" / "hello.md"
        calls = {"n": 0}

        def changing_copy(root: Path, records, dst_root: Path) -> None:
            real_copy(root, records, dst_root)
            # After the vault copy, mutate a source file so scan B (taken
            # after the copy) differs from scan A: the first attempt must
            # fail convergence, and the retry must then converge.
            if root == vault_dir:
                calls["n"] += 1
                if calls["n"] == 1:
                    marker.write_text("# Hello\nchanged during copy\n", encoding="utf-8")

        with LockContext(lock_path), mock.patch.object(core, "copy_tree", changing_copy):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
                convergence_attempts=3, retry_delay=0,
            )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(manifest["convergence"]["attempts"], 2)
        # The published generation must equal the FINAL source state.
        gen_dir = staging / manifest["generation_id"]
        self.assertEqual(core.scan_tree(gen_dir / "vault"), core.scan_tree(vault_dir))
        self.assertEqual(core.scan_tree(gen_dir / ".gbrain"), core.scan_tree(gbrain_dir))

    def test_convergence_exhaustion_fails_without_readiness(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        real_copy = core.copy_tree
        churn = vault_dir / "churn.txt"
        calls = {"n": 0}

        def never_settles(root: Path, records, dst_root: Path) -> None:
            real_copy(root, records, dst_root)
            # Every attempt changes the source AFTER the copy, so scan B can
            # never equal scan A: bounded retries must exhaust and fail.
            if root == vault_dir:
                calls["n"] += 1
                churn.write_text(f"churn-{calls['n']}\n", encoding="utf-8")

        with LockContext(lock_path), mock.patch.object(core, "copy_tree", never_settles):
            with self.assertRaises(core.ConvergenceError):
                core.export_generation(
                    gbrain_dir, vault_dir, staging,
                    gbrain_bin=fake_bin, lock_path=str(lock_path),
                    convergence_attempts=3, retry_delay=0,
                )
        self.assertEqual(calls["n"], 3)
        # No generation dir, no READY, no latest pointer, no leftover temp.
        self.assertFalse(any(core.is_valid_generation_id(p.name) for p in staging.iterdir()))
        self.assertFalse((staging / "latest").exists())
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    def test_convergence_succeeds_on_first_attempt_when_quiet(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )
        self.assertEqual(manifest["convergence"]["attempts"], 1)
        self.assertEqual(
            manifest["convergence"]["source_scan_a_digest"],
            manifest["convergence"]["source_scan_b_digest"],
        )
        trees = manifest["trees"]
        self.assertEqual(trees[".gbrain"]["scan_digest"], trees[".gbrain"]["staged_digest"])
        self.assertEqual(trees["vault"]["scan_digest"], trees["vault"]["staged_digest"])

    # ------------------------------------------------------------------
    # Doctor preflight contract
    # ------------------------------------------------------------------

    def test_doctor_report_ok(self) -> None:
        summary = core.validate_doctor_report(_doctor_ok())
        self.assertEqual(
            summary["required_checks"],
            {"connection": "ok", "jsonb_integrity": "ok", "schema_version": "ok", "pgvector": "ok"},
        )
        self.assertEqual(summary["report_schema_version"], 2)

    def test_doctor_warnings_allowed(self) -> None:
        report = _doctor_ok(checks=_doctor_ok()["checks"] + [
            {"name": "embedding_env_override", "status": "warn", "message": "warn"},
        ])
        summary = core.validate_doctor_report(report)
        self.assertEqual(summary["check_counts"]["warn"], 1)

    def test_doctor_required_check_missing(self) -> None:
        report = _doctor_ok()
        report["checks"] = [c for c in report["checks"] if c["name"] != "pgvector"]
        with self.assertRaises(core.DoctorPreflightError):
            core.validate_doctor_report(report)

    def test_doctor_required_check_duplicated(self) -> None:
        report = _doctor_ok()
        report["checks"].append({"name": "pgvector", "status": "ok", "message": "again"})
        with self.assertRaises(core.DoctorPreflightError):
            core.validate_doctor_report(report)

    def test_doctor_required_check_not_ok(self) -> None:
        report = _doctor_ok()
        for check in report["checks"]:
            if check["name"] == "connection":
                check["status"] = "warn"
        with self.assertRaises(core.DoctorPreflightError):
            core.validate_doctor_report(report)

    def test_doctor_any_fail_rejects(self) -> None:
        report = _doctor_ok(checks=_doctor_ok()["checks"] + [
            {"name": "sync_failures", "status": "fail", "message": "boom"},
        ])
        with self.assertRaises(core.DoctorPreflightError):
            core.validate_doctor_report(report)

    def test_doctor_missing_checks_array(self) -> None:
        with self.assertRaises(core.DoctorPreflightError):
            core.validate_doctor_report({"schema_version": 2})

    def test_doctor_nonzero_exit_blocks_export(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok(), exit_code=1)
        with LockContext(lock_path):
            with self.assertRaises(core.DoctorPreflightError):
                core.export_generation(
                    gbrain_dir, vault_dir, staging,
                    gbrain_bin=fake_bin, lock_path=str(lock_path),
                )
        self.assertFalse((staging / "latest").exists())
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    def test_doctor_invalid_json_blocks_export(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        script = "#!/usr/bin/env python3\nprint('not json')\n"
        fake_bin = Path(tempfile.mkdtemp(prefix="vr-fake-")) / "gbrain-native"
        fake_bin.write_text(script, encoding="utf-8")
        os.chmod(fake_bin, 0o755)
        with LockContext(lock_path):
            with self.assertRaises(core.DoctorPreflightError):
                core.export_generation(
                    gbrain_dir, vault_dir, staging,
                    gbrain_bin=str(fake_bin), lock_path=str(lock_path),
                )

    # ------------------------------------------------------------------
    # Active-PGLite indicator rejection
    # ------------------------------------------------------------------

    def test_active_indicator_detection(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        self.assertEqual(core.find_active_pglite_indicators(gbrain_dir), [])
        (gbrain_dir / "postmaster.pid").write_text("1\n", encoding="utf-8")
        self.assertEqual(
            core.find_active_pglite_indicators(gbrain_dir), ["postmaster.pid"]
        )
        (gbrain_dir / "postmaster.pid").unlink()
        (gbrain_dir / "base" / ".s.PGSQL.5432").write_text("", encoding="utf-8")
        self.assertIn("base/.s.PGSQL.5432", core.find_active_pglite_indicators(gbrain_dir))
        (gbrain_dir / "base" / ".s.PGSQL.5432").unlink()
        os.mkfifo(gbrain_dir / "not-a-socket")
        self.assertEqual(core.find_active_pglite_indicators(gbrain_dir), [])

    def test_active_indicator_blocks_export(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        (gbrain_dir / "postmaster.opts").write_text("-p 5432\n", encoding="utf-8")
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            with self.assertRaises(core.ActiveIndicatorError):
                core.export_generation(
                    gbrain_dir, vault_dir, staging,
                    gbrain_bin=fake_bin, lock_path=str(lock_path),
                )
        self.assertFalse((staging / "latest").exists())
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    # ------------------------------------------------------------------
    # Shared-lock enforcement
    # ------------------------------------------------------------------

    def test_export_requires_shared_lock(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with self.assertRaises(core.LockError):
            core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )

    def test_shared_flock_is_rejected(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path, shared=True):
            with self.assertRaises(core.LockError):
                core.export_generation(
                    gbrain_dir, vault_dir, staging,
                    gbrain_bin=fake_bin, lock_path=str(lock_path),
                )

    def test_lock_held_by_runner_rejects_wrong_fd(self) -> None:
        lock_path = self.tmp / "tasknotes.lock"
        other = self.tmp / "other.lock"
        other.write_text("x", encoding="utf-8")
        with LockContext(lock_path):
            fd = os.open(other, os.O_RDWR)
            try:
                old = os.environ["TASKNOTES_LOCK_FD"]
                os.environ["TASKNOTES_LOCK_FD"] = str(fd)
                try:
                    self.assertFalse(core.lock_held_by_runner(str(lock_path)))
                finally:
                    os.environ["TASKNOTES_LOCK_FD"] = old
            finally:
                os.close(fd)

    # ------------------------------------------------------------------
    # Generation layout, publication, manifest
    # ------------------------------------------------------------------

    def test_export_end_to_end_layout(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )
        gen_id = manifest["generation_id"]
        gen_dir = staging / gen_id
        self.assertTrue(gen_dir.is_dir())
        self.assertTrue((gen_dir / "vault" / "notes" / "hello.md").is_file())
        self.assertTrue((gen_dir / ".gbrain" / "config.json").is_file())
        self.assertTrue((gen_dir / "manifest.json").is_file())
        self.assertTrue((gen_dir / "READY").is_file())
        self.assertEqual((gen_dir / "READY").read_text(encoding="utf-8").strip(), gen_id)
        # Atomic latest pointer.
        self.assertEqual((staging / "latest").read_text(encoding="utf-8").strip(), gen_id)
        # No temp leftovers.
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    def test_manifest_schema(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )
        for key in ("schema_version", "generation_id", "created_at_utc", "phase",
                    "remote", "sources", "trees", "doctor", "convergence", "exporter"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["phase"], 1)
        self.assertFalse(manifest["remote"]["uploaded"])
        self.assertEqual(
            set(manifest["trees"]), {".gbrain", "vault"}
        )
        for tree in manifest["trees"].values():
            self.assertEqual(
                set(tree), {"entries", "dirs", "files", "bytes", "root_mode",
                            "scan_digest", "staged_digest", "entries_file",
                            "entries_digest"}
            )
            self.assertEqual(tree["scan_digest"], tree["staged_digest"])
            self.assertRegex(tree["root_mode"], r"^0o[0-7]{3,4}$")
            self.assertRegex(tree["entries_file"], r"^.+\.entries\.txt$")
            self.assertRegex(tree["entries_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(manifest["doctor"]["required_checks"]),
            set(core.REQUIRED_DOCTOR_CHECKS),
        )
        # Staged .gbrain trees are byte-identical to the sources.
        stored = json.loads((staging / manifest["generation_id"] / "manifest.json").read_text("utf-8"))
        self.assertEqual(stored, manifest)

    def test_list_and_latest(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )
        gens = core.list_generations(staging)
        self.assertEqual([g["generation_id"] for g in gens], [manifest["generation_id"]])
        self.assertEqual(gens[0]["manifest"]["generation_id"], manifest["generation_id"])
        latest = core.read_latest(staging)
        self.assertEqual(latest["generation_id"], manifest["generation_id"])
        # A non-READY dir is never listed.
        (staging / "20260101T000000000000Z-deadbeef").mkdir()
        self.assertEqual([g["generation_id"] for g in core.list_generations(staging)],
                         [manifest["generation_id"]])

    def test_staged_tree_equals_source_after_export(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )
        gen_dir = staging / manifest["generation_id"]
        self.assertEqual(core.scan_tree(gen_dir / ".gbrain"), core.scan_tree(gbrain_dir))
        self.assertEqual(core.scan_tree(gen_dir / "vault"), core.scan_tree(vault_dir))

    # ------------------------------------------------------------------
    # Openat-style no-follow: symlink roots, intermediate components,
    # scan-error fail-closed
    # ------------------------------------------------------------------

    def test_scan_tree_records_root_mode(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        os.chmod(gbrain_dir, 0o750)
        records = core.scan_tree(gbrain_dir)
        root = next(r for r in records if r["path"] == "")
        self.assertEqual(root["type"], "dir")
        self.assertEqual(root["mode"], "0o750")

    def test_scan_tree_rejects_symlink_root(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        link = self.tmp / "link-to-gbrain"
        link.symlink_to(gbrain_dir, target_is_directory=True)
        with self.assertRaises(core.TreeScanError):
            core.scan_tree(link)

    def test_scan_tree_rejects_symlinked_intermediate_dir(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        real_base = self.tmp / "elsewhere"
        real_base.mkdir()
        (real_base / "PG_VERSION").write_text("16\n", encoding="utf-8")
        # Replace the scanned dir's child with a symlink to an external dir:
        # the openat descent must refuse the symlinked component.
        (gbrain_dir / "base" / "PG_VERSION").unlink()
        (gbrain_dir / "base").rmdir()
        (gbrain_dir / "base").symlink_to(real_base, target_is_directory=True)
        with self.assertRaises(core.TreeScanError):
            core.scan_tree(gbrain_dir)

    def test_scan_tree_fails_closed_on_read_error(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        with mock.patch("os.scandir", side_effect=OSError("boom")):
            with self.assertRaises(core.TreeScanError):
                core.scan_tree(gbrain_dir)

    def test_indicator_scan_fails_closed_on_read_error(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        with mock.patch("os.scandir", side_effect=OSError("boom")):
            with self.assertRaises(core.TreeScanError):
                core.find_active_pglite_indicators(gbrain_dir)

    def test_indicator_scan_fails_closed_on_symlinked_component(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        (gbrain_dir / "base" / "PG_VERSION").unlink()
        (gbrain_dir / "base").rmdir()
        (gbrain_dir / "base").symlink_to(self.tmp, target_is_directory=True)
        with self.assertRaises(core.TreeScanError):
            core.find_active_pglite_indicators(gbrain_dir)

    def test_copy_tree_rejects_symlink_root(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        records = core.scan_tree(gbrain_dir)
        link = self.tmp / "copy-link"
        link.symlink_to(gbrain_dir, target_is_directory=True)
        with self.assertRaises(core.TreeScanError):
            core.copy_tree(link, records, self.tmp / "dst")

    def test_copy_tree_component_race_fails_closed_without_publication(self) -> None:
        # A source component swapped between scan and copy (the openat open
        # refuses it) must fail the export closed with nothing published.
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path), mock.patch.object(
            core, "_open_file_no_follow", side_effect=core.TreeScanError("race")
        ):
            with self.assertRaises(core.TreeScanError):
                core.export_generation(
                    gbrain_dir, vault_dir, staging,
                    gbrain_bin=fake_bin, lock_path=str(lock_path),
                )
        self.assertFalse(any(core.is_valid_generation_id(p.name) for p in staging.iterdir()))
        self.assertFalse((staging / "latest").exists())
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    # ------------------------------------------------------------------
    # Root and directory modes (read-only sources copy successfully)
    # ------------------------------------------------------------------

    def test_copy_tree_copies_into_readonly_dirs_and_preserves_modes(self) -> None:
        gbrain_dir, _ = _sources(self.tmp)
        (gbrain_dir / "base").chmod(0o555)
        records = core.scan_tree(gbrain_dir)
        dst = self.tmp / "copy-ro"
        core.copy_tree(gbrain_dir, records, dst)
        self.assertEqual(core.scan_tree(dst), records)
        self.assertEqual(stat.S_IMODE((dst / "base").stat().st_mode), 0o555)
        self.assertEqual(
            (dst / "base" / "PG_VERSION").read_text(encoding="utf-8"), "16\n"
        )

    def test_export_preserves_root_modes(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        os.chmod(gbrain_dir, 0o750)
        os.chmod(vault_dir, 0o710)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )
        gen_dir = staging / manifest["generation_id"]
        self.assertEqual(stat.S_IMODE((gen_dir / ".gbrain").stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((gen_dir / "vault").stat().st_mode), 0o710)
        self.assertEqual(manifest["trees"][".gbrain"]["root_mode"], "0o750")
        self.assertEqual(manifest["trees"]["vault"]["root_mode"], "0o710")

    def test_export_with_readonly_nested_dirs_and_root(self) -> None:
        gbrain_dir, vault_dir = _sources(self.tmp)
        (vault_dir / "notes").chmod(0o555)
        os.chmod(vault_dir, 0o555)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )
        gen_dir = staging / manifest["generation_id"]
        self.assertEqual(core.scan_tree(gen_dir / "vault"), core.scan_tree(vault_dir))
        self.assertEqual(stat.S_IMODE((gen_dir / "vault" / "notes").stat().st_mode), 0o555)
        self.assertEqual(
            (gen_dir / "vault" / "notes" / "hello.md").read_text(encoding="utf-8"),
            "# Hello\nmarker-1\n",
        )


class VaultRecoveryIdentityTests(unittest.TestCase):
    """Hermes-identity enforcement at the direct exporter boundary."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-identity-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_uid = os.environ.get("HERMES_UID")
        self.addCleanup(
            lambda: (os.environ.pop("HERMES_UID", None)
                     if self._old_uid is None
                     else os.environ.__setitem__("HERMES_UID", self._old_uid))
        )

    def test_resolve_hermes_uid_prefers_validated_env(self) -> None:
        os.environ["HERMES_UID"] = "12345"
        self.assertEqual(core.resolve_hermes_uid(), 12345)

    def test_resolve_hermes_uid_rejects_invalid_env(self) -> None:
        os.environ["HERMES_UID"] = "not-a-uid"
        self.assertIsNone(core.resolve_hermes_uid())

    def test_resolve_hermes_uid_falls_back_to_system_hermes_user(self) -> None:
        os.environ.pop("HERMES_UID", None)
        with mock.patch("pwd.getpwnam", return_value=mock.Mock(pw_uid=4242)):
            self.assertEqual(core.resolve_hermes_uid(), 4242)

    def test_resolve_hermes_uid_falls_back_to_default(self) -> None:
        os.environ.pop("HERMES_UID", None)
        with mock.patch("pwd.getpwnam", side_effect=KeyError("hermes")):
            self.assertEqual(core.resolve_hermes_uid(), core.DEFAULT_HERMES_UID)

    def test_ensure_identity_rejects_root(self) -> None:
        os.environ.pop("HERMES_UID", None)
        with mock.patch("pwd.getpwnam", side_effect=KeyError("hermes")), \
                mock.patch.object(os, "geteuid", return_value=0):
            with self.assertRaises(core.IdentityError):
                core.ensure_hermes_identity()

    def test_ensure_identity_rejects_root_even_when_configured(self) -> None:
        os.environ["HERMES_UID"] = "0"
        with mock.patch.object(os, "geteuid", return_value=0):
            with self.assertRaises(core.IdentityError):
                core.ensure_hermes_identity()

    def test_ensure_identity_rejects_arbitrary_non_hermes_uid(self) -> None:
        os.environ["HERMES_UID"] = "10000"
        with mock.patch.object(os, "geteuid", return_value=9999):
            with self.assertRaises(core.IdentityError):
                core.ensure_hermes_identity()

    def test_ensure_identity_accepts_hermes_uid(self) -> None:
        os.environ["HERMES_UID"] = "10000"
        with mock.patch.object(os, "geteuid", return_value=10000):
            core.ensure_hermes_identity()

    def test_main_cli_boundary_rejects_non_hermes_identity(self) -> None:
        os.environ["HERMES_UID"] = "10000"
        with mock.patch.object(os, "geteuid", return_value=0):
            rc = core.main(["list", "--staging-dir", str(self.tmp / "staging")])
        self.assertEqual(rc, 2)

    def test_main_cli_boundary_accepts_hermes_identity(self) -> None:
        os.environ["HERMES_UID"] = "10000"
        with mock.patch.object(os, "geteuid", return_value=10000):
            rc = core.main(["list", "--staging-dir", str(self.tmp / "staging")])
        self.assertEqual(rc, 0)

    def test_shell_wrappers_enforce_exact_hermes_identity(self) -> None:
        # Contract: both the thin wrapper and the cron entrypoint reject root
        # AND arbitrary non-Hermes uids against the resolved Hermes identity
        # (HERMES_UID env -> system hermes user -> default 10000).
        wrapper = (Path(__file__).resolve().parents[2] / "scripts"
                   / "vault-recovery-export.sh").read_text(encoding="utf-8")
        cron = (Path(__file__).resolve().parents[2] / "scripts"
                / "hermes-vault-recovery-export-cron.sh").read_text(encoding="utf-8")
        for script in (wrapper, cron):
            self.assertIn('"$uid" = "0"', script)
            self.assertIn('"$uid" != "$expected_uid"', script)
            self.assertIn('expected_uid="${HERMES_UID:-}"', script)
            self.assertIn('id -u hermes', script)
            self.assertIn("refuses to run as uid $uid", script)


class VaultRecoveryDurabilityTests(unittest.TestCase):
    """fsync durability: call coverage and fail-closed boundaries."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-durability-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_lock_fd = os.environ.get("TASKNOTES_LOCK_FD")
        self.addCleanup(
            lambda: (os.environ.pop("TASKNOTES_LOCK_FD", None)
                     if self._old_lock_fd is None
                     else os.environ.__setitem__("TASKNOTES_LOCK_FD", self._old_lock_fd))
        )

    def _export(self, staging: Path, **kw) -> dict:
        gbrain_dir, vault_dir = _sources(self.tmp)
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            return core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path), **kw,
            )

    def test_fsync_dir_boundary_failures(self) -> None:
        # fsync on a missing directory fails closed with VaultRecoveryError.
        with self.assertRaises(core.VaultRecoveryError):
            core._fsync_dir(self.tmp / "does-not-exist")

    def test_fsync_fd_boundary_failures(self) -> None:
        with mock.patch.object(core.os, "fsync", side_effect=OSError("EIO")):
            with self.assertRaises(core.VaultRecoveryError):
                core._fsync_fd(12345)

    def test_export_fsyncs_files_dirs_manifest_ready_and_staging(self) -> None:
        staging = self.tmp / "staging"
        fsynced_dirs: list = []
        fsynced_fds: list = []
        real_fsync_dir = core._fsync_dir
        real_fsync_fd = core._fsync_fd

        def record_dir(path) -> None:
            fsynced_dirs.append(str(path))
            return real_fsync_dir(path)

        def record_fd(fd) -> None:
            fsynced_fds.append(fd)
            return real_fsync_fd(fd)

        with mock.patch.object(core, "_fsync_dir", side_effect=record_dir), \
                mock.patch.object(core, "_fsync_fd", side_effect=record_fd):
            manifest = self._export(staging)
        gen_id = manifest["generation_id"]
        # Every copied file's fd was fsynced (config.json, PG_VERSION,
        # hello.md, deep/nested.md = 4 files) plus manifest + READY.
        self.assertGreaterEqual(len(fsynced_fds), 6)
        # Every staged directory + tree roots + the temp generation dir +
        # the staging root were fsynced (the dirs are fsynced while still in
        # the hidden temp dir, before the atomic rename).
        tmp_gen = staging / f".{gen_id}.tmp"
        for expected in (
            str(tmp_gen / ".gbrain"), str(tmp_gen / ".gbrain" / "base"),
            str(tmp_gen / ".gbrain" / "empty-dir"),
            str(tmp_gen / "vault"), str(tmp_gen / "vault" / "notes"),
            str(tmp_gen / "vault" / "notes" / "deep"), str(tmp_gen / "vault" / "empty"),
            str(tmp_gen), str(staging),
        ):
            self.assertIn(expected, fsynced_dirs)

    def test_copy_tree_fsync_failure_aborts_without_publication(self) -> None:
        staging = self.tmp / "staging"
        real_fsync_fd = core._fsync_fd

        def fail_on_regular_files(fd) -> None:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise core.VaultRecoveryError("fsync failed (test EIO)")
            return real_fsync_fd(fd)

        with mock.patch.object(core, "_fsync_fd", side_effect=fail_on_regular_files):
            with self.assertRaises(core.VaultRecoveryError):
                self._export(staging)
        # Nothing published, nothing left behind.
        self.assertFalse(any(core.is_valid_generation_id(p.name) for p in staging.iterdir()))
        self.assertFalse((staging / "latest").exists())
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    def test_manifest_dir_fsync_failure_aborts_without_publication(self) -> None:
        staging = self.tmp / "staging"
        real_fsync_dir = core._fsync_dir

        def fail_on_tmp_gen(path) -> None:
            if path.name.endswith(".tmp") and path.parent == staging:
                raise core.VaultRecoveryError("fsync failed (test EIO)")
            return real_fsync_dir(path)

        with mock.patch.object(core, "_fsync_dir", side_effect=fail_on_tmp_gen):
            with self.assertRaises(core.VaultRecoveryError):
                self._export(staging)
        self.assertFalse(any(core.is_valid_generation_id(p.name) for p in staging.iterdir()))
        self.assertFalse((staging / "latest").exists())
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    def test_staging_root_fsync_failure_after_rename_rolls_back(self) -> None:
        staging = self.tmp / "staging"
        # A previous generation is published and pointed to by `latest`.
        first = self._export(staging)
        self.assertTrue((staging / "latest").exists())
        real_fsync_dir = core._fsync_dir

        def fail_on_staging_root(path) -> None:
            if path == staging:
                raise core.VaultRecoveryError("fsync failed (test EIO)")
            return real_fsync_dir(path)

        # The durability failure happens right after the generation dir was
        # renamed into place (fsync of the staging root). The export must
        # fail AND roll back: no new generation dir, `latest` restored to the
        # previous generation.
        with mock.patch.object(core, "_fsync_dir", side_effect=fail_on_staging_root):
            with self.assertRaises(core.VaultRecoveryError):
                self._export(staging)
        gens = [p.name for p in staging.iterdir()
                if core.is_valid_generation_id(p.name)]
        self.assertEqual(gens, [first["generation_id"]])
        self.assertEqual(
            (staging / "latest").read_text(encoding="utf-8").strip(),
            first["generation_id"],
        )
        # The rolled-back generation still has its READY; the failed one has
        # no leftover temp dir.
        self.assertTrue((staging / first["generation_id"] / "READY").exists())
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    def test_latest_write_fsync_failure_rolls_back_pointer(self) -> None:
        staging = self.tmp / "staging"
        first = self._export(staging)
        real_fsync_dir = core._fsync_dir
        calls = {"n": 0}

        def fail_on_second_staging_fsync(path) -> None:
            # Let the post-rename generation fsync pass; fail the fsync that
            # happens after the `latest` pointer was renamed into place.
            if path == staging:
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise core.VaultRecoveryError("fsync failed (test EIO)")
            return real_fsync_dir(path)

        with mock.patch.object(core, "_fsync_dir", side_effect=fail_on_second_staging_fsync):
            with self.assertRaises(core.VaultRecoveryError):
                self._export(staging)
        gens = [p.name for p in staging.iterdir()
                if core.is_valid_generation_id(p.name)]
        self.assertEqual(gens, [first["generation_id"]])
        self.assertEqual(
            (staging / "latest").read_text(encoding="utf-8").strip(),
            first["generation_id"],
        )
        self.assertEqual(list(staging.glob(".*.tmp")), [])

    def test_file_mode_applied_before_final_content_fsync(self) -> None:
        """The recorded source file mode must be applied BEFORE the final
        content fsync: at the moment the staged temp file is fsynced it must
        already carry the source mode, so mode and content are equally
        crash-durable (a mode applied after the fsync would be lost on a
        crash between fsync and rename)."""
        gbrain_dir, _ = _sources(self.tmp)
        # Distinctive non-default modes: a staging temp file is created
        # 0o600, so observing the source mode at fsync time proves the
        # fchmod happened before the fsync.
        (gbrain_dir / "config.json").chmod(0o604)
        (gbrain_dir / "base" / "PG_VERSION").chmod(0o640)
        records = core.scan_tree(gbrain_dir)
        dst = self.tmp / "copy"
        modes_at_fsync = []
        real_fsync_fd = core._fsync_fd

        def record_mode(fd) -> None:
            try:
                st = os.fstat(fd)
            except OSError:
                return real_fsync_fd(fd)
            if stat.S_ISREG(st.st_mode):
                modes_at_fsync.append(stat.S_IMODE(st.st_mode))
            return real_fsync_fd(fd)

        with mock.patch.object(core, "_fsync_fd", side_effect=record_mode):
            core.copy_tree(gbrain_dir, records, dst)
        # Every staged file was fsynced only AFTER its source mode was
        # applied (both distinctive modes visible at fsync time), and the
        # published copies still carry those modes.
        self.assertEqual(sorted(modes_at_fsync), [0o604, 0o640])
        self.assertEqual(stat.S_IMODE((dst / "config.json").stat().st_mode), 0o604)
        self.assertEqual(
            stat.S_IMODE((dst / "base" / "PG_VERSION").stat().st_mode), 0o640
        )

    def test_first_generation_pointer_fsync_failure_leaves_no_dangling_latest(self) -> None:
        """On the FIRST publication, a staging-root fsync failure right after
        the `latest` pointer was renamed into place must remove the dangling
        newly-installed pointer (it points at a deleted generation), remove
        the temp generation, and fsync the staging root: nothing may point
        at nothing."""
        staging = self.tmp / "staging"
        real_fsync_dir = core._fsync_dir

        def fail_when_pointer_visible(path) -> None:
            # Fail the fsync that happens after the `latest` pointer was
            # renamed into place (the pointer already exists at that point);
            # earlier staging-root fsyncs (staging creation, post-gen
            # rename) pass, as does the rollback fsync after the dangling
            # pointer was removed.
            if path == staging and (staging / "latest").exists():
                raise core.VaultRecoveryError("fsync failed (test EIO)")
            return real_fsync_dir(path)

        with mock.patch.object(core, "_fsync_dir", side_effect=fail_when_pointer_visible):
            with self.assertRaises(core.VaultRecoveryError):
                self._export(staging)
        # Nothing published and NO dangling `latest`: the rollback removed
        # the pointer, the newly installed generation dir, and the temp
        # generation, and fsynced the staging root.
        self.assertFalse(any(core.is_valid_generation_id(p.name) for p in staging.iterdir()))
        self.assertFalse((staging / "latest").exists())
        self.assertIsNone(core.read_latest(staging))
        self.assertEqual(list(staging.glob(".*.tmp")), [])


def _valid_manifest() -> dict:
    """A schema-version-1 manifest that satisfies every strict check."""
    return {
        "schema_version": 1,
        "generation_id": "20260802T012247123456Z-a1b2c3d4",
        "created_at_utc": "2026-08-02T01:22:47Z",
        "phase": 1,
        "remote": {"uploaded": False, "note": "test"},
        "sources": {
            "gbrain_state_dir": "/opt/data/.gbrain",
            "vault_dir": "/opt/data/obsidian",
        },
        "trees": {
            ".gbrain": {
                "entries": 2, "dirs": 1, "files": 1, "bytes": 10,
                "root_mode": "0o700",
                "scan_digest": "0" * 64, "staged_digest": "0" * 64,
                "entries_file": ".gbrain.entries.txt",
                "entries_digest": "0" * 64,
            },
            "vault": {
                "entries": 2, "dirs": 1, "files": 1, "bytes": 10,
                "root_mode": "0o755",
                "scan_digest": "0" * 64, "staged_digest": "0" * 64,
                "entries_file": "vault.entries.txt",
                "entries_digest": "0" * 64,
            },
        },
        "doctor": {
            "report_schema_version": 2,
            "report_status": "healthy",
            "required_checks": {name: "ok" for name in core.REQUIRED_DOCTOR_CHECKS},
            "check_counts": {"ok": 5, "warn": 0, "fail": 0},
        },
        "convergence": {
            "attempts": 1, "max_attempts": 3,
            "source_scan_a_digest": "0" * 64, "source_scan_b_digest": "0" * 64,
        },
        "exporter": {"version": "2", "python": "3.12"},
    }


class ManifestSchemaStrictTests(unittest.TestCase):
    """Strict full-schema validation (council fix): a manifest is accepted
    ONLY when every block/key/type/digest of the schema-version-1 contract
    holds exactly. A well-formed-JSON-but-structurally-drifted manifest
    (unknown keys, malformed digests, missing blocks, wrong tree set,
    non-zero doctor failures) is refused — the shell uploader/recover gates
    enforce JSON well-formedness, this validator is the authoritative
    schema check the restore/verify/install core runs."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-schema-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_strict_schema_accepts_valid_manifest(self) -> None:
        summary = core.validate_manifest_schema(_valid_manifest())
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["generation_id"], "20260802T012247123456Z-a1b2c3d4")
        self.assertEqual(summary["trees"], [".gbrain", "vault"])
        self.assertTrue(summary["digest_ok"])

    def test_strict_schema_rejects_non_object(self) -> None:
        for bad in (None, [], "manifest", 1):
            with self.assertRaises(core.VaultRecoveryError):
                core.validate_manifest_schema(bad)

    def test_strict_schema_rejects_unknown_top_level_key(self) -> None:
        manifest = _valid_manifest()
        manifest["tampered"] = True
        with self.assertRaises(core.VaultRecoveryError) as cm:
            core.validate_manifest_schema(manifest)
        self.assertIn("unknown key", str(cm.exception))

    def test_strict_schema_rejects_missing_required_block(self) -> None:
        manifest = _valid_manifest()
        del manifest["convergence"]
        with self.assertRaises(core.VaultRecoveryError) as cm:
            core.validate_manifest_schema(manifest)
        self.assertIn("missing required key", str(cm.exception))

    def test_strict_schema_rejects_bad_schema_version(self) -> None:
        manifest = _valid_manifest()
        manifest["schema_version"] = 99
        with self.assertRaises(core.VaultRecoveryError) as cm:
            core.validate_manifest_schema(manifest)
        self.assertIn("schema_version", str(cm.exception))

    def test_strict_schema_rejects_bad_generation_id(self) -> None:
        manifest = _valid_manifest()
        manifest["generation_id"] = "../../evil"
        with self.assertRaises(core.VaultRecoveryError) as cm:
            core.validate_manifest_schema(manifest)
        self.assertIn("generation_id", str(cm.exception))

    def test_strict_schema_rejects_wrong_trees_key_set(self) -> None:
        manifest = _valid_manifest()
        manifest["trees"]["extra"] = dict(manifest["trees"][".gbrain"])
        with self.assertRaises(core.VaultRecoveryError) as cm:
            core.validate_manifest_schema(manifest)
        self.assertIn("trees", str(cm.exception))

    def test_strict_schema_rejects_unknown_tree_key(self) -> None:
        manifest = _valid_manifest()
        manifest["trees"]["vault"]["extra"] = 1
        with self.assertRaises(core.VaultRecoveryError):
            core.validate_manifest_schema(manifest)

    def test_strict_schema_rejects_malformed_digest(self) -> None:
        manifest = _valid_manifest()
        manifest["trees"][".gbrain"]["scan_digest"] = "not-a-sha256"
        with self.assertRaises(core.VaultRecoveryError) as cm:
            core.validate_manifest_schema(manifest)
        self.assertIn("scan_digest", str(cm.exception))

    def test_strict_schema_rejects_bad_entries_file_name(self) -> None:
        manifest = _valid_manifest()
        manifest["trees"]["vault"]["entries_file"] = "vault.entries.bak"
        with self.assertRaises(core.VaultRecoveryError):
            core.validate_manifest_schema(manifest)

    def test_strict_schema_rejects_bad_root_mode(self) -> None:
        manifest = _valid_manifest()
        manifest["trees"]["vault"]["root_mode"] = "755"
        with self.assertRaises(core.VaultRecoveryError):
            core.validate_manifest_schema(manifest)

    def test_strict_schema_rejects_nonzero_doctor_failures(self) -> None:
        manifest = _valid_manifest()
        manifest["doctor"]["check_counts"]["fail"] = 1
        with self.assertRaises(core.VaultRecoveryError) as cm:
            core.validate_manifest_schema(manifest)
        self.assertIn("check_counts.fail", str(cm.exception))

    def test_strict_schema_rejects_non_ok_required_check(self) -> None:
        manifest = _valid_manifest()
        manifest["doctor"]["required_checks"]["connection"] = "fail"
        with self.assertRaises(core.VaultRecoveryError):
            core.validate_manifest_schema(manifest)

    def test_strict_schema_rejects_bad_convergence_digest(self) -> None:
        manifest = _valid_manifest()
        manifest["convergence"]["source_scan_b_digest"] = "zz" * 32
        with self.assertRaises(core.VaultRecoveryError):
            core.validate_manifest_schema(manifest)

    def test_strict_schema_accepts_real_exporter_manifest(self) -> None:
        """The manifest the PRODUCTION exporter publishes passes the strict
        validator (the exporter also self-checks before publication)."""
        gbrain_dir, vault_dir = _sources(self.tmp)
        staging = self.tmp / "staging"
        lock_path = self.tmp / "tasknotes.lock"
        fake_bin = _fake_gbrain_bin(_doctor_ok())
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_bin, lock_path=str(lock_path),
            )
        summary = core.validate_manifest_schema(manifest)
        self.assertEqual(summary["generation_id"], manifest["generation_id"])


if __name__ == "__main__":
    unittest.main()
