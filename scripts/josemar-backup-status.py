#!/opt/hermes/.venv/bin/python3 -I
"""josemar-backup-status - bounded direct local reader for backup staging lanes.

Read-only status tool for the two Hermes-side backup lanes:

  * vault     -> vault-recovery generations   (/opt/data/vault-recovery/staging)
  * mnemosyne -> mnemosyne encrypted backups  (/opt/data/mnemosyne-backup/staging)

Exact CLI: ``josemar-backup-status <vault|mnemosyne> <list|latest>``.

Contract (Oracle-approved direct local reader):

 1. FIXED staging roots. The staging roots are module constants and are NEVER
    env-overridable: no environment variable can redirect this reader (env
    path poisoning is impossible by construction).
 2. Identity. The reader must run as the actual system `hermes` user: euid 0
    (root) is always denied; the euid must equal the system `hermes` user's
    uid resolved via pwd(5). When HERMES_UID is set it must be a nonzero
    ASCII-decimal string AND equal the system `hermes` uid - the env alone
    never authorizes anything; if the system `hermes` user does not exist the
    reader fails closed.
 3. No-follow descriptor-relative traversal, fully anchored. The absolute
    staging root is descended component by component from ``/`` with
    openat-style O_NOFOLLOW|O_DIRECTORY opens (every intermediate component
    is lstat-verified and inode/device-identity-verified against the opened
    fd, so no pathname is ever re-resolved and an intermediate symlink or a
    same-type component swap is rejected). Every directory and every
    READY/manifest/latest file is opened O_NOFOLLOW|O_NONBLOCK relative to a
    directory fd (directories additionally O_DIRECTORY): a FIFO/other
    special is rejected IMMEDIATELY instead of blocking on open, and fstat
    re-verifies both the type AND the (st_dev, st_ino) identity captured by
    the preceding lstat, so a same-type TOCTOU swap (e.g. a regular file
    replaced by another regular file with a different inode) fails the run
    closed with the stable error code `rejected`. Symlinks, special files
    (fifo/socket/device), non-dir entries where a generation dir is
    expected, and any type/identity change fail the whole run closed.
 4. Strictly bounded. Directory depth, directory count, regular-file count,
    regular-file byte total, READY bytes, manifest bytes, snapshot count,
    per-directory entry count (enumeration is buffered in bounded slices
    and stopped once the cap is exceeded - a directory with an unbounded
    number of entries can never force unbounded materialization), and the
    emitted JSON are all bounded by module constants; exceeding any
    traversal/output cap sets ``truncated: true`` (the report is partial and
    says so). An oversized/malformed READY or manifest is a per-snapshot
    observation (``local_ready_manifest_observation.ready`` /
    ``.manifest`` false), never a crash and never a leak.
 5. Lane READY rules. vault READY must be EXACTLY ``<id>\\n``. mnemosyne
    READY must be EXACTLY ``<id>\\n<sha256>\\n`` with a 64-lowercase-hex
    sha256 that BINDS to the generation's ``manifest.json``
    ``artifact.sha256`` (and the manifest's ``generation_id`` must bind to
    the directory id); without a binding manifest the mnemosyne READY is not
    valid. Generation ids must embed a REAL UTC calendar/time value, not
    just the shape: the 20-character timestamp prefix is range-validated
    (year 0001-9999, real month/day including leap years, hour/minute/second
    00-59, microseconds 000000-999999) before any timestamp is derived.
 6. Output. Success output is strict bounded JSON (fixed key order):
    schema_version, lane, operation, scope="local_staging",
    remote_status="unknown_operator_only" (this reader never touches remote
    state), truncated, snapshots[]. Each snapshot carries generation_id,
    timestamp (derived from the id, never from mtime),
    local_ready_manifest_observation {ready, manifest} (the local-only
    marker-manifest observation), total_regular_file_count,
    total_regular_file_bytes.
 7. Failures. Every failure emits ONLY a bounded fixed JSON object on stdout
    with a stable error code (usage/identity/staging/rejected/invalid/
    internal) and a fixed message - no traceback, no paths, no raw exception
    text, nothing on stderr - and exits nonzero.
 8. No subprocess/shell/network/Docker/rclone/PGLite/locks. Standard library
    only (json, os, pwd, re, stat, sys).

The production staging roots and caps are module-level constants; public
functions take keyword test seams only (the CLI entrypoint always uses the
constants, mirroring scripts/vault_recovery_core.py).
"""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants (fixed production paths; never env-overridable at runtime)
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
SCOPE = "local_staging"
REMOTE_STATUS = "unknown_operator_only"

