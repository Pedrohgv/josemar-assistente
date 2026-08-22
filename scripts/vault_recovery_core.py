#!/opt/hermes/.venv/bin/python3 -I
"""Vault recovery export core (Phase 1 + Phase 2 manifest extension).

Hermes-side exporter for the vault-recovery disaster-recovery lane. The
exporter creates local staged immutable generations on a dedicated staging
volume: `<generation-id>/vault/`, `<generation-id>/.gbrain/`,
`manifest.json`, `<tree>.entries.txt` (Phase-2 machine-readable per-entry
index, digest-bound to the manifest), `READY`, then an atomic `latest`
pointer. Remote upload/recovery/install is the default deployment lane and
lives in separate scripts/containers; the plaintext obsidian-backup service
is retired (Phase 3).

Design contract (see docs/vault-recovery-operations.md):

 1. Runs ONLY as the actual Hermes runtime user, under the existing shared
   TaskNotes/gbrain cooperative lock at /opt/data/.locks/tasknotes.lock. The
   CLI boundary enforces the Hermes identity (configured HERMES_UID, else the
   system `hermes` user's uid, else the default 10000): root and arbitrary
   non-Hermes uids are rejected. The caller (scripts/vault-recovery-export.sh)
   runs this core through scripts/tasknotes_lock_run.py, which holds the
   exclusive flock for the whole child lifetime and hands the lock fd down via
   TASKNOTES_LOCK_FD. This core validates that inherited fd (exact lock path +
   exclusive flock actually held) and fails closed otherwise. It NEVER calls
   the public `gbrain` adapter or `josemar-gbrain` (no nested lock); gbrain
   access is limited to a direct invocation of the private pinned native
   binary.
 2. Preflight: runs `gbrain doctor --json` through the private native binary
   (/opt/josemar/libexec/gbrain-native) with the canonical env — a strict
   actual DB-open preflight against the pinned v0.46.26.0 doctor schema. The
   report must contain the checks `connection`, `jsonb_integrity`,
   `schema_version`, and `pgvector` EXACTLY ONCE each with status `ok`;
   warnings on any other check are allowed, any `fail` rejects the export.
 3. Active-PGLite indicator check: after the doctor exits and before the
   physical copy, the `.gbrain` tree is scanned (no-follow, directory-fd
   based) for runtime artifacts that would indicate a database still open or a
   crashed holder: socket-type entries and the Postgres runtime marker names
   `postmaster.pid`, `postmaster.opts`, `.s.PGSQL.*`. Any hit fails the export
   closed — the exporter never guesses that an indicator is stale. Scan
   errors (unreadable directory, failed lstat) also fail closed: the exporter
   cannot prove the absence of indicators it could not inspect.
 4. Whole-tree copy with convergence: source scan A -> physical copy ->
   source scan B -> staged-tree scan. Both the scan and the copy are
   directory-fd / openat-style (O_NOFOLLOW|O_DIRECTORY chain descents, fstatat
   entry stats, dir_fd-relative opens) so symlink roots, symlinked components,
   and every component race are rejected — the copy never follows anything
   and never re-resolves a pathname. Symlinks and specials are rejected; file
   modes and empty directories are preserved; the ROOT mode is recorded too.
   Directory modes are applied ONLY after all children are copied (deepest
   first) so read-only source directories copy successfully. The generation
   is published ONLY when scan A == scan B and the staged tree equals scan A
   (path/type/mode/content hashes/dirs, root mode included). Divergence
   triggers a bounded retry (fresh copy each attempt); after the bound the
   export fails with NO READY marker and nothing published. The lock is held
   through source/staging validation and publication.
 5. Durability: every copied file is fsynced BEFORE its rename with its
    source mode already applied (mode is as crash-durable as content), every
    created directory (including the tree roots), `manifest.json`, `READY`,
    the generation directory, and the staging root are fsynced. Any fsync
    failure aborts the export and nothing remains published: no generation
    dir, no READY, no `latest` update (a `latest` pointer already renamed
    into place is removed on first publication or rolled back to the
    previous generation, and the staging root is fsynced so the rollback
    itself is durable).
 6. Publication is atomic: the generation is built in a hidden
   `. <generation-id>.tmp` directory on the staging volume, `manifest.json`
   then `READY` are written last inside it, the directory is renamed into
   place, and only then is the `latest` pointer atomically replaced.

The module is import-safe and standard-library only, so source/contract
tests can run anywhere. Production paths are module-level constants; keyword
parameters on the public functions are test seams only (the CLI entrypoint
always uses the constants, mirroring scripts/gbrain_chat_run.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants (fixed production paths; never env-overridable at runtime)
# ---------------------------------------------------------------------------

GBRAIN_BIN = "/opt/josemar/libexec/gbrain-native"
GBRAIN_HOME = "/opt/data"
GBRAIN_STATE_DIR = "/opt/data/.gbrain"
VAULT_DIR = "/opt/data/obsidian"
TASKNOTES_LOCK = "/opt/data/.locks/tasknotes.lock"
SCHEMA_PACK_FILE = "/opt/data/.gbrain/active-schema-pack"
DEFAULT_SCHEMA_PACK = "josemar"

DEFAULT_STAGING_DIR = "/opt/data/vault-recovery/staging"

# The Hermes runtime identity. The exporter may run ONLY as this uid: the
# configured HERMES_UID env (validated) when set, else the system `hermes`
# user's uid, else this default (the compose default).
DEFAULT_HERMES_UID = 10000

MANIFEST_NAME = "manifest.json"
READY_SENTINEL_NAME = "READY"
LATEST_POINTER_NAME = "latest"
VAULT_TREE_NAME = "vault"
GBRAIN_TREE_NAME = ".gbrain"

# The pinned v0.46.26.0 doctor contract: these checks must each appear
# exactly once in the `checks` array with status `ok`. Warnings on any other
# check are allowed; any `fail` anywhere rejects the preflight.
REQUIRED_DOCTOR_CHECKS = ("connection", "jsonb_integrity", "schema_version", "pgvector")

# Postgres/PGLite runtime artifacts that indicate an actively-open (or
# crashed-but-uncleaned) database. The exporter refuses to copy while any of
# these is present instead of guessing that it is stale.
PGLITE_RUNTIME_FILE_NAMES = frozenset({"postmaster.pid", "postmaster.opts"})

# Generation ID format: lexically sortable UTC timestamp with microseconds
# plus a short random suffix, e.g. 20260802T012247123456Z-a1b2c3d4.
GENERATION_ID_TS_FORMAT = "%Y%m%dT%H%M%S%fZ"
GENERATION_ID_RE = re.compile(r"^\d{8}T\d{6}\d{6}Z-[0-9a-f]{8}$")
GENERATION_ID_LEN = 31

# Safe permissions for exporter-owned files on the staging volume.
DIR_MODE = 0o700
FILE_MODE = 0o600

CONVERGENCE_ATTEMPTS = 3
CONVERGENCE_RETRY_DELAY = 1.0
DOCTOR_TIMEOUT = 120.0

MANIFEST_SCHEMA_VERSION = 1
EXPORTER_VERSION = "2"

# Phase-2 additive manifest extension: per-tree machine-readable entries index
# files. Each generation carries `<tree>.entries.txt` (one line per staged
# scan record: `type\tmode\tsize\tsha256\tpath`, root path "."), bound to the
# manifest via `trees.<name>.entries_digest` (sha256 of the file content).
# The shell uploader/recover steps (rclone image, no Python) use the index for
# FULL manifest/hashes/tree validation before any transfer; the Hermes-side
# verifier re-scans with scan_tree and compares records exactly.
ENTRIES_FILE_SUFFIX = ".entries.txt"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VaultRecoveryError(RuntimeError):
    """Base error for the vault recovery core."""


class LockError(VaultRecoveryError):
    """The core is not running under the shared tasknotes lock."""


class IdentityError(VaultRecoveryError):
    """The core is not running as the actual Hermes runtime user."""


class DoctorPreflightError(VaultRecoveryError):
    """The native doctor preflight failed or violated the pinned contract."""


class ActiveIndicatorError(VaultRecoveryError):
    """Active PGLite runtime artifacts were found before the physical copy."""


class TreeScanError(VaultRecoveryError):
    """The source tree contains a symlink or special file (no-follow rule)."""


class ConvergenceError(VaultRecoveryError):
    """The source tree did not converge across the bounded retry window."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_generation_id() -> str:
    ts = datetime.now(timezone.utc).strftime(GENERATION_ID_TS_FORMAT)
    suffix = uuid.uuid4().hex[:8]
    return f"{ts}-{suffix}"


