#!/opt/hermes/.venv/bin/python3 -I
"""Vault recovery verify/install core (Phase 2) - Hermes-side.

Consumes a verified recovery handoff produced by the short-lived rclone
download step (scripts/vault-recovery-recover.sh, which runs in an rclone
image and writes RECOVERY_READY into the disposable recovery volume) and
provides three operator commands:

  verify   - validate the restored FULL bundle (manifest, entries digest,
             exact tree re-scan against the manifest-bound entries index)
             and run the PINNED doctor against a DISPOSABLE copy of the
             restored `.gbrain` (never opens/mutates the live state).
             Writes VERIFIED_READY only on full success; any stale
             VERIFIED_READY is removed (durably) up front so the sentinel
             can only ever reflect the most recent verification.
  install  - explicit operator confirmation required; journaled two-tree
             rollback transaction. Both trees are staged on the DESTINATION
             filesystems (fresh paths, no in-place overwrites) and swapped:
             - `.gbrain` (plain dir): single atomic rename swap.
             - vault: the backup/staging root must live INSIDE the live
               vault tree (same filesystem as the vault volume), so
               rename(2) cannot move the whole tree (EINVAL for a plain
               dir; EBUSY when the live vault root is the mount point of a
               mounted volume, as in production) and a journaled
               per-top-level-entry swap is used. Every rename is journaled
               WRITE-AHEAD (the step is fsynced as "pending" before the
               rename and flipped to "done" after); any failure triggers an
               automatic rollback that restores the previous live trees, and
               a crash mid-transaction is recoverable by the operator
               `rollback` command (pending steps are probed against the
               filesystem and undone idempotently).
  rollback - reverse a journaled transaction from its journal (crash
             recovery / operator-driven rollback).

Security/operational contract (issue #110 conventions):
  - Runs ONLY as the actual Hermes runtime user (root and arbitrary
    non-Hermes uids are rejected, same boundary as the exporter).
  - The install and the rollback acquire the shared TaskNotes/gbrain
    cooperative lock (/opt/data/.locks/tasknotes.lock) EXCLUSIVELY and
    nonblocking: if any gbrain user (TaskNotes MCP, refresh cron, export) is
    active the install/rollback refuses. The VERIFY step takes the same
    exclusive nonblocking lock while it writes VERIFIED_READY into the
    shared recovery handoff (fix 5: the verified handoff is consumed under
    the lock, so a concurrent verifier can never replace the sentinel an
    install is reading). The install acquires the lock BEFORE any handoff
    read/validation and retains it through the whole transaction; the
    bundle is re-validated immediately before the first mutation, closing
    the lock-less rclone recover-step replacement window.
  - Never calls the public `gbrain` adapter or `josemar-gbrain` (no nested
    lock); doctor access is a direct invocation of the private pinned native
    binary against disposable state.
  - The install requires the explicit confirmation flag
    `--i-confirm-this-overwrites-production`; there is no automated
    production overwrite.

The module is import-safe and standard-library only. Production paths are
module-level constants; keyword parameters on the public functions are test
seams only (the CLI always uses the constants).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# The wrapper invokes this module with `python -I` (isolated mode). Since
# Python 3.11, -I implies -P, which does NOT put the script's directory on
# sys.path — so the sibling exporter core would not be importable. Make the
# module's own directory importable explicitly (a no-op when this module is
# loaded as a library with the name already resolvable).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Reuse the exporter core's public machinery (same repo, same container).
import vault_recovery_core as core

GBRAIN_BIN = core.GBRAIN_BIN
TASKNOTES_LOCK = core.TASKNOTES_LOCK
SCHEMA_PACK_FILE = core.SCHEMA_PACK_FILE
DEFAULT_SCHEMA_PACK = core.DEFAULT_SCHEMA_PACK
DOCTOR_TIMEOUT = core.DOCTOR_TIMEOUT

DEFAULT_RECOVERY_DIR = "/recovery"
# Installer-owned hidden dirs inside each destination filesystem. The vault
# staging/backup root lives INSIDE the vault volume (same fs as the live
# vault); the .gbrain staging/backup root lives beside the live .gbrain
# (same fs). Both are excluded from the per-entry swap.
INSTALL_DIR_NAME = ".vault-recovery-install"
DEFAULT_JOURNAL_ROOT = "/opt/data/vault-recovery/install-journal"

RECOVERY_READY_NAME = "RECOVERY_READY"
VERIFIED_READY_NAME = "VERIFIED_READY"
MANIFEST_NAME = "manifest.json"
READY_SENTINEL_NAME = "READY"
VAULT_TREE_NAME = "vault"
GBRAIN_TREE_NAME = ".gbrain"
ENTRIES_FILE_SUFFIX = core.ENTRIES_FILE_SUFFIX

FILE_MODE = 0o600
DIR_MODE = 0o700
JOURNAL_SCHEMA_VERSION = 1
# The manifest schema version this restore core can install (must match the
# exporter's MANIFEST_SCHEMA_VERSION; future schemas are refused).
MANIFEST_SCHEMA_VERSION = 1
# Handoff sentinels (READY, RECOVERY_READY, VERIFIED_READY) are machine-
# written with a few short lines; reads are bounded so an oversized/garbage
# sentinel can never drive unbounded input processing.
SENTINEL_MAX_BYTES = 4096
# The install journal is machine-written and small (a few steps per top-
# level vault entry); bound the read as a DoS guard before parsing.
JOURNAL_MAX_BYTES = 16 * 1024 * 1024


class VaultRecoveryRestoreError(RuntimeError):
    """Base error for the restore core."""


class IdentityError(VaultRecoveryRestoreError):
    """Not running as the actual Hermes runtime user."""


class LockError(VaultRecoveryRestoreError):
    """A gbrain user holds the shared lock; install refuses."""


class HandoffError(VaultRecoveryRestoreError):
    """Recovery handoff missing/inconsistent."""


class ValidationError(VaultRecoveryRestoreError):
    """The bundle failed full validation."""


class InstallError(VaultRecoveryRestoreError):
    """Install refused or the journaled transaction failed."""


class JournalError(VaultRecoveryRestoreError):
    """Journal missing/corrupt; rollback cannot proceed."""


# ---------------------------------------------------------------------------
# Small local helpers (durability + atomic writes, same contract as the
# exporter core)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise VaultRecoveryRestoreError(
            f"cannot open directory {path} for fsync: {exc}"
        ) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise VaultRecoveryRestoreError(
            f"fsync failed for directory {path}: {exc}"
        ) from exc
    finally:
        os.close(fd)


def _safe_write_text(path: Path, text: str) -> None:
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
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _active_schema_pack(marker_path: str) -> str:
    try:
        pack = Path(marker_path).read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_SCHEMA_PACK
    if pack and all(c.isalnum() or c in "._-" for c in pack):
        return pack
    return DEFAULT_SCHEMA_PACK


# ---------------------------------------------------------------------------
# Handoff / entries helpers
# ---------------------------------------------------------------------------


def _sentinel_lines(path: Path) -> List[str]:
    """Read a handoff sentinel file, bounded. Raises HandoffError when the
    file is missing, oversized, or unreadable."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HandoffError(f"cannot stat {path}: {exc}") from exc
    if size > SENTINEL_MAX_BYTES:
        raise HandoffError(
            f"{path} is oversized ({size} bytes > {SENTINEL_MAX_BYTES}); refusing"
        )
    try:
        return path.read_text("utf-8").splitlines()
    except OSError as exc:
        raise HandoffError(f"cannot read {path}: {exc}") from exc


