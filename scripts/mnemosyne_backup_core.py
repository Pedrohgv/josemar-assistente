"""Mnemosyne encrypted-backup core (Phase 2).

Hermes-side exporter and restore helper for the Mnemosyne disaster-recovery
(backup/restore) flow. This module is intentionally core-only: it does NOT
schedule exports, mount volumes, or perform remote uploads. A separate
rclone uploader service (see docker-compose.mnemosyne-backup.yml and
scripts/mnemosyne-backup-uploader.sh) consumes the staging directory this
exporter produces.

Design contract (see docs/mnemosyne-operations.md):

1. Inspects the EXACT pinned supported DR seam ``mnemosyne.dr.recovery`` and
   fails clearly on signature drift. Its current SQL-dump implementation is
   defective for real Beam FTS schemas, so the core uses a narrow binary
   ``sqlite3.Connection.backup`` snapshot with sqlite-vec loaded on both
   connections, gzip-compressed for transport. Never raw-copies live
   SQLite/WAL/SHM and never uses a generic ``_safe_copy_db``.
2. Builds each generation in a temp generation directory on the staging
   volume, verifies restoreability/integrity into a disposable temp DB,
   SHA-256s every artifact, writes a machine-readable manifest (timestamp,
   package versions, source contract) with NO memory contents, then
   atomically publishes the generation directory + an atomic ``latest``
   pointer/manifest last.
3. Safe permissions and a mkdir/flock-equivalent export lock.
4. Restore downloads through crypt, verifies SHA/manifest, restores the binary
   snapshot to a NEW path, runs integrity verification,
   and only then (operator-only, writers stopped, explicit confirmation)
   atomically replaces the live DB while retaining a rollback copy. The
   ``verify-restore`` and ``install-restore`` commands are separate so tests
   cannot accidentally target production. No automated production overwrite.

This module is import-safe without the mnemosyne package present (the DR seam
is imported lazily inside commands that need it) so source/contract tests can
run in environments where only the source is available.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The exact pinned supported DR seam. Drift here must fail clearly.
DR_MODULE = "mnemosyne.dr.recovery"
DR_CREATE_BACKUP = "create_backup"
DR_RESTORE_BACKUP = "restore_backup"
DR_VERIFY_INTEGRITY = "verify_integrity"

# Expected signatures (parameter names) of the DR seam. Used to detect drift.
EXPECTED_CREATE_BACKUP_PARAMS = ("db_path", "backup_dir")
EXPECTED_RESTORE_BACKUP_PARAMS = ("backup_path", "db_path")
EXPECTED_VERIFY_INTEGRITY_PARAMS = ("db_path",)

MANIFEST_NAME = "manifest.json"
LATEST_POINTER_NAME = "latest"
LATEST_MANIFEST_NAME = "latest.manifest.json"
BACKUP_ARTIFACT_NAME = "mnemosyne.db.gz"
READY_SENTINEL_NAME = "READY"
EXPORT_LOCK_DIR_NAME = ".export.lock"

# Uploader-state ledger file name. The uploader atomically appends every
# successfully uploaded generation id to this bounded/parseable ledger in its
# OWN writable state volume. The exporter reads it read-only (via
# MNEMOSYNE_BACKUP_UPLOADER_STATE_DIR) to learn which generations are safely
# remote before pruning anything locally.
UPLOADED_LEDGER_NAME = "uploaded-generations.jsonl"

DEFAULT_STAGING_DIR = "/opt/data/mnemosyne-backup/staging"
DEFAULT_UPLOADER_STATE_DIR = "/opt/data/mnemosyne-backup/uploader-state"
DEFAULT_GENERATIONS_KEEP = 5

# Generation ID format: a lexically sortable UTC timestamp with microseconds
# followed by a short random UUID suffix, e.g.
#   20260802T012247123456Z-a1b2c3d4
# The timestamp is 20 chars (YYYYmmddTHHMMSSffffffZ); the suffix is '-' plus 8
# hex chars from uuid4. This is collision-resistant across processes even when
# two cron processes run in the same wall-clock microsecond, and remains
# lexically sortable by creation time. Strict validation lives in
# ``is_valid_generation_id``.
GENERATION_ID_TS_FORMAT = "%Y%m%dT%H%M%S%fZ"
GENERATION_ID_RE = re.compile(r"^\d{8}T\d{6}\d{6}Z-[0-9a-f]{8}$")

# Safe permissions: directories 0700, files 0600. Backup artifacts contain
# memory-derived data (even though the manifest does not) so restrict.
DIR_MODE = 0o700
FILE_MODE = 0o600

UNIQUE_PLAINTEXT_MARKER = "MNEMOSYNE_BACKUP_CONTRACT_MARKER_9f2c1a"
BACKUP_METHOD = "sqlite3_connection_backup_binary_gzip"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MnemosyneBackupError(RuntimeError):
    """Base error for the mnemosyne backup core."""


class DRSeamError(MnemosyneBackupError):
    """The pinned DR seam drifted or is unavailable."""


# ---------------------------------------------------------------------------
# DR seam inspection (fail clearly on drift)
# ---------------------------------------------------------------------------


def _inspect_signature(func) -> List[str]:
    import inspect

    sig = inspect.signature(func)
    return list(sig.parameters.keys())


def load_dr_seam() -> Any:
    """Import the pinned DR module and validate its signatures.

    Returns the module object. Raises DRSeamError on any drift so the caller
    fails clearly instead of silently using a changed API.
    """
    try:
        mod = importlib.import_module(DR_MODULE)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DRSeamError(
            f"Pinned DR seam '{DR_MODULE}' is not importable: {exc}. "
            "Ensure mnemosyne-memory is installed in the Hermes venv."
        ) from exc

    for name, expected in (
        (DR_CREATE_BACKUP, EXPECTED_CREATE_BACKUP_PARAMS),
        (DR_RESTORE_BACKUP, EXPECTED_RESTORE_BACKUP_PARAMS),
        (DR_VERIFY_INTEGRITY, EXPECTED_VERIFY_INTEGRITY_PARAMS),
    ):
        if not hasattr(mod, name):
            raise DRSeamError(
                f"DR seam drift: '{DR_MODULE}.{name}' is missing. "
                f"Expected {expected}."
            )
        actual = _inspect_signature(getattr(mod, name))
        if tuple(actual) != expected:
            raise DRSeamError(
                f"DR seam drift: '{DR_MODULE}.{name}' signature changed. "
                f"Expected params {expected}, got {actual}."
            )
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec when available, failing if the installed extension is bad."""
    try:
        import sqlite_vec
    except ImportError:
        return
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)