VAULT_STAGING_ROOT = "/opt/data/vault-recovery/staging"
MNEMOSYNE_STAGING_ROOT = "/opt/data/mnemosyne-backup/staging"

LANES = ("vault", "mnemosyne")
OPERATIONS = ("list", "latest")

READY_NAME = "READY"
MANIFEST_NAME = "manifest.json"
LATEST_POINTER_NAME = "latest"

# Generation id: lexically sortable UTC timestamp + 8-hex suffix, e.g.
# 20260802T012247123456Z-a1b2c3d4 (exactly 31 chars).
GENERATION_ID_RE = re.compile(r"^\d{8}T\d{6}\d{6}Z-[0-9a-f]{8}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Bounds. Exceeding a traversal/output bound sets truncated=True (partial
# report, flagged); exceeding READY/manifest byte caps marks the snapshot's
# observation false (input invalid, not truncation).
MAX_DEPTH = 64
MAX_DIRS = 1 << 16
MAX_FILES = 1 << 16
MAX_BYTES = 1 << 39  # 512 GiB of staged regular-file bytes
MAX_SNAPSHOTS = 128
MAX_DIR_ENTRIES = 1 << 16  # per-directory enumeration buffer cap
MAX_READY_BYTES = 256
MAX_MANIFEST_BYTES = 1 << 20  # 1 MiB

# Stable failure codes -> stable exit codes. Messages are FIXED (no paths,
# no input, no exception text): bounded and leak-free by construction.
FAILURE_EXIT_CODES = {
    "usage": 2,
    "identity": 3,
    "staging": 4,
    "rejected": 5,
    "invalid": 6,
    "internal": 7,
}
FAILURE_MESSAGES = {
    "usage": "invalid arguments; usage: josemar-backup-status <vault|mnemosyne> <list|latest>",
    "identity": "refusing to run: must run as the non-root system hermes user; HERMES_UID when set must be a nonzero decimal matching the system hermes uid",
    "staging": "staging root exists but is not an accessible directory",
    "rejected": "refused to continue: unexpected filesystem condition during scan (symlink, special file, or changed entry type)",
    "invalid": "staging state is invalid: latest pointer is malformed or points at a missing generation",
    "internal": "internal error",
}


class _ReaderError(Exception):
    """Internal control-flow error carrying a stable failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ScanStop(Exception):
    """Raised when a traversal bound is exceeded (report becomes truncated)."""


class _ScanState:
    __slots__ = ("lane", "files", "bytes_total", "dirs", "truncated")

    def __init__(self, lane: str) -> None:
        self.lane = lane
        self.files = 0
        self.bytes_total = 0
        self.dirs = 0
        self.truncated = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_valid_generation_id(gen_id: Any) -> bool:
    """Strict generation-id validation: exact shape (no slash, no traversal)
    AND a REAL UTC calendar/time value in the embedded timestamp prefix.

    The 22-character prefix ``YYYYmmddTHHMMSSffffffZ`` is range-validated
    with ``datetime.strptime`` (standard library only): year 0001-9999, real
    month/day including leap years (e.g. 20240229 valid, 20230229 and
    19000229 invalid), hour/minute/second 00-59, microseconds
    000000-999999. Impossible values (month 13, day 00/32, hour 24, ...) are
    rejected before any timestamp is derived from the id.
    """
    if not isinstance(gen_id, str):
        return False
    if len(gen_id) != 31:
        return False
    if not GENERATION_ID_RE.match(gen_id):
        return False
    try:
        datetime.strptime(gen_id[:22], "%Y%m%dT%H%M%S%fZ")
    except ValueError:
        return False
    return True


def _timestamp_from_id(gen_id: str) -> str:
    """Derive the ISO-8601 UTC timestamp from the id's embedded timestamp.

    ``20260802T012247123456Z-a1b2c3d4`` -> ``2026-08-02T01:22:47.123456Z``.
    Purely lexical (the id is regex-validated beforehand); never mtime-based.
    """
    return "{}T{}:{}:{}.{}Z".format(
        f"{gen_id[:4]}-{gen_id[4:6]}-{gen_id[6:8]}",
        gen_id[9:11],
        gen_id[11:13],
        gen_id[13:15],
        gen_id[15:21],
    )


# ---------------------------------------------------------------------------
# Identity (issue #110 conventions; env never authorizes alone)
# ---------------------------------------------------------------------------


def resolve_system_hermes_uid() -> Optional[int]:
    """The system `hermes` user's uid via pwd(5), or None when absent.

    There is deliberately NO default fallback: the spec requires the euid to
    equal the system `hermes` uid, so a machine without that user cannot be
    authorized for this tool at all (fail closed).
    """
    try:
        return pwd.getpwnam("hermes").pw_uid
    except (KeyError, ImportError):
        return None


def check_identity() -> None:
    """Enforce the reader's identity contract; raises ``_ReaderError`` with
    code ``identity`` on any violation:

      * euid 0 (root) is always denied;
      * the system `hermes` user must exist and be non-root;
      * HERMES_UID, when set, must be a nonzero ASCII-decimal string that
        EXACTLY matches the system `hermes` uid (env alone never authorizes:
        the euid must equal the system uid regardless);
      * the effective uid must equal the system `hermes` uid.
    """
    euid = os.geteuid()
    if euid == 0:
        raise _ReaderError("identity")
    system_uid = resolve_system_hermes_uid()
    if system_uid is None or system_uid <= 0:
        raise _ReaderError("identity")
    raw = os.environ.get("HERMES_UID")
    if raw:
        if not re.fullmatch(r"[0-9]+", raw):
            raise _ReaderError("identity")
        configured = int(raw)
        if configured <= 0 or configured != system_uid:
            raise _ReaderError("identity")
    if euid != system_uid:
        raise _ReaderError("identity")


# ---------------------------------------------------------------------------
# No-follow descriptor-relative primitives
# ---------------------------------------------------------------------------


def _open_dir_no_follow(name: str, dir_fd: Optional[int] = None, expected: Optional[tuple] = None) -> int:
    """openat-style O_NOFOLLOW|O_DIRECTORY|O_NONBLOCK open (a symlink at the
    final component is ELOOP, a FIFO/special is rejected by O_DIRECTORY
    without blocking). fstat re-verifies the type and, when ``expected``
    (st_dev, st_ino) is given, the inode/device identity captured by the
    preceding lstat (same-type TOCTOU swap rejected). Raises
    ``_ReaderError`` with code ``rejected`` on any violation."""
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=dir_fd,
        )
    except OSError:
        raise _ReaderError("rejected") from None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise _ReaderError("rejected") from None
    if not stat.S_ISDIR(st.st_mode):
        os.close(fd)
        raise _ReaderError("rejected")
    if expected is not None and (st.st_dev, st.st_ino) != expected:
        os.close(fd)
        raise _ReaderError("rejected")
    return fd


def _open_regular_no_follow(name: str, dir_fd: int, expected: Optional[tuple] = None) -> int:
    """O_NOFOLLOW|O_NONBLOCK open of a regular file (a FIFO/special opens
    immediately in nonblocking mode and is then rejected by the fstat type
    check instead of hanging). fstat re-verifies the type AND, when
    ``expected`` (st_dev, st_ino) is given, the inode/device identity
    captured by the preceding lstat (race between lstat and open fails
    closed, including same-type swaps)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        raise _ReaderError("rejected") from None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise _ReaderError("rejected") from None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise _ReaderError("rejected")
    if expected is not None and (st.st_dev, st.st_ino) != expected:
        os.close(fd)
        raise _ReaderError("rejected")
    return fd


def _read_bounded(
    name: str, dir_fd: int, cap: int, expected: Optional[tuple] = None
) -> Tuple[bytes, bool]:
    """Read ``name`` (dir_fd-relative, O_NOFOLLOW|O_NONBLOCK) up to ``cap``
    bytes.

    Returns ``(data, oversized)``: ``oversized`` is True when the file
    exceeds ``cap`` (the caller treats the input as invalid, never reads on).
    """
    fd = _open_regular_no_follow(name, dir_fd, expected=expected)
    try:
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                return (b"", True)
            chunks.append(chunk)
        return (b"".join(chunks), False)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Per-generation observation
# ---------------------------------------------------------------------------


def _manifest_sha256(manifest: Any) -> Optional[str]:
    """The generation's binding artifact sha256, or None when absent/invalid."""
    if not isinstance(manifest, dict):
        return None
    try:
        sha = manifest["artifact"]["sha256"]
    except (KeyError, TypeError):
        return None
    if not isinstance(sha, str) or not _HEX64_RE.match(sha):
        return None
    return sha


def _parse_manifest(data: bytes, oversized: bool, gen_id: str) -> Optional[Dict[str, Any]]:
    """Bounded manifest parse + generation binding.

    Valid only when: not oversized, valid UTF-8 JSON, a JSON object, and
    ``generation_id`` exactly equals the directory id. Returns the parsed
    object when valid, else None.
    """
    if oversized or data is None:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("generation_id") != gen_id:
        return None
    return obj


def _ready_ok(
    lane: str,
    gen_id: str,
    ready_data: Optional[bytes],
    ready_oversized: bool,
    manifest: Optional[Dict[str, Any]],
) -> bool:
    """Lane READY rules:

      * vault: READY content EXACTLY ``<id>\\n``.
      * mnemosyne: READY content EXACTLY ``<id>\\n<64-lowercase-hex>\\n`` AND
        the sha256 binds to the manifest's ``artifact.sha256`` (a missing or
        invalid manifest makes the READY invalid - fail closed).
    """
    if ready_oversized or ready_data is None:
        return False
    if lane == "vault":
        return ready_data == (gen_id + "\n").encode("ascii")
    prefix = (gen_id + "\n").encode("ascii")
    if not ready_data.startswith(prefix):
        return False
    rest = ready_data[len(prefix):]
    if len(rest) != 65 or not rest.endswith(b"\n"):
        return False
    try:
        sha = rest[:-1].decode("ascii")
    except UnicodeDecodeError:
        return False
    if not _HEX64_RE.match(sha):
        return False
    manifest_sha = _manifest_sha256(manifest)
    return manifest_sha is not None and manifest_sha == sha


def _walk_dir(
    state: _ScanState,
    dir_fd: int,
    depth: int,
    top_level: bool,
    gen_id: str,
) -> Tuple[Optional[bytes], bool, Optional[Dict[str, Any]]]:
    """Scan one directory (fd-relative, no-follow): count regular files and
    bytes, descend into subdirectories (O_NOFOLLOW), and at the top level of
    a generation additionally read READY/manifest.

    Enumeration is BUFFERED IN BOUNDED SLICES: entries are collected up to
    ``MAX_DIR_ENTRIES`` and the scan iterator is stopped at the cap (a
    directory with an unbounded number of entries - valid or invalid - can
    never force unbounded materialization); the buffered slice is sorted for
    deterministic processing, and a cap overflow raises ``_ScanStop`` after
    the slice is processed (report flagged truncated by the caller). Every
    opened child AND every counted regular payload file is
    identity-verified (st_dev, st_ino) against its lstat via an
    O_NOFOLLOW|O_NONBLOCK descriptor-relative open + fstat, so a same-type
    swap or a swap to a special file in ANY file position is rejected before
    it is counted.

    Returns ``(ready_data, ready_oversized, manifest)`` (meaningful only at
    top level). Raises ``_ReaderError("rejected")`` on any symlink, special
    file, type change, or identity change; raises ``_ScanStop`` when a
    traversal bound is exceeded (the caller flags the report truncated).
    """
    ready_data: Optional[bytes] = None
    ready_oversized = False
    manifest: Optional[Dict[str, Any]] = None
    overflow = False
    with os.scandir(dir_fd) as it:
        buffered = []
        for entry in it:
            if len(buffered) >= MAX_DIR_ENTRIES:
                overflow = True
                break
            buffered.append(entry)
        for entry in sorted(buffered, key=lambda e: e.name):
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                raise _ReaderError("rejected") from None
            if stat.S_ISLNK(st.st_mode):
                raise _ReaderError("rejected")
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise _ReaderError("rejected")
            identity = (st.st_dev, st.st_ino)
            if top_level and entry.name in (READY_NAME, MANIFEST_NAME):
                if not stat.S_ISREG(st.st_mode):
                    raise _ReaderError("rejected")
                state.files += 1
                state.bytes_total += st.st_size
                if state.files > MAX_FILES or state.bytes_total > MAX_BYTES:
                    raise _ScanStop()
                if entry.name == READY_NAME:
                    ready_data, ready_oversized = _read_bounded(
                        entry.name, dir_fd, MAX_READY_BYTES, expected=identity
                    )
                else:
                    data, oversized = _read_bounded(
                        entry.name, dir_fd, MAX_MANIFEST_BYTES, expected=identity
                    )
                    manifest = _parse_manifest(data, oversized, gen_id)
            elif stat.S_ISDIR(st.st_mode):
                if depth + 1 > MAX_DEPTH:
                    raise _ScanStop()
                child = _open_dir_no_follow(
                    entry.name, dir_fd=dir_fd, expected=identity
                )
                state.dirs += 1
                if state.dirs > MAX_DIRS:
                    os.close(child)
                    raise _ScanStop()
                try:
                    _walk_dir(state, child, depth + 1, False, gen_id)
                finally:
                    os.close(child)
            else:  # regular file: hardened counting - every payload file is
                # opened O_NOFOLLOW|O_NONBLOCK relative to the dir fd and
                # fstat-verified (regular type + lstat inode/device identity)
                # before it is counted, so a same-type swap or a swap to a
                # special (fifo/socket) between lstat and open is rejected
                # and can never inflate the aggregate counts.
                fd = _open_regular_no_follow(entry.name, dir_fd=dir_fd, expected=identity)
                try:
                    st_fd = os.fstat(fd)
                finally:
                    os.close(fd)
                state.files += 1
                state.bytes_total += st_fd.st_size
                if state.files > MAX_FILES or state.bytes_total > MAX_BYTES:
                    raise _ScanStop()
        if overflow:
            raise _ScanStop()
    return (ready_data, ready_oversized, manifest)


def _scan_generation(state: _ScanState, gen_fd: int, gen_id: str) -> Dict[str, Any]:
    """Build the snapshot observation for one generation directory.

    Counts are per-generation; bounds are global (shared ``state``). A bound
    overflow marks ``state.truncated`` and returns the partial observation.
    """
    files_before = state.files
    bytes_before = state.bytes_total
    try:
        ready_data, ready_oversized, manifest = _walk_dir(
            state, gen_fd, 0, True, gen_id
        )
    except _ScanStop:
        state.truncated = True
        ready_data, ready_oversized, manifest = None, False, None
    manifest_valid = manifest is not None
    ready_ok = _ready_ok(state.lane, gen_id, ready_data, ready_oversized, manifest)
    return {
        "generation_id": gen_id,
        "timestamp": _timestamp_from_id(gen_id),
        "local_ready_manifest_observation": {"ready": ready_ok, "manifest": manifest_valid},
        "total_regular_file_count": state.files - files_before,
        "total_regular_file_bytes": state.bytes_total - bytes_before,
    }


def _snapshot_for_gen_dir(state: _ScanState, root_fd: int, gen_id: str) -> Dict[str, Any]:
    """Open one generation directory (root-relative, no-follow) and build its
    snapshot. The entry is lstat-verified first and the opened fd is
    inode/device-identity-verified against that lstat (same-type TOCTOU swap
    rejected)."""
    try:
        entry_st = os.stat(gen_id, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        raise _ReaderError("rejected") from None
    if not stat.S_ISDIR(entry_st.st_mode):
        raise _ReaderError("rejected")
    fd = _open_dir_no_follow(
        gen_id, dir_fd=root_fd, expected=(entry_st.st_dev, entry_st.st_ino)
    )
    try:
        return _scan_generation(state, fd, gen_id)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Public reader (test seams only: staging_root keyword)
# ---------------------------------------------------------------------------


def _empty_result(lane: str, operation: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "lane": lane,
        "operation": operation,
        "scope": SCOPE,
        "remote_status": REMOTE_STATUS,
        "truncated": False,
        "snapshots": [],
    }


def _open_staging_root(staging_root: str) -> Optional[int]:
    """Open the fixed staging root fully anchored; returns an fd, or None
    when missing.

    The absolute path is descended component by component from ``/`` with
    openat-style opens: every intermediate component is opened with
    O_DIRECTORY|O_NOFOLLOW|O_NONBLOCK relative to the already-verified parent
    fd (no pathname is ever re-resolved), and each component's lstat
    baseline is verified against the opened fd's inode/device identity, so a
    symlinked/racing intermediate component and a same-type component swap
    are rejected.

    A missing root (any component missing) is a legitimate "nothing staged
    yet" state (the exporters also treat it as empty); a root that exists
    but cannot be opened as a real directory chain (symlink, regular file,
    unreadable, identity change) fails closed with code ``staging``.
    """
    abs_root = os.path.abspath(staging_root)
    components = [comp for comp in abs_root.split("/") if comp]
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise _ReaderError("staging") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise _ReaderError("staging")
        for comp in components:
            try:
                entry_st = os.stat(comp, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.close(fd)
                return None
            except OSError:
                raise _ReaderError("staging") from None
            if not stat.S_ISDIR(entry_st.st_mode):
                raise _ReaderError("staging")
            try:
                child = os.open(
                    comp,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                os.close(fd)
                return None
            except OSError:
                raise _ReaderError("staging") from None
            try:
                child_st = os.fstat(child)
            except OSError:
                os.close(child)
                raise _ReaderError("staging") from None
            if not stat.S_ISDIR(child_st.st_mode) or (
                child_st.st_dev, child_st.st_ino
            ) != (entry_st.st_dev, entry_st.st_ino):
                os.close(child)
                raise _ReaderError("staging")
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_status(
    lane: str, operation: str, staging_root: Optional[str] = None
) -> Dict[str, Any]:
    """Read the local staging status for one lane. ``staging_root`` is a test
    seam only; the CLI always uses the fixed module constants.

    Returns the success dict (or raises ``_ReaderError`` on fail-closed
    conditions). Unknown lane/operation are programming errors (the CLI
    validates argv before calling) and raise ValueError.
    """
    if lane not in LANES:
        raise ValueError(f"unknown lane: {lane!r}")
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation: {operation!r}")
    root = staging_root
    if root is None:
        root = VAULT_STAGING_ROOT if lane == "vault" else MNEMOSYNE_STAGING_ROOT
    root_fd = _open_staging_root(root)
    if root_fd is None:
        return _empty_result(lane, operation)
    try:
        if operation == "latest":
            return _read_latest(lane, root_fd)
        return _read_list(lane, root_fd)
    finally:
        os.close(root_fd)


def _read_list(lane: str, root_fd: int) -> Dict[str, Any]:
    state = _ScanState(lane)
    snapshots: List[Dict[str, Any]] = []
    try:
        overflow = False
        with os.scandir(root_fd) as it:
            buffered = []
            for entry in it:
                if len(buffered) >= MAX_DIR_ENTRIES:
                    overflow = True
                    break
                buffered.append(entry)
            for entry in sorted(buffered, key=lambda e: e.name):
                if not is_valid_generation_id(entry.name):
                    continue  # non-generation entries are never opened/followed
                state.dirs += 1
                if state.dirs > MAX_DIRS:
                    raise _ScanStop()
                snapshots.append(_snapshot_for_gen_dir(state, root_fd, entry.name))
            if overflow:
                raise _ScanStop()
    except _ScanStop:
        state.truncated = True
    snapshots.sort(key=lambda s: s["generation_id"], reverse=True)
    if len(snapshots) > MAX_SNAPSHOTS:
        snapshots = snapshots[:MAX_SNAPSHOTS]
        state.truncated = True
    return {
        "schema_version": SCHEMA_VERSION,
        "lane": lane,
        "operation": "list",
        "scope": SCOPE,
        "remote_status": REMOTE_STATUS,
        "truncated": state.truncated,
        "snapshots": snapshots,
    }


def _read_latest(lane: str, root_fd: int) -> Dict[str, Any]:
    state = _ScanState(lane)
    # The `latest` pointer must be a regular file whose content is EXACTLY
    # `<id>\n` with a valid generation id; anything else is corrupt state.
    # It is lstat-verified first (symlink/special rejected without opening),
    # then opened O_NOFOLLOW|O_NONBLOCK (a FIFO swapped in at open time
    # cannot block the reader) and inode/device-identity-verified.
    try:
        entry_st = os.stat(LATEST_POINTER_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _empty_result(lane, "latest")
    except OSError:
        raise _ReaderError("rejected") from None
    if not stat.S_ISREG(entry_st.st_mode):
        raise _ReaderError("rejected")
    try:
        pfd = os.open(
            LATEST_POINTER_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=root_fd,
        )
    except OSError:
        raise _ReaderError("rejected") from None
    try:
        st = os.fstat(pfd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (
            entry_st.st_dev,
            entry_st.st_ino,
        ):
            raise _ReaderError("rejected")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(pfd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_READY_BYTES:
                raise _ReaderError("invalid")
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(pfd)
    if not data.endswith(b"\n"):
        raise _ReaderError("invalid")
    try:
        gen_id = data[:-1].decode("ascii")
    except UnicodeDecodeError:
        raise _ReaderError("invalid") from None
    if not is_valid_generation_id(gen_id):
        raise _ReaderError("invalid")
    # Resolve the pointed-to generation: a dangling pointer is corrupt state.
    # lstat + no-follow open + inode/device identity verification, so a
    # same-type swap of the pointed-to directory is rejected.
    try:
        entry_st = os.stat(gen_id, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise _ReaderError("invalid") from None
    except OSError:
        raise _ReaderError("rejected") from None
    if not stat.S_ISDIR(entry_st.st_mode):
        raise _ReaderError("rejected")
    gfd = _open_dir_no_follow(
        gen_id, dir_fd=root_fd, expected=(entry_st.st_dev, entry_st.st_ino)
    )
    try:
        snapshot = _scan_generation(state, gfd, gen_id)
    finally:
        os.close(gfd)
    return {
        "schema_version": SCHEMA_VERSION,
        "lane": lane,
        "operation": "latest",
        "scope": SCOPE,
        "remote_status": REMOTE_STATUS,
        "truncated": state.truncated,
        "snapshots": [snapshot],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(obj: Dict[str, Any]) -> bool:
    """Write one bounded JSON line to stdout (nothing ever goes to stderr)."""
    try:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def _failure(code: str, lane: Optional[str], operation: Optional[str]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "lane": lane,
        "operation": operation,
        "scope": SCOPE,
        "remote_status": REMOTE_STATUS,
        "truncated": False,
        "ok": False,
        "error": {"code": code, "message": FAILURE_MESSAGES[code]},
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Exact argv: ``[vault|mnemosyne] [list|latest]``. Never writes to
    stderr; every failure path returns a nonzero exit code with bounded JSON."""
    if argv is None:
        argv = sys.argv[1:]
    lane: Optional[str] = None
    operation: Optional[str] = None
    if (
        isinstance(argv, list)
        and len(argv) == 2
        and argv[0] in LANES
        and argv[1] in OPERATIONS
    ):
        lane, operation = argv[0], argv[1]
    else:
        _emit(_failure("usage", None, None))
        return FAILURE_EXIT_CODES["usage"]
    assert lane is not None and operation is not None
    try:
        check_identity()
    except _ReaderError as exc:
        _emit(_failure(exc.code, lane, operation))
        return FAILURE_EXIT_CODES[exc.code]
    except BaseException:
        _emit(_failure("internal", lane, operation))
        return FAILURE_EXIT_CODES["internal"]
    try:
        result = read_status(lane, operation)
    except _ReaderError as exc:
        _emit(_failure(exc.code, lane, operation))
        return FAILURE_EXIT_CODES[exc.code]
    except BaseException:
        _emit(_failure("internal", lane, operation))
        return FAILURE_EXIT_CODES["internal"]
    if not _emit(result):
        return FAILURE_EXIT_CODES["internal"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