def _read_first_line(path: Path) -> str:
    lines = _sentinel_lines(path)
    return lines[0].strip() if lines else ""


def parse_entries_file(path: Path) -> List[Dict[str, Any]]:
    """Parse a manifest-bound entries index back into scan records.

    Line format (machine-written by the exporter)::

        file\t<mode-octal>\t<size>\t<sha256>\t<path>   (root path ".")
        dir\t<mode-octal>\t-\t-\t<path>

    Returns records in the exact scan_tree shape (path "", "0o"-prefixed
    modes, int sizes) so `scans_equal` comparisons are exact.
    """
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError as exc:
        raise ValidationError(f"cannot read entries index {path}: {exc}") from exc
    records: List[Dict[str, Any]] = []
    for lineno, line in enumerate(lines, start=1):
        parts = line.split("\t")
        if len(parts) != 5:
            raise ValidationError(
                f"entries index {path} line {lineno} is malformed "
                f"({len(parts)} fields, expected 5)"
            )
        etype, mode, size, sha, raw_path = parts
        rel = "" if raw_path == "." else raw_path
        if etype == "dir":
            records.append(
                {"path": rel, "type": "dir", "mode": f"0o{mode}"}
            )
        elif etype == "file":
            if not size.isdigit() or len(sha) != 64:
                raise ValidationError(
                    f"entries index {path} line {lineno} has invalid file fields"
                )
            records.append(
                {
                    "path": rel,
                    "type": "file",
                    "mode": f"0o{mode}",
                    "size": int(size),
                    "sha256": sha,
                }
            )
        else:
            raise ValidationError(
                f"entries index {path} line {lineno} has unknown type {etype!r}"
            )
    records.sort(key=lambda r: r["path"])
    return records