def _verify_binary_database(db_path: Path) -> bool:
    """Verify a binary snapshot with the same extension environment as Beam."""
    conn = sqlite3.connect(str(db_path))
    try:
        _load_sqlite_vec(conn)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            return False
        # Force schema materialization, including FTS/vec virtual tables.
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        if "fts_working" in names:
            conn.execute("SELECT count(*) FROM fts_working").fetchone()
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def _create_binary_backup(db_path: Path, artifact_path: Path) -> None:
    """Create a consistent compressed binary SQLite snapshot.

    The pinned DR seam currently serializes ``iterdump()`` SQL. That format
    does not preserve the real Beam FTS virtual-table schema (``fts_working``),
    so it cannot be used for provider databases. Connection.backup copies the
    live database pages consistently, including WAL frames, and preserves all
    native SQLite virtual-table structures when sqlite-vec is loaded.
    """
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    # Backup directly to a temporary binary database, then gzip its bytes.
    # sqlite3.Connection.backup is the only snapshot operation here.
    source = sqlite3.connect(str(db_path))
    binary_tmp = artifact_path.with_suffix(".sqlite.tmp")
    try:
        _load_sqlite_vec(source)
        target = sqlite3.connect(str(binary_tmp))
        try:
            _load_sqlite_vec(target)
            source.backup(target)
            target.commit()
        finally:
            target.close()
        with open(binary_tmp, "rb") as raw, gzip.open(artifact_path, "wb") as compressed:
            shutil.copyfileobj(raw, compressed)
    finally:
        source.close()
        binary_tmp.unlink(missing_ok=True)