def is_valid_generation_id(gen_id: str) -> bool:
    """Strictly validate a generation id (no slash, no traversal, exact shape)."""
    if not isinstance(gen_id, str):
        return False
    if len(gen_id) != GENERATION_ID_LEN:
        return False
    return bool(GENERATION_ID_RE.match(gen_id))


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_OCTAL_MODE_RE = re.compile(r"^0o[0-7]{3,4}$")

# The exact schema-version-1 manifest shape (the exporter writes it with
# json.dumps(indent=2, sort_keys=True)). Strict validation rejects any
# structural drift: unknown keys, wrong types, malformed digests, or a
# missing/malformed block fail the generation closed instead of being
# accepted on partial field matches.
MANIFEST_TOP_KEYS = frozenset(
    {
        "schema_version",
        "generation_id",
        "created_at_utc",
        "phase",
        "remote",
        "sources",
        "trees",
        "doctor",
        "convergence",
        "exporter",
    }
)
MANIFEST_TREE_KEYS = frozenset(
    {
        "entries",
        "dirs",
        "files",
        "bytes",
        "root_mode",
        "scan_digest",
        "staged_digest",
        "entries_file",
        "entries_digest",
    }
)
MANIFEST_REMOTE_KEYS = frozenset({"uploaded", "note"})
MANIFEST_SOURCES_KEYS = frozenset({"gbrain_state_dir", "vault_dir"})
MANIFEST_CONVERGENCE_KEYS = frozenset(
    {"attempts", "max_attempts", "source_scan_a_digest", "source_scan_b_digest"}
)
MANIFEST_DOCTOR_KEYS = frozenset(
    {"report_schema_version", "report_status", "required_checks", "check_counts"}
)
MANIFEST_EXPORTER_KEYS = frozenset({"version", "python"})


def _require_keys(block: Any, expected: frozenset, where: str) -> None:
    if not isinstance(block, dict):
        raise VaultRecoveryError(f"manifest {where} is not an object")
    unknown = set(block) - expected
    if unknown:
        raise VaultRecoveryError(
            f"manifest {where} carries unknown key(s): {sorted(unknown)}"
        )
    missing = expected - set(block)
    if missing:
        raise VaultRecoveryError(
            f"manifest {where} is missing required key(s): {sorted(missing)}"
        )