def _validate_bundle(recovery_dir: Path, gen_id: str) -> Dict[str, Any]:
    """Full validation of a downloaded bundle (shared by verify and install).

    RECOVERY_READY (generation id + manifest sha256), READY sentinel,
    manifest generation_id/schema_version, entries-index digests bound to
    the manifest, and an EXACT re-scan of both trees against the parsed
    entries (path/type/mode/size/sha256/dirs). Returns the manifest.
    """
    bundle = recovery_dir / gen_id
    if not bundle.is_dir():
        raise HandoffError(f"bundle directory not found: {bundle}")
    ready = bundle / READY_SENTINEL_NAME
    if not ready.exists():
        raise HandoffError(f"bundle {gen_id} missing READY sentinel")
    if _read_first_line(ready) != gen_id:
        raise HandoffError(f"READY sentinel does not match generation {gen_id}")
    manifest_path = bundle / MANIFEST_NAME
    if not manifest_path.exists():
        raise HandoffError(f"bundle {gen_id} missing manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError(f"manifest of {gen_id} is not readable JSON: {exc}") from exc
    if manifest.get("generation_id") != gen_id:
        raise HandoffError(
            f"manifest generation_id mismatch: {manifest.get('generation_id')!r}"
        )
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise HandoffError(
            f"manifest schema_version is {manifest.get('schema_version')!r} "
            f"(expected {MANIFEST_SCHEMA_VERSION}); refusing"
        )
    # The RECOVERY_READY handoff binds the bundle: its second line is the
    # manifest sha256 recorded by the recover step. A bundle that does not
    # match its own handoff was swapped/tampered after download.
    handoff = _sentinel_lines(recovery_dir / RECOVERY_READY_NAME)
    if not handoff or handoff[0].strip() != gen_id:
        raise HandoffError(
            f"RECOVERY_READY generation mismatch: sentinel={handoff[0].strip() if handoff else ''!r}"
        )
    if len(handoff) < 2 or len(handoff[1].strip()) != 64:
        raise HandoffError(f"RECOVERY_READY carries no manifest sha256 for {gen_id}")
    if handoff[1].strip() != _sha256_file(manifest_path):
        raise HandoffError(
            f"RECOVERY_READY manifest sha256 does not match the bundle manifest of {gen_id}"
        )
    # Strict full-schema validation (council fix): beyond generation_id and
    # schema_version, every block/key/type/digest of the schema-version-1
    # manifest must hold exactly. A manifest that is well-formed JSON but
    # structurally drifted (unknown keys, malformed digests, missing blocks,
    # non-zero doctor failures) is refused here — the shell uploader/recover
    # gates enforce well-formedness, this is the authoritative schema check.
    core.validate_manifest_schema(manifest)
    for tree in (GBRAIN_TREE_NAME, VAULT_TREE_NAME):
        tree_meta = manifest.get("trees", {}).get(tree)
        if not isinstance(tree_meta, dict):
            raise HandoffError(f"manifest has no trees.{tree} block")
        entries_file = bundle / f"{tree}{ENTRIES_FILE_SUFFIX}"
        if not entries_file.exists():
            raise HandoffError(f"bundle {gen_id} missing entries index for {tree}")
        expected = tree_meta.get("entries_digest")
        actual = _sha256_file(entries_file)
        if not isinstance(expected, str) or expected != actual:
            raise ValidationError(
                f"entries index digest mismatch for tree {tree}: "
                f"manifest={expected} file={actual}"
            )
        disk_records = core.scan_tree(bundle / tree)
        entries_records = parse_entries_file(entries_file)
        # Content-exact, mode-relaxed: the rclone crypt transport cannot
        # round-trip POSIX modes, so the downloaded bundle's on-disk modes
        # are NOT part of the transport integrity contract. The install
        # re-applies the exact recorded modes from the entries index via
        # copy_tree (the installed tree is mode-exact regardless).
        if not core.scans_equal(disk_records, entries_records, ignore_mode=True):
            raise ValidationError(
                f"restored tree {tree} does not exactly match its manifest "
                f"entries (path/type/size/hash/dirs)"
            )
    return manifest


# ---------------------------------------------------------------------------
# Doctor against DISPOSABLE state (never the live DB)
# ---------------------------------------------------------------------------


def _run_doctor_at(
    gbrain_bin: str,
    home_root: Path,
    brain_repo: Path,
    schema_pack: str,
    timeout: float,
) -> Dict[str, Any]:
    env = os.environ.copy()
    env.update(
        {
            "GBRAIN_HOME": str(home_root),
            "GBRAIN_BRAIN_REPO": str(brain_repo),
            "GBRAIN_SCHEMA_PACK": schema_pack,
            "GBRAIN_SKIP_STARTUP_HOOKS": "1",
            # Working-directory and environment containment (council fix:
            # relative verifier containment): the doctor ALWAYS runs with
            # cwd inside the disposable root and HOME inside it too, so a
            # relative path in the (contained) config can only resolve into
            # the disposable layout — never into the live tree. Escaping
            # relative paths are refused by _contain_absolute_paths before
            # this point; this pins the resolution base the doctor sees.
            "HOME": str(home_root),
        }
    )
    try:
        proc = subprocess.run(
            [gbrain_bin, "doctor", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(home_root),
        )
    except subprocess.TimeoutExpired as exc:
        raise VaultRecoveryRestoreError(
            f"gbrain doctor --json (disposable state) did not finish within "
            f"{timeout:.0f}s"
        ) from exc
    except OSError as exc:
        raise VaultRecoveryRestoreError(
            f"could not execute the pinned native gbrain binary {gbrain_bin}: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise ValidationError(
            f"gbrain doctor --json on the disposable restored state exited "
            f"{proc.returncode}: {proc.stderr.strip()[:2000] or proc.stdout.strip()[:2000]}"
        )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"gbrain doctor --json on the disposable restored state produced "
            f"invalid JSON: {exc}"
        ) from exc
    return report


def _contain_absolute_paths(
    config: Any,
    disposable_gbrain: Path,
    bundle_vault: Path,
    key_path: str = "",
) -> Any:
    """Fail-closed containment of EVERY absolute path in the disposable
    config, regardless of which key carries it (the exported config.json
    records the LIVE absolute paths recorded by the operator runtime; the
    pinned doctor merges the config FILE into the engine config, so ANY
    live path left in the disposable copy could make the doctor open —
    and, when the live tree is destroyed, RE-CREATE — the live
    PGLite/vault instead of the disposable copy).

    Applied recursively to every string value; returns the (possibly
    rewritten) config:
      - ``<live gbrain dir>/<rest>``        -> ``disposable_gbrain/<rest>``
      - ``<live vault dir>/<rest>``         -> ``bundle_vault/<rest>``
      - any path already inside the disposable root or the bundle vault
        is kept (normalized);
      - any OTHER absolute path cannot be contained -> ValidationError
        (fail closed: the doctor never runs with an unconfinable path).
    Relative paths are contained too (council fix: relative verifier
    containment): the doctor runs with cwd and HOME pinned inside the
    disposable root (``_run_doctor_at``), so a relative path resolves
    there; a RELATIVE path whose normalized form still escapes that cwd
    (``../`` at the start, after collapsing ``.``/``..``) is REFUSED —
    e.g. ``../..`` or ``sub/../../..`` would resolve into production from
    a disposable cwd. Relative strings that stay inside (``base/1234``,
    ``America/Sao_Paulo``) are left untouched: they resolve inside the
    disposable layout by construction.

    Containment is NORMALIZED and RESOLVED-SAFE (council fix): every
    candidate is lexically normalized with ``os.path.normpath`` (collapsing
    "." and "..") BEFORE any comparison, and the containment roots are
    ``os.path.realpath``-resolved (a symlinked disposable root resolves to
    its true location). A raw string-prefix check alone can be bypassed by
    a ``..`` component — e.g. ``<disposable root>/../../etc/passwd`` or a
    live-prefixed path like ``/opt/data/.gbrain/../../x`` whose rewrite
    would escape the disposable root — so both the "already inside" test
    and the live-path rewrites are validated on the normalized/resolved
    forms, and a rewrite that would land outside the disposable layout is
    REFUSED instead of silently escaping.
    """
    live_gbrain = core.GBRAIN_STATE_DIR
    live_vault = core.VAULT_DIR
    if isinstance(config, dict):
        for key, value in config.items():
            config[key] = _contain_absolute_paths(
                value, disposable_gbrain, bundle_vault,
                key_path=f"{key_path}.{key}" if key_path else key,
            )
        return config
    if isinstance(config, list):
        for i, value in enumerate(config):
            config[i] = _contain_absolute_paths(
                value, disposable_gbrain, bundle_vault,
                key_path=f"{key_path}[{i}]",
            )
        return config
    if not isinstance(config, str):
        return config
    if config.startswith("/"):
        return _contain_absolute_value(
            config, disposable_gbrain, bundle_vault, key_path
        )
    # RELATIVE string: the doctor's cwd is pinned inside the disposable
    # root, so a relative path resolves there — unless it escapes via "..".
    # Normalize and refuse any relative form that walks out of the cwd
    # (fail closed: such a value could resolve into the live tree).
    rel_norm = os.path.normpath(config)
    if rel_norm == ".." or rel_norm.startswith("../"):
        raise ValidationError(
            f"disposable config carries a relative path that escapes the "
            f"constrained doctor working directory at key {key_path or '<root>'!r}: "
            f"{config!r} (normalized {rel_norm!r}); refusing to run the doctor "
            f"on it (fail closed)"
        )
    return config


def _contain_absolute_value(
    value: str,
    disposable_gbrain: Path,
    bundle_vault: Path,
    key_path: str,
) -> str:
    """Contain ONE absolute path value (the absolute half of
    ``_contain_absolute_paths``; split out so the relative branch stays
    readable)."""
    live_gbrain = core.GBRAIN_STATE_DIR
    live_vault = core.VAULT_DIR
    # Normalized candidate + resolved containment roots: any ".." is
    # collapsed lexically before the comparisons, so an escaping path can
    # never pass a raw prefix test; the roots are realpath-resolved so a
    # symlinked disposable/bundle root cannot be escaped through the
    # resolved location.
    norm = os.path.normpath(value)
    disposable_root_real = os.path.realpath(str(disposable_gbrain.parent))
    vault_real = os.path.realpath(str(bundle_vault))
    for contained_root in (disposable_root_real, vault_real):
        if norm == contained_root or norm.startswith(contained_root + "/"):
            return norm  # already inside the disposable layout (normalized)
    live_gbrain_norm = os.path.normpath(live_gbrain)
    live_vault_norm = os.path.normpath(live_vault)
    if norm == live_gbrain_norm or norm.startswith(live_gbrain_norm + "/"):
        # Rewrite into the disposable .gbrain copy, preserving the layout.
        # `norm` is already `..`-free (normalized BEFORE the prefix test),
        # so the rewritten value cannot escape the resolved disposable
        # root; the containment re-check below is a defensive invariant
        # that keeps this property explicit if the rewrite is ever changed
        # to consume the RAW path instead of the normalized one.
        rest = norm[len(live_gbrain_norm) :].lstrip("/")
        rewritten = os.path.normpath(
            os.path.join(os.path.realpath(str(disposable_gbrain)), rest)
        )
        if not (
            rewritten == disposable_root_real
            or rewritten.startswith(disposable_root_real + "/")
        ):
            raise ValidationError(
                f"disposable config carries a live-gbrain path whose rewrite "
                f"escapes the disposable copy at key {key_path or '<root>'!r}: "
                f"{value!r} (normalized {norm!r}); refusing to run the doctor "
                f"on it (fail closed)"
            )
        return rewritten
    if norm == live_vault_norm or norm.startswith(live_vault_norm + "/"):
        # Rewrite into the bundle vault copy, preserving the layout (same
        # normalized + resolved containment invariant as above).
        rest = norm[len(live_vault_norm) :].lstrip("/")
        rewritten = os.path.normpath(os.path.join(vault_real, rest))
        if not (
            rewritten == vault_real or rewritten.startswith(vault_real + "/")
        ):
            raise ValidationError(
                f"disposable config carries a live-vault path whose rewrite "
                f"escapes the bundle vault at key {key_path or '<root>'!r}: "
                f"{value!r} (normalized {norm!r}); refusing to run the doctor "
                f"on it (fail closed)"
            )
        return rewritten
    raise ValidationError(
        f"disposable config carries an unconfinable absolute path at "
        f"key {key_path or '<root>'!r}: {value!r} resolves outside the "
        f"disposable copy and the bundle vault; refusing to run the doctor "
        f"on it (fail closed)"
    )


def _sanitize_disposable_config(disposable_gbrain: Path, bundle_vault: Path) -> None:
    """Contain the disposable doctor inside the recovery handoff.

    The exported ``.gbrain/config.json`` carries the LIVE absolute paths
    recorded by the operator runtime: ``database_path`` points at the live
    PGLite and ``sync.repo_path`` at the live vault. ``loadConfig`` merges
    the config FILE into the engine config, so the pinned doctor would open
    (and, when the live tree is destroyed — the DR drill — RE-CREATE) the
    live PGLite at the live path instead of opening the disposable copy,
    silently "verifying" the wrong database and polluting the live tree.
    Rewrite the disposable copy so the PGLite path resolves INSIDE the
    disposable root and the brain repo points at the bundle vault (a
    read-only copy on the recovery volume), then enforce fail-closed
    containment of EVERY remaining absolute path (see
    ``_contain_absolute_paths``): the doctor never runs while any absolute
    path could escape the disposable layout. Missing/unparsable config is
    left alone: the doctor then derives everything from GBRAIN_HOME.
    """
    config_path = disposable_gbrain / "config.json"
    if not config_path.exists():
        return
    try:
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(config, dict):
        return
    database_path = config.get("database_path")
    if isinstance(database_path, str) and database_path:
        # Preserve the subdirectory structure of the LIVE database path
        # (e.g. `/opt/data/.gbrain/base/1234` -> `<disposable_gbrain>/base/1234`)
        # so the PGLite layout is not flattened; any other ABSOLUTE path
        # collapses to the basename (the old contract).
        if database_path.startswith(core.GBRAIN_STATE_DIR + "/"):
            rest = database_path[len(core.GBRAIN_STATE_DIR) + 1 :]
            config["database_path"] = str(disposable_gbrain / rest)
        elif database_path.startswith("/"):
            config["database_path"] = str(
                disposable_gbrain / Path(database_path).name
            )
        else:
            # RELATIVE database_path (council fix: relative verifier
            # containment): resolve it against the config file's own
            # directory — the disposable .gbrain copy — so the doctor
            # opens the COPY. The resolved absolute value then goes
            # through the generic containment below: a relative form that
            # walks out of the disposable copy (e.g.
            # "../../../opt/data/.gbrain") is refused there as
            # unconfinable, never silently resolved into production.
            config["database_path"] = str(disposable_gbrain / database_path)
    sync = config.get("sync")
    if isinstance(sync, dict):
        repo_path = sync.get("repo_path")
        if isinstance(repo_path, str) and repo_path:
            if repo_path.startswith("/"):
                sync["repo_path"] = str(bundle_vault)
            else:
                # RELATIVE repo_path: resolve against the bundle vault copy
                # (same containment rationale as database_path above); the
                # generic containment below refuses an escaping resolution.
                sync["repo_path"] = str(bundle_vault / repo_path)
    # Fail-closed containment over the WHOLE config (after the rewrite):
    # any absolute path that still escapes the disposable layout refuses
    # the verification, so the doctor can never reach the live tree through
    # the config regardless of which key carries the path.
    _contain_absolute_paths(config, disposable_gbrain, bundle_vault)
    _safe_write_text(config_path, json.dumps(config, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Verify (disposable doctor; never touches live state)
# ---------------------------------------------------------------------------


def verify_recovery(
    recovery_dir: Path,
    *,
    gbrain_bin: str = GBRAIN_BIN,
    schema_pack_file: str = SCHEMA_PACK_FILE,
    doctor_timeout: float = DOCTOR_TIMEOUT,
    lock_path: str = TASKNOTES_LOCK,
) -> Dict[str, Any]:
    """Validate the restored bundle and the pinned doctor on a DISPOSABLE
    copy of its `.gbrain`. Live vault/.gbrain paths are never referenced.
    The Hermes runtime identity is enforced at the CLI boundary.

    The verifier writes VERIFIED_READY into the shared recovery handoff, so
    it holds the same exclusive nonblocking shared TaskNotes/gbrain lock as
    the install (acquired BEFORE any handoff read): a concurrent install
    (or any other gbrain user) refuses while the verification runs, and a
    concurrent verification cannot replace the sentinel the install is
    consuming (fix 5: verified handoff under lock)."""
    recovery_dir = Path(recovery_dir)
    lock_fd = _acquire_install_lock(lock_path)
    try:
        return _verify_recovery_locked(
            recovery_dir,
            gbrain_bin=gbrain_bin,
            schema_pack_file=schema_pack_file,
            doctor_timeout=doctor_timeout,
        )
    finally:
        os.close(lock_fd)


def _verify_recovery_locked(
    recovery_dir: Path,
    *,
    gbrain_bin: str,
    schema_pack_file: str,
    doctor_timeout: float,
) -> Dict[str, Any]:
    verified = recovery_dir / VERIFIED_READY_NAME
    # Stale VERIFIED_READY removal, fsynced BEFORE any verification work
    # (fsync-before-verification): the sentinel may only ever exist when the
    # MOST RECENT verification completed successfully, so a re-verify that
    # fails (or crashes mid-run) must never leave an earlier run's
    # VERIFIED_READY behind for install to consume. The removal is made
    # durable up front, so even a power loss during the doctor run cannot
    # resurrect the stale sentinel.
    if verified.exists():
        try:
            verified.unlink()
        except OSError as exc:
            raise HandoffError(
                f"cannot remove stale {VERIFIED_READY_NAME}: {exc}"
            ) from exc
        _fsync_dir(recovery_dir)
    ready = recovery_dir / RECOVERY_READY_NAME
    if not ready.exists():
        raise HandoffError(
            f"no RECOVERY_READY in {recovery_dir}; run "
            f"vault-recovery-recover.sh download <gen-id> first"
        )
    gen_id = _read_first_line(ready)
    if not core.is_valid_generation_id(gen_id):
        raise HandoffError(f"RECOVERY_READY carries an invalid generation id: {gen_id!r}")

    manifest = _validate_bundle(recovery_dir, gen_id)
    bundle = recovery_dir / gen_id
    gbrain_records = parse_entries_file(bundle / f"{GBRAIN_TREE_NAME}{ENTRIES_FILE_SUFFIX}")
    vault_records = parse_entries_file(bundle / f"{VAULT_TREE_NAME}{ENTRIES_FILE_SUFFIX}")

    # Isolated disposable doctor: copy the restored .gbrain to a fresh
    # disposable root under the recovery dir and open THAT. The live .gbrain
    # is never opened or mutated. The disposable copy is removed when the
    # step finishes (success or failure) so stale copies never accumulate on
    # the disposable recovery volume.
    disposable_root = recovery_dir / f".verify-{gen_id}"
    if disposable_root.exists():
        shutil.rmtree(disposable_root, ignore_errors=True)
    disposable_root.mkdir(mode=DIR_MODE)
    _fsync_dir(recovery_dir)
    try:
        disposable_gbrain = disposable_root / GBRAIN_TREE_NAME
        core.copy_tree(bundle / GBRAIN_TREE_NAME, gbrain_records, disposable_gbrain)
        staged_check = core.scan_tree(disposable_gbrain)
        if not core.scans_equal(staged_check, gbrain_records):
            raise ValidationError(
                "disposable .gbrain copy does not match the bundle entries; "
                "refusing to run doctor on it"
            )
        # Containment: the copied config.json still carries the LIVE absolute
        # database_path/sync.repo_path; rewrite them into the disposable root
        # so the doctor opens the COPY (never the live tree).
        _sanitize_disposable_config(disposable_gbrain, bundle / VAULT_TREE_NAME)
        schema_pack = _active_schema_pack(
            str(bundle / GBRAIN_TREE_NAME / "active-schema-pack")
        )
        report = _run_doctor_at(
            gbrain_bin, disposable_root, bundle / VAULT_TREE_NAME, schema_pack, doctor_timeout
        )
        doctor_summary = core.validate_doctor_report(report)

        manifest_sha = _sha256_file(bundle / MANIFEST_NAME)
        _safe_write_text(
            verified, f"{gen_id}\n{manifest_sha}\n{json.dumps(doctor_summary, sort_keys=True)}\n"
        )
        return {
            "generation_id": gen_id,
            "manifest_sha256": manifest_sha,
            "doctor": doctor_summary,
            "trees": {
                tree: {
                    "entries": len(records),
                    "exact_match": True,
                }
                for tree, records in (
                    (GBRAIN_TREE_NAME, gbrain_records),
                    (VAULT_TREE_NAME, vault_records),
                )
            },
            "verified_at_utc": _utc_now_iso(),
        }
    finally:
        shutil.rmtree(disposable_root, ignore_errors=True)
        _fsync_dir(recovery_dir)


# ---------------------------------------------------------------------------
# Journaled two-tree install transaction
# ---------------------------------------------------------------------------


def _write_journal(journal_path: Path, data: Dict[str, Any]) -> None:
    _safe_write_text(journal_path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _load_journal(journal_path: Path) -> Dict[str, Any]:
    try:
        size = journal_path.stat().st_size
    except OSError as exc:
        raise JournalError(f"journal {journal_path} unreadable: {exc}") from exc
    if size > JOURNAL_MAX_BYTES:
        raise JournalError(
            f"journal {journal_path} is oversized ({size} bytes > "
            f"{JOURNAL_MAX_BYTES}); refusing"
        )
    try:
        data = json.loads(journal_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JournalError(f"journal {journal_path} unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise JournalError(f"journal {journal_path} has an unsupported schema")
    return data


def _append_step(
    journal_path: Path, journal: Dict[str, Any], tree: str, op: str,
    name: str = "", state: str = "pending",
) -> None:
    """Write-ahead journaling: every mutation step is recorded (and fsynced)
    with state "pending" BEFORE the rename happens, then flipped to "done"
    (via ``_mark_step_done``) after the rename + parent-dir fsync. A crash
    between the two leaves a pending step: the rename may or may not have
    happened, so rollback probes the filesystem (see ``_rollback_tree``)."""
    journal.setdefault("steps", []).append(
        {"tree": tree, "op": op, "name": name, "state": state}
    )
    _write_journal(journal_path, journal)


def _mark_step_done(journal_path: Path, journal: Dict[str, Any]) -> None:
    """Flip the most recently appended step (the write-ahead record of the
    rename that just completed) to done and fsync the journal."""
    steps = journal.get("steps") or []
    if not steps or steps[-1].get("state") != "pending":
        raise VaultRecoveryRestoreError(
            "internal journal error: no pending step to mark done"
        )
    steps[-1]["state"] = "done"
    _write_journal(journal_path, journal)


def _fsync_rename(src: Path, dst: Path) -> None:
    """rename(2) then fsync both parent directories so the rename itself is
    crash-durable before the journal step is marked done."""
    os.rename(str(src), str(dst))
    _fsync_dir(src.parent)
    _fsync_dir(dst.parent)


def _rmtree_durable(path: Path) -> None:
    """Remove a file, symlink, or tree + fsync of the parent (used during
    rollback).

    Plain files (and symlinks) must be unlinked directly:
    ``shutil.rmtree(path, ignore_errors=True)`` silently no-ops on a regular
    file — its ``os.scandir`` raises ``NotADirectoryError``, which
    ``ignore_errors`` swallows — leaving rollback entries in place.
    """
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return  # already gone; nothing to fsync
    if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
        os.unlink(path)
    else:
        shutil.rmtree(path, ignore_errors=True)
    _fsync_dir(path.parent)


def _swap_tree(
    staged: Path,
    live: Path,
    backup: Path,
    journal_path: Path,
    journal: Dict[str, Any],
    tree: str,
) -> str:
    """Swap ``staged`` into ``live`` with rollback journaling.

    First tries the WHOLE-TREE atomic swap (rename live -> backup, rename
    staged -> live). When the live root cannot be renamed — the production
    vault at /opt/data/obsidian is the root of a mounted volume and the
    kernel refuses rename(2) to/from a mount point with EBUSY — falls back
    to the journaled per-top-level-entry swap (still no in-place overwrites:
    every entry is moved aside to the backup root before the staged twin is
    renamed in). A plain (non-mount) live vault whose backup root lives
    INSIDE the live tree fails rename(2) with EINVAL (the destination is a
    subdirectory of the source); that layout is deliberate (the backup must
    stay on the same filesystem as the vault), so EINVAL also falls back to
    the per-entry swap.     Returns "atomic" or "per-entry".
    """
    # Write-ahead: record the step BEFORE the rename so a crash between the
    # journal write and the rename leaves a recoverable pending step.
    _append_step(journal_path, journal, tree, "move-live-tree", state="pending")
    try:
        _fsync_rename(live, backup)
    except OSError as exc:
        if exc.errno not in (errno.EBUSY, errno.EXDEV, errno.EINVAL):
            raise InstallError(
                f"cannot move live {tree} aside ({exc}); aborting before any change"
            ) from exc
        # The rename was REFUSED (never happened): drop the pending step —
        # keeping it would make rollback discard the live tree because the
        # per-entry swap's backup root exists. The per-entry swap journals
        # its own steps.
        steps = journal.get("steps") or []
        if steps and steps[-1].get("op") == "move-live-tree" and steps[-1].get("state") == "pending":
            journal["steps"] = steps[:-1]
            _write_journal(journal_path, journal)
        _per_entry_swap(staged, live, backup, journal_path, journal, tree)
        return "per-entry"
    _mark_step_done(journal_path, journal)
    _append_step(journal_path, journal, tree, "move-staged-tree", state="pending")
    _fsync_rename(staged, live)
    _mark_step_done(journal_path, journal)
    return "atomic"


def _per_entry_swap(
    staged: Path,
    live: Path,
    backup: Path,
    journal_path: Path,
    journal: Dict[str, Any],
    tree: str,
) -> None:
    backup.mkdir(mode=DIR_MODE, parents=True)
    _fsync_dir(backup.parent)
    live_names = {
        e.name for e in live.iterdir() if e.name != INSTALL_DIR_NAME
    }
    staged_names = {
        e.name for e in staged.iterdir() if e.name != INSTALL_DIR_NAME
    }
    # Entries only in the live tree: move aside (remove from live).
    for name in sorted(live_names - staged_names):
        _append_step(journal_path, journal, tree, "remove-entry", name, state="pending")
        _fsync_rename(live / name, backup / name)
        _mark_step_done(journal_path, journal)
    # Entries in both: swap through the backup root (old aside, new in).
    for name in sorted(live_names & staged_names):
        _append_step(journal_path, journal, tree, "move-live-entry", name, state="pending")
        _fsync_rename(live / name, backup / name)
        _mark_step_done(journal_path, journal)
        _append_step(journal_path, journal, tree, "move-staged-entry", name, state="pending")
        _fsync_rename(staged / name, live / name)
        _mark_step_done(journal_path, journal)
    # Entries only in the staged tree: add.
    for name in sorted(staged_names - live_names):
        _append_step(journal_path, journal, tree, "add-entry", name, state="pending")
        _fsync_rename(staged / name, live / name)
        _mark_step_done(journal_path, journal)


def _rollback_tree(
    journal_path: Path,
    journal: Dict[str, Any],
    tree: str,
    live: Path,
    backup: Path,
    staged: Path,
) -> None:
    """Reverse a tree's journaled steps (automatic rollback and operator
    crash recovery). Every step is handled by PROBING the filesystem, so a
    write-ahead step in "pending" state (its rename may or may not have
    happened) is undone idempotently and safely in both cases.

    The invariant that makes the probes safe: within a tree's step sequence
    the old content is ALWAYS moved to ``backup`` BEFORE any new content
    arrives at ``live``, so ``backup`` (or ``backup/<name>``) existing is the
    authoritative sign that the old content is safe elsewhere and the live
    location may be discarded; conversely, nothing at ``live`` is ever
    touched while the corresponding old content is NOT in the backup.
    """
    steps = [s for s in journal.get("steps", []) if s.get("tree") == tree]
    for step in reversed(steps):
        state = step.get("state")
        if state not in ("pending", "done"):
            continue
        op = step.get("op")
        name = step.get("name", "")
        if op == "move-live-tree":
            # Atomic swap, rename live -> backup. Pending: the rename may not
            # have happened (live still holds the OLD tree); only discard
            # live when the backup actually holds the old tree.
            if backup.exists():
                _rmtree_durable(live)
                _fsync_rename(backup, live)
        elif op == "move-staged-tree":
            # Atomic swap, rename staged -> live. Undo: discard what the
            # transaction put at live. Safe ONLY when the old tree is in the
            # backup; otherwise live still IS the old tree and must not be
            # touched (pending + rename never happened).
            if backup.exists():
                _rmtree_durable(live)
        elif op == "move-live-entry":
            # Per-entry rename live/<name> -> backup/<name>. Pending: the
            # rename may not have happened (old content still live); only
            # discard live/<name> when backup/<name> holds the old content.
            if (backup / name).exists():
                _rmtree_durable(live / name)
                _fsync_rename(backup / name, live / name)
        elif op == "move-staged-entry":
            # Per-entry rename staged/<name> -> live/<name>. The old twin
            # was moved to backup/<name> by the preceding (done) move-live-
            # entry; live/<name> holds new content only if this rename
            # happened. Discard it only when backup/<name> exists.
            if (backup / name).exists():
                _rmtree_durable(live / name)
        elif op == "add-entry":
            # Per-entry rename staged/<name> -> live/<name> for a name that
            # was NOT in the live tree. live/<name> exists iff the rename
            # happened; remove it when present.
            if (live / name).exists():
                _rmtree_durable(live / name)
        elif op == "remove-entry":
            # Per-entry rename live/<name> -> backup/<name> for a live-only
            # name. Restore from the backup when the rename happened.
            if (backup / name).exists():
                _fsync_rename(backup / name, live / name)
    journal["steps"] = [s for s in journal.get("steps", []) if s.get("tree") != tree]
    _write_journal(journal_path, journal)


def _acquire_install_lock(lock_path: str) -> int:
    """Exclusive nonblocking flock on the shared tasknotes lock. Refuses the
    install when any gbrain user is active. The caller must close the fd."""
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise LockError(f"cannot open shared lock {lock_path}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise LockError(
            "the shared TaskNotes/gbrain lock is held by another process; "
            "pause all gbrain jobs and stop Hermes/Syncthing before install"
        ) from exc
    return fd


def install_generation(
    recovery_dir: Path,
    live_vault: Path,
    live_gbrain: Path,
    *,
    confirm: bool = False,
    lock_path: str = TASKNOTES_LOCK,
    journal_root: Optional[Path] = None,
    generation: Optional[str] = None,
) -> Dict[str, Any]:
    """Journaled two-tree install of a verified bundle over the live trees.

    Explicit confirmation is mandatory. Both trees are staged on the
    DESTINATION filesystems (same fs as their live target) at fresh hidden
    paths — no in-place overwrites — then swapped with per-step journaling;
    any failure automatically rolls the transaction back to the previous
    live state. Backups and the completed journal remain for operator-driven
    rollback.

    ``generation`` binds the operator's REQUESTED generation id into the
    install: the requested id is validated and compared against the
    RECOVERY_READY handoff generation AFTER the lock is acquired (and
    before any further handoff read/validation), so a lock-less rclone
    recover step that replaced the handoff with a DIFFERENT generation can
    never be installed — the mismatch is rejected under the lock. The
    shell wrapper's own pre-check is only a fast-fail convenience; this
    check is the authoritative one.

    The Hermes runtime identity is enforced at the CLI boundary (same
    pattern as the exporter core); the wrapper/round-trip run this as the
    hermes user.
    """
    if not confirm:
        raise InstallError(
            "install requires --i-confirm-this-overwrites-production; stop "
            "Hermes, server Syncthing and all gbrain jobs first"
        )
    recovery_dir = Path(recovery_dir)
    live_vault = Path(live_vault)
    live_gbrain = Path(live_gbrain)
    for label, path in (("live vault", live_vault), ("live .gbrain", live_gbrain)):
        if not path.is_dir():
            raise InstallError(f"{label} target not found: {path}")

    # No gbrain user may be active during the transaction. The lock is
    # acquired BEFORE any handoff read/validation (fix 5 — install TOCTOU):
    # sentinel reads, bundle validation, staging, and the swap all happen
    # under the lock, so a concurrent verifier (which takes the same lock)
    # can never replace VERIFIED_READY/VERIFIED_READY mid-install, and the
    # bundle re-validation right before the first mutation closes the
    # lock-less rclone recover-step replacement window. The requested
    # --generation binding is also enforced under this lock (see
    # ``_install_locked``).
    lock_fd = _acquire_install_lock(lock_path)
    try:
        return _install_locked(
            recovery_dir,
            live_vault,
            live_gbrain,
            lock_path=lock_path,
            journal_root=journal_root,
            generation=generation,
        )
    finally:
        os.close(lock_fd)


def _install_locked(
    recovery_dir: Path,
    live_vault: Path,
    live_gbrain: Path,
    *,
    lock_path: str,
    journal_root: Optional[Path],
    generation: Optional[str] = None,
) -> Dict[str, Any]:
    """Locked body of the journaled two-tree install (see
    ``install_generation``): every handoff read, validation, staging, and
    mutation step runs while the shared TaskNotes/gbrain lock is held."""
    if generation is not None and not core.is_valid_generation_id(generation):
        raise HandoffError(
            f"requested generation id is invalid: {generation!r} "
            f"(refusing to install a different generation)"
        )
    ready = recovery_dir / RECOVERY_READY_NAME
    verified = recovery_dir / VERIFIED_READY_NAME
    if not ready.exists():
        raise HandoffError(f"no {RECOVERY_READY_NAME} in {recovery_dir}; run the download step first")
    if not verified.exists():
        raise HandoffError(f"no {VERIFIED_READY_NAME} in {recovery_dir}; run verify-recovery first")
    gen_id = _read_first_line(ready)
    if not core.is_valid_generation_id(gen_id):
        raise HandoffError(f"RECOVERY_READY carries an invalid generation id: {gen_id!r}")
    # Requested-generation binding, UNDER THE LOCK and before any further
    # handoff validation (council fix): the operator explicitly requested a
    # generation; if the handoff (possibly replaced lock-lessly by a
    # concurrent recover download) carries a DIFFERENT generation, the
    # install refuses right here — nothing is read beyond the sentinel, no
    # bundle validation, no staging, no journal. The requested id is never
    # installable as a different generation.
    if generation is not None and gen_id != generation:
        raise HandoffError(
            f"requested generation {generation} does not match the "
            f"RECOVERY_READY handoff generation {gen_id}; refusing to "
            f"install a different generation"
        )
    verified_lines = _sentinel_lines(verified)
    if not verified_lines or verified_lines[0].strip() != gen_id:
        raise HandoffError(
            f"VERIFIED_READY generation mismatch: sentinel="
            f"{verified_lines[0].strip() if verified_lines else ''!r} bundle={gen_id}"
        )
    if len(verified_lines) < 2 or len(verified_lines[1].strip()) != 64:
        raise HandoffError(f"VERIFIED_READY carries no manifest sha256 for {gen_id}")

    # The VERIFIED_READY manifest-sha binding is checked BEFORE the strict
    # bundle validation: a bundle replaced after verification is refused by
    # the binding itself (the exact failure the sentinel protects against),
    # not by a schema detail of the swapped bundle. Only when the binding
    # holds is the bundle re-validated in full (RECOVERY_READY binding +
    # strict manifest schema + entries digests + exact tree re-scan).
    bundle_manifest = recovery_dir / gen_id / MANIFEST_NAME
    if not bundle_manifest.exists():
        raise HandoffError(f"bundle {gen_id} missing manifest.json")
    if verified_lines[1].strip() != _sha256_file(bundle_manifest):
        raise HandoffError(
            f"VERIFIED_READY manifest sha256 does not match the bundle manifest "
            f"of {gen_id}; the bundle changed after verification"
        )
    manifest = _validate_bundle(recovery_dir, gen_id)

    journal_root = Path(journal_root or DEFAULT_JOURNAL_ROOT)
    journal_dir = journal_root / gen_id
    journal_path = journal_dir / "journal.json"
    if journal_path.exists():
        raise InstallError(
            f"a journal already exists for {gen_id} at {journal_path}; "
            f"resolve it (rollback) before installing again"
        )
    journal_dir.mkdir(mode=DIR_MODE, parents=True)
    _fsync_dir(journal_root)

    vault_install_root = live_vault / INSTALL_DIR_NAME / gen_id
    gbrain_install_root = live_gbrain.parent / INSTALL_DIR_NAME / gen_id
    staged_vault = vault_install_root / "vault-staged"
    backup_vault = vault_install_root / "vault-backup"
    staged_gbrain = gbrain_install_root / "gbrain-staged"
    backup_gbrain = gbrain_install_root / "gbrain-backup"
    bundle = recovery_dir / gen_id
    vault_records = parse_entries_file(bundle / f"{VAULT_TREE_NAME}{ENTRIES_FILE_SUFFIX}")
    gbrain_records = parse_entries_file(bundle / f"{GBRAIN_TREE_NAME}{ENTRIES_FILE_SUFFIX}")

    # Installer-owned roots on the DESTINATION filesystems: the vault
    # staging/backup live INSIDE the vault volume (same fs as the live
    # vault), the .gbrain staging/backup beside the live .gbrain (same
    # fs). Both names are excluded from the per-entry swap below.
    vault_install_root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    gbrain_install_root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    _fsync_dir(vault_install_root.parent)
    _fsync_dir(gbrain_install_root.parent)

    journal: Dict[str, Any] = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "generation_id": gen_id,
        "created_at_utc": _utc_now_iso(),
        "status": "in-progress",
        "live_vault": str(live_vault),
        "live_gbrain": str(live_gbrain),
        "staged_vault": str(staged_vault),
        "backup_vault": str(backup_vault),
        "staged_gbrain": str(staged_gbrain),
        "backup_gbrain": str(backup_gbrain),
        "steps": [],
    }
    _write_journal(journal_path, journal)
    try:
        # Stage both trees on the DESTINATION filesystems (fresh paths).
        core.copy_tree(bundle / VAULT_TREE_NAME, vault_records, staged_vault)
        core.copy_tree(bundle / GBRAIN_TREE_NAME, gbrain_records, staged_gbrain)
        for label, staged, records in (
            ("vault", staged_vault, vault_records),
            (".gbrain", staged_gbrain, gbrain_records),
        ):
            if not core.scans_equal(core.scan_tree(staged), records):
                raise InstallError(f"staged {label} tree does not match the bundle")

        # Handoff-unchanged re-check immediately before the first mutation
        # (fix 5): the lock serializes the Hermes-side verifier, but the
        # rclone recover step is lock-less — re-read both sentinels and
        # re-hash the bundle manifest so a recovery download that replaced
        # the handoff mid-install aborts BEFORE any rename, leaving the
        # live trees untouched.
        if not ready.exists() or not verified.exists():
            raise HandoffError(
                f"recovery handoff disappeared during install ({gen_id}); aborting before any change"
            )
        if _read_first_line(ready) != gen_id:
            raise HandoffError(
                f"RECOVERY_READY changed during install ({gen_id}); aborting before any change"
            )
        current_verified = _sentinel_lines(verified)
        if (
            not current_verified
            or current_verified[0].strip() != gen_id
            or len(current_verified) < 2
            or current_verified[1].strip() != verified_lines[1].strip()
        ):
            raise HandoffError(
                f"VERIFIED_READY changed during install ({gen_id}); aborting before any change"
            )
        if _sha256_file(bundle / MANIFEST_NAME) != verified_lines[1].strip():
            raise HandoffError(
                f"bundle manifest changed during install ({gen_id}); aborting before any change"
            )

        # Swap .gbrain first (atomic rename; plain dir on hermes-data).
        gbrain_mode = _swap_tree(
            staged_gbrain, live_gbrain, backup_gbrain, journal_path, journal, GBRAIN_TREE_NAME
        )
        # Then the vault (atomic when renameable, journaled per-entry for
        # the mount-root case).
        vault_mode = _swap_tree(
            staged_vault, live_vault, backup_vault, journal_path, journal, VAULT_TREE_NAME
        )
        journal["status"] = "complete"
        journal["swap_modes"] = {GBRAIN_TREE_NAME: gbrain_mode, VAULT_TREE_NAME: vault_mode}
        _write_journal(journal_path, journal)
    except BaseException:
        # Automatic rollback: reverse every completed step.
        try:
            _rollback_tree(journal_path, journal, GBRAIN_TREE_NAME, live_gbrain, backup_gbrain, staged_gbrain)
            _rollback_tree(journal_path, journal, VAULT_TREE_NAME, live_vault, backup_vault, staged_vault)
            journal["status"] = "rolled-back"
            _write_journal(journal_path, journal)
        except BaseException:
            journal["status"] = "rollback-failed"
            try:
                _write_journal(journal_path, journal)
            except BaseException:
                pass
            raise
        raise
    return {
        "generation_id": gen_id,
        "status": "complete",
        "journal": str(journal_path),
        "swap_modes": {GBRAIN_TREE_NAME: gbrain_mode, VAULT_TREE_NAME: vault_mode},
        "live_vault": str(live_vault),
        "live_gbrain": str(live_gbrain),
        "backups": {
            "vault": str(backup_vault),
            "gbrain": str(backup_gbrain),
        },
    }


def rollback_generation(
    journal_root: Path,
    gen_id: str,
    *,
    lock_path: str = TASKNOTES_LOCK,
) -> Dict[str, Any]:
    """Reverse a journaled install transaction from its journal (crash
    recovery or operator-driven rollback). Identity is enforced at the CLI.

    Rollback mutates the LIVE trees, so it acquires the same exclusive
    nonblocking shared TaskNotes/gbrain lock as the install: if any gbrain
    user is active, the rollback refuses.
    """
    if not core.is_valid_generation_id(gen_id):
        raise JournalError(f"invalid generation id: {gen_id!r}")
    journal_root = Path(journal_root)
    journal_path = journal_root / gen_id / "journal.json"
    # The lock is acquired BEFORE the journal is read (fix 5): a concurrent
    # install (which holds the same lock) cannot be mid-transaction while
    # the rollback reads and reverses the journal. The journal-existence
    # check itself happens under the lock too (council fix: lock before all
    # check/read), so a journal cannot appear or disappear between the
    # check and the read.
    lock_fd = _acquire_install_lock(lock_path)
    try:
        if not journal_path.exists():
            raise JournalError(f"no journal for {gen_id} at {journal_path}")
        journal = _load_journal(journal_path)
        if journal.get("status") == "rolled-back":
            return {"generation_id": gen_id, "status": "already-rolled-back"}
        if journal.get("status") == "rollback-failed":
            raise JournalError(
                f"journal {journal_path} is in rollback-failed state; manual "
                f"recovery required (see docs/vault-recovery-operations.md)"
            )
        live_vault = Path(journal["live_vault"])
        live_gbrain = Path(journal["live_gbrain"])
        staged_vault = Path(journal["staged_vault"])
        backup_vault = Path(journal["backup_vault"])
        staged_gbrain = Path(journal["staged_gbrain"])
        backup_gbrain = Path(journal["backup_gbrain"])
        _rollback_tree(journal_path, journal, GBRAIN_TREE_NAME, live_gbrain, backup_gbrain, staged_gbrain)
        _rollback_tree(journal_path, journal, VAULT_TREE_NAME, live_vault, backup_vault, staged_vault)
        journal["status"] = "rolled-back"
        journal["rolled_back_at_utc"] = _utc_now_iso()
        _write_journal(journal_path, journal)
    finally:
        os.close(lock_fd)
    return {
        "generation_id": gen_id,
        "status": "rolled-back",
        "journal": str(journal_path),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vault-recovery-restore",
        description="Vault recovery verify/install/rollback core (Phase 2).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="Verify a recovery handoff (disposable doctor).")
    p_verify.add_argument(
        "recovery_dir",
        nargs="?",
        default=os.environ.get("VAULT_RECOVERY_RECOVERY_DIR", DEFAULT_RECOVERY_DIR),
    )
    p_verify.set_defaults(func=_cmd_verify)

    p_install = sub.add_parser("install", help="Install a verified bundle (journaled).")
    p_install.add_argument("recovery_dir")
    p_install.add_argument("--live-vault", required=True)
    p_install.add_argument("--live-gbrain", required=True)
    p_install.add_argument(
        "--generation",
        default=None,
        help="Requested generation id; the install refuses (under the lock) "
        "when the RECOVERY_READY handoff carries a different generation.",
    )
    p_install.add_argument(
        "--journal-root",
        default=os.environ.get("VAULT_RECOVERY_JOURNAL_ROOT", DEFAULT_JOURNAL_ROOT),
    )
    p_install.add_argument("--i-confirm-this-overwrites-production", action="store_true")
    p_install.set_defaults(func=_cmd_install)

    p_rollback = sub.add_parser("rollback", help="Roll back a journaled install.")
    p_rollback.add_argument("generation_id")
    p_rollback.add_argument(
        "--journal-root",
        default=os.environ.get("VAULT_RECOVERY_JOURNAL_ROOT", DEFAULT_JOURNAL_ROOT),
    )
    p_rollback.set_defaults(func=_cmd_rollback)
    return parser


def _cmd_verify(args: argparse.Namespace) -> int:
    result = verify_recovery(Path(args.recovery_dir))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    result = install_generation(
        Path(args.recovery_dir),
        Path(args.live_vault),
        Path(args.live_gbrain),
        confirm=args.i_confirm_this_overwrites_production,
        journal_root=Path(args.journal_root),
        generation=args.generation,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    result = rollback_generation(Path(args.journal_root), args.generation_id)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        # The core CLI boundary: only the actual Hermes runtime user may run
        # the verifier/installer (root and arbitrary non-Hermes uids are
        # rejected; the shell wrapper enforces the same identity — defense in
        # depth, not a substitute).
        core.ensure_hermes_identity()
        return args.func(args)
    except VaultRecoveryRestoreError as exc:
        print(f"[vault-recovery-restore] ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[vault-recovery-restore] UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