def _restore_binary_backup(artifact_path: Path, dest_db: Path) -> None:
    """Decompress a binary snapshot to a new DB and verify its native schema."""
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="mnem-snapshot-", suffix=".db", dir=dest_db.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with gzip.open(artifact_path, "rb") as compressed, open(tmp_path, "wb") as raw:
            shutil.copyfileobj(compressed, raw)
        _fsync_file(tmp_path)
        if not _verify_binary_database(tmp_path):
            raise MnemosyneBackupError(f"Binary snapshot integrity check failed: {tmp_path}")
        os.replace(tmp_path, dest_db)
    finally:
        tmp_path.unlink(missing_ok=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_generation_id() -> str:
    """Return a collision-resistant, lexically-sortable generation id.

    Format: ``YYYYmmddTHHMMSSffffffZ-<uuid8>`` where the timestamp has
    microsecond precision and ``<uuid8>`` is 8 hex chars from ``uuid4``. Two
    processes (or two sequential cron invocations) producing an id in the
    same wall-clock microsecond still differ by the random suffix, so there
    is no per-process counter that resets and collides. The id is lexically
    sortable by creation time.
    """
    ts = datetime.now(timezone.utc).strftime(GENERATION_ID_TS_FORMAT)
    suffix = uuid.uuid4().hex[:8]
    return f"{ts}-{suffix}"


def is_valid_generation_id(gen_id: str) -> bool:
    """Strictly validate a generation id.

    Rejects anything that is not exactly the new format (no slash, no ``..``,
    no traversal, no legacy compact-timestamp-counter form). This is the
    single source of truth used by both the exporter and the uploader to
    guard against path traversal via the ``latest`` pointer.
    """
    if not isinstance(gen_id, str):
        return False
    if len(gen_id) != 31:  # 22 (ts incl. Z) + 1 ('-') + 8 (hex)
        return False
    return bool(GENERATION_ID_RE.match(gen_id))


def _safe_makedirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        # Best-effort; staging may be on a volume that does not honor chmod.
        pass


def _safe_write_file(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        try:
            os.chmod(tmp, FILE_MODE)
        except OSError:
            pass
    os.replace(tmp, path)


def _safe_write_text(path: Path, text: str) -> None:
    _safe_write_file(path, text.encode("utf-8"))


def _package_versions() -> Dict[str, str]:
    """Collect package versions for the manifest (no memory contents)."""
    versions: Dict[str, str] = {}
    for pkg in ("mnemosyne-memory", "mnemosyne-hermes", "sqlite-vec"):
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except Exception:  # pragma: no cover - environment dependent
            versions[pkg] = "unknown"
    versions["python"] = sys.version.split()[0]
    return versions


def _source_contract() -> Dict[str, Any]:
    """Record the source contract the exporter relied on (for restore)."""
    return {
        "dr_module": DR_MODULE,
        "create_backup": DR_CREATE_BACKUP,
        "restore_backup": DR_RESTORE_BACKUP,
        "verify_integrity": DR_VERIFY_INTEGRITY,
        "create_backup_params": list(EXPECTED_CREATE_BACKUP_PARAMS),
        "restore_backup_params": list(EXPECTED_RESTORE_BACKUP_PARAMS),
        "verify_integrity_params": list(EXPECTED_VERIFY_INTEGRITY_PARAMS),
    }


# ---------------------------------------------------------------------------
# Export lock (mkdir + flock-equivalent)
# ---------------------------------------------------------------------------


class ExportLock:
    """mkdir-based lock with an additional flock on a lock file inside.

    mkdir is atomic on POSIX filesystems and is the classic flock-equivalent
    for shell/Python cross-process exclusion. The extra flock guards against
    concurrent Python processes in the same container.
    """

    def __init__(self, staging_dir: Path) -> None:
        self.lock_dir = staging_dir / EXPORT_LOCK_DIR_NAME
        self._lock_fd: Optional[Any] = None

    def __enter__(self) -> "ExportLock":
        _safe_makedirs(self.lock_dir.parent)
        try:
            self.lock_dir.mkdir()
        except FileExistsError as exc:
            raise MnemosyneBackupError(
                f"Export already in progress (lock: {self.lock_dir}). "
                "Remove the lock dir only if no export is running."
            ) from exc
        # Additional flock for same-process-thread / robustness.
        lock_file = self.lock_dir / "lock"
        self._lock_fd = open(lock_file, "w")
        try:
            fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_fd.close()
            try:
                self.lock_dir.rmdir()
            except OSError:
                pass
            raise MnemosyneBackupError(
                f"Could not acquire export flock: {exc}"
            ) from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._lock_fd.close()
        # Remove the lock file inside before rmdir so the dir can be removed.
        lock_file = self.lock_dir / "lock"
        try:
            lock_file.unlink()
        except OSError:
            pass
        try:
            self.lock_dir.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_backup(
    db_path: Path,
    staging_dir: Path,
    *,
    generations_keep: int = DEFAULT_GENERATIONS_KEEP,
    uploader_state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create one immutable backup generation on the staging volume.

    Steps:
      1. Acquire export lock.
      2. Inspect DR seam (fail on drift).
      3. Build generation in a temp dir on the staging volume.
      4. Native online backup via ``create_backup`` (sqlite-vec-aware).
      5. Verify restoreability into a disposable temp DB via
         ``restore_backup`` + ``verify_integrity``.
      6. SHA-256 every artifact.
      7. Write manifest (timestamp, package versions, source contract, no
         memory contents).
      8. Atomically publish generation dir + atomic ``latest`` pointer +
         ``latest.manifest.json`` last.
      9. Acknowledgement-based local pruning: only generations explicitly
         acknowledged as uploaded in the uploader's ledger (observed
         read-only via ``uploader_state_dir``) are candidates for deletion,
         and only after retaining at least ``generations_keep`` local
         generations. The current/latest generation is never pruned. If the
         uploader state dir is unavailable or empty, NO automatic deletion
         occurs (safety over convenience).

    Returns the manifest dict.
    """
    if not db_path.exists():
        raise MnemosyneBackupError(f"Source DB not found: {db_path}")

    _safe_makedirs(staging_dir)

    with ExportLock(staging_dir):
        dr = load_dr_seam()

        generation_id = _next_generation_id()
        if not is_valid_generation_id(generation_id):
            raise MnemosyneBackupError(
                f"Generated invalid generation id: {generation_id}"
            )
        gen_dir = staging_dir / generation_id
        tmp_gen_dir = staging_dir / f".{generation_id}.tmp"

        # Clean any stale temp dir from a previous failed run.
        if tmp_gen_dir.exists():
            shutil.rmtree(tmp_gen_dir, ignore_errors=True)
        _safe_makedirs(tmp_gen_dir)

        try:
            artifact_path = tmp_gen_dir / BACKUP_ARTIFACT_NAME
            # Do not call dr.create_backup: its SQL dump loses Beam's FTS
            # virtual-table schema. The narrow binary snapshot preserves the
            # provider DB page/schema layout while remaining WAL-safe.
            _create_binary_backup(db_path, artifact_path)

            # 2. Verify restoreability into a disposable temp DB.
            with tempfile.TemporaryDirectory(prefix="mnem-verify-") as vtmp:
                verify_db = Path(vtmp) / "verify.db"
                _restore_binary_backup(artifact_path, verify_db)
                if not _verify_binary_database(verify_db):
                    raise MnemosyneBackupError(
                        f"verify_integrity failed for restored DB {verify_db}"
                    )

            # 3. SHA-256 every artifact.
            sha = _sha256_file(artifact_path)

            # 4. Manifest (no memory contents).
            manifest: Dict[str, Any] = {
                "generation_id": generation_id,
                "created_at_utc": _utc_now_iso(),
                "source_db_path": str(db_path),
                "source_db_size": db_path.stat().st_size,
                "artifact": {
                    "name": BACKUP_ARTIFACT_NAME,
                    "sha256": sha,
                    "size": artifact_path.stat().st_size,
                    "compressed": True,
                },
                "backup_method": BACKUP_METHOD,
                "dr_seam": _source_contract(),
                "package_versions": _package_versions(),
                "restore_verified": True,
                "integrity_verified": True,
            }
            _safe_write_text(tmp_gen_dir / MANIFEST_NAME,
                             json.dumps(manifest, indent=2, sort_keys=True))

            # 5. READY sentinel (written last within the temp dir).
            _safe_write_text(tmp_gen_dir / READY_SENTINEL_NAME,
                             f"{generation_id}\n{sha}\n")

            # 6. Atomically publish the generation directory.
            os.replace(tmp_gen_dir, gen_dir)

            # 7. Atomic latest pointer + latest manifest (written last).
            latest_pointer = staging_dir / LATEST_POINTER_NAME
            latest_manifest = staging_dir / LATEST_MANIFEST_NAME
            _safe_write_text(latest_pointer, f"{generation_id}\n")
            _safe_write_text(latest_manifest,
                             json.dumps(manifest, indent=2, sort_keys=True))

            # 8. Acknowledgement-based local pruning of generations on the
            # staging volume ONLY (never the remote). Safety contract:
            #   - Only generations explicitly acknowledged as uploaded in the
            #     uploader's ledger (observed read-only) may be deleted.
            #   - The current/latest generation is NEVER pruned.
            #   - At least ``generations_keep`` local generations are always
            #     retained (acknowledged or not).
            #   - If the uploader state dir is unavailable or empty, NO
            #     automatic deletion occurs (safety over convenience).
            _prune_staging(
                staging_dir,
                generations_keep,
                uploader_state_dir=uploader_state_dir,
                current_generation_id=generation_id,
            )

            return manifest
        except Exception:
            # Clean up the temp generation dir on any failure so a stale
            # half-written generation is never published.
            if tmp_gen_dir.exists():
                shutil.rmtree(tmp_gen_dir, ignore_errors=True)
            raise


def _read_uploaded_ledger(uploader_state_dir: Optional[Path]) -> Set[str]:
    """Read the set of generation ids the uploader has acknowledged uploaded.

    The ledger is a bounded JSONL file (one generation id per line) in the
    uploader's writable state volume, observed read-only by the exporter.
    Returns an empty set if the dir/file is absent or unreadable (which
    disables automatic pruning). Malformed lines are skipped.
    """
    acked: Set[str] = set()
    if uploader_state_dir is None:
        return acked
    ledger = uploader_state_dir / UPLOADED_LEDGER_NAME
    if not ledger.exists():
        return acked
    try:
        with open(ledger, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Each line is either a bare generation id or a JSON object
                # with a "generation_id" field (forward-compatible).
                gen_id: Optional[str] = None
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                        gen_id = obj.get("generation_id")
                    except Exception:
                        continue
                else:
                    gen_id = line
                if gen_id and is_valid_generation_id(gen_id):
                    acked.add(gen_id)
    except OSError:
        # Read-only mount missing/unavailable -> no acks -> no pruning.
        return acked
    return acked


def _prune_staging(
    staging_dir: Path,
    keep: int,
    *,
    uploader_state_dir: Optional[Path] = None,
    current_generation_id: Optional[str] = None,
) -> List[str]:
    """Acknowledgement-based pruning of OLD generation dirs on the staging
    volume.

    Safety contract (replaces the old count-only pruning which could delete a
    generation the uploader was actively reading):

      - Only directories matching the strict generation id format
        (``is_valid_generation_id``) that contain a READY sentinel are
        candidates.
      - A generation is deletable ONLY if ALL of:
          * it is explicitly acknowledged as uploaded in the uploader's
            ledger (observed read-only via ``uploader_state_dir``);
          * it is NOT the current/latest generation
            (``current_generation_id``);
          * it is NOT pointed to by the ``latest`` pointer file;
          * deleting it would still leave at least ``keep`` local
            generations retained.
      - If the uploader state dir is unavailable or the ledger is empty,
        NO automatic deletion occurs (safety over convenience). Operators
        may prune manually.
      - NEVER touches the remote, the ``latest`` pointer/manifest, or any
        non-generation file.

    Returns the list of removed generation ids (sorted oldest-first).
    """
    if keep <= 0:
        return []

    # Read the latest pointer so we never delete the currently-pointed
    # generation even if the caller did not pass current_generation_id.
    latest_pointer = staging_dir / LATEST_POINTER_NAME
    latest_gen: Optional[str] = None
    if latest_pointer.exists():
        try:
            latest_gen = latest_pointer.read_text("utf-8").strip()
            if not is_valid_generation_id(latest_gen):
                latest_gen = None
        except OSError:
            latest_gen = None

    acked = _read_uploaded_ledger(uploader_state_dir)

    gens: List[Tuple[str, Path]] = []
    for entry in staging_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or name == EXPORT_LOCK_DIR_NAME:
            continue
        if not is_valid_generation_id(name):
            continue
        if not (entry / READY_SENTINEL_NAME).exists():
            continue
        gens.append((name, entry))
    # Sort newest-first (lexically sortable ids).
    gens.sort(key=lambda t: t[0], reverse=True)

    if len(gens) <= keep:
        return []

    removed: List[str] = []
    # gens[keep:] are the oldest beyond the retained window. Only delete
    # those that are acknowledged AND not current/latest.
    for name, path in gens[keep:]:
        if current_generation_id is not None and name == current_generation_id:
            continue
        if latest_gen is not None and name == latest_gen:
            continue
        if name not in acked:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(name)
    return removed


def list_generations(staging_dir: Path) -> List[Dict[str, Any]]:
    """List published generations on the staging volume with their manifests.

    Only directories matching the strict generation id format that contain a
    READY sentinel are listed.
    """
    out: List[Dict[str, Any]] = []
    if not staging_dir.exists():
        return out
    for entry in sorted(staging_dir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or name == EXPORT_LOCK_DIR_NAME:
            continue
        if not is_valid_generation_id(name):
            continue
        if not (entry / READY_SENTINEL_NAME).exists():
            continue
        manifest_path = entry / MANIFEST_NAME
        info: Dict[str, Any] = {"generation_id": name, "path": str(entry)}
        if manifest_path.exists():
            try:
                info["manifest"] = json.loads(manifest_path.read_text("utf-8"))
            except Exception:
                info["manifest"] = None
        out.append(info)
    return out


def read_latest(staging_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the latest manifest only when its pointer and id agree."""
    latest_pointer = staging_dir / LATEST_POINTER_NAME
    latest_manifest = staging_dir / LATEST_MANIFEST_NAME
    if not latest_pointer.exists() or not latest_manifest.exists():
        return None
    try:
        generation_id = latest_pointer.read_text("utf-8").strip()
        if not is_valid_generation_id(generation_id):
            return None
        manifest = json.loads(latest_manifest.read_text("utf-8"))
        if manifest.get("generation_id") != generation_id:
            return None
        return manifest
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Restore (verify-restore + install-restore are separate)
# ---------------------------------------------------------------------------


def verify_restore(
    backup_artifact: Path,
    dest_db: Path,
    *,
    expected_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify a backup artifact restores cleanly to a NEW disposable path.

    This NEVER touches the live DB. It:
      1. Optionally verifies the artifact SHA-256 against ``expected_sha256``.
      2. Restores via native ``restore_backup`` to ``dest_db`` (a NEW path).
      3. Runs ``verify_integrity`` on the restored DB.

    Returns a dict with sha256, integrity_check, and restored path. Raises on
    any failure.
    """
    if not backup_artifact.exists():
        raise MnemosyneBackupError(f"Backup artifact not found: {backup_artifact}")
    if dest_db.exists():
        raise MnemosyneBackupError(
            f"Restore destination already exists (refusing to overwrite): {dest_db}"
        )

    actual_sha = _sha256_file(backup_artifact)
    if expected_sha256 is not None and actual_sha != expected_sha256:
        raise MnemosyneBackupError(
            f"SHA-256 mismatch for {backup_artifact}: "
            f"expected {expected_sha256}, got {actual_sha}"
        )

    dest_db.parent.mkdir(parents=True, exist_ok=True)
    _restore_binary_backup(backup_artifact, dest_db)
    if not _verify_binary_database(dest_db):
        raise MnemosyneBackupError(
            f"verify_integrity failed for {dest_db}"
        )
    return {
        "sha256": actual_sha,
        "integrity_check": True,
        "restored_db": str(dest_db),
    }


def _fsync_file(path: Path) -> None:
    """Best-effort fsync of an open file descriptor for durability."""
    try:
        with open(path, "rb") as f:
            os.fsync(f.fileno())
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory's parent for durability."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def install_restore(
    verified_db: Path,
    live_db: Path,
    *,
    confirm: bool = False,
    rollback_suffix: str = ".rollback",
) -> Dict[str, Any]:
    """Operator-only: atomically replace the live DB with a verified restore.

    This is the ONLY function that touches the live DB path. It is
    cross-filesystem safe: the verified restore may live on a different
    filesystem than the live DB (``os.replace`` across filesystems raises
    EXDEV), so this function stages a copy into the live DB's parent
    directory first and then performs an atomic ``os.replace`` within the
    same filesystem.

    Steps:
      1. Requires ``confirm=True`` (explicit operator confirmation). The
         caller MUST ensure writers are stopped.
      2. Re-runs native ``verify_integrity`` on the input verified DB (defend
         against a tampered/corrupted staged input).
      3. Retains a rollback copy of the current live DB (if it exists) plus
         any ``-wal``/``-shm`` files.
      4. Copies the verified DB to a secure temp file in ``live_db.parent``
         (same filesystem as the live DB), fsyncs the staged copy, and
         re-verifies integrity on the staged copy.
      5. Atomically replaces the live DB via ``os.replace`` (same-parent,
         same-filesystem).
      6. fsyncs the live DB's parent directory if practical.
      7. Removes stale ``-wal``/``-shm`` so SQLite does not replay old frames.
      8. The input verified DB is PRESERVED (never consumed/moved).

    On any failure the staged temp file is removed. The rollback copy is
    retained safely and never deleted by this function. No automated
    production overwrite is performed by this module.
    """
    if not confirm:
        raise MnemosyneBackupError(
            "install_restore requires explicit confirm=True. Stop writers "
            "and confirm before replacing the live DB."
        )
    if not verified_db.exists():
        raise MnemosyneBackupError(f"Verified restore DB not found: {verified_db}")

    live_parent = live_db.parent
    live_parent.mkdir(parents=True, exist_ok=True)

    # 2. Re-run native integrity verification on the input verified DB.
    if not _verify_binary_database(verified_db):
        raise MnemosyneBackupError(
            f"Input verified DB failed integrity re-check: {verified_db}"
        )

    # 3. Retain a rollback copy of the current live DB (if it exists).
    rollback_path: Optional[Path] = None
    if live_db.exists():
        rollback_path = live_db.with_suffix(live_db.suffix + rollback_suffix)
        # Atomic copy of the current live DB for rollback.
        shutil.copy2(live_db, rollback_path)
        # Also retain WAL/SHM if present (best-effort).
        for ext in ("-wal", "-shm"):
            side = Path(str(live_db) + ext)
            if side.exists():
                shutil.copy2(side, Path(str(rollback_path) + ext))

    # 4. Stage a copy of the verified DB into the live DB's parent directory
    # (same filesystem) so os.replace is atomic and never hits EXDEV. The
    # input verified_db is PRESERVED (we copy, not move).
    staged: Optional[Path] = None
    try:
        staged = Path(tempfile.mkstemp(
            prefix=".mnem-restore-", suffix=".dbtmp", dir=str(live_parent)
        )[1])
        shutil.copy2(verified_db, staged)
        _fsync_file(staged)
        # Re-verify integrity on the staged copy (defend against copy errors).
        if not _verify_binary_database(staged):
            raise MnemosyneBackupError(
                f"Staged restore copy failed integrity check: {staged}"
            )

        # 5. Atomic replace within the live filesystem (same parent).
        os.replace(staged, live_db)
        staged = None  # consumed by os.replace

        # 6. fsync the live DB and its parent directory if practical.
        _fsync_file(live_db)
        _fsync_dir(live_parent)

        # 7. Remove any stale WAL/SHM from the live path so SQLite does not
        # replay old frames against the new DB.
        for ext in ("-wal", "-shm"):
            side = Path(str(live_db) + ext)
            if side.exists():
                try:
                    side.unlink()
                except OSError:
                    pass
    except Exception:
        # Clean up the staged temp file on any failure. The rollback copy is
        # retained safely and never deleted here.
        if staged is not None and staged.exists():
            try:
                staged.unlink()
            except OSError:
                pass
        raise

    return {
        "installed": True,
        "live_db": str(live_db),
        "rollback_path": str(rollback_path) if rollback_path else None,
        "input_verified_db_preserved": str(verified_db),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db-path",
        default=os.environ.get("MNEMOSYNE_DATA_DIR", "/opt/data/mnemosyne/data") + "/mnemosyne.db",
        help="Source Mnemosyne DB path (default: $MNEMOSYNE_DATA_DIR/mnemosyne.db)",
    )
    p.add_argument(
        "--staging-dir",
        default=os.environ.get("MNEMOSYNE_BACKUP_STAGING_DIR", DEFAULT_STAGING_DIR),
        help="Staging volume root for generations (default: %(default)s)",
    )


def _cmd_export(args: argparse.Namespace) -> int:
    uploader_state_dir = os.environ.get("MNEMOSYNE_BACKUP_UPLOADER_STATE_DIR")
    manifest = export_backup(
        db_path=Path(args.db_path),
        staging_dir=Path(args.staging_dir),
        generations_keep=int(getattr(args, "generations_keep", DEFAULT_GENERATIONS_KEEP)),
        uploader_state_dir=Path(uploader_state_dir) if uploader_state_dir else None,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    gens = list_generations(Path(args.staging_dir))
    print(json.dumps(gens, indent=2, sort_keys=True))
    return 0


def _cmd_latest(args: argparse.Namespace) -> int:
    latest = read_latest(Path(args.staging_dir))
    if latest is None:
        print("No generation published yet.", file=sys.stderr)
        return 1
    print(json.dumps(latest, indent=2, sort_keys=True))
    return 0


def _cmd_verify_restore(args: argparse.Namespace) -> int:
    res = verify_restore(
        backup_artifact=Path(args.backup_artifact),
        dest_db=Path(args.dest_db),
        expected_sha256=args.sha256,
    )
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


def _cmd_install_restore(args: argparse.Namespace) -> int:
    res = install_restore(
        verified_db=Path(args.verified_db),
        live_db=Path(args.live_db),
        confirm=args.i_confirm_this_overwrites_production,
    )
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    """Manual/ack-only pruning of acknowledged-uploaded old generations.

    This is the safe manual pruning path. It applies the same
    acknowledgement-based contract as the automatic pruning done during
    export: only generations explicitly acknowledged as uploaded in the
    uploader's ledger (observed read-only) and beyond the retained window
    are removed; the current/latest generation is never pruned. If the
    uploader state dir is unavailable or empty, nothing is removed.
    """
    uploader_state_dir = os.environ.get("MNEMOSYNE_BACKUP_UPLOADER_STATE_DIR")
    removed = _prune_staging(
        Path(args.staging_dir),
        int(getattr(args, "generations_keep", DEFAULT_GENERATIONS_KEEP)),
        uploader_state_dir=Path(uploader_state_dir) if uploader_state_dir else None,
    )
    print(json.dumps({"removed": removed}, indent=2, sort_keys=True))
    return 0


def _cmd_inspect_dr(args: argparse.Namespace) -> int:
    """Print the inspected DR seam signatures (for diagnostics/tests)."""
    dr = load_dr_seam()
    info = {
        "module": DR_MODULE,
        "create_backup": _inspect_signature(dr.create_backup),
        "restore_backup": _inspect_signature(dr.restore_backup),
        "verify_integrity": _inspect_signature(dr.verify_integrity),
    }
    print(json.dumps(info, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mnemosyne-backup",
        description="Mnemosyne encrypted-backup core (Phase 2).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export", help="Create one backup generation.")
    _add_common_args(p_export)
    p_export.add_argument(
        "--generations-keep",
        type=int,
        default=int(os.environ.get("MNEMOSYNE_BACKUP_GENERATIONS_KEEP", str(DEFAULT_GENERATIONS_KEEP))),
        help="Number of local staging generations to retain (default: %(default)s)",
    )
    p_export.set_defaults(func=_cmd_export)

    p_list = sub.add_parser("list", help="List published generations.")
    _add_common_args(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_latest = sub.add_parser("latest", help="Print the latest manifest.")
    _add_common_args(p_latest)
    p_latest.set_defaults(func=_cmd_latest)

    p_vr = sub.add_parser("verify-restore", help="Verify a backup restores to a NEW path.")
    p_vr.add_argument("backup_artifact", help="Path to the .gz backup artifact")
    p_vr.add_argument("dest_db", help="NEW destination DB path (must not exist)")
    p_vr.add_argument("--sha256", default=None, help="Expected SHA-256 of the artifact")
    p_vr.set_defaults(func=_cmd_verify_restore)

    p_ir = sub.add_parser(
        "install-restore",
        help="Operator-only: replace the live DB with a verified restore.",
    )
    p_ir.add_argument("verified_db", help="Path to the verified restored DB")
    p_ir.add_argument("live_db", help="Path to the live DB to replace")
    p_ir.add_argument(
        "--i-confirm-this-overwrites-production",
        action="store_true",
        help="Explicit operator confirmation required.",
    )
    p_ir.set_defaults(func=_cmd_install_restore)

    p_prune = sub.add_parser(
        "prune",
        help="Manually prune acknowledged-uploaded old generations (ack-only).",
    )
    _add_common_args(p_prune)
    p_prune.add_argument(
        "--generations-keep",
        type=int,
        default=int(os.environ.get("MNEMOSYNE_BACKUP_GENERATIONS_KEEP", str(DEFAULT_GENERATIONS_KEEP))),
        help="Minimum local staging generations to retain (default: %(default)s)",
    )
    p_prune.set_defaults(func=_cmd_prune)

    p_inspect = sub.add_parser("inspect-dr", help="Print inspected DR seam signatures.")
    p_inspect.set_defaults(func=_cmd_inspect_dr)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MnemosyneBackupError as exc:
        print(f"[mnemosyne-backup] ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[mnemosyne-backup] UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