def validate_manifest_schema(manifest: Any) -> Dict[str, Any]:
    """Strict full-schema validation of a schema-version-1 manifest.

    This is the AUTHORITATIVE manifest contract (council fix: strict JSON
    schema validation). Every block, key, type, and digest format is
    checked exactly; any structural drift raises ``VaultRecoveryError``.
    The shell uploader/recover steps enforce JSON well-formedness plus the
    field checks they can express; this validator is the complete schema
    check used by the Hermes-side restore/verify/install core, so a
    generation that passes the shell gates always restores here.

    Returns a compact summary dict (recorded in manifests/tests).
    """
    if not isinstance(manifest, dict):
        raise VaultRecoveryError("manifest is not a JSON object")
    _require_keys(manifest, MANIFEST_TOP_KEYS, "top level")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise VaultRecoveryError(
            f"manifest schema_version is {manifest['schema_version']!r} "
            f"(expected {MANIFEST_SCHEMA_VERSION})"
        )
    gen_id = manifest["generation_id"]
    if not is_valid_generation_id(gen_id):
        raise VaultRecoveryError(
            f"manifest generation_id is not a valid generation id: {gen_id!r}"
        )
    if not isinstance(manifest["created_at_utc"], str):
        raise VaultRecoveryError("manifest created_at_utc is not a string")
    if manifest["phase"] != 1:
        raise VaultRecoveryError(f"manifest phase is {manifest['phase']!r} (expected 1)")

    remote = manifest["remote"]
    _require_keys(remote, MANIFEST_REMOTE_KEYS, "remote")
    if not isinstance(remote["uploaded"], bool):
        raise VaultRecoveryError("manifest remote.uploaded is not a boolean")
    if not isinstance(remote["note"], str):
        raise VaultRecoveryError("manifest remote.note is not a string")

    sources = manifest["sources"]
    _require_keys(sources, MANIFEST_SOURCES_KEYS, "sources")
    for key in ("gbrain_state_dir", "vault_dir"):
        if not isinstance(sources[key], str) or not sources[key]:
            raise VaultRecoveryError(f"manifest sources.{key} is not a non-empty string")

    trees = manifest["trees"]
    if not isinstance(trees, dict) or set(trees) != {GBRAIN_TREE_NAME, VAULT_TREE_NAME}:
        raise VaultRecoveryError(
            f"manifest trees must contain exactly {GBRAIN_TREE_NAME!r} and "
            f"{VAULT_TREE_NAME!r}"
        )
    for tree, block in trees.items():
        _require_keys(block, MANIFEST_TREE_KEYS, f"trees.{tree}")
        for int_key in ("entries", "dirs", "files", "bytes"):
            value = block[int_key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VaultRecoveryError(
                    f"manifest trees.{tree}.{int_key} is not a non-negative integer"
                )
        root_mode = block["root_mode"]
        if not isinstance(root_mode, str) or not _OCTAL_MODE_RE.match(root_mode):
            raise VaultRecoveryError(
                f"manifest trees.{tree}.root_mode is not an octal mode: {root_mode!r}"
            )
        for digest_key in ("scan_digest", "staged_digest", "entries_digest"):
            digest = block[digest_key]
            if not isinstance(digest, str) or not _HEX64_RE.match(digest):
                raise VaultRecoveryError(
                    f"manifest trees.{tree}.{digest_key} is not a 64-hex sha256: "
                    f"{digest!r}"
                )
        if block["entries_file"] != f"{tree}{ENTRIES_FILE_SUFFIX}":
            raise VaultRecoveryError(
                f"manifest trees.{tree}.entries_file is "
                f"{block['entries_file']!r} (expected {tree}{ENTRIES_FILE_SUFFIX!r})"
            )

    doctor = manifest["doctor"]
    _require_keys(doctor, MANIFEST_DOCTOR_KEYS, "doctor")
    required_checks = doctor["required_checks"]
    if not isinstance(required_checks, dict) or set(required_checks) != set(
        REQUIRED_DOCTOR_CHECKS
    ):
        raise VaultRecoveryError(
            "manifest doctor.required_checks must contain exactly the "
            f"{sorted(REQUIRED_DOCTOR_CHECKS)} checks"
        )
    for name in REQUIRED_DOCTOR_CHECKS:
        if required_checks[name] != "ok":
            raise VaultRecoveryError(
                f"manifest doctor.required_checks.{name} is "
                f"{required_checks[name]!r} (expected 'ok')"
            )
    check_counts = doctor["check_counts"]
    if not isinstance(check_counts, dict) or set(check_counts) != {"ok", "warn", "fail"}:
        raise VaultRecoveryError(
            "manifest doctor.check_counts must contain exactly ok/warn/fail"
        )
    for name in ("ok", "warn", "fail"):
        value = check_counts[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VaultRecoveryError(
                f"manifest doctor.check_counts.{name} is not a non-negative integer"
            )
    if check_counts["fail"] != 0:
        raise VaultRecoveryError(
            f"manifest doctor.check_counts.fail is {check_counts['fail']} (must be 0)"
        )

    convergence = manifest["convergence"]
    _require_keys(convergence, MANIFEST_CONVERGENCE_KEYS, "convergence")
    for int_key in ("attempts", "max_attempts"):
        value = convergence[int_key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise VaultRecoveryError(
                f"manifest convergence.{int_key} is not a positive integer"
            )
    for digest_key in ("source_scan_a_digest", "source_scan_b_digest"):
        digest = convergence[digest_key]
        if not isinstance(digest, str) or not _HEX64_RE.match(digest):
            raise VaultRecoveryError(
                f"manifest convergence.{digest_key} is not a 64-hex sha256: {digest!r}"
            )

    exporter = manifest["exporter"]
    _require_keys(exporter, MANIFEST_EXPORTER_KEYS, "exporter")
    for key in ("version", "python"):
        if not isinstance(exporter[key], str) or not exporter[key]:
            raise VaultRecoveryError(f"manifest exporter.{key} is not a non-empty string")

    return {
        "schema_version": manifest["schema_version"],
        "generation_id": gen_id,
        "phase": manifest["phase"],
        "trees": sorted(trees),
        "digest_ok": True,
    }


def _safe_makedirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass
    # The new directory (and any parents it created) must survive a crash
    # before anything is written into it.
    _fsync_dir(path)


def _fsync_fd(fd: int) -> None:
    """fsync ``fd`` or fail the export closed (a durability error is never
    ignored: a generation that might not survive a crash must not publish)."""
    try:
        os.fsync(fd)
    except OSError as exc:
        raise VaultRecoveryError(f"fsync failed (fd {fd}): {exc}") from exc


def _fsync_dir(path: Path) -> None:
    """fsync a directory so renames/creates inside it are durable."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise VaultRecoveryError(
            f"cannot open directory {path} for fsync: {exc}"
        ) from exc
    try:
        _fsync_fd(fd)
    finally:
        os.close(fd)


def _safe_write_text(path: Path, text: str) -> None:
    """Atomic write (temp file + rename) with exporter-owned permissions.

    The temp file is fsynced BEFORE the rename (content and mode durability;
    the mode is applied before the fsync) and the parent directory is fsynced
    AFTER it (rename durability). Any failure removes the temp file and
    raises: a partially durable write is never presented as success.
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fchmod(fh.fileno(), FILE_MODE)
            except OSError:
                pass
            # Content AND mode durability BEFORE the rename: a mode applied
            # after the fsync would not be crash-durable.
            _fsync_fd(fh.fileno())
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    _fsync_dir(path.parent)


# ---------------------------------------------------------------------------
# Hermes runtime identity (issue #110 conventions)
# ---------------------------------------------------------------------------


def resolve_hermes_uid() -> Optional[int]:
    """The uid the exporter may run as: validated ``HERMES_UID`` env when set,
    else the system ``hermes`` user's uid, else ``DEFAULT_HERMES_UID``.
    Returns None when the configured value is not a valid uid.
    """
    raw = os.environ.get("HERMES_UID")
    if raw:
        try:
            uid = int(raw)
        except ValueError:
            return None
        return uid
    try:
        import pwd

        return pwd.getpwnam("hermes").pw_uid
    except (KeyError, ImportError):
        return DEFAULT_HERMES_UID


def ensure_hermes_identity() -> None:
    """The exporter must run as the ACTUAL Hermes runtime user.

    Root and arbitrary non-Hermes uids are rejected; the resolved identity is
    never 0 (a misconfigured HERMES_UID=0 cannot buy root execution). This is
    the core CLI boundary check — the shell wrappers/cron enforce the same
    identity before they even invoke the core.
    """
    expected = resolve_hermes_uid()
    if expected is None or expected <= 0:
        raise IdentityError(
            "cannot determine a valid non-root Hermes runtime uid "
            "(HERMES_UID is not a valid uid and no system `hermes` user exists)"
        )
    uid = os.geteuid()
    if uid != expected:
        raise IdentityError(
            f"refusing to run as uid {uid}; vault-recovery-export must run as "
            f"the Hermes runtime user (uid {expected})"
        )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _active_schema_pack(marker_path: str) -> str:
    """Runtime source of truth for GBRAIN_SCHEMA_PACK (same contract as the
    public adapter); fails closed to the canonical default."""
    try:
        pack = Path(marker_path).read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_SCHEMA_PACK
    if re.fullmatch(r"[a-z0-9._-]+", pack):
        return pack
    return DEFAULT_SCHEMA_PACK


def _canonical_env(schema_pack: str) -> Dict[str, str]:
    """Canonical gbrain env for the native binary (startup hooks off)."""
    return {
        "GBRAIN_HOME": GBRAIN_HOME,
        "GBRAIN_BRAIN_REPO": VAULT_DIR,
        "GBRAIN_SCHEMA_PACK": schema_pack,
        "GBRAIN_SKIP_STARTUP_HOOKS": "1",
    }


# ---------------------------------------------------------------------------
# Shared-lock validation (issue #110 conventions)
# ---------------------------------------------------------------------------


def lock_held_by_runner(lock_path: str) -> bool:
    """True only when TASKNOTES_LOCK_FD refers to the exact configured lock
    file AND that fd's open file description holds an EXCLUSIVE flock right
    now (same verification as josemar-gbrain's lock_held_by_runner). A shared
    lock, a boolean env var, or an fd to any other file can never satisfy it.
    """
    raw = os.environ.get("TASKNOTES_LOCK_FD")
    if not raw:
        return False
    try:
        fd = int(raw)
    except ValueError:
        return False
    try:
        st_fd = os.fstat(fd)
        st_path = os.stat(lock_path)
    except OSError:
        return False
    if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
        return False
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="utf-8") as fh:
            info = fh.read()
    except OSError:
        return False
    for line in info.splitlines():
        # Exclusive flock: "FLOCK ... WRITE"; shared (LOCK_SH) shows READ.
        if line.startswith("lock:") and "FLOCK" in line and "WRITE" in line:
            return True
    return False


def ensure_under_lock(lock_path: str) -> None:
    if not lock_held_by_runner(lock_path):
        raise LockError(
            "vault-recovery-export must run under the shared tasknotes lock "
            "(scripts/tasknotes_lock_run.py); TASKNOTES_LOCK_FD is missing, "
            "not exclusive, or not the exact /opt/data/.locks/tasknotes.lock."
        )


# ---------------------------------------------------------------------------
# Doctor preflight (strict actual DB-open)
# ---------------------------------------------------------------------------


def run_doctor(
    gbrain_bin: str,
    schema_pack: str,
    timeout: float = DOCTOR_TIMEOUT,
) -> Dict[str, Any]:
    """Run `gbrain doctor --json` through the private native binary.

    Direct native invocation by design: the exporter holds the shared lock
    and must NOT re-enter the public adapter or josemar-gbrain (nested lock).
    """
    env = os.environ.copy()
    env.update(_canonical_env(schema_pack))
    try:
        proc = subprocess.run(
            [gbrain_bin, "doctor", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DoctorPreflightError(
            f"gbrain doctor --json did not finish within {timeout:.0f}s"
        ) from exc
    except OSError as exc:
        raise DoctorPreflightError(
            f"could not execute the pinned native gbrain binary {gbrain_bin}: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise DoctorPreflightError(
            f"gbrain doctor --json exited {proc.returncode}: "
            f"{proc.stderr.strip()[:2000] or proc.stdout.strip()[:2000]}"
        )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DoctorPreflightError(
            f"gbrain doctor --json produced invalid JSON: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise DoctorPreflightError("gbrain doctor --json output is not a JSON object")
    return report


def validate_doctor_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a doctor report against the pinned v0.46.26.0 contract.

    Required checks (connection, jsonb_integrity, schema_version, pgvector)
    must each appear exactly once with status `ok`; warnings on any other
    check are allowed; any `fail` status rejects the preflight. Returns a
    summary dict recorded in the manifest.
    """
    checks = report.get("checks")
    if not isinstance(checks, list):
        raise DoctorPreflightError("doctor report has no `checks` array")
    seen: Dict[str, List[str]] = {}
    for check in checks:
        if not isinstance(check, dict):
            raise DoctorPreflightError("doctor `checks` entry is not an object")
        name = check.get("name")
        status = check.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            raise DoctorPreflightError("doctor check entry lacks name/status strings")
        seen.setdefault(name, []).append(status)
    for required in REQUIRED_DOCTOR_CHECKS:
        statuses = seen.get(required)
        if statuses is None:
            raise DoctorPreflightError(
                f"doctor preflight failed: required check {required!r} is missing"
            )
        if len(statuses) != 1:
            raise DoctorPreflightError(
                f"doctor preflight failed: required check {required!r} "
                f"appears {len(statuses)} times (exactly once required)"
            )
        if statuses[0] != "ok":
            raise DoctorPreflightError(
                f"doctor preflight failed: required check {required!r} "
                f"status is {statuses[0]!r}, expected 'ok'"
            )
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for name, statuses in seen.items():
        for status in statuses:
            if status == "fail":
                raise DoctorPreflightError(
                    f"doctor preflight failed: check {name!r} status is 'fail'"
                )
            counts[status] = counts.get(status, 0) + 1
    return {
        "report_schema_version": report.get("schema_version"),
        "report_status": report.get("status"),
        "required_checks": {name: seen[name][0] for name in REQUIRED_DOCTOR_CHECKS},
        "check_counts": counts,
    }


# ---------------------------------------------------------------------------
# Active-PGLite indicator check
# ---------------------------------------------------------------------------


def _is_pglite_runtime_name(name: str) -> bool:
    return name in PGLITE_RUNTIME_FILE_NAMES or name.startswith(".s.PGSQL.")


def find_active_pglite_indicators(state_dir: Path) -> List[str]:
    """Return relative paths of active-runtime artifacts under ``state_dir``.

    Directory-fd / openat-style no-follow walk: socket-type entries anywhere,
    plus entries whose names match the Postgres runtime marker set. Any hit
    means the exporter must fail closed before the physical copy. Scan errors
    (unreadable directory, failed lstat, symlinked component) FAIL CLOSED with
    TreeScanError: the exporter cannot prove the absence of indicators it
    could not inspect.
    """
    indicators: List[str] = []
    if not state_dir.is_dir():
        return indicators
    root_fd = _open_dir_no_follow(str(state_dir))
    stack: List[Tuple[str, int]] = [("", root_fd)]
    try:
        while stack:
            rel, dir_fd = stack.pop()
            try:
                with os.scandir(dir_fd) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            raise TreeScanError(
                                f"cannot lstat {rel}/{entry.name}: {exc}"
                            ) from exc
                        rel_path = f"{rel}/{entry.name}" if rel else entry.name
                        if stat.S_ISSOCK(st.st_mode):
                            indicators.append(rel_path)
                        elif stat.S_ISLNK(st.st_mode):
                            # A symlink could point at runtime artifacts; the
                            # no-follow contract never follows anything.
                            raise TreeScanError(
                                f"refusing to scan symlink {rel_path} "
                                f"(no-follow rule)"
                            )
                        elif _is_pglite_runtime_name(entry.name):
                            indicators.append(rel_path)
                        elif stat.S_ISDIR(st.st_mode):
                            child = _open_dir_no_follow(entry.name, dir_fd=dir_fd)
                            stack.append((rel_path, child))
                        elif stat.S_ISREG(st.st_mode):
                            pass  # benign regular file
                        else:
                            # fifo/device: not a PGLite runtime artifact here;
                            # the tree scan rejects specials before the copy.
                            pass
            except OSError as exc:
                raise TreeScanError(
                    f"cannot read directory {rel or state_dir}: {exc}"
                ) from exc
            finally:
                os.close(dir_fd)
    except BaseException:
        for _, fd in stack:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    return sorted(indicators)


# ---------------------------------------------------------------------------
# No-follow tree scan / copy (directory-fd / openat style)
#
# Every operation is relative to a directory file descriptor (os.open with
# dir_fd, os.scandir on an fd, os.mkdir/os.replace with dir_fd) and every
# final component is opened with O_NOFOLLOW. A symlink source root, a
# symlinked intermediate component, or any component swapped between lstat
# and open fails the operation closed with TreeScanError: nothing is ever
# followed and no pathname is ever re-resolved after it was verified.
# ---------------------------------------------------------------------------


def _open_dir_no_follow(path: str, dir_fd: Optional[int] = None) -> int:
    """openat-style directory open: O_NOFOLLOW|O_DIRECTORY rejects a symlink
    at the final component; a symlink anywhere earlier in the chain was
    already rejected when that component was opened."""
    try:
        return os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd
        )
    except OSError as exc:
        raise TreeScanError(f"refusing to open directory {path!r}: {exc}") from exc


def _open_file_no_follow(name: str, dir_fd: int) -> int:
    """Open a regular file relative to ``dir_fd`` with O_NOFOLLOW; verify via
    fstat that it is still a regular file (component-race fail-closed)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise TreeScanError(f"cannot open source file {name!r}: {exc}") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise TreeScanError(f"cannot fstat source file {name!r}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise TreeScanError(f"{name!r} changed into a non-regular file during copy")
    return fd


def _descend_dirs(dir_fd: int, parts: List[str]) -> int:
    """Openat-style descent through ``parts`` (single path components each).

    Returns the fd of the final component: a NEW owned fd when ``parts`` is
    non-empty, or the (borrowed, never closed) ``dir_fd`` itself when empty.
    Intermediate fds are closed as the descent proceeds; on failure all fds
    opened by this call are closed and the error propagates.
    """
    fd = dir_fd
    try:
        for part in parts:
            child = _open_dir_no_follow(part, dir_fd=fd)
            if fd != dir_fd:
                os.close(fd)
            fd = child
    except BaseException:
        if fd != dir_fd:
            os.close(fd)
        raise
    return fd


def _ensure_dst_dirs(dst_root_fd: int, parts: List[str]) -> int:
    """Open (creating when missing) the destination dir chain for ``parts``.

    Same ownership contract as ``_descend_dirs``: returns a NEW owned fd for
    the final component when ``parts`` is non-empty, else the borrowed
    ``dst_root_fd``. Each create is a mkdirat (dir_fd) immediately verified
    by an O_NOFOLLOW|O_DIRECTORY open.
    """
    fd = dst_root_fd
    try:
        for part in parts:
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except OSError as exc:
                    raise TreeScanError(
                        f"cannot create staging directory {part!r}: {exc}"
                    ) from exc
                child = _open_dir_no_follow(part, dir_fd=fd)
            except OSError as exc:
                raise TreeScanError(
                    f"cannot open staging directory {part!r}: {exc}"
                ) from exc
            if fd != dst_root_fd:
                os.close(fd)
            fd = child
    except BaseException:
        if fd != dst_root_fd:
            os.close(fd)
        raise
    return fd


def _mkdir_no_follow(name: str, dir_fd: int) -> None:
    """mkdirat with an O_NOFOLLOW|O_DIRECTORY verification open.

    The directory is created WRITABLE (0o700); the recorded source mode is
    applied later, only after every child was copied (see copy_tree), so a
    read-only source directory never blocks copying into its staging twin.
    """
    try:
        os.mkdir(name, 0o700, dir_fd=dir_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise TreeScanError(
            f"cannot create staging directory {name!r}: {exc}"
        ) from exc
    fd = _open_dir_no_follow(name, dir_fd=dir_fd)
    os.close(fd)


def _scan_record(rel: str, st: os.stat_result, sha256: Optional[str]) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "path": rel,
        "type": "dir" if stat.S_ISDIR(st.st_mode) else "file",
        "mode": oct(st.st_mode & 0o7777),
    }
    if record["type"] == "file":
        record["size"] = st.st_size
        record["sha256"] = sha256
    return record


def scan_tree(root: Path) -> List[Dict[str, Any]]:
    """Directory-fd / openat-style no-follow traversal of ``root``.

    Records the ROOT itself (path ``""``, dir, mode) and every other
    directory (including empty ones) and regular file with path, type, mode,
    and (for files) size + content SHA-256. Rejects symlink roots, symlinked
    components, and special files (fifo/socket/device) with TreeScanError;
    the copy must never follow anything. Scan errors (unreadable directory,
    failed lstat) also raise TreeScanError — fail closed.
    """
    root_fd = _open_dir_no_follow(str(root))
    root_st = os.fstat(root_fd)
    stack: List[Tuple[str, int]] = [("", root_fd)]
    records: List[Dict[str, Any]] = []
    try:
        while stack:
            rel, dir_fd = stack.pop()
            try:
                with os.scandir(dir_fd) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            raise TreeScanError(
                                f"cannot lstat {rel}/{entry.name}: {exc}"
                            ) from exc
                        rel_path = f"{rel}/{entry.name}" if rel else entry.name
                        if stat.S_ISLNK(st.st_mode):
                            raise TreeScanError(
                                f"refusing to scan symlink {rel_path} "
                                f"(no-follow copy rule)"
                            )
                        if stat.S_ISDIR(st.st_mode):
                            records.append(_scan_record(rel_path, st, None))
                            child = _open_dir_no_follow(entry.name, dir_fd=dir_fd)
                            stack.append((rel_path, child))
                        elif stat.S_ISREG(st.st_mode):
                            digest = _hash_file_no_follow(entry.name, dir_fd)
                            records.append(_scan_record(rel_path, st, digest))
                        else:
                            raise TreeScanError(
                                f"refusing to scan special file {rel_path} "
                                f"(mode {oct(st.st_mode)})"
                            )
            except OSError as exc:
                raise TreeScanError(
                    f"cannot read directory {rel or root}: {exc}"
                ) from exc
            finally:
                os.close(dir_fd)
        # The root itself is part of the record set: its mode is preserved by
        # the copy and participates in the convergence digest.
        records.append(_scan_record("", root_st, None))
    except BaseException:
        for _, fd in stack:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    records.sort(key=lambda r: r["path"])
    return records


def _hash_file_no_follow(name: str, dir_fd: int) -> str:
    fd = _open_file_no_follow(name, dir_fd)
    try:
        with os.fdopen(fd, "rb", closefd=True) as fh:
            h = hashlib.sha256()
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        raise TreeScanError(f"cannot hash {name!r}: {exc}") from exc


def scan_digest(records: List[Dict[str, Any]]) -> str:
    """Canonical digest over the full record set (path/type/mode/hash/dirs)."""
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(canonical.encode("utf-8"))


def scans_equal(
    a: List[Dict[str, Any]],
    b: List[Dict[str, Any]],
    *,
    ignore_mode: bool = False,
) -> bool:
    """Exact record-set equality (path/type/mode/size/sha256/dirs).

    With ``ignore_mode=True`` the comparison covers structure and CONTENT
    only (path/type/size/sha256/dirs, no modes). The encrypted transport
    (rclone crypt) cannot round-trip POSIX modes, so downloaded bundles are
    validated content-exactly with modes ignored; the install re-applies the
    exact recorded modes from the entries index via ``copy_tree``.
    """
    if len(a) != len(b):
        return False
    if not ignore_mode:
        return all(ra == rb for ra, rb in zip(a, b))
    return all(
        {k: v for k, v in ra.items() if k != "mode"}
        == {k: v for k, v in rb.items() if k != "mode"}
        for ra, rb in zip(a, b)
    )


def copy_tree(root: Path, records: List[Dict[str, Any]], dst_root: Path) -> None:
    """Materialize ``records`` (from ``scan_tree(root)``) under ``dst_root``.

    Directory-fd / openat-style on BOTH sides: source files are opened with
    dir_fd-relative O_NOFOLLOW opens, destination files/dirs are created with
    mkdirat/openat and renamed with dir_fd-relative os.replace. Only regular
    files and directories are created; empty directories are created. File
    content and the recorded source mode are applied to a temp sibling and
    fsynced BEFORE the rename, so a verifier never observes a half-written
    file and both content and mode are durable.
    Directory modes (including the root's) are applied ONLY AFTER all
    children are copied, deepest first, so read-only source directories copy
    successfully. Finally every directory (deepest first) and the tree root
    are fsynced so the whole staged tree is durable.
    """
    try:
        dst_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TreeScanError(f"cannot create staging root {dst_root}: {exc}") from exc
    src_root_fd = _open_dir_no_follow(str(root))
    dst_root_fd = _open_dir_no_follow(str(dst_root))
    dir_modes: List[Tuple[str, int]] = []
    try:
        for record in records:
            rel = record["path"]
            parts = rel.split("/")
            if record["type"] == "dir":
                mode = int(record["mode"], 8)
                if rel == "":
                    # The root mode is applied to dst_root at the very end.
                    dir_modes.append((rel, mode))
                    continue
                dst_parent = _ensure_dst_dirs(dst_root_fd, parts[:-1])
                try:
                    _mkdir_no_follow(parts[-1], dst_parent)
                finally:
                    if dst_parent != dst_root_fd:
                        os.close(dst_parent)
                # Applied after children are copied (see below).
                dir_modes.append((rel, mode))
                continue
            # --- regular file ---
            src_parent = _descend_dirs(src_root_fd, parts[:-1])
            dst_parent = _ensure_dst_dirs(dst_root_fd, parts[:-1])
            src_fd: Optional[int] = None
            try:
                src_fd = _open_file_no_follow(parts[-1], src_parent)
                tmp_name = parts[-1] + ".tmp"
                try:
                    dst_fd = os.open(
                        tmp_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=dst_parent,
                    )
                except OSError as exc:
                    raise TreeScanError(
                        f"cannot create staging file {tmp_name!r}: {exc}"
                    ) from exc
                try:
                    with os.fdopen(src_fd, "rb", closefd=True) as src_fh, os.fdopen(
                        dst_fd, "wb", closefd=True
                    ) as dst_fh:
                        shutil.copyfileobj(src_fh, dst_fh, length=1 << 20)
                        dst_fh.flush()
                        try:
                            os.fchmod(dst_fh.fileno(), int(record["mode"], 8))
                        except OSError:
                            pass
                        # Content AND mode durability BEFORE the rename
                        # publishes the file: the source mode is applied
                        # first so it is as crash-durable as the content.
                        _fsync_fd(dst_fh.fileno())
                    os.replace(
                        tmp_name,
                        parts[-1],
                        src_dir_fd=dst_parent,
                        dst_dir_fd=dst_parent,
                    )
                except BaseException:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                raise TreeScanError(f"cannot copy {rel}: {exc}") from exc
            finally:
                if src_fd is not None:
                    try:
                        os.close(src_fd)
                    except OSError:
                        pass
                if src_parent != src_root_fd:
                    os.close(src_parent)
                if dst_parent != dst_root_fd:
                    os.close(dst_parent)
    except BaseException:
        os.close(src_root_fd)
        os.close(dst_root_fd)
        raise
    os.close(src_root_fd)
    os.close(dst_root_fd)

    # Directory modes are applied ONLY after every child was copied, deepest
    # first (a read-only dir must not block copying into it).
    for rel, mode in sorted(dir_modes, key=lambda m: len(m[0]), reverse=True):
        target = dst_root if rel == "" else dst_root / rel
        try:
            os.chmod(target, mode)
        except OSError:
            pass

    # Durability: fsync every directory, deepest first, then the tree root.
    for rel, _mode in sorted(dir_modes, key=lambda m: len(m[0]), reverse=True):
        if rel != "":
            _fsync_dir(dst_root / rel)
    _fsync_dir(dst_root)


def tree_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    dirs = sum(1 for r in records if r["type"] == "dir")
    files = sum(1 for r in records if r["type"] == "file")
    total = sum(r.get("size", 0) for r in records)
    return {"entries": len(records), "dirs": dirs, "files": files, "bytes": total}


def write_entries_index(
    gen_dir: Path, tree_name: str, records: List[Dict[str, Any]]
) -> str:
    """Write the machine-readable per-entry index for one staged tree.

    ``<gen_dir>/<tree_name><ENTRIES_FILE_SUFFIX>`` contains one line per scan
    record in the same sorted order::

        file\t<mode-octal>\t<size>\t<sha256>\t<relative-path>
        dir\t<mode-octal>\t-\t-\t<relative-path>     (root path is ".")

    Modes are written WITHOUT the ``0o`` prefix so shell ``stat -c %a`` output
    compares directly (rclone-image validation has no Python). The file is
    written atomically with exporter-owned permissions and the returned sha256
    is recorded in the manifest as ``trees.<name>.entries_digest``, binding
    the index to the authoritative manifest. Paths containing a tab or
    newline cannot be represented in the index and fail the export closed.
    """
    lines: List[str] = []
    for record in records:
        path = record["path"] or "."
        if "\t" in path or "\n" in path or "\r" in path:
            raise VaultRecoveryError(
                f"refusing to write entries index: path contains a control "
                f"character (tab/newline/CR): {path!r}"
            )
        mode = record["mode"][2:]  # strip the "0o" prefix (oct() output)
        if record["type"] == "dir":
            lines.append(f"dir\t{mode}\t-\t-\t{path}")
        else:
            lines.append(
                f"file\t{mode}\t{record['size']}\t{record['sha256']}\t{path}"
            )
    text = "".join(line + "\n" for line in lines)
    digest = _sha256_bytes(text.encode("utf-8"))
    _safe_write_text(gen_dir / f"{tree_name}{ENTRIES_FILE_SUFFIX}", text)
    return digest


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _build_generation(
    tmp_gen_dir: Path,
    gbrain_state_dir: Path,
    vault_dir: Path,
    scans: Dict[str, List[Dict[str, Any]]],
) -> None:
    """Copy both source trees into the temp generation directory."""
    if tmp_gen_dir.exists():
        shutil.rmtree(tmp_gen_dir, ignore_errors=True)
    _safe_makedirs(tmp_gen_dir)
    _safe_makedirs(tmp_gen_dir / GBRAIN_TREE_NAME)
    _safe_makedirs(tmp_gen_dir / VAULT_TREE_NAME)
    copy_tree(gbrain_state_dir, scans[GBRAIN_TREE_NAME], tmp_gen_dir / GBRAIN_TREE_NAME)
    copy_tree(vault_dir, scans[VAULT_TREE_NAME], tmp_gen_dir / VAULT_TREE_NAME)


def export_generation(
    gbrain_state_dir: Path,
    vault_dir: Path,
    staging_dir: Path,
    *,
    gbrain_bin: str = GBRAIN_BIN,
    lock_path: str = TASKNOTES_LOCK,
    schema_pack_file: str = SCHEMA_PACK_FILE,
    convergence_attempts: int = CONVERGENCE_ATTEMPTS,
    retry_delay: float = CONVERGENCE_RETRY_DELAY,
    doctor_timeout: float = DOCTOR_TIMEOUT,
) -> Dict[str, Any]:
    """Create one immutable vault-recovery generation on the staging volume.

    The shared tasknotes lock must be held by the caller (validated). On any
    failure nothing is published: no generation dir, no READY, no `latest`
    update, and the temp dir is removed.
    """
    ensure_under_lock(lock_path)
    for label, root in (("gbrain state", gbrain_state_dir), ("vault", vault_dir)):
        if not root.is_dir():
            raise VaultRecoveryError(f"source {label} directory not found: {root}")

    _safe_makedirs(staging_dir)
    schema_pack = _active_schema_pack(schema_pack_file)

    # --- Preflight: strict actual doctor DB-open through the native binary.
    report = run_doctor(gbrain_bin, schema_pack, timeout=doctor_timeout)
    doctor_summary = validate_doctor_report(report)

    # --- Preflight: no active PGLite runtime artifacts before the copy.
    indicators = find_active_pglite_indicators(gbrain_state_dir)
    if indicators:
        raise ActiveIndicatorError(
            "active PGLite runtime artifacts found before copy; refusing to "
            f"snapshot: {', '.join(indicators[:10])}"
        )

    generation_id = _next_generation_id()
    if not is_valid_generation_id(generation_id):
        raise VaultRecoveryError(f"generated invalid generation id: {generation_id}")
    gen_dir = staging_dir / generation_id
    tmp_gen_dir = staging_dir / f".{generation_id}.tmp"

    # --- Whole-tree convergence: scan A -> copy -> scan B -> staged scan.
    attempt = 0
    last_error: Optional[Exception] = None
    scan_a: Optional[Dict[str, List[Dict[str, Any]]]] = None
    scan_b: Optional[Dict[str, List[Dict[str, Any]]]] = None
    staged_scans: Optional[Dict[str, List[Dict[str, Any]]]] = None

    # Previous `latest` pointer, restored if a durability failure happens
    # after the new pointer was already renamed into place.
    previous_latest: Optional[str] = None
    pointer = staging_dir / LATEST_POINTER_NAME
    if pointer.exists():
        try:
            previous_latest = pointer.read_text("utf-8")
        except OSError:
            previous_latest = None

    try:
        while attempt < convergence_attempts:
            attempt += 1
            last_error = None  # a previous failed attempt must not fail this one
            scan_a = {
                GBRAIN_TREE_NAME: scan_tree(gbrain_state_dir),
                VAULT_TREE_NAME: scan_tree(vault_dir),
            }
            _build_generation(tmp_gen_dir, gbrain_state_dir, vault_dir, scan_a)
            scan_b = {
                GBRAIN_TREE_NAME: scan_tree(gbrain_state_dir),
                VAULT_TREE_NAME: scan_tree(vault_dir),
            }
            if not (
                scans_equal(scan_a[GBRAIN_TREE_NAME], scan_b[GBRAIN_TREE_NAME])
                and scans_equal(scan_a[VAULT_TREE_NAME], scan_b[VAULT_TREE_NAME])
            ):
                last_error = ConvergenceError(
                    f"source tree changed during copy (attempt {attempt}/{convergence_attempts})"
                )
            else:
                staged_scans = {
                    GBRAIN_TREE_NAME: scan_tree(tmp_gen_dir / GBRAIN_TREE_NAME),
                    VAULT_TREE_NAME: scan_tree(tmp_gen_dir / VAULT_TREE_NAME),
                }
                if not (
                    scans_equal(scan_a[GBRAIN_TREE_NAME], staged_scans[GBRAIN_TREE_NAME])
                    and scans_equal(scan_a[VAULT_TREE_NAME], staged_scans[VAULT_TREE_NAME])
                ):
                    last_error = ConvergenceError(
                        f"staged tree does not match the converged source "
                        f"(attempt {attempt}/{convergence_attempts})"
                    )
                else:
                    break
            if tmp_gen_dir.exists():
                shutil.rmtree(tmp_gen_dir, ignore_errors=True)
            if attempt < convergence_attempts:
                time.sleep(retry_delay)
        if last_error is not None:
            raise last_error
        assert scan_a is not None and scan_b is not None and staged_scans is not None

        # --- Manifest (structural only; no note contents, no secrets).
        trees_manifest: Dict[str, Any] = {}
        for name in (GBRAIN_TREE_NAME, VAULT_TREE_NAME):
            root_mode = next(
                (r["mode"] for r in scan_a[name] if r["path"] == ""), None
            )
            # Phase-2 additive: the machine-readable entries index written
            # with the manifest (digest-bound) so the shell uploader/recover
            # steps can validate the full tree entry-by-entry.
            entries_digest = write_entries_index(
                tmp_gen_dir, name, staged_scans[name]
            )
            trees_manifest[name] = {
                **tree_stats(scan_a[name]),
                "root_mode": root_mode,
                "scan_digest": scan_digest(scan_a[name]),
                "staged_digest": scan_digest(staged_scans[name]),
                "entries_file": f"{name}{ENTRIES_FILE_SUFFIX}",
                "entries_digest": entries_digest,
            }
        manifest: Dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "generation_id": generation_id,
            "created_at_utc": _utc_now_iso(),
            "phase": 1,
            "remote": {
                "uploaded": False,
                "note": "local generations are uploaded/committed by the "
                "phase-2 encrypted uploader (scripts/vault-recovery-uploader.sh); "
                "the manifest is never rewritten after publication (staging "
                "mount is read-only to the uploader). The uploader's ack "
                "ledger is the remote-truth acknowledgement.",
            },
            "sources": {
                "gbrain_state_dir": str(gbrain_state_dir),
                "vault_dir": str(vault_dir),
            },
            "trees": trees_manifest,
            "doctor": doctor_summary,
            "convergence": {
                "attempts": attempt,
                "max_attempts": convergence_attempts,
                "source_scan_a_digest": scan_digest(
                    scan_a[GBRAIN_TREE_NAME] + scan_a[VAULT_TREE_NAME]
                ),
                "source_scan_b_digest": scan_digest(
                    scan_b[GBRAIN_TREE_NAME] + scan_b[VAULT_TREE_NAME]
                ),
            },
            "exporter": {"version": EXPORTER_VERSION, "python": sys.version.split()[0]},
        }
        # Self-check before publication: the exporter never publishes a
        # manifest that violates its own strict schema (council fix: strict
        # JSON schema validation).
        validate_manifest_schema(manifest)
        _safe_write_text(
            tmp_gen_dir / MANIFEST_NAME,
            json.dumps(manifest, indent=2, sort_keys=True),
        )
        # The whole staged tree + manifest must be durable before READY.
        _fsync_dir(tmp_gen_dir)

        # READY is written last inside the temp dir, then the dir is published
        # atomically, then the `latest` pointer. Every step is fsynced; any
        # durability failure rolls the publication back.
        _safe_write_text(tmp_gen_dir / READY_SENTINEL_NAME, f"{generation_id}\n")
        os.replace(tmp_gen_dir, gen_dir)
        _fsync_dir(staging_dir)
        _safe_write_text(pointer, f"{generation_id}\n")
    except BaseException:
        # Never leave a partially published generation behind: no generation
        # dir, no READY, no `latest` update. A `latest` pointer already
        # renamed into place is removed (first publication) or rolled back to
        # the previous generation (later publications), and the staging root
        # is fsynced so the rollback itself is durable. Rollback is best
        # effort — readers fail closed even if it cannot run.
        shutil.rmtree(tmp_gen_dir, ignore_errors=True)
        shutil.rmtree(gen_dir, ignore_errors=True)
        try:
            if previous_latest is not None and is_valid_generation_id(
                previous_latest.strip()
            ):
                # A prior generation exists: restore the `latest` pointer to
                # it (atomic rename + staging-root fsync inside
                # _safe_write_text).
                _safe_write_text(pointer, previous_latest)
            else:
                # First publication (or unreadable/invalid prior pointer):
                # remove the dangling newly-installed `latest` pointer so
                # nothing points at the deleted generation, then make the
                # removal durable.
                pointer.unlink(missing_ok=True)
                _fsync_dir(staging_dir)
        except Exception:
            pass
        raise
    return manifest


def list_generations(staging_dir: Path) -> List[Dict[str, Any]]:
    """List published generations with their manifests (READY required)."""
    out: List[Dict[str, Any]] = []
    if not staging_dir.exists():
        return out
    for entry in sorted(staging_dir.iterdir(), reverse=True):
        if not entry.is_dir() or not is_valid_generation_id(entry.name):
            continue
        if not (entry / READY_SENTINEL_NAME).exists():
            continue
        info: Dict[str, Any] = {"generation_id": entry.name, "path": str(entry)}
        manifest_path = entry / MANIFEST_NAME
        if manifest_path.exists():
            try:
                info["manifest"] = json.loads(manifest_path.read_text("utf-8"))
            except Exception:
                info["manifest"] = None
        out.append(info)
    return out


def read_latest(staging_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the latest generation's manifest via the atomic pointer."""
    pointer = staging_dir / LATEST_POINTER_NAME
    if not pointer.exists():
        return None
    try:
        generation_id = pointer.read_text("utf-8").strip()
    except OSError:
        return None
    if not is_valid_generation_id(generation_id):
        return None
    gen_dir = staging_dir / generation_id
    manifest_path = gen_dir / MANIFEST_NAME
    if not (gen_dir / READY_SENTINEL_NAME).exists() or not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except Exception:
        return None
    if manifest.get("generation_id") != generation_id:
        return None
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_from_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vault-recovery",
        description="Vault recovery export core (Phase 1: local staging only).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export", help="Create one vault-recovery generation.")
    p_export.add_argument(
        "--staging-dir",
        default=_default_from_env("VAULT_RECOVERY_STAGING_DIR", DEFAULT_STAGING_DIR),
    )
    p_export.add_argument(
        "--gbrain-state-dir", default=GBRAIN_STATE_DIR,
        help="Source .gbrain state tree (default: %(default)s)",
    )
    p_export.add_argument(
        "--vault-dir", default=VAULT_DIR,
        help="Source vault tree (default: %(default)s)",
    )
    p_export.add_argument(
        "--convergence-attempts", type=int,
        default=int(_default_from_env("VAULT_RECOVERY_CONVERGENCE_ATTEMPTS", str(CONVERGENCE_ATTEMPTS))),
    )
    p_export.add_argument(
        "--retry-delay", type=float,
        default=float(_default_from_env("VAULT_RECOVERY_RETRY_DELAY", str(CONVERGENCE_RETRY_DELAY))),
    )
    p_export.set_defaults(func=_cmd_export)

    p_list = sub.add_parser("list", help="List published generations.")
    p_list.add_argument(
        "--staging-dir",
        default=_default_from_env("VAULT_RECOVERY_STAGING_DIR", DEFAULT_STAGING_DIR),
    )
    p_list.set_defaults(func=_cmd_list)

    p_latest = sub.add_parser("latest", help="Print the latest generation manifest.")
    p_latest.add_argument(
        "--staging-dir",
        default=_default_from_env("VAULT_RECOVERY_STAGING_DIR", DEFAULT_STAGING_DIR),
    )
    p_latest.set_defaults(func=_cmd_latest)
    return parser


def _cmd_export(args: argparse.Namespace) -> int:
    if args.convergence_attempts < 1:
        print("[vault-recovery] convergence-attempts must be >= 1", file=sys.stderr)
        return 2
    if args.retry_delay < 0:
        print("[vault-recovery] retry-delay must be >= 0", file=sys.stderr)
        return 2
    manifest = export_generation(
        Path(args.gbrain_state_dir),
        Path(args.vault_dir),
        Path(args.staging_dir),
        convergence_attempts=args.convergence_attempts,
        retry_delay=args.retry_delay,
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        # The core CLI boundary: only the actual Hermes runtime user may run
        # the exporter (root and arbitrary non-Hermes uids are rejected).
        # The shell wrappers/cron enforce the same identity before invoking
        # this core (defense in depth, not a substitute).
        ensure_hermes_identity()
        return args.func(args)
    except VaultRecoveryError as exc:
        print(f"[vault-recovery] ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[vault-recovery] UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
