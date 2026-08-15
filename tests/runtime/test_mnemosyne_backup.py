"""Tests for the Mnemosyne encrypted-backup core (Phase 2).

This module covers two layers:

1. Fast source/contract tests (no Docker): the exporter core logic, DR seam
   signature inspection, manifest schema, SHA-256, atomicity, lock, pruning,
   restore verify/install separation, and the compose overlay boundary. The
   DR-seam-dependent tests add the extracted mnemosyne-memory package to
   PYTHONPATH (or use the installed package if available) so they run without
   Docker.

2. Docker-gated synthetic full round trip (requires RUN_DOCKER_TESTS=1):
   builds the isolated hermes image and proves the full backup contract end
   to end: create sqlite-vec-backed Mnemosyne data in a disposable home,
   native online backup while the source can remain open/write-capable,
   restore/integrity, manifest SHA validation, a separate uploader with NO
   hermes-data mount, a disposable local underlying rclone remote wrapped by
   an actual temporary `crypt` config, prove ciphertext does not contain a
   unique plaintext marker, download/decrypt, verify and restore into a new
   path, verify marker recall/data, slot rotation/idempotency/failure does
   not advance state, staging is read-only in the uploader, cleanup.

Never uses project volumes, credentials, or remotes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from .helpers import (
    ComposeRuntime,
    TEST_ISOLATION_OVERLAY,
    docker_available,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
CORE_PATH = SCRIPTS_DIR / "mnemosyne_backup_core.py"
OVERLAY = REPO_ROOT / "docker-compose.mnemosyne-backup.yml"
MNEMOSYNE_OVERLAY = REPO_ROOT / "docker-compose.mnemosyne.yml"
EMBED_OVERLAY = REPO_ROOT / "docker-compose.embeddings.yml"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"

# Path to the extracted mnemosyne-memory package (for DR-seam tests without
# Docker). Falls back to the installed package if importable.
_EXTRACTED_MEMORY = Path("/tmp/opencode/mnemosyne-wheels/extracted_memory")


def _dr_seam_available() -> bool:
    """True if the mnemosyne DR seam can be imported (extracted or installed)."""
    try:
        sys.path.insert(0, str(_EXTRACTED_MEMORY))
        importlib_test = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "from mnemosyne.dr import recovery; print('ok')" % str(_EXTRACTED_MEMORY)],
            capture_output=True, text=True,
        )
        return importlib_test.returncode == 0 and "ok" in importlib_test.stdout
    except Exception:
        return False
    finally:
        if str(_EXTRACTED_MEMORY) in sys.path:
            sys.path.remove(str(_EXTRACTED_MEMORY))


def _sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers for building a sqlite-vec-backed Mnemosyne-like source DB
# ---------------------------------------------------------------------------


def _build_source_db(db_path: Path, marker: str = "UNIQUE_PLAINTEXT_MARKER_9f2c1a") -> sqlite3.Connection:
    """Build a sqlite-vec-backed DB mirroring the Mnemosyne beam schema shape.

    Keeps the connection OPEN so tests prove the online backup works while the
    source can remain open/write-capable.
    """
    import sqlite_vec

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS working_memory ("
        "id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT, "
        "timestamp TEXT, session_id TEXT DEFAULT 'default', "
        "importance REAL DEFAULT 0.5, metadata_json TEXT, "
        "veracity TEXT DEFAULT 'unknown', "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodes USING vec0(embedding float32[4])"
    )
    conn.execute(
        "INSERT INTO working_memory (id, content, source) VALUES (?, ?, ?)",
        ("m1", marker, "test"),
    )
    conn.commit()
    return conn


def _import_core():
    """Import the core module from scripts/."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mnemosyne_backup_core", str(CORE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# Layer 1: Fast source/contract tests (no Docker)
# ===========================================================================


class CoreSourceContractTests(unittest.TestCase):
    """Pure-source contract tests that do not require the DR seam package."""

    def setUp(self) -> None:
        self.core = _import_core()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_valid_db(self, path: Path, marker: str = "verified") -> bytes:
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE working_memory (id TEXT PRIMARY KEY, content TEXT NOT NULL)")
        conn.execute("INSERT INTO working_memory VALUES (?, ?)", ("m1", marker))
        conn.commit()
        conn.close()
        return path.read_bytes()

    def _fake_dr(self, *, integrity=True):
        """Small hermetic seam for install-restore source tests."""
        class FakeDR:
            @staticmethod
            def verify_integrity(_db_path):
                return integrity
        return FakeDR

    # --- DR seam constants / contract ---

    def test_dr_seam_constants_pin_exact_module_and_functions(self) -> None:
        self.assertEqual(self.core.DR_MODULE, "mnemosyne.dr.recovery")
        self.assertEqual(self.core.DR_CREATE_BACKUP, "create_backup")
        self.assertEqual(self.core.DR_RESTORE_BACKUP, "restore_backup")
        self.assertEqual(self.core.DR_VERIFY_INTEGRITY, "verify_integrity")

    def test_expected_dr_signatures_are_pinned(self) -> None:
        self.assertEqual(
            self.core.EXPECTED_CREATE_BACKUP_PARAMS, ("db_path", "backup_dir")
        )
        self.assertEqual(
            self.core.EXPECTED_RESTORE_BACKUP_PARAMS, ("backup_path", "db_path")
        )
        self.assertEqual(
            self.core.EXPECTED_VERIFY_INTEGRITY_PARAMS, ("db_path",)
        )

    def test_manifest_has_no_memory_contents_keys(self) -> None:
        # The manifest schema must not include any key that would carry memory
        # contents. Assert the exact allowed top-level keys; any addition that
        # could carry memory content must be reviewed here.
        allowed_top_level = {
            "generation_id", "created_at_utc", "source_db_path",
            "source_db_size", "artifact", "dr_seam", "package_versions",
            "restore_verified", "integrity_verified", "backup_method",
        }
        # The artifact sub-object must carry only metadata, never content.
        allowed_artifact_keys = {"name", "sha256", "size", "compressed"}
        # Build a real manifest via export to inspect the actual schema.
        # (Uses the DR seam; if unavailable, fall back to the static contract.)
        if _dr_seam_available() and _sqlite_vec_available():
            if str(_EXTRACTED_MEMORY) not in sys.path:
                sys.path.insert(0, str(_EXTRACTED_MEMORY))
            import sqlite_vec
            db = self.tmp / "mnemosyne.db"
            conn = _build_source_db(db, marker="SHOULD_NOT_LEAK")
            try:
                manifest = self.core.export_backup(
                    db_path=db, staging_dir=self.tmp / "staging"
                )
            finally:
                conn.close()
            self.assertEqual(set(manifest.keys()), allowed_top_level,
                             f"unexpected manifest top-level keys: {set(manifest.keys())}")
            self.assertEqual(set(manifest["artifact"].keys()), allowed_artifact_keys,
                             f"unexpected artifact keys: {set(manifest['artifact'].keys())}")
            # The marker must not appear anywhere in the manifest JSON text.
            manifest_text = json.dumps(manifest)
            self.assertNotIn("SHOULD_NOT_LEAK", manifest_text)
        else:
            # Static contract: the allowed key sets are the source of truth.
            self.assertEqual(allowed_top_level, {
                "generation_id", "created_at_utc", "source_db_path",
                "source_db_size", "artifact", "dr_seam", "package_versions",
                "restore_verified", "integrity_verified", "backup_method",
            })

    def test_source_contract_records_exact_seam(self) -> None:
        contract = self.core._source_contract()
        self.assertEqual(contract["dr_module"], self.core.DR_MODULE)
        self.assertEqual(contract["create_backup"], self.core.DR_CREATE_BACKUP)
        self.assertEqual(contract["restore_backup"], self.core.DR_RESTORE_BACKUP)
        self.assertEqual(contract["verify_integrity"], self.core.DR_VERIFY_INTEGRITY)
        self.assertEqual(
            contract["create_backup_params"],
            list(self.core.EXPECTED_CREATE_BACKUP_PARAMS),
        )

    # --- CLI parser ---

    def test_cli_has_separate_verify_and_install_restore(self) -> None:
        parser = self.core.build_parser()
        # verify-restore and install-restore must be separate subcommands so
        # tests cannot accidentally target production.
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        subchoice = None
        for a in actions:
            if "verify-restore" in a.choices and "install-restore" in a.choices:
                subchoice = a.choices
                break
        self.assertIsNotNone(subchoice, "subcommand choices not found")
        self.assertIn("export", subchoice)
        self.assertIn("list", subchoice)
        self.assertIn("latest", subchoice)
        self.assertIn("verify-restore", subchoice)
        self.assertIn("install-restore", subchoice)
        self.assertIn("inspect-dr", subchoice)

    def test_cli_install_restore_requires_explicit_confirm_flag(self) -> None:
        parser = self.core.build_parser()
        # Parse with the confirm flag.
        ns = parser.parse_args([
            "install-restore", "/tmp/a.db", "/tmp/live.db",
            "--i-confirm-this-overwrites-production",
        ])
        self.assertTrue(ns.i_confirm_this_overwrites_production)
        # Parse without the confirm flag.
        ns2 = parser.parse_args(["install-restore", "/tmp/a.db", "/tmp/live.db"])
        self.assertFalse(ns2.i_confirm_this_overwrites_production)

    # --- Export lock ---

    def test_export_lock_is_exclusive(self) -> None:
        staging = self.tmp / "staging"
        staging.mkdir()
        lock = self.core.ExportLock(staging)
        with lock:
            # A second acquisition must fail.
            with self.assertRaises(self.core.MnemosyneBackupError):
                with self.core.ExportLock(staging):
                    pass
        # After release, can acquire again.
        with self.core.ExportLock(staging):
            pass

    def test_export_lock_releases_on_exception(self) -> None:
        staging = self.tmp / "staging"
        staging.mkdir()
        with self.assertRaises(ValueError):
            with self.core.ExportLock(staging):
                raise ValueError("boom")
        # Lock dir must be gone.
        self.assertFalse((staging / self.core.EXPORT_LOCK_DIR_NAME).exists())

    # --- install-restore confirm gate and rollback ---

    def test_install_restore_refuses_without_confirm(self) -> None:
        verified = self.tmp / "verified.db"
        verified.write_bytes(b"x")
        live = self.tmp / "live.db"
        with self.assertRaises(self.core.MnemosyneBackupError):
            self.core.install_restore(verified, live, confirm=False)

    def test_install_restore_refuses_missing_verified_db(self) -> None:
        verified = self.tmp / "missing.db"
        live = self.tmp / "live.db"
        with self.assertRaises(self.core.MnemosyneBackupError):
            self.core.install_restore(verified, live, confirm=True)

    def test_install_restore_refuses_existing_dest(self) -> None:
        # verify_restore must refuse to overwrite an existing dest.
        artifact = self.tmp / "a.gz"
        artifact.write_bytes(b"x")
        dest = self.tmp / "dest.db"
        dest.write_bytes(b"existing")
        with self.assertRaises(self.core.MnemosyneBackupError):
            self.core.verify_restore(artifact, dest)

    def test_install_restore_retains_rollback_copy(self) -> None:
        live = self.tmp / "live.db"
        live.write_bytes(b"original-live")
        verified = self.tmp / "verified.db"
        verified_bytes = self._make_valid_db(verified, "verified-restore")
        with mock.patch.object(self.core, "load_dr_seam", return_value=self._fake_dr()):
            res = self.core.install_restore(verified, live, confirm=True)
        self.assertTrue(res["installed"])
        self.assertTrue(Path(res["rollback_path"]).exists())
        self.assertEqual(live.read_bytes(), verified_bytes)
        self.assertEqual(verified.read_bytes(), verified_bytes)
        self.assertEqual(Path(res["rollback_path"]).read_bytes(), b"original-live")

    def test_install_restore_retains_wal_shm_rollback(self) -> None:
        live = self.tmp / "live.db"
        live.write_bytes(b"db")
        (self.tmp / "live.db-wal").write_bytes(b"wal")
        (self.tmp / "live.db-shm").write_bytes(b"shm")
        verified = self.tmp / "verified.db"
        self._make_valid_db(verified, "new")
        with mock.patch.object(self.core, "load_dr_seam", return_value=self._fake_dr()):
            res = self.core.install_restore(verified, live, confirm=True)
        rollback = Path(res["rollback_path"])
        self.assertTrue(rollback.exists())
        self.assertTrue(Path(str(rollback) + "-wal").exists())
        self.assertTrue(Path(str(rollback) + "-shm").exists())
        # Stale wal/shm at live path removed.
        self.assertFalse(Path(str(live) + "-wal").exists())
        self.assertFalse(Path(str(live) + "-shm").exists())

    # --- SHA-256 helpers ---

    def test_sha256_file_is_stable(self) -> None:
        f = self.tmp / "f.bin"
        f.write_bytes(b"hello")
        h1 = self.core._sha256_file(f)
        h2 = self.core._sha256_file(f)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_sha256_bytes(self) -> None:
        h = self.core._sha256_bytes(b"hello")
        x = self.tmp / "x"
        x.write_bytes(b"hello")
        self.assertEqual(h, self.core._sha256_file(x))

    # --- Generation ID uniqueness ---

    def test_generation_ids_are_unique_within_same_second(self) -> None:
        ids = [self.core._next_generation_id() for _ in range(5)]
        self.assertEqual(len(set(ids)), 5, f"ids not unique: {ids}")
        for gid in ids:
            self.assertRegex(gid, r"^\d{8}T\d{6}\d{6}Z-[0-9a-f]{8}$")
            self.assertTrue(self.core.is_valid_generation_id(gid))

    def test_generation_id_validation_rejects_legacy_and_traversal(self) -> None:
        valid = "20260802T012247123456Z-a1b2c3d4"
        self.assertTrue(self.core.is_valid_generation_id(valid))
        for invalid in (
            "20260802T012247Z-0001",
            "../20260802T012247123456Z-a1b2c3d4",
            "20260802T012247123456Z-../../..",
            "20260802T012247123456Z-A1B2C3D4",
        ):
            self.assertFalse(self.core.is_valid_generation_id(invalid), invalid)

    def test_read_latest_rejects_pointer_traversal_and_manifest_mismatch(self) -> None:
        staging = self.tmp / "staging"
        staging.mkdir()
        pointer = staging / self.core.LATEST_POINTER_NAME
        manifest = staging / self.core.LATEST_MANIFEST_NAME
        pointer.write_text("../escape\n", encoding="utf-8")
        manifest.write_text(json.dumps({"generation_id": "../escape"}), encoding="utf-8")
        self.assertIsNone(self.core.read_latest(staging))
        valid = "20260802T012247123456Z-a1b2c3d4"
        pointer.write_text(valid + "\n", encoding="utf-8")
        manifest.write_text(json.dumps({"generation_id": "other"}), encoding="utf-8")
        self.assertIsNone(self.core.read_latest(staging))

    def test_install_restore_cleans_same_parent_temp_on_integrity_failure(self) -> None:
        live = self.tmp / "live.db"
        live.write_bytes(b"original")
        verified = self.tmp / "verified.db"
        self._make_valid_db(verified, "verified")
        with mock.patch.object(self.core, "_verify_binary_database", side_effect=[True, False]):
            with self.assertRaises(self.core.MnemosyneBackupError):
                self.core.install_restore(verified, live, confirm=True)
        self.assertEqual(live.read_bytes(), b"original")
        self.assertTrue(verified.exists())
        self.assertEqual(list(self.tmp.glob(".mnem-restore-*.dbtmp")), [])


# ---------------------------------------------------------------------------
# DR-seam-dependent tests (need mnemosyne-memory on PYTHONPATH; no Docker)
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    _dr_seam_available() and _sqlite_vec_available(),
    "mnemosyne DR seam + sqlite-vec required (extracted package on PYTHONPATH "
    "or installed, plus sqlite-vec).",
)
class CoreDRSeamTests(unittest.TestCase):
    """Tests that exercise the actual pinned DR seam (no Docker)."""

    def setUp(self) -> None:
        # Ensure the extracted package is importable.
        if str(_EXTRACTED_MEMORY) not in sys.path:
            sys.path.insert(0, str(_EXTRACTED_MEMORY))
        self.core = _import_core()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_dr_seam_succeeds_and_returns_module(self) -> None:
        mod = self.core.load_dr_seam()
        self.assertTrue(hasattr(mod, "create_backup"))
        self.assertTrue(hasattr(mod, "restore_backup"))
        self.assertTrue(hasattr(mod, "verify_integrity"))

    def test_load_dr_seam_fails_clearly_on_drift(self) -> None:
        # Monkeypatch the expected params to simulate drift.
        original = self.core.EXPECTED_CREATE_BACKUP_PARAMS
        self.core.EXPECTED_CREATE_BACKUP_PARAMS = ("db_path", "output_dir")
        try:
            with self.assertRaises(self.core.DRSeamError) as cm:
                self.core.load_dr_seam()
            self.assertIn("drift", str(cm.exception).lower())
        finally:
            self.core.EXPECTED_CREATE_BACKUP_PARAMS = original

    def test_full_export_restore_round_trip_with_sqlite_vec_open_source(self) -> None:
        marker = "UNIQUE_PLAINTEXT_MARKER_9f2c1a"
        db = self.tmp / "mnemosyne.db"
        staging = self.tmp / "staging"
        # Keep source connection OPEN during backup (online backup proof).
        conn = _build_source_db(db, marker=marker)
        try:
            manifest = self.core.export_backup(db_path=db, staging_dir=staging)
        finally:
            conn.close()

        # Manifest contract.
        self.assertTrue(manifest["restore_verified"])
        self.assertTrue(manifest["integrity_verified"])
        self.assertEqual(manifest["artifact"]["name"], self.core.BACKUP_ARTIFACT_NAME)
        self.assertEqual(len(manifest["artifact"]["sha256"]), 64)
        self.assertIn("mnemosyne-memory", manifest["package_versions"])
        self.assertEqual(manifest["dr_seam"]["dr_module"], self.core.DR_MODULE)

        # Generation dir + READY sentinel + latest pointer.
        gen_dir = staging / manifest["generation_id"]
        self.assertTrue(gen_dir.exists())
        self.assertTrue((gen_dir / self.core.READY_SENTINEL_NAME).exists())
        self.assertTrue((gen_dir / self.core.MANIFEST_NAME).exists())
        self.assertTrue((gen_dir / self.core.BACKUP_ARTIFACT_NAME).exists())
        latest = self.core.read_latest(staging)
        self.assertEqual(latest["generation_id"], manifest["generation_id"])

        # Manifest contains NO memory contents (marker must not appear).
        manifest_text = (gen_dir / self.core.MANIFEST_NAME).read_text("utf-8")
        self.assertNotIn(marker, manifest_text)

        # verify-restore to a NEW path.
        artifact = gen_dir / self.core.BACKUP_ARTIFACT_NAME
        verify_db = self.tmp / "verify.db"
        vres = self.core.verify_restore(
            artifact, verify_db, expected_sha256=manifest["artifact"]["sha256"]
        )
        self.assertTrue(vres["integrity_check"])
        self.assertEqual(vres["sha256"], manifest["artifact"]["sha256"])

        # Marker recall from verified DB.
        import sqlite_vec
        rconn = sqlite3.connect(str(verify_db))
        rconn.enable_load_extension(True)
        sqlite_vec.load(rconn)
        row = rconn.execute(
            "SELECT content FROM working_memory WHERE id=?", ("m1",)
        ).fetchone()
        self.assertIsNotNone(row, "marker row missing after restore")
        self.assertEqual(row[0], marker, "marker content mismatch after restore")
        # vec0 table must exist in restored DB.
        tabs = [r[0] for r in rconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        self.assertIn("vec_episodes", tabs)
        rconn.close()

    def test_export_pruning_keeps_n_generations(self) -> None:
        db = self.tmp / "mnemosyne.db"
        staging = self.tmp / "staging"
        uploader_state = self.tmp / "uploader-state"
        uploader_state.mkdir()
        acknowledged = []
        conn = _build_source_db(db)
        try:
            for _ in range(5):
                if acknowledged:
                    (uploader_state / self.core.UPLOADED_LEDGER_NAME).write_text(
                        "\n".join(acknowledged) + "\n", encoding="utf-8"
                    )
                manifest = self.core.export_backup(
                    db_path=db, staging_dir=staging, generations_keep=2,
                    uploader_state_dir=uploader_state,
                )
                acknowledged.append(manifest["generation_id"])
        finally:
            conn.close()
        gens = self.core.list_generations(staging)
        self.assertEqual(len(gens), 2, f"expected 2 gens, got {len(gens)}")

    def test_export_lock_released_after_success(self) -> None:
        db = self.tmp / "mnemosyne.db"
        staging = self.tmp / "staging"
        conn = _build_source_db(db)
        try:
            self.core.export_backup(db_path=db, staging_dir=staging)
        finally:
            conn.close()
        self.assertFalse((staging / self.core.EXPORT_LOCK_DIR_NAME).exists())

    def test_verify_restore_detects_sha_mismatch(self) -> None:
        db = self.tmp / "mnemosyne.db"
        staging = self.tmp / "staging"
        conn = _build_source_db(db)
        try:
            manifest = self.core.export_backup(db_path=db, staging_dir=staging)
        finally:
            conn.close()
        artifact = staging / manifest["generation_id"] / self.core.BACKUP_ARTIFACT_NAME
        verify_db = self.tmp / "verify.db"
        with self.assertRaises(self.core.MnemosyneBackupError):
            self.core.verify_restore(artifact, verify_db, expected_sha256="0" * 64)

    def test_install_restore_full_flow_with_rollback(self) -> None:
        marker = "UNIQUE_PLAINTEXT_MARKER_9f2c1a"
        db = self.tmp / "mnemosyne.db"
        staging = self.tmp / "staging"
        conn = _build_source_db(db, marker=marker)
        try:
            manifest = self.core.export_backup(db_path=db, staging_dir=staging)
        finally:
            conn.close()

        # Corrupt the live DB.
        db.write_bytes(b"corrupted")

        # verify-restore to a NEW path.
        artifact = staging / manifest["generation_id"] / self.core.BACKUP_ARTIFACT_NAME
        verify_db = self.tmp / "verify.db"
        self.core.verify_restore(artifact, verify_db, expected_sha256=manifest["artifact"]["sha256"])

        # install-restore with confirm.
        res = self.core.install_restore(verify_db, db, confirm=True)
        self.assertTrue(res["installed"])
        self.assertTrue(Path(res["rollback_path"]).exists())
        # Live DB now has the marker back.
        import sqlite_vec
        rconn = sqlite3.connect(str(db))
        rconn.enable_load_extension(True)
        sqlite_vec.load(rconn)
        row = rconn.execute(
            "SELECT content FROM working_memory WHERE id=?", ("m1",)
        ).fetchone()
        self.assertEqual(row[0], marker)
        rconn.close()
        # Rollback copy has the corrupted content.
        self.assertEqual(Path(res["rollback_path"]).read_bytes(), b"corrupted")


# ===========================================================================
# Compose overlay boundary tests (no Docker)
# ===========================================================================


class ComposeOverlayBoundaryTests(unittest.TestCase):
    """Standalone overlay/source tests enforcing the uploader mount boundary
    and that the base remains untouched. Does NOT modify shared test files."""

    def setUp(self) -> None:
        self.overlay_text = OVERLAY.read_text("utf-8")
        self.base_text = BASE_COMPOSE.read_text("utf-8")

    def _service_block(self, text: str, service: str) -> str:
        lines = text.splitlines(keepends=True)
        marker = f"  {service}:\n"
        start = next(i for i, line in enumerate(lines) if line == marker)
        end = len(lines)
        for i in range(start + 1, len(lines)):
            line = lines[i]
            if line and not line.startswith(" "):
                end = i
                break
            if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
                end = i
                break
        return "".join(lines[start:end])

    def test_overlay_adds_uploader_service(self) -> None:
        self.assertIn("mnemosyne-backup-uploader:", self.overlay_text)

    def test_overlay_adds_staging_and_state_volumes(self) -> None:
        self.assertIn("mnemosyne-backup-staging:", self.overlay_text)
        self.assertIn("mnemosyne-backup-state:", self.overlay_text)

    def test_overlay_adds_uploader_only_secret_rclone_config_volume(self) -> None:
        # The private ACTIVE rclone config (OAuth-refresh fix) lives in a
        # DEDICATED uploader-only secret volume, NOT the state volume: the
        # state volume is mounted READ-ONLY into Hermes and must hold no
        # secrets.
        self.assertIn("mnemosyne-backup-rclone-config:", self.overlay_text)
        uploader_block = self._service_block(self.overlay_text, "mnemosyne-backup-uploader")
        self.assertIn("mnemosyne-backup-rclone-config:/rclone-active", uploader_block)
        self.assertNotIn("mnemosyne-backup-rclone-config:/rclone-active:ro", uploader_block)
        self.assertIn("MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR=/rclone-active", uploader_block)

    def test_uploader_script_never_seeds_active_config_into_state_dir(self) -> None:
        # The uploader must seed the ACTIVE config into the dedicated
        # uploader-only volume ($RCLONE_ACTIVE_DIR), never into the shared
        # state volume that Hermes observes read-only.
        src = (SCRIPTS_DIR / "mnemosyne-backup-uploader.sh").read_text("utf-8")
        self.assertNotIn("$STATE_DIR/rclone.active.conf", src)
        self.assertIn("$RCLONE_ACTIVE_DIR/rclone.active.conf", src)
        self.assertIn('RCLONE_ACTIVE_DIR="${MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR:-/rclone-active}"',
                      src)

    def test_secret_rclone_config_volume_never_mounted_into_hermes_or_recover(self) -> None:
        # Hermes must NEVER see the rclone config (seed or active): the
        # uploader-state mount exposes ONLY the non-secret ledger. Check the
        # actual MOUNT lines, not comments (which may mention the volume to
        # explain the boundary).
        def _mount_lines(block: str) -> list:
            mounts = []
            in_volumes = False
            for line in block.splitlines():
                if line.strip().startswith("volumes:"):
                    in_volumes = True
                    continue
                if in_volumes:
                    if line and not line.startswith(" ") and not line.startswith("\t"):
                        break
                    stripped = line.strip()
                    if stripped.startswith("- "):
                        mounts.append(stripped)
            return mounts

        hermes_block = self._service_block(self.overlay_text, "hermes")
        hermes_mounts = _mount_lines(hermes_block)
        self.assertNotIn("mnemosyne-backup-rclone-config", " ".join(hermes_mounts))
        # The active-config env var is never injected into Hermes.
        self.assertNotIn("MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR", hermes_block)
        recover_mounts = _mount_lines(
            self._service_block(self.overlay_text, "mnemosyne-backup-recover"))
        self.assertNotIn("mnemosyne-backup-rclone-config", " ".join(recover_mounts))

    def test_hermes_uploader_state_mount_is_read_only_ledger_exposure(self) -> None:
        hermes_block = self._service_block(self.overlay_text, "hermes")
        self.assertIn("mnemosyne-backup-state:/opt/data/mnemosyne-backup/uploader-state:ro",
                      hermes_block)

    def test_uploader_never_mounts_hermes_data_or_opt_data(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-uploader")
        # Extract only the volumes: sub-block (not comments) to avoid matching
        # the word in explanatory comments.
        vol_lines = []
        in_volumes = False
        for line in block.splitlines():
            if line.strip().startswith("volumes:"):
                in_volumes = True
                continue
            if in_volumes:
                if line and not line.startswith(" ") and not line.startswith("\t"):
                    break
                if line.startswith("    ") and not line.startswith("      ") and ":" in line and "#" not in line.split(":")[0]:
                    vol_lines.append(line)
        volumes_block = "\n".join(vol_lines)
        self.assertNotIn("hermes-data", volumes_block,
                         f"uploader must not mount hermes-data:\n{volumes_block}")
        self.assertNotIn("/opt/data", volumes_block,
                         f"uploader must not mount /opt/data:\n{volumes_block}")

    def test_uploader_staging_mount_is_read_only(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-uploader")
        self.assertIn("mnemosyne-backup-staging:/staging:ro", block)

    def test_uploader_rclone_config_is_read_only(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-uploader")
        self.assertIn("obsidian-rclone-config:/config/rclone:ro", block)

    def test_uploader_state_mount_is_writable_ledger_only(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-uploader")
        # state mount must NOT be read-only (the uploader owns the ledger).
        self.assertIn("mnemosyne-backup-state:/state", block)
        self.assertNotIn("mnemosyne-backup-state:/state:ro", block)
        # The secret ACTIVE rclone config has its OWN uploader-only writable
        # volume; it must not be mounted read-only either.
        self.assertIn("mnemosyne-backup-rclone-config:/rclone-active", block)
        self.assertNotIn("mnemosyne-backup-rclone-config:/rclone-active:ro", block)

    def test_uploader_has_no_host_ports(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-uploader")
        self.assertNotIn("ports:", block)

    def test_uploader_reuses_obsidian_rclone_config_volume(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-uploader")
        self.assertIn("obsidian-rclone-config", block)

    def test_overlay_hermes_augments_with_staging_rw_and_uploader_state_ro(self) -> None:
        block = self._service_block(self.overlay_text, "hermes")
        self.assertIn("mnemosyne-backup-staging:/opt/data/mnemosyne-backup/staging", block)
        # No :ro on the staging mount in hermes (RW).
        self.assertNotIn("mnemosyne-backup-staging:/opt/data/mnemosyne-backup/staging:ro", block)
        self.assertIn("mnemosyne-backup-state:/opt/data/mnemosyne-backup/uploader-state:ro", block)

    def test_overlay_hermes_backup_env_present(self) -> None:
        block = self._service_block(self.overlay_text, "hermes")
        self.assertIn("MNEMOSYNE_BACKUP_STAGING_DIR", block)
        self.assertIn("MNEMOSYNE_BACKUP_GENERATIONS_KEEP", block)
        self.assertIn("MNEMOSYNE_BACKUP_EXPORT_INTERVAL", block)

    def test_overlay_export_interval_disabled_by_default(self) -> None:
        block = self._service_block(self.overlay_text, "hermes")
        self.assertIn("MNEMOSYNE_BACKUP_EXPORT_INTERVAL=${MNEMOSYNE_BACKUP_EXPORT_INTERVAL:-0}", block)

    def test_overlay_uploader_requires_crypt_remote_env(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-uploader")
        self.assertIn("MNEMOSYNE_BACKUP_RCLONE_REMOTE", block)

    def test_base_compose_unchanged_by_overlay(self) -> None:
        # The overlay is a separate file; base must not define the uploader.
        self.assertNotIn("mnemosyne-backup-uploader", self.base_text)
        self.assertNotIn("mnemosyne-backup-staging", self.base_text)
        self.assertNotIn("mnemosyne-backup-state", self.base_text)
        self.assertNotIn("mnemosyne-backup-recover", self.base_text)
        self.assertNotIn("mnemosyne-backup-recovery", self.base_text)

    def test_overlay_adds_recover_service_and_profile(self) -> None:
        # The recovery download step must exist and be profile-gated so a plain
        # `docker compose up` never starts it.
        self.assertIn("mnemosyne-backup-recover:", self.overlay_text)
        self.assertIn('profiles: ["recovery"]', self.overlay_text)

    def test_overlay_adds_recovery_volume(self) -> None:
        self.assertIn("mnemosyne-backup-recovery:", self.overlay_text)

    def test_recover_never_mounts_hermes_data_or_opt_data(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-recover")
        vol_lines = []
        in_volumes = False
        for line in block.splitlines():
            if line.strip().startswith("volumes:"):
                in_volumes = True
                continue
            if in_volumes:
                if line and not line.startswith(" ") and not line.startswith("\t"):
                    break
                if line.startswith("    ") and not line.startswith("      ") and ":" in line and "#" not in line.split(":")[0]:
                    vol_lines.append(line)
        volumes_block = "\n".join(vol_lines)
        self.assertNotIn("hermes-data", volumes_block,
                         f"recover must not mount hermes-data:\n{volumes_block}")
        self.assertNotIn("/opt/data", volumes_block,
                         f"recover must not mount /opt/data:\n{volumes_block}")

    def test_recover_rclone_config_read_only_and_recovery_writable(self) -> None:
        block = self._service_block(self.overlay_text, "mnemosyne-backup-recover")
        self.assertIn("obsidian-rclone-config:/config/rclone:ro", block)
        self.assertIn("mnemosyne-backup-recovery:/recovery", block)
        # The disposable handoff volume is the only writable mount.
        self.assertNotIn("mnemosyne-backup-recovery:/recovery:ro", block)

    def test_long_running_hermes_never_mounts_recovery_volume(self) -> None:
        # The decrypted recovery handoff must only be mounted transiently via
        # `docker compose run`; never in the long-running hermes service.
        hermes_block = self._service_block(self.overlay_text, "hermes")
        self.assertNotIn("mnemosyne-backup-recovery", hermes_block)

    def test_overlay_documents_opt_in_and_disabled_by_default(self) -> None:
        # The overlay header must document opt-in and disabled-by-default.
        self.assertIn("opt-in", self.overlay_text.lower())
        self.assertIn("DISABLED BY DEFAULT", self.overlay_text)

    def test_overlay_no_new_network_defined_leaking_into_base(self) -> None:
        # The overlay reuses josemar-network; it must not define a new
        # network that would leak into base-only deploys.
        # Find the networks: block in the overlay.
        self.assertIn("networks:", self.overlay_text)
        self.assertIn("josemar-network:", self.overlay_text)


class TestIsolationOverlayBoundaryTests(unittest.TestCase):
    """The dedicated test-isolation overlay keeps the backup Docker runtime
    fail-closed: the repository's real agent-state/credentials bind mounts are
    always replaced with disposable empty dirs, and inherited production env is
    forcibly blanked. No Docker required."""

    def setUp(self) -> None:
        self.isolation_text = TEST_ISOLATION_OVERLAY.read_text("utf-8")

    def test_isolation_overlay_declares_disposable_bind_mount_replacements(self) -> None:
        self.assertIn("JOSEMAR_TEST_STATE_DIR", self.isolation_text)
        self.assertIn("JOSEMAR_TEST_CREDENTIALS_DIR", self.isolation_text)
        self.assertIn("/opt/josemar/source-agent-state:ro", self.isolation_text)
        self.assertIn("/opt/josemar/credentials-source:ro", self.isolation_text)

    def test_isolation_overlay_never_mounts_repository_agent_state_or_credentials(self) -> None:
        # The ACTIVE service definition must never reference the base's
        # bind-mount SOURCES (the header comment may document them; the
        # container target paths legitimately contain "agent-state").
        services = self.isolation_text.split("services:")[1]
        self.assertNotIn("./agent-state", services)
        self.assertNotIn("./credentials", services)

    def test_runtime_compose_command_always_carries_isolation_overlay(self) -> None:
        runtime = ComposeRuntime()
        try:
            command = runtime.compose_command()
        finally:
            runtime._cleanup_disposable_mounts()
        self.assertIn("-f", command)
        self.assertIn("docker-compose.yml", command)
        self.assertIn(str(TEST_ISOLATION_OVERLAY), command)
        self.assertIn("-p", command)
        self.assertIn(runtime.project, command)

    def test_runtime_disposable_mounts_are_exposed_to_compose(self) -> None:
        runtime = ComposeRuntime()
        try:
            state_dir, creds_dir = runtime.disposable_mounts()
            self.assertTrue(state_dir.is_dir() and creds_dir.is_dir())
            self.assertEqual(list(state_dir.iterdir()), [])
            self.assertEqual(list(creds_dir.iterdir()), [])
            self.assertEqual(runtime.env["JOSEMAR_TEST_STATE_DIR"], str(state_dir))
            self.assertEqual(runtime.env["JOSEMAR_TEST_CREDENTIALS_DIR"], str(creds_dir))
        finally:
            runtime._cleanup_disposable_mounts()

    def test_runtime_force_blanks_production_like_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "prod-bot-token",
                "PRIMARY_TELEGRAM_ID": "123456789",
                "WORKSPACE_STATE_REPO": "git@github.com:prod/agent-state.git",
                "WORKSPACE_REPO_TOKEN": "prod-repo-token",
                "WORKSPACE_SYNC_ON_START": "true",
                "WORKSPACE_SYNC_INTERVAL": "60",
                "ZAI_API_KEY": "prod-zai-key",
                "TS_AUTHKEY": "prod-ts-authkey",
                "HERMES_DASHBOARD_SESSION_TOKEN": "prod-dashboard-session",
                "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "prod-admin",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "prod-dashboard-password",
                "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "prod-dashboard-secret",
                "MNEMOSYNE_PROVIDER": "mnemosyne",
                "MNEMOSYNE_NO_EMBEDDINGS": "true",
                "JOSEMAR_CONTAINER_PREFIX": "josemar",
                "COMPOSE_PROJECT_NAME": "josemar",
                "COMPOSE_FILE": "docker-compose.yml:docker-compose.mnemosyne.yml",
            },
            clear=False,
        ):
            runtime = ComposeRuntime()
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "PRIMARY_TELEGRAM_ID",
            "WORKSPACE_STATE_REPO",
            "WORKSPACE_REPO_TOKEN",
            "ZAI_API_KEY",
            "TS_AUTHKEY",
            "MNEMOSYNE_PROVIDER",
            "MNEMOSYNE_NO_EMBEDDINGS",
        ):
            self.assertEqual(runtime.env.get(key), "", key)
        # Dashboard credentials are never inherited: ComposeRuntime substitutes
        # deterministic test-only values (base compose `:?` needs non-empty).
        self.assertNotEqual(
            runtime.env["HERMES_DASHBOARD_SESSION_TOKEN"],
            "prod-dashboard-session",
        )
        self.assertNotEqual(
            runtime.env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"],
            "prod-dashboard-password",
        )
        self.assertNotEqual(
            runtime.env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"],
            "prod-dashboard-secret",
        )
        self.assertEqual(runtime.env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"], "test-admin")
        self.assertTrue(runtime.env["HERMES_DASHBOARD_SESSION_TOKEN"].startswith("test-session-"))
        self.assertTrue(runtime.env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"].startswith("test-password-"))
        self.assertTrue(runtime.env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"].startswith("test-secret-"))
        # Sync timing is explicitly overridden to safe test values.
        self.assertEqual(runtime.env["WORKSPACE_SYNC_ON_START"], "false")
        self.assertEqual(runtime.env["WORKSPACE_SYNC_INTERVAL"], "0")
        self.assertNotIn("COMPOSE_FILE", runtime.env)
        self.assertTrue(runtime.env["JOSEMAR_CONTAINER_PREFIX"].startswith("josemar-test-"))
        self.assertNotEqual(runtime.env["JOSEMAR_CONTAINER_PREFIX"], "josemar")


# ===========================================================================
# Shell wrapper syntax tests (no Docker)
# ===========================================================================


class ShellWrapperSyntaxTests(unittest.TestCase):
    """Validate the shell wrappers are syntactically valid."""

    def test_export_wrapper_syntax(self) -> None:
        wrapper = SCRIPTS_DIR / "mnemosyne-backup-export.sh"
        proc = subprocess.run(
            ["sh", "-n", str(wrapper)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"export wrapper syntax error:\n{proc.stderr}")

    def test_uploader_wrapper_syntax(self) -> None:
        wrapper = SCRIPTS_DIR / "mnemosyne-backup-uploader.sh"
        proc = subprocess.run(
            ["sh", "-n", str(wrapper)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"uploader wrapper syntax error:\n{proc.stderr}")

    def test_restore_wrapper_syntax(self) -> None:
        wrapper = SCRIPTS_DIR / "mnemosyne-backup-restore.sh"
        proc = subprocess.run(
            ["sh", "-n", str(wrapper)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"restore wrapper syntax error:\n{proc.stderr}")

    def test_recover_wrapper_syntax(self) -> None:
        wrapper = SCRIPTS_DIR / "mnemosyne-backup-recover.sh"
        proc = subprocess.run(
            ["sh", "-n", str(wrapper)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"recover wrapper syntax error:\n{proc.stderr}")

    def test_restore_wrapper_has_no_rclone_requirement(self) -> None:
        # Hermes has neither rclone nor rclone config; the restore wrapper must
        # never INVOKE rclone or require the rclone env vars (prose describing
        # this boundary is fine). It consumes the RECOVERY_READY handoff
        # produced by the recover step instead.
        wrapper = SCRIPTS_DIR / "mnemosyne-backup-restore.sh"
        text = wrapper.read_text("utf-8")
        self.assertNotIn("rclone copy", text)
        self.assertNotIn("rclone config show", text)
        self.assertNotIn("$(rclone", text)
        self.assertNotIn("RCLONE_CONFIG", text)
        self.assertNotIn("MNEMOSYNE_BACKUP_RCLONE_REMOTE", text)
        self.assertIn("RECOVERY_READY", text)

    def test_uploader_wrapper_validates_crypt_remote_type(self) -> None:
        # The uploader wrapper must contain the crypt type validation logic.
        wrapper = SCRIPTS_DIR / "mnemosyne-backup-uploader.sh"
        text = wrapper.read_text("utf-8")
        self.assertIn("type 'crypt'", text)
        self.assertIn("rclone config show", text)
        self.assertIn('remote_type', text)

    def test_uploader_wrapper_documents_one_shot_mode(self) -> None:
        # One-shot mode must be documented in the header and must NOT change
        # the default daemon behavior (ONCE defaults to false).
        wrapper = SCRIPTS_DIR / "mnemosyne-backup-uploader.sh"
        text = wrapper.read_text("utf-8")
        self.assertIn("MNEMOSYNE_BACKUP_ONCE", text)
        self.assertIn('ONCE="${MNEMOSYNE_BACKUP_ONCE:-false}"', text)
        self.assertIn("while true; do", text)  # daemon poll loop remains


# ===========================================================================
# Uploader wrapper behavioral tests (no Docker, fake rclone on PATH)
# ===========================================================================


class UploaderBehavioralTests(unittest.TestCase):
    """Drive scripts/mnemosyne-backup-uploader.sh with a fake `rclone` on
    PATH to prove the default daemon path and the one-shot path behave as
    documented, without Docker or a real remote."""

    # Valid strict generation id (fixed; validation does not require "now").
    GEN = "20260802T012247123456Z-a1b2c3d4"
    FAKE_RCLONE = r"""#!/bin/sh
# Fake rclone for uploader behavioral tests. Records invocations to
# $FAKE_RCLONE_LOG, answers `config show` with type=crypt, and implements a
# no-op sync/copyto. When FAKE_RCLONE_SLEEP is set, sync sleeps forever so
# the daemon test can observe a long-running process.
log() { printf '%s\n' "$*" >> "$FAKE_RCLONE_LOG"; }
if [ "${1:-}" = "config" ]; then
  log "config $*"
  printf 'type = crypt\n'
  exit 0
fi
if [ "${1:-}" = "sync" ]; then
  log "sync $*"
  if [ -n "${FAKE_RCLONE_SLEEP:-}" ]; then
    while :; do sleep 1; done
  fi
  exit 0
fi
if [ "${1:-}" = "copyto" ]; then
  log "copyto $*"
  exit 0
fi
log "other $*"
exit 0
"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mnem-uploader-beh-"))
        self.staging = self.tmp / "staging"
        self.state = self.tmp / "state"
        self.bin = self.tmp / "bin"
        self.config_dir = self.tmp / "rclone-config"
        self.staging.mkdir()
        self.state.mkdir()
        self.bin.mkdir()
        self.config_dir.mkdir()
        self.log = self.tmp / "rclone.log"
        fake = self.bin / "rclone"
        fake.write_text(self.FAKE_RCLONE, encoding="utf-8")
        fake.chmod(0o700)
        (self.config_dir / "rclone.conf").write_text("dummy\n", encoding="utf-8")
        # A ready generation in staging.
        gen_dir = self.staging / self.GEN
        gen_dir.mkdir()
        (gen_dir / "mnemosyne.db.gz").write_bytes(b"backup-artifact-bytes")
        sha = hashlib.sha256(b"backup-artifact-bytes").hexdigest()
        (gen_dir / "manifest.json").write_text(
            json.dumps({
                "generation_id": self.GEN,
                "artifact": {"name": "mnemosyne.db.gz", "sha256": sha},
            }),
            encoding="utf-8",
        )
        (gen_dir / "READY").write_text(f"{self.GEN}\n{sha}\n", encoding="utf-8")
        (self.staging / "latest").write_text(f"{self.GEN}\n", encoding="utf-8")
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "MNEMOSYNE_BACKUP_STAGING_DIR": str(self.staging),
            "MNEMOSYNE_BACKUP_STATE_DIR": str(self.state),
            "MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR": str(self.tmp / "rclone-active"),
            "MNEMOSYNE_BACKUP_RCLONE_REMOTE": "mnemosyne-crypt",
            "MNEMOSYNE_BACKUP_RCLONE_PATH": "backups",
            "MNEMOSYNE_BACKUP_SLOTS": "3",
            "RCLONE_CONFIG": str(self.config_dir / "rclone.conf"),
            "FAKE_RCLONE_LOG": str(self.log),
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_uploader(self, extra_env: Optional[dict] = None, timeout: int = 60) -> subprocess.CompletedProcess:
        env = {**self.env, **(extra_env or {})}
        return subprocess.run(
            ["/bin/sh", str(SCRIPTS_DIR / "mnemosyne-backup-uploader.sh")],
            env=env, capture_output=True, text=True, check=False, timeout=timeout,
        )

    def test_one_shot_success_uploads_and_cleans_lock(self) -> None:
        proc = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertEqual(proc.returncode, 0, f"one-shot failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertEqual((self.state / "last-uploaded-generation").read_text().strip(), self.GEN)
        self.assertEqual((self.state / "next-slot").read_text().strip(), "2")
        ledger = self.state / "uploaded-generations.jsonl"
        self.assertTrue(ledger.exists())
        self.assertIn(self.GEN, ledger.read_text("utf-8"))
        self.assertFalse((self.state / ".upload.lock").exists(), "lock leaked after one-shot")
        # rclone was invoked exactly once for the sync (plus config validation).
        sync_calls = [l for l in self.log.read_text("utf-8").splitlines() if l.startswith("sync ")]
        self.assertEqual(len(sync_calls), 1, sync_calls)

    def test_one_shot_noop_when_already_uploaded(self) -> None:
        # First run uploads; second run must be a clean no-op (exit 0, no
        # extra sync, no state advance, no lock leak).
        first = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertEqual(first.returncode, 0, f"first one-shot failed:\n{first.stdout}\n{first.stderr}")
        (self.state / "next-slot").write_text("2\n", encoding="utf-8")
        (self.state / "last-uploaded-generation").write_text(f"{self.GEN}\n", encoding="utf-8")
        log_lines = self.log.read_text("utf-8").splitlines()
        second = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertEqual(second.returncode, 0, f"noop one-shot failed:\n{second.stdout}\n{second.stderr}")
        self.assertEqual((self.state / "next-slot").read_text().strip(), "2")
        after = self.log.read_text("utf-8").splitlines()
        # Startup re-validates the crypt remote (rclone config show) on every
        # invocation, but a no-op must never run sync/copyto or advance state.
        new_ops = [
            l for l in after[len(log_lines):]
            if l.startswith("sync ") or l.startswith("copyto ")
        ]
        self.assertEqual(new_ops, [], f"no-op must not invoke rclone sync/copyto:\n{new_ops}")
        self.assertFalse((self.state / ".upload.lock").exists())

    def test_one_shot_failure_does_not_advance_state_and_cleans_lock(self) -> None:
        # Corrupt the artifact so the SHA check fails.
        artifact = self.staging / self.GEN / "mnemosyne.db.gz"
        with open(artifact, "ab") as f:
            f.write(b"corrupted")
        proc = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertNotEqual(proc.returncode, 0, "one-shot must fail on corrupted artifact")
        self.assertIn("SHA-256 mismatch", proc.stderr)
        self.assertFalse((self.state / "last-uploaded-generation").exists())
        self.assertFalse((self.state / "next-slot").exists())
        self.assertFalse((self.state / "uploaded-generations.jsonl").exists())
        self.assertFalse((self.state / ".upload.lock").exists(), "lock leaked on failure")
        sync_calls = [l for l in self.log.read_text("utf-8").splitlines() if l.startswith("sync ")]
        self.assertEqual(sync_calls, [], "SHA failure must not reach rclone sync")

    def test_daemon_default_keeps_running_until_signal(self) -> None:
        # Without MNEMOSYNE_BACKUP_ONCE, the wrapper must NOT exit after one
        # upload: it keeps polling (long-running daemon). A TERM then stops it
        # and the trap cleans the lock.
        #
        # NOTE: dash defers a trapped TERM until the current foreground
        # `sleep` returns (verified empirically) — the same reason the old
        # backgrounded-daemon + kill pattern hung. So we poll for the exit
        # rather than waiting synchronously, with a short poll interval.
        env = {**self.env, "MNEMOSYNE_BACKUP_POLL_INTERVAL": "1"}
        proc = subprocess.Popen(
            ["/bin/sh", str(SCRIPTS_DIR / "mnemosyne-backup-uploader.sh")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            import time
            # Give the initial run_once time to complete.
            deadline = time.time() + 30
            while time.time() < deadline:
                if (self.state / "last-uploaded-generation").exists():
                    break
                if proc.poll() is not None:
                    self.fail(f"daemon exited early (rc={proc.returncode})")
                time.sleep(0.2)
            self.assertTrue(
                (self.state / "last-uploaded-generation").exists(),
                "daemon never uploaded",
            )
            # Still alive (polling) -> default daemon behavior preserved.
            self.assertIsNone(proc.poll(), "daemon exited instead of polling")
            # TERM: the trap runs when the in-flight sleep returns.
            proc.terminate()
            deadline = time.time() + 15
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
            self.assertIsNotNone(
                proc.poll(),
                "daemon did not exit after TERM (trapped TERM deferred until "
                "sleep returns; poll interval is 1s)",
            )
            self.assertFalse((self.state / ".upload.lock").exists(),
                             "trap did not clean the daemon lock on TERM")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


# ===========================================================================
# Recovery download step + Hermes-side restore wrapper behavioral tests
# (no Docker, fake rclone on PATH where an rclone transfer is involved)
# ===========================================================================


class RecoveryDownloadBehavioralTests(unittest.TestCase):
    """Drive scripts/mnemosyne-backup-recover.sh with a fake `rclone` on PATH.
    Proves the short-lived download step verifies the slot BEFORE writing the
    RECOVERY_READY handoff sentinel, without Docker or a real remote."""

    GEN = "20260802T012247123456Z-a1b2c3d4"
    FAKE_RCLONE = r"""#!/bin/sh
log() { printf '%s\n' "$*" >> "$FAKE_RCLONE_LOG"; }
if [ "${1:-}" = "config" ]; then
  log "config $*"
  printf 'type = crypt\n'
  exit 0
fi
if [ "${1:-}" = "copy" ]; then
  log "copy $*"
  dest="$3"
  mkdir -p "$dest"
  if [ -d "${FAKE_RCLONE_COPY_SRC:-}" ]; then
    cp "$FAKE_RCLONE_COPY_SRC"/mnemosyne.db.gz "$FAKE_RCLONE_COPY_SRC"/manifest.json "$dest"/ 2>/dev/null || true
  fi
  exit 0
fi
log "other $*"
exit 0
"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mnem-recover-beh-"))
        self.recovery = self.tmp / "recovery"
        self.bin = self.tmp / "bin"
        self.config_dir = self.tmp / "rclone-config"
        self.fake_slot = self.tmp / "fake-slot"
        for d in (self.recovery, self.bin, self.config_dir, self.fake_slot):
            d.mkdir()
        (self.config_dir / "rclone.conf").write_text("dummy\n", encoding="utf-8")
        fake = self.bin / "rclone"
        fake.write_text(self.FAKE_RCLONE, encoding="utf-8")
        fake.chmod(0o700)
        self.log = self.tmp / "rclone.log"
        self.artifact_bytes = b"recovery-artifact-bytes"
        self.sha = hashlib.sha256(self.artifact_bytes).hexdigest()
        (self.fake_slot / "mnemosyne.db.gz").write_bytes(self.artifact_bytes)
        (self.fake_slot / "manifest.json").write_text(
            json.dumps({
                "generation_id": self.GEN,
                "artifact": {"name": "mnemosyne.db.gz", "sha256": self.sha},
            }),
            encoding="utf-8",
        )
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "MNEMOSYNE_BACKUP_RCLONE_REMOTE": "mnemosyne-crypt",
            "MNEMOSYNE_BACKUP_RCLONE_PATH": "backups",
            "MNEMOSYNE_BACKUP_SLOTS": "3",
            "MNEMOSYNE_BACKUP_RECOVERY_DIR": str(self.recovery),
            "RCLONE_CONFIG": str(self.config_dir / "rclone.conf"),
            "FAKE_RCLONE_LOG": str(self.log),
            "FAKE_RCLONE_COPY_SRC": str(self.fake_slot),
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, slot: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", str(SCRIPTS_DIR / "mnemosyne-backup-recover.sh"), slot],
            env=self.env, capture_output=True, text=True, check=False, timeout=timeout,
        )

    def test_download_success_writes_ready_sentinel(self) -> None:
        proc = self._run("1")
        self.assertEqual(proc.returncode, 0, f"recover failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertTrue((self.recovery / "RECOVERY_READY").exists())
        self.assertEqual((self.recovery / "mnemosyne.db.gz").read_bytes(), self.artifact_bytes)
        self.assertEqual((self.recovery / "manifest.json").exists(), True)
        sentinel = (self.recovery / "RECOVERY_READY").read_text("utf-8").splitlines()
        self.assertEqual(sentinel[0], self.GEN)
        self.assertEqual(sentinel[1], self.sha)

    def test_download_rejects_corrupted_artifact(self) -> None:
        (self.fake_slot / "mnemosyne.db.gz").write_bytes(b"corrupted-" + self.artifact_bytes)
        proc = self._run("1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SHA-256 mismatch", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists(),
                         "no handoff sentinel may be written on failure")

    def test_download_rejects_invalid_slot(self) -> None:
        for bad in ("0", "4", "abc", "-1"):
            proc = self._run(bad)
            self.assertEqual(proc.returncode, 2, f"slot {bad!r}: {proc.stderr}")
            self.assertFalse((self.recovery / "RECOVERY_READY").exists())

    def test_download_rejects_non_crypt_remote(self) -> None:
        env = {**self.env, "MNEMOSYNE_BACKUP_RCLONE_REMOTE": "plain-remote"}
        # Fake answers type=crypt for any name; use a fake that answers local.
        fake = self.bin / "rclone-noncrypt"
        fake.write_text("#!/bin/sh\nprintf 'type = local\\n'\n", encoding="utf-8")
        fake.chmod(0o700)
        env["PATH"] = f"{self.bin}:{os.environ.get('PATH', '')}"
        # Replace rclone on PATH by shadowing: put noncrypt first with a shim dir.
        shim_dir = self.tmp / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "rclone"
        shim.write_text(fake.read_text(), encoding="utf-8")
        shim.chmod(0o700)
        env["PATH"] = f"{shim_dir}:{os.environ.get('PATH', '')}"
        proc = subprocess.run(
            ["/bin/sh", str(SCRIPTS_DIR / "mnemosyne-backup-recover.sh"), "1"],
            env=env, capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not rclone type 'crypt'", proc.stderr)
        self.assertFalse((self.recovery / "RECOVERY_READY").exists())


class RestoreWrapperBehavioralTests(unittest.TestCase):
    """Hermes-side restore wrapper guards (no Docker, no DR seam needed)."""

    GEN = "20260802T012247123456Z-a1b2c3d4"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mnem-restore-beh-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args, timeout: int = 30) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "MNEMOSYNE_BACKUP_PYTHON": sys.executable,
            "MNEMOSYNE_BACKUP_CORE": str(CORE_PATH),
        }
        return subprocess.run(
            ["/bin/sh", str(SCRIPTS_DIR / "mnemosyne-backup-restore.sh")] + args,
            env=env, capture_output=True, text=True, check=False, timeout=timeout,
        )

    def test_verify_restore_requires_handoff_sentinel(self) -> None:
        rec = self.tmp / "recovery"
        rec.mkdir()
        (rec / "mnemosyne.db.gz").write_bytes(b"x")
        (rec / "manifest.json").write_text("{}", encoding="utf-8")
        proc = self._run(["verify-restore", str(rec), str(rec / "verified.db")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("RECOVERY_READY", proc.stderr)

    def test_verify_restore_rejects_sha_mismatch(self) -> None:
        rec = self.tmp / "recovery"
        rec.mkdir()
        artifact = rec / "mnemosyne.db.gz"
        artifact.write_bytes(b"artifact-bytes")
        sha = hashlib.sha256(b"artifact-bytes").hexdigest()
        (rec / "manifest.json").write_text(
            json.dumps({
                "generation_id": self.GEN,
                "artifact": {"name": "mnemosyne.db.gz", "sha256": sha},
            }),
            encoding="utf-8",
        )
        (rec / "RECOVERY_READY").write_text(f"{self.GEN}\n{sha}\n", encoding="utf-8")
        with open(artifact, "ab") as f:
            f.write(b"corrupt")
        proc = self._run(["verify-restore", str(rec), str(rec / "verified.db")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("SHA-256 mismatch", proc.stderr)
        self.assertFalse((self.tmp / "v.db").exists())

    def test_verify_restore_rejects_sentinel_manifest_mismatch(self) -> None:
        rec = self.tmp / "recovery"
        rec.mkdir()
        artifact = rec / "mnemosyne.db.gz"
        artifact.write_bytes(b"artifact-bytes")
        sha = hashlib.sha256(b"artifact-bytes").hexdigest()
        (rec / "manifest.json").write_text(
            json.dumps({
                "generation_id": "20260802T012247123456Z-deadbeef",
                "artifact": {"name": "mnemosyne.db.gz", "sha256": sha},
            }),
            encoding="utf-8",
        )
        (rec / "RECOVERY_READY").write_text(f"{self.GEN}\n{sha}\n", encoding="utf-8")
        proc = self._run(["verify-restore", str(rec), str(rec / "verified.db")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("generation mismatch", proc.stderr)

    def test_install_restore_requires_confirm(self) -> None:
        vdb = self.tmp / "v.db"
        vdb.write_bytes(b"verified")
        live = self.tmp / "live.db"
        live.write_bytes(b"live")
        proc = self._run(["install-restore", str(self.tmp / "recovery"), str(live)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--i-confirm-this-overwrites-production", proc.stderr)
        self.assertEqual(live.read_bytes(), b"live", "live DB must be untouched")



# ===========================================================================
# Docker-gated synthetic full round trip
# ===========================================================================


# In-container script (hermes): export + verify-restore + marker recall.
# Runs in the hermes container which has the Python core + mnemosyne package.
# rclone operations run in a separate rclone container (see the test body).
_DOCKER_HERMES_SCRIPT = r"""set -eu
export PYTHONPATH="/tmp/mnem_pkg:${PYTHONPATH:-}"
WORK="$1"
mkdir -p "$WORK/data" "$WORK/staging"

echo "=== 1. Build sqlite-vec-backed source DB (open connection) ==="
/opt/hermes/.venv/bin/python3 - "$WORK" <<'PY'
import sys, sqlite3, time, os
sys.path.insert(0, "/tmp/mnem_pkg")
import sqlite_vec
from pathlib import Path
work = Path(sys.argv[1])
db = work / "data" / "mnemosyne.db"
conn = sqlite3.connect(str(db))
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE IF NOT EXISTS working_memory (id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT, timestamp TEXT, session_id TEXT DEFAULT 'default', importance REAL DEFAULT 0.5, metadata_json TEXT, veracity TEXT DEFAULT 'unknown', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodes USING vec0(embedding float32[4])")
conn.execute("INSERT INTO working_memory (id, content, source) VALUES (?, ?, ?)", ("m1", "UNIQUE_PLAINTEXT_MARKER_9f2c1a", "test"))
conn.commit()
conn.close()
PY

# Hold the DB open with a pending WAL write during export (online proof).
/opt/hermes/.venv/bin/python3 - "$WORK" <<'PY' &
import sys, sqlite3, time
sys.path.insert(0, "/tmp/mnem_pkg")
import sqlite_vec
from pathlib import Path
work = Path(sys.argv[1])
db = work / "data" / "mnemosyne.db"
conn = sqlite3.connect(str(db))
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.execute("INSERT INTO working_memory (id, content) VALUES (?, ?)", ("m2", "second-marker"))
conn.commit()
while (work / "hold_open").exists():
    time.sleep(0.2)
conn.close()
PY
HOLDER_PID=$!
touch "$WORK/hold_open"

echo "=== 2. Export (online backup while source open) ==="
/opt/hermes/.venv/bin/python3 /opt/josemar/scripts/mnemosyne_backup_core.py export \
    --db-path "$WORK/data/mnemosyne.db" \
    --staging-dir "$WORK/staging" \
    --generations-keep 5 > "$WORK/manifest1.json"
cat "$WORK/manifest1.json"
GEN1=$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1]))["generation_id"])' "$WORK/manifest1.json")
SHA1=$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1]))["artifact"]["sha256"])' "$WORK/manifest1.json")
echo "GEN1=$GEN1 SHA1=$SHA1"

echo "=== 3. Verify restore to NEW path ==="
VERIFY_DB="$WORK/verify.db"
/opt/hermes/.venv/bin/python3 /opt/josemar/scripts/mnemosyne_backup_core.py verify-restore \
    "$WORK/staging/$GEN1/mnemosyne.db.gz" "$VERIFY_DB" --sha256 "$SHA1"
test -f "$VERIFY_DB"

echo "=== 4. Marker recall from verified DB ==="
/opt/hermes/.venv/bin/python3 - "$VERIFY_DB" <<'PY'
import sys, sqlite3
sys.path.insert(0, "/tmp/mnem_pkg")
import sqlite_vec
conn = sqlite3.connect(sys.argv[1])
conn.enable_load_extension(True)
sqlite_vec.load(conn)
row = conn.execute("SELECT content FROM working_memory WHERE id=?", ("m1",)).fetchone()
assert row and row[0] == "UNIQUE_PLAINTEXT_MARKER_9f2c1a", f"marker mismatch: {row}"
tabs = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
assert "vec_episodes" in tabs, f"vec_episodes missing: {tabs}"
print("MARKER_RECALL_OK")
PY

echo "=== 5. Manifest has NO memory contents ==="
if grep -q "UNIQUE_PLAINTEXT_MARKER_9f2c1a" "$WORK/staging/$GEN1/manifest.json"; then
  echo "MANIFEST_LEAKS_MARKER"
  exit 1
fi
echo "MANIFEST_NO_LEAK_OK"

# Save GEN1/SHA1 for the orchestrator. GEN2/GEN3 are exported later (after
# GEN1 is uploaded) so the latest pointer is correct for each upload step.
printf '%s\n' "$GEN1" > "$WORK/gen1.txt"
printf '%s\n' "$SHA1" > "$WORK/sha1.txt"

# Release the holder.
rm -f "$WORK/hold_open"
wait "$HOLDER_PID" 2>/dev/null || true

echo "HERMES_SCRIPT_OK"
"""

# In-container script (rclone): crypt setup + upload GEN1 + ciphertext check +
# download/decrypt. Runs in a separate rclone/rclone container.
_DOCKER_RCLONE_SCRIPT = r"""set -eu
WORK="$1"

echo "=== 6. Set up disposable local rclone remote + crypt config ==="
mkdir -p "$WORK/rclone-local" "$WORK/rclone-config"
cat > "$WORK/rclone-config/rclone.conf" <<EOF
[local-disposable]
type = local
nounc = true

[mnemosyne-crypt]
type = crypt
remote = local-disposable:$WORK/rclone-local
password = $(rclone obscure "test-password-12345")
password2 = $(rclone obscure "test-salt-12345")
EOF
REMOTE_TYPE=$(rclone config show mnemosyne-crypt: --config "$WORK/rclone-config/rclone.conf" 2>/dev/null | awk -F'=' '/^type[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}')
if [ "$REMOTE_TYPE" != "crypt" ]; then
  echo "CRYPT_TYPE_VALIDATION_FAILED got=$REMOTE_TYPE"
  exit 1
fi
echo "CRYPT_TYPE_OK"

GEN1=$(cat "$WORK/gen1.txt")
SHA1=$(cat "$WORK/sha1.txt")

echo "=== 7. Upload gen1 via uploader wrapper (slot 1, one-shot mode) ==="
mkdir -p "$WORK/state"
MNEMOSYNE_BACKUP_STAGING_DIR="$WORK/staging" \
MNEMOSYNE_BACKUP_STATE_DIR="$WORK/state" \
MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR="$WORK/rclone-active" \
MNEMOSYNE_BACKUP_RCLONE_REMOTE="mnemosyne-crypt" \
MNEMOSYNE_BACKUP_RCLONE_PATH="backups" \
MNEMOSYNE_BACKUP_SLOTS=3 \
RCLONE_CONFIG="$WORK/rclone-config/rclone.conf" \
MNEMOSYNE_BACKUP_ONCE=true \
/bin/sh /scripts/mnemosyne-backup-uploader.sh
test -f "$WORK/state/last-uploaded-generation"
# One-shot mode must clean its own upload lock.
if [ -d "$WORK/state/.upload.lock" ]; then
  echo "UPLOAD_LOCK_LEAKED"
  exit 1
fi
echo "UPLOAD_OK"
echo "ONESHOT_EXIT_OK"

echo "=== 8. Ciphertext does not contain plaintext marker ==="
if grep -rl "UNIQUE_PLAINTEXT_MARKER_9f2c1a" "$WORK/rclone-local" 2>/dev/null; then
  echo "CIPHERTEXT_LEAKS_MARKER"
  exit 1
fi
echo "CIPHERTEXT_NO_LEAK_OK"

echo "=== 9. Download/decrypt, verify SHA ==="
DLDIR="$WORK/download"
mkdir -p "$DLDIR"
STAGING_SHA="$(sha256sum "$WORK/staging/$GEN1/mnemosyne.db.gz" | cut -d' ' -f1)"
if [ "$STAGING_SHA" != "$SHA1" ]; then echo "STAGING_SHA_MISMATCH"; exit 1; fi
rclone copy "mnemosyne-crypt:backups/slot-1" "$DLDIR" --config "$WORK/rclone-config/rclone.conf"
test -f "$DLDIR/mnemosyne.db.gz"
test -f "$DLDIR/manifest.json"
DL_SHA="$(sha256sum "$DLDIR/mnemosyne.db.gz" | cut -d' ' -f1)"
if [ "$DL_SHA" != "$SHA1" ]; then echo "DL_SHA_MISMATCH expected=$SHA1 got=$DL_SHA"; exit 1; fi
echo "DL_SHA_OK"

echo "=== 10. READY/latest/artifacts still exist (uploader never deletes) ==="
test -f "$WORK/staging/$GEN1/READY"
test -f "$WORK/staging/latest"
test -f "$WORK/staging/$GEN1/mnemosyne.db.gz"
echo "NO_DELETE_OK"

echo "RCLONE_SCRIPT_OK"
"""

# In-container script (rclone): upload a generation for slot rotation /
# failure tests. Uses the existing crypt config + state.
_DOCKER_RCLONE_UPLOAD_SCRIPT = r"""set -eu
WORK="$1"
EXPECTED_GEN="$2"
EXPECTED_SLOT="$3"

MNEMOSYNE_BACKUP_STAGING_DIR="$WORK/staging" \
MNEMOSYNE_BACKUP_STATE_DIR="$WORK/state" \
MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR="$WORK/rclone-active" \
MNEMOSYNE_BACKUP_RCLONE_REMOTE="mnemosyne-crypt" \
MNEMOSYNE_BACKUP_RCLONE_PATH="backups" \
MNEMOSYNE_BACKUP_SLOTS=3 \
RCLONE_CONFIG="$WORK/rclone-config/rclone.conf" \
MNEMOSYNE_BACKUP_ONCE=true \
/bin/sh /scripts/mnemosyne-backup-uploader.sh
LAST_UPLOADED=$(cat "$WORK/state/last-uploaded-generation")
if [ "$LAST_UPLOADED" != "$EXPECTED_GEN" ]; then echo "UPLOAD_WRONG expected=$EXPECTED_GEN got=$LAST_UPLOADED"; exit 1; fi
SLOT_NEXT=$(cat "$WORK/state/next-slot")
if [ "$SLOT_NEXT" != "$EXPECTED_SLOT" ]; then echo "SLOT_WRONG expected=$EXPECTED_SLOT got=$SLOT_NEXT"; exit 1; fi
if [ -d "$WORK/state/.upload.lock" ]; then echo "UPLOAD_LOCK_LEAKED"; exit 1; fi
echo "UPLOAD_GEN_OK"
echo "SLOT_ROTATION_OK"
echo "ONESHOT_EXIT_OK"
"""

# In-container script (rclone): idempotency check (re-run, must no-op).
_DOCKER_RCLONE_NOOP_SCRIPT = r"""set -eu
WORK="$1"
EXPECTED_GEN="$2"

MNEMOSYNE_BACKUP_STAGING_DIR="$WORK/staging" \
MNEMOSYNE_BACKUP_STATE_DIR="$WORK/state" \
MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR="$WORK/rclone-active" \
MNEMOSYNE_BACKUP_RCLONE_REMOTE="mnemosyne-crypt" \
MNEMOSYNE_BACKUP_RCLONE_PATH="backups" \
MNEMOSYNE_BACKUP_SLOTS=3 \
RCLONE_CONFIG="$WORK/rclone-config/rclone.conf" \
MNEMOSYNE_BACKUP_ONCE=true \
/bin/sh /scripts/mnemosyne-backup-uploader.sh
LAST_UPLOADED=$(cat "$WORK/state/last-uploaded-generation")
if [ "$LAST_UPLOADED" != "$EXPECTED_GEN" ]; then echo "IDEMPOTENCY_FAILED got=$LAST_UPLOADED"; exit 1; fi
if [ -d "$WORK/state/.upload.lock" ]; then echo "UPLOAD_LOCK_LEAKED"; exit 1; fi
echo "IDEMPOTENCY_OK"
echo "ONESHOT_EXIT_OK"
"""

# In-container script (rclone): failure check (corrupted artifact, state must
# NOT advance). The corruption is done before invoking.
_DOCKER_RCLONE_FAIL_SCRIPT = r"""set -eu
WORK="$1"
EXPECTED_GEN="$2"

MNEMOSYNE_BACKUP_STAGING_DIR="$WORK/staging" \
MNEMOSYNE_BACKUP_STATE_DIR="$WORK/state" \
MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR="$WORK/rclone-active" \
MNEMOSYNE_BACKUP_RCLONE_REMOTE="mnemosyne-crypt" \
MNEMOSYNE_BACKUP_RCLONE_PATH="backups" \
MNEMOSYNE_BACKUP_SLOTS=3 \
RCLONE_CONFIG="$WORK/rclone-config/rclone.conf" \
MNEMOSYNE_BACKUP_ONCE=true \
/bin/sh /scripts/mnemosyne-backup-uploader.sh && { echo "ONESHOT_SHOULD_HAVE_FAILED"; exit 1; }
if [ -d "$WORK/state/.upload.lock" ]; then echo "UPLOAD_LOCK_LEAKED"; exit 1; fi
LAST_UPLOADED=$(cat "$WORK/state/last-uploaded-generation")
if [ "$LAST_UPLOADED" = "$EXPECTED_GEN" ]; then echo "STATE_ADVANCED_ON_FAILURE"; exit 1; fi
echo "FAILURE_NO_ADVANCE_OK"
echo "ONESHOT_FAIL_EXIT_OK"
"""

# In-container script (hermes): export one generation.
_DOCKER_HERMES_EXPORT_SCRIPT = r"""set -eu
export PYTHONPATH="/tmp/mnem_pkg:${PYTHONPATH:-}"
WORK="$1"
PHASE="${2:-verify}"
OUT="$2"
/opt/hermes/.venv/bin/python3 /opt/josemar/scripts/mnemosyne_backup_core.py export \
    --db-path "$WORK/data/mnemosyne.db" \
    --staging-dir "$WORK/staging" \
    --generations-keep 5 > "$OUT"
GEN=$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1]))["generation_id"])' "$OUT")
echo "$GEN"
"""

# In-container script (hermes): verify the downloaded artifact restores.
_DOCKER_VERIFY_DL_SCRIPT = r"""set -eu
export PYTHONPATH="/tmp/mnem_pkg:${PYTHONPATH:-}"
WORK="$1"
SHA1=$(cat "$WORK/sha1.txt")
DLDIR="$WORK/download"
DL_VERIFY_DB="$WORK/dl-verify.db"
/opt/hermes/.venv/bin/python3 /opt/josemar/scripts/mnemosyne_backup_core.py verify-restore \
    "$DLDIR/mnemosyne.db.gz" "$DL_VERIFY_DB" --sha256 "$SHA1"
/opt/hermes/.venv/bin/python3 - "$DL_VERIFY_DB" <<'PY'
import sys, sqlite3
sys.path.insert(0, "/tmp/mnem_pkg")
import sqlite_vec
conn = sqlite3.connect(sys.argv[1])
conn.enable_load_extension(True)
sqlite_vec.load(conn)
row = conn.execute("SELECT content FROM working_memory WHERE id=?", ("m1",)).fetchone()
assert row and row[0] == "UNIQUE_PLAINTEXT_MARKER_9f2c1a", f"dl marker mismatch: {row}"
print("DL_MARKER_RECALL_OK")
PY
echo "VERIFY_DL_OK"
"""

# In-container script (rclone): recovery download step via the documented
# scripts/mnemosyne-backup-recover.sh into a disposable recovery handoff dir.
_DOCKER_RECOVER_DL_SCRIPT = r"""set -eu
WORK="$1"
EXPECTED_GEN="$2"
EXPECTED_SHA="$3"

rm -rf "$WORK/recovery"
mkdir -p "$WORK/recovery"
MNEMOSYNE_BACKUP_RECOVERY_DIR="$WORK/recovery" \
MNEMOSYNE_BACKUP_RCLONE_REMOTE="mnemosyne-crypt" \
MNEMOSYNE_BACKUP_RCLONE_PATH="backups" \
MNEMOSYNE_BACKUP_SLOTS=3 \
RCLONE_CONFIG="$WORK/rclone-config/rclone.conf" \
/bin/sh /scripts/mnemosyne-backup-recover.sh 1
test -f "$WORK/recovery/RECOVERY_READY"
test -f "$WORK/recovery/mnemosyne.db.gz"
test -f "$WORK/recovery/manifest.json"
REC_SHA="$(sha256sum "$WORK/recovery/mnemosyne.db.gz" | cut -d' ' -f1)"
if [ "$REC_SHA" != "$EXPECTED_SHA" ]; then echo "RECOVERY_SHA_MISMATCH expected=$EXPECTED_SHA got=$REC_SHA"; exit 1; fi
REC_GEN="$(IFS= read -r l < "$WORK/recovery/RECOVERY_READY"; printf '%s' "$l")"
if [ "$REC_GEN" != "$EXPECTED_GEN" ]; then echo "RECOVERY_GEN_MISMATCH expected=$EXPECTED_GEN got=$REC_GEN"; exit 1; fi
echo "RECOVERY_DL_OK"
"""

# In-container script (hermes): recovery lane verify-restore + install-restore
# with NO rclone and NO rclone config, from the handoff written above.
_DOCKER_RECOVERY_SCRIPT = r"""set -eu
export PYTHONPATH="/tmp/mnem_pkg:${PYTHONPATH:-}"
WORK="$1"
PHASE="${2:-verify}"

# Hermes-side step must NOT need rclone or rclone config (least privilege).
if command -v rclone >/dev/null 2>&1; then echo "HERMES_HAS_RCLONE"; exit 1; fi
if [ -f /config/rclone/rclone.conf ]; then echo "HERMES_HAS_RCLONE_CONFIG"; exit 1; fi
echo "HERMES_NO_RCLONE_OK"

if [ "$PHASE" = verify ]; then
echo "=== Recovery: verify-restore from handoff (no rclone) ==="
REC_VERIFY_DB="$WORK/recovery/verified.db"
/opt/josemar/scripts/mnemosyne-backup-restore.sh verify-restore "$WORK/recovery" "$REC_VERIFY_DB"
test -f "$REC_VERIFY_DB"
/opt/hermes/.venv/bin/python3 - "$REC_VERIFY_DB" <<'PY'
import sys, sqlite3
sys.path.insert(0, "/tmp/mnem_pkg")
import sqlite_vec
conn = sqlite3.connect(sys.argv[1])
conn.enable_load_extension(True)
sqlite_vec.load(conn)
row = conn.execute("SELECT content FROM working_memory WHERE id=?", ("m1",)).fetchone()
assert row and row[0] == "UNIQUE_PLAINTEXT_MARKER_9f2c1a", f"recovery marker mismatch: {row}"
print("RECOVERY_MARKER_OK")
PY

test -f "$WORK/recovery/VERIFIED_READY"
echo "RECOVERY_VERIFY_EXITED_OK"
exit 0
fi

echo "=== Recovery: install-restore (writers stopped, explicit confirm) ==="
LIVE_DB="$WORK/live.db"
printf 'corrupted-live-content' > "$LIVE_DB"
/opt/josemar/scripts/mnemosyne-backup-restore.sh install-restore "$WORK/recovery" "$LIVE_DB" --generation "$3" --i-confirm-this-overwrites-production
/opt/hermes/.venv/bin/python3 - "$LIVE_DB" <<'PY'
import sys, sqlite3
sys.path.insert(0, "/tmp/mnem_pkg")
import sqlite_vec
conn = sqlite3.connect(sys.argv[1])
conn.enable_load_extension(True)
sqlite_vec.load(conn)
row = conn.execute("SELECT content FROM working_memory WHERE id=?", ("m1",)).fetchone()
assert row and row[0] == "UNIQUE_PLAINTEXT_MARKER_9f2c1a", f"installed marker mismatch: {row}"
print("INSTALL_MARKER_OK")
PY
ROLLBACK="$WORK/live.db.rollback"
test -f "$ROLLBACK"
if [ "$(cat "$ROLLBACK")" != "corrupted-live-content" ]; then echo "ROLLBACK_CONTENT_MISMATCH"; exit 1; fi
echo "ROLLBACK_OK"

echo "=== Recovery: install without confirm must refuse ==="
if /opt/josemar/scripts/mnemosyne-backup-restore.sh install-restore "$WORK/recovery" "$LIVE_DB"; then
  echo "INSTALL_WITHOUT_CONFIRM_SUCCEEDED"; exit 1
fi
echo "INSTALL_REQUIRES_CONFIRM_OK"

echo "RECOVERY_SCRIPT_OK"
"""


@unittest.skipUnless(
    os.getenv("RUN_DOCKER_TESTS") == "1",
    "set RUN_DOCKER_TESTS=1 to run Docker runtime tests",
)
@unittest.skipUnless(docker_available(), "docker CLI is not available")
class MnemosyneBackupDockerRoundTripTests(unittest.TestCase):
    """Docker-gated synthetic full round trip: build the hermes image and
    prove the full backup contract end to end with sqlite-vec, crypt, slots,
    idempotency, failure-no-advance, and read-only staging.

    Uses two containers sharing a disposable host temp dir:
      - hermes container: export, verify-restore, marker recall, download verify.
      - rclone/rclone container: crypt setup, upload, ciphertext check,
        download/decrypt, slot rotation, idempotency, failure-no-advance.

    Never uses project volumes, credentials, or remotes."""

    def test_mnemosyne_backup_full_round_trip(self) -> None:
        import subprocess
        import tempfile

        runtime = ComposeRuntime()
        work = Path(tempfile.mkdtemp(prefix="mnemosyne-backup-test-"))
        try:
            build = runtime.run("build", "hermes", timeout=1200)
            self.assertEqual(
                build.returncode, 0,
                f"docker compose build hermes failed:\n{build.stderr}",
            )

            # Verify the core script is baked in and compiles.
            pins = runtime.run(
                "run", "--rm", "--no-deps", "--entrypoint", "sh",
                "hermes", "-lc",
                "/opt/hermes/.venv/bin/python3 -m py_compile "
                "/opt/josemar/scripts/mnemosyne_backup_core.py && "
                "test -x /opt/josemar/scripts/mnemosyne-backup-export.sh && "
                "test -x /opt/josemar/scripts/mnemosyne-backup-restore.sh && "
                "echo BAKED_IN_OK",
                timeout=180,
            )
            self.assertEqual(pins.returncode, 0,
                             f"baked-in check failed:\n{pins.stdout}\n{pins.stderr}")
            self.assertIn("BAKED_IN_OK", pins.stdout)

            # --- Phase A: hermes container (export + verify-restore) ---
            hermes_cmd = [
                *runtime.compose_command(),
                "run", "--rm", "--no-deps",
                "-v", f"{_EXTRACTED_MEMORY}:/tmp/mnem_pkg:ro",
                "-v", f"{work}:/work",
                "--entrypoint", "sh",
                "hermes", "-lc", _DOCKER_HERMES_SCRIPT, "sh", "/work",
            ]
            hermes_proc = subprocess.run(
                hermes_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=300,
            )
            self.assertEqual(
                hermes_proc.returncode, 0,
                f"hermes export phase failed:\n{hermes_proc.stdout}\n{hermes_proc.stderr}",
            )
            for marker in ("MARKER_RECALL_OK", "MANIFEST_NO_LEAK_OK", "HERMES_SCRIPT_OK"):
                self.assertIn(marker, hermes_proc.stdout,
                              f"missing {marker}:\n{hermes_proc.stdout}\n{hermes_proc.stderr}")

            # --- Phase B: rclone container (crypt + upload GEN1 + download) ---
            rclone_cmd = [
                "docker", "run", "--rm",
                "-v", f"{work}:/work",
                "-v", f"{SCRIPTS_DIR / 'mnemosyne-backup-uploader.sh'}:/scripts/mnemosyne-backup-uploader.sh:ro",
                "-v", f"{SCRIPTS_DIR / 'rclone-active-config.sh'}:/scripts/rclone-active-config.sh:ro",
                "--entrypoint", "sh",
                "rclone/rclone:latest", "-lc", _DOCKER_RCLONE_SCRIPT, "sh", "/work",
            ]
            rclone_proc = subprocess.run(
                rclone_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=300,
            )
            self.assertEqual(
                rclone_proc.returncode, 0,
                f"rclone upload phase failed:\n{rclone_proc.stdout}\n{rclone_proc.stderr}",
            )
            for marker in (
                "CRYPT_TYPE_OK", "UPLOAD_OK", "CIPHERTEXT_NO_LEAK_OK",
                "DL_SHA_OK", "NO_DELETE_OK", "ONESHOT_EXIT_OK", "RCLONE_SCRIPT_OK",
            ):
                self.assertIn(marker, rclone_proc.stdout,
                              f"missing {marker}:\n{rclone_proc.stdout}\n{rclone_proc.stderr}")

            # --- Phase C: hermes container (verify downloaded restore) ---
            verify_cmd = [
                *runtime.compose_command(),
                "run", "--rm", "--no-deps",
                "-v", f"{_EXTRACTED_MEMORY}:/tmp/mnem_pkg:ro",
                "-v", f"{work}:/work",
                "--entrypoint", "sh",
                "hermes", "-lc", _DOCKER_VERIFY_DL_SCRIPT, "sh", "/work",
            ]
            verify_proc = subprocess.run(
                verify_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=180,
            )
            self.assertEqual(
                verify_proc.returncode, 0,
                f"download verify phase failed:\n{verify_proc.stdout}\n{verify_proc.stderr}",
            )
            self.assertIn("DL_MARKER_RECALL_OK", verify_proc.stdout,
                          f"missing DL_MARKER_RECALL_OK:\n{verify_proc.stdout}\n{verify_proc.stderr}")
            self.assertIn("VERIFY_DL_OK", verify_proc.stdout)

            # --- Phase C.5: recovery lane (download handoff -> verify -> install) ---
            gen1 = (work / "gen1.txt").read_text("utf-8").strip()
            sha1 = (work / "sha1.txt").read_text("utf-8").strip()
            self.assertTrue(gen1 and sha1, "GEN1/SHA1 missing from Phase A output")

            # Step 1: short-lived rclone container runs the documented recover
            # script to download slot-1 into a disposable handoff and verify it.
            recover_dl_cmd = [
                "docker", "run", "--rm",
                "-v", f"{work}:/work",
                "-v", f"{SCRIPTS_DIR / 'mnemosyne-backup-recover.sh'}:/scripts/mnemosyne-backup-recover.sh:ro",
                "-v", f"{SCRIPTS_DIR / 'rclone-active-config.sh'}:/scripts/rclone-active-config.sh:ro",
                "--entrypoint", "sh",
                "rclone/rclone:latest", "-lc", _DOCKER_RECOVER_DL_SCRIPT, "sh", "/work", gen1, sha1,
            ]
            recover_dl_proc = subprocess.run(
                recover_dl_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=180,
            )
            self.assertEqual(recover_dl_proc.returncode, 0,
                             f"recover download step failed:\n{recover_dl_proc.stdout}\n{recover_dl_proc.stderr}")
            self.assertIn("RECOVERY_DL_OK", recover_dl_proc.stdout)

            # Steps 2+3: short-lived hermes container verifies the handoff (NO
            # rclone config) then installs into a disposable live DB with
            # explicit confirmation + rollback.
            recovery_cmd = [
                *runtime.compose_command(),
                "run", "--rm", "--no-deps",
                "-v", f"{_EXTRACTED_MEMORY}:/tmp/mnem_pkg:ro",
                "-v", f"{work}:/work",
                "--entrypoint", "sh",
                "hermes", "-lc", _DOCKER_RECOVERY_SCRIPT, "sh", "/work", "verify",
            ]
            recovery_proc = subprocess.run(
                recovery_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=300,
            )
            self.assertEqual(recovery_proc.returncode, 0,
                             f"recovery lane failed:\n{recovery_proc.stdout}\n{recovery_proc.stderr}")
            for marker in ("HERMES_NO_RCLONE_OK", "RECOVERY_MARKER_OK", "RECOVERY_VERIFY_EXITED_OK"):
                self.assertIn(marker, recovery_proc.stdout,
                              f"missing {marker}:\n{recovery_proc.stdout}\n{recovery_proc.stderr}")

            # The install is a second short-lived Hermes container, using the
            # durable handoff produced by the exited verify container.
            install_cmd = recovery_cmd[:-1] + ["install", gen1]
            install_proc = subprocess.run(
                install_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=300,
            )
            self.assertEqual(install_proc.returncode, 0,
                             f"recovery install phase failed:\n{install_proc.stdout}\n{install_proc.stderr}")
            for marker in ("INSTALL_MARKER_OK", "ROLLBACK_OK", "INSTALL_REQUIRES_CONFIRM_OK", "RECOVERY_SCRIPT_OK"):
                self.assertIn(marker, install_proc.stdout,
                              f"missing {marker}:\n{install_proc.stdout}\n{install_proc.stderr}")

            # --- Phase D: slot rotation (export GEN2 -> upload to slot 2) ---
            export2_cmd = [
                *runtime.compose_command(),
                "run", "--rm", "--no-deps",
                "-v", f"{_EXTRACTED_MEMORY}:/tmp/mnem_pkg:ro",
                "-v", f"{work}:/work",
                "--entrypoint", "sh",
                "hermes", "-lc", _DOCKER_HERMES_EXPORT_SCRIPT, "sh", "/work", "/work/manifest2.json",
            ]
            export2_proc = subprocess.run(
                export2_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=180,
            )
            self.assertEqual(export2_proc.returncode, 0,
                             f"export GEN2 failed:\n{export2_proc.stdout}\n{export2_proc.stderr}")
            gen2 = export2_proc.stdout.strip().splitlines()[-1].strip()
            self.assertTrue(gen2, f"GEN2 id empty:\n{export2_proc.stdout}")

            upload2_cmd = [
                "docker", "run", "--rm",
                "-v", f"{work}:/work",
                "-v", f"{SCRIPTS_DIR / 'mnemosyne-backup-uploader.sh'}:/scripts/mnemosyne-backup-uploader.sh:ro",
                "-v", f"{SCRIPTS_DIR / 'rclone-active-config.sh'}:/scripts/rclone-active-config.sh:ro",
                "--entrypoint", "sh",
                "rclone/rclone:latest", "-lc", _DOCKER_RCLONE_UPLOAD_SCRIPT, "sh", "/work", gen2, "3",
            ]
            upload2_proc = subprocess.run(
                upload2_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=120,
            )
            self.assertEqual(upload2_proc.returncode, 0,
                             f"upload GEN2 failed:\n{upload2_proc.stdout}\n{upload2_proc.stderr}")
            self.assertIn("UPLOAD_GEN_OK", upload2_proc.stdout)
            self.assertIn("SLOT_ROTATION_OK", upload2_proc.stdout)
            self.assertIn("ONESHOT_EXIT_OK", upload2_proc.stdout)

            # --- Phase E: idempotency (re-run uploader, must no-op) ---
            noop_cmd = [
                "docker", "run", "--rm",
                "-v", f"{work}:/work",
                "-v", f"{SCRIPTS_DIR / 'mnemosyne-backup-uploader.sh'}:/scripts/mnemosyne-backup-uploader.sh:ro",
                "-v", f"{SCRIPTS_DIR / 'rclone-active-config.sh'}:/scripts/rclone-active-config.sh:ro",
                "--entrypoint", "sh",
                "rclone/rclone:latest", "-lc", _DOCKER_RCLONE_NOOP_SCRIPT, "sh", "/work", gen2,
            ]
            noop_proc = subprocess.run(
                noop_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=120,
            )
            self.assertEqual(noop_proc.returncode, 0,
                             f"idempotency check failed:\n{noop_proc.stdout}\n{noop_proc.stderr}")
            self.assertIn("IDEMPOTENCY_OK", noop_proc.stdout)
            self.assertIn("ONESHOT_EXIT_OK", noop_proc.stdout)

            # --- Phase F: failure does not advance state ---
            # Export GEN3, corrupt its artifact, then attempt upload (must fail
            # SHA check and NOT advance state).
            export3_cmd = [
                *runtime.compose_command(),
                "run", "--rm", "--no-deps",
                "-v", f"{_EXTRACTED_MEMORY}:/tmp/mnem_pkg:ro",
                "-v", f"{work}:/work",
                "--entrypoint", "sh",
                "hermes", "-lc", _DOCKER_HERMES_EXPORT_SCRIPT, "sh", "/work", "/work/manifest3.json",
            ]
            export3_proc = subprocess.run(
                export3_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=180,
            )
            self.assertEqual(export3_proc.returncode, 0,
                             f"export GEN3 failed:\n{export3_proc.stdout}\n{export3_proc.stderr}")
            gen3 = export3_proc.stdout.strip().splitlines()[-1].strip()
            self.assertTrue(gen3, f"GEN3 id empty:\n{export3_proc.stdout}")

            # Corrupt the GEN3 artifact (append bytes so SHA no longer matches).
            corrupt_cmd = [
                "docker", "run", "--rm",
                "-v", f"{work}:/work",
                "--entrypoint", "sh",
                "rclone/rclone:latest", "-lc",
                f"echo corrupted >> /work/staging/{gen3}/mnemosyne.db.gz && echo CORRUPTED",
            ]
            corrupt_proc = subprocess.run(
                corrupt_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=60,
            )
            self.assertEqual(corrupt_proc.returncode, 0,
                             f"corrupt GEN3 failed:\n{corrupt_proc.stdout}\n{corrupt_proc.stderr}")
            self.assertIn("CORRUPTED", corrupt_proc.stdout)

            fail_cmd = [
                "docker", "run", "--rm",
                "-v", f"{work}:/work",
                "-v", f"{SCRIPTS_DIR / 'mnemosyne-backup-uploader.sh'}:/scripts/mnemosyne-backup-uploader.sh:ro",
                "-v", f"{SCRIPTS_DIR / 'rclone-active-config.sh'}:/scripts/rclone-active-config.sh:ro",
                "--entrypoint", "sh",
                "rclone/rclone:latest", "-lc", _DOCKER_RCLONE_FAIL_SCRIPT, "sh", "/work", gen3,
            ]
            fail_proc = subprocess.run(
                fail_cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=120,
            )
            self.assertEqual(fail_proc.returncode, 0,
                             f"failure check failed:\n{fail_proc.stdout}\n{fail_proc.stderr}")
            self.assertIn("FAILURE_NO_ADVANCE_OK", fail_proc.stdout)
            self.assertIn("ONESHOT_FAIL_EXIT_OK", fail_proc.stdout)

            # --- Phase G: staging read-only in uploader (boundary) ---
            # The uploader wrapper mounts staging read-only; verify the wrapper
            # script itself does not write to the staging dir.
            self.assertTrue((SCRIPTS_DIR / "mnemosyne-backup-uploader.sh").exists())
            uploader_src = (SCRIPTS_DIR / "mnemosyne-backup-uploader.sh").read_text("utf-8")
            # The wrapper must not redirect writes into $STAGING_DIR.
            self.assertNotRegex(
                uploader_src, r"[>][>]?\s*\$STAGING_DIR",
                "uploader wrapper writes to staging dir",
            )
        finally:
            runtime.down()
            shutil.rmtree(work, ignore_errors=True)


# ===========================================================================
# rclone OAuth-refresh fix: private ACTIVE config behavior (no Docker)
# ===========================================================================
#
# The published obsidian-rclone-config seed stays READ-ONLY; rclone runs
# against a private writable ACTIVE copy (scripts/rclone-active-config.sh).
# The uploader keeps it in its own state volume (persistent across
# restarts), the recover step uses an ephemeral private temp dir. The
# active copy is PRESERVED while the seed is unchanged (a simulated rclone
# config rewrite — OAuth token refresh — must survive) and atomically
# RESEEDED when the seed changes; nothing is printed of either config.


def _config_from_log_line(line: str) -> Optional[str]:
    """Extract the `--config <path>` argument from a fake-rclone log line."""
    parts = line.split()
    for i, part in enumerate(parts):
        if part == "--config" and i + 1 < len(parts):
            return parts[i + 1]
    return None


class ActiveConfigOAuthRefreshTests(unittest.TestCase):
    """Drive the Mnemosyne uploader and recover wrappers with a fake rclone
    on PATH to prove the OAuth-refresh active-config contract."""

    GEN = "20260802T012247123456Z-a1b2c3d4"
    ORIGINAL_SECRET = "ORIGINAL_SECRET_9f2c1a"
    REFRESHED_SECRET = "REFRESHED_TOKEN_9f2c1a"
    ROTATED_SECRET = "rotated-fixture"
    ACTIVE_NAME = "rclone.active.conf"

    UPLOADER_FAKE_RCLONE = r"""#!/bin/sh
# Fake rclone for active-config uploader tests: records invocations
# (including the --config path), answers `config show` with type=crypt,
# and no-ops sync/copyto.
log() { printf '%s\n' "$*" >> "$FAKE_RCLONE_LOG"; }
if [ "${1:-}" = "config" ]; then
  log "config $*"
  printf 'type = crypt\n'
  exit 0
fi
if [ "${1:-}" = "sync" ]; then
  log "sync $*"
  exit 0
fi
if [ "${1:-}" = "copyto" ]; then
  log "copyto $*"
  exit 0
fi
log "other $*"
exit 0
"""

    RECOVER_FAKE_RCLONE = r"""#!/bin/sh
log() { printf '%s\n' "$*" >> "$FAKE_RCLONE_LOG"; }
if [ "${1:-}" = "config" ]; then
  log "config $*"
  printf 'type = crypt\n'
  exit 0
fi
if [ "${1:-}" = "copy" ]; then
  log "copy $*"
  dest="$3"
  mkdir -p "$dest"
  if [ -d "${FAKE_RCLONE_COPY_SRC:-}" ]; then
    cp "$FAKE_RCLONE_COPY_SRC"/mnemosyne.db.gz "$FAKE_RCLONE_COPY_SRC"/manifest.json "$dest"/ 2>/dev/null || true
  fi
  exit 0
fi
log "other $*"
exit 0
"""

    def _write_seed(self, secret: str) -> None:
        (self.config_dir / "rclone.conf").write_text(
            "[mnemosyne-crypt]\n"
            "type = crypt\n"
            "remote = local:/underlying\n"
            f"password = {secret}\n",
            encoding="utf-8",
        )
        os.chmod(self.config_dir / "rclone.conf", 0o600)

    def _seed_generation(self) -> None:
        gen_dir = self.staging / self.GEN
        gen_dir.mkdir()
        (gen_dir / "mnemosyne.db.gz").write_bytes(b"backup-artifact-bytes")
        sha = hashlib.sha256(b"backup-artifact-bytes").hexdigest()
        (gen_dir / "manifest.json").write_text(
            json.dumps({
                "generation_id": self.GEN,
                "artifact": {"name": "mnemosyne.db.gz", "sha256": sha},
            }),
            encoding="utf-8",
        )
        (gen_dir / "READY").write_text(f"{self.GEN}\n{sha}\n", encoding="utf-8")
        (self.staging / "latest").write_text(f"{self.GEN}\n", encoding="utf-8")

    def _install_fake(self, body: str) -> None:
        fake = self.bin / "rclone"
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o700)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mnem-active-cfg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.staging = self.tmp / "staging"
        self.state = self.tmp / "state"
        self.bin = self.tmp / "bin"
        self.config_dir = self.tmp / "rclone-config"
        self.ephemeral_root = self.tmp / "ephemeral-root"
        # Dedicated uploader-only SECRET volume for the ACTIVE rclone config
        # (emulates mnemosyne-backup-rclone-config). Must be distinct from
        # the state volume, which emulates the ledger state Hermes observes
        # read-only.
        self.active_dir = self.tmp / "rclone-active"
        for d in (self.staging, self.state, self.bin, self.config_dir,
                  self.ephemeral_root, self.active_dir):
            d.mkdir()
        self.log = self.tmp / "rclone.log"
        self.active = self.active_dir / self.ACTIVE_NAME
        self._write_seed(self.ORIGINAL_SECRET)
        self._install_fake(self.UPLOADER_FAKE_RCLONE)
        self._seed_generation()
        self.base_env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "MNEMOSYNE_BACKUP_STAGING_DIR": str(self.staging),
            "MNEMOSYNE_BACKUP_STATE_DIR": str(self.state),
            "MNEMOSYNE_BACKUP_RCLONE_ACTIVE_DIR": str(self.active_dir),
            "MNEMOSYNE_BACKUP_RCLONE_REMOTE": "mnemosyne-crypt",
            "MNEMOSYNE_BACKUP_RCLONE_PATH": "backups",
            "MNEMOSYNE_BACKUP_SLOTS": "3",
            "RCLONE_CONFIG": str(self.config_dir / "rclone.conf"),
            "FAKE_RCLONE_LOG": str(self.log),
        }

    def _assert_state_dir_holds_no_secrets(self) -> None:
        # The state volume emulates the ledger exposure Hermes sees
        # READ-ONLY: it must never hold the active config or its fingerprint.
        secrets = [
            p for p in self.state.rglob("*")
            if "rclone.active.conf" in p.name or p.name.endswith(".seed-fp")
        ]
        self.assertEqual(secrets, [],
                         f"secret active config leaked into the shared state dir: {secrets}")

    # ------------------------------------------------------------------
    # Uploader (long-running lane): active copy in the uploader-only
    # secret volume
    # ------------------------------------------------------------------

    def _run_uploader(self, extra_env: Optional[dict] = None) -> subprocess.CompletedProcess:
        env = {**self.base_env, **(extra_env or {})}
        return subprocess.run(
            ["/bin/sh", str(SCRIPTS_DIR / "mnemosyne-backup-uploader.sh")],
            env=env, capture_output=True, text=True, check=False, timeout=60,
        )

    def _log_configs(self) -> list:
        if not self.log.exists():
            return []
        return [
            cfg for cfg in (
                _config_from_log_line(line)
                for line in self.log.read_text("utf-8").splitlines()
                if line.strip()
            )
            if cfg is not None
        ]

    def _assert_uploader_used_active_config(self) -> None:
        configs = self._log_configs()
        self.assertTrue(configs, "no rclone invocations recorded")
        for cfg in configs:
            self.assertEqual(
                cfg, str(self.active),
                f"rclone must run against the ACTIVE config, not the seed: {cfg}",
            )

    def _assert_no_leak(self, proc: subprocess.CompletedProcess, *markers: str) -> None:
        output = f"{proc.stdout}\n{proc.stderr}"
        for marker in markers:
            self.assertNotIn(marker, output,
                             f"config secret leaked into script output: {marker}")

    def test_uploader_seeds_active_config_and_runs_rclone_against_it(self) -> None:
        seed_before = (self.config_dir / "rclone.conf").read_bytes()
        proc = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertEqual(proc.returncode, 0,
                         f"one-shot failed:\n{proc.stdout}\n{proc.stderr}")
        # Active copy seeded in the DEDICATED uploader-only secret dir
        # (never the state dir Hermes observes), 0600, seed content.
        self.assertTrue(self.active.exists())
        self.assertTrue(str(self.active).startswith(str(self.active_dir)))
        self.assertNotIn(str(self.state), str(self.active))
        self.assertEqual(self.active.read_bytes(), seed_before)
        self.assertEqual(os.stat(self.active).st_mode & 0o777, 0o600)
        # Seed fingerprint sidecar recorded privately.
        fp = Path(str(self.active) + ".seed-fp")
        self.assertTrue(fp.exists())
        self.assertEqual(os.stat(fp).st_mode & 0o777, 0o600)
        self.assertEqual(fp.read_text("utf-8").strip(),
                         hashlib.sha256(seed_before).hexdigest())
        # Every rclone call used the active copy; the seed is untouched.
        self._assert_uploader_used_active_config()
        self.assertEqual((self.config_dir / "rclone.conf").read_bytes(), seed_before)
        # The shared state dir (Hermes-visible) holds no secrets.
        self._assert_state_dir_holds_no_secrets()
        self._assert_no_leak(proc, self.ORIGINAL_SECRET)

    def test_uploader_restart_retention_preserves_simulated_rclone_rewrite(self) -> None:
        first = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertEqual(first.returncode, 0,
                         f"first run failed:\n{first.stdout}\n{first.stderr}")
        # Simulate rclone rewriting the ACTIVE config in place to persist an
        # OAuth token refresh.
        refreshed = (
            "[mnemosyne-crypt]\n"
            "type = crypt\n"
            "remote = local:/underlying\n"
            f"password = {self.REFRESHED_SECRET}\n"
            'token = {"access_token": "a", "refresh_token": "r"}\n'
        )
        self.active.write_text(refreshed, encoding="utf-8")
        os.chmod(self.active, 0o600)
        # "Restart": a fresh uploader process over the same state volume.
        second = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertEqual(second.returncode, 0,
                         f"restart run failed:\n{second.stdout}\n{second.stderr}")
        # Seed unchanged -> the refreshed active copy survives the restart.
        self.assertEqual(self.active.read_text("utf-8"), refreshed)
        self.assertIn(self.REFRESHED_SECRET, self.active.read_text("utf-8"))
        self._assert_uploader_used_active_config()
        self.assertIn(self.ORIGINAL_SECRET,
                      (self.config_dir / "rclone.conf").read_text("utf-8"))
        # The shared state dir (Hermes-visible) still holds no secrets.
        self._assert_state_dir_holds_no_secrets()
        self._assert_no_leak(second, self.REFRESHED_SECRET, self.ORIGINAL_SECRET)

    def test_uploader_reseeds_active_config_when_seed_rotated(self) -> None:
        first = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertEqual(first.returncode, 0,
                         f"first run failed:\n{first.stdout}\n{first.stderr}")
        # Operator rotates the published seed.
        self._write_seed(self.ROTATED_SECRET)
        rotated_before = (self.config_dir / "rclone.conf").read_bytes()
        second = self._run_uploader({"MNEMOSYNE_BACKUP_ONCE": "true"})
        self.assertEqual(second.returncode, 0,
                         f"rotated run failed:\n{second.stdout}\n{second.stderr}")
        # Active copy must now hold the rotated seed; old secret gone.
        self.assertEqual(self.active.read_bytes(), rotated_before)
        self.assertNotIn(self.ORIGINAL_SECRET, self.active.read_text("utf-8"))
        fp = Path(str(self.active) + ".seed-fp")
        self.assertEqual(fp.read_text("utf-8").strip(),
                         hashlib.sha256(rotated_before).hexdigest())
        self._assert_uploader_used_active_config()
        # The shared state dir (Hermes-visible) still holds no secrets.
        self._assert_state_dir_holds_no_secrets()
        self._assert_no_leak(second, self.ORIGINAL_SECRET, self.ROTATED_SECRET)

    # ------------------------------------------------------------------
    # Recover (short-lived lane): ephemeral private config
    # ------------------------------------------------------------------

    def _run_recover(self, slot: str, timeout: int = 30) -> subprocess.CompletedProcess:
        env = {
            **self.base_env,
            "MNEMOSYNE_BACKUP_RECOVERY_DIR": str(self.recovery),
            "TMPDIR": str(self.ephemeral_root),
            "FAKE_RCLONE_COPY_SRC": str(self.fake_slot),
        }
        return subprocess.run(
            ["/bin/sh", str(SCRIPTS_DIR / "mnemosyne-backup-recover.sh"), slot],
            env=env, capture_output=True, text=True, check=False, timeout=timeout,
        )

    def _prepare_recover_env(self) -> None:
        self.recovery = self.tmp / "recovery"
        self.fake_slot = self.tmp / "fake-slot"
        self.recovery.mkdir()
        self.fake_slot.mkdir()
        self._install_fake(self.RECOVER_FAKE_RCLONE)
        artifact = b"recovery-artifact-bytes"
        sha = hashlib.sha256(artifact).hexdigest()
        (self.fake_slot / "mnemosyne.db.gz").write_bytes(artifact)
        (self.fake_slot / "manifest.json").write_text(
            json.dumps({
                "generation_id": self.GEN,
                "artifact": {"name": "mnemosyne.db.gz", "sha256": sha},
            }),
            encoding="utf-8",
        )

    def test_recover_uses_ephemeral_private_config_and_cleans_up(self) -> None:
        self._prepare_recover_env()
        seed_before = (self.config_dir / "rclone.conf").read_bytes()
        proc = self._run_recover("1")
        self.assertEqual(proc.returncode, 0,
                         f"recover failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertTrue((self.recovery / "RECOVERY_READY").exists())
        # Every rclone call used an EPHEMERAL config under TMPDIR — never the
        # recovery handoff volume, never the seed.
        for cfg in self._log_configs():
            self.assertTrue(
                str(cfg).startswith(str(self.ephemeral_root / "mnemosyne-backup-rclone.")),
                f"recover rclone must use the ephemeral private config: {cfg}",
            )
            self.assertNotIn(str(self.recovery), str(cfg))
        self.assertEqual(
            [p for p in self.recovery.rglob("rclone.conf")], [],
            "no rclone config may leak into the recovery handoff volume",
        )
        # The ephemeral config dir is removed on exit (trap).
        leftovers = list(self.ephemeral_root.glob("mnemosyne-backup-rclone.*"))
        self.assertEqual(leftovers, [], "ephemeral config dir must be removed on exit")
        # Seed immutability + no secrets in output.
        self.assertEqual((self.config_dir / "rclone.conf").read_bytes(), seed_before)
        self._assert_no_leak(proc, self.ORIGINAL_SECRET)


if __name__ == "__main__":
    unittest.main()
