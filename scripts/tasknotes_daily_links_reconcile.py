#!/usr/bin/env python3
"""Fixed-purpose Daily-links reconciliation CLI (issue #139 W3, refresh-only).

This CLI has exactly one job: run ONE step of the approved W2 Daily-links
reconciliation lifecycle on the fixed vault, when invoked by the
``josemar-gbrain refresh`` wrapper in the required order:

  1. ``reconcile`` - prepare the plan and apply it with the targeted
     commit (the W2 pending record stays replayable on any failure),
  2. (the wrapper then runs its native sync/extract while still holding
     the shared lock), and
  3. ``finalize`` - advance the cursor; the wrapper invokes this strictly
     after its native sync returned success, which satisfies the core's
     sync-success requirement by construction.

Fixed-purpose by construction:
  - Exactly two positional verbs (``reconcile``/``finalize``); no options,
    no path arguments, no generic action dispatch, and no generic
    note/task writer interface.
  - All locations are fixed constants (vault, shared lock file, and the
    core's fixed cursor/pending state paths); nothing is
    caller-configurable.
  - Reconciliation is never reimplemented here: the approved W2 core API
    (``prepare_daily_links_reconciliation`` /
    ``apply_daily_links_reconciliation`` /
    ``finalize_daily_links_reconciliation``) does all the work.

Safety invariants:
  - The master reconcile flag (``TASKNOTES_DAILY_LINKS_ENABLED``) and the
    slave reconcile flag (``TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED``) are
    both validated strictly: missing or empty means disabled, a nonempty
    value must be exactly ``true`` or ``false`` (case-insensitive), and
    anything else is a structured failure (never coerced). Reconciliation
    is fully inert unless BOTH flags are enabled: when either is disabled
    the CLI does no cursor/pending read or write, no vault access, and no
    lock requirement - it exits successfully so refresh proceeds exactly
    as before the feature existed.
  - Refuses root execution outright: the shared lock and all state access
    belong to the hermes runtime user.
  - Requires the INHERITED shared tasknotes flock: ``TASKNOTES_LOCK_FD``
    must refer to the exact configured lock file with an exclusive flock
    actually held (the same validation as the wrapper's
    ``lock_held_by_runner``). The CLI never acquires a lock itself, never
    creates a new lock, and never invokes the public ``gbrain`` wrapper;
    it only runs inside the wrapper's lock-held chain.
  - The one validated ``DailyNotesConfig`` snapshot is loaded once per
    invocation and passed unchanged through the W2 calls. Finalize does
    NOT re-read vault config: it reconstructs the exact applied snapshot
    from the validated private pending record's pinned
    ``daily_folder``/``daily_format`` (via the W2 public core pending
    reader), so the applied config is preserved across the separate CLI
    phases and no mutable vault config read happens after native sync.
  - All output is JSON envelopes; messages are capped and content-free.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

# Make the co-installed core engine importable under the isolated (-I)
# interpreter: the CLI and tasknotes_mcp_core.py ship in the same fixed
# image directory (/opt/josemar/scripts), which isolated mode removes from
# sys.path. The directory is derived from this file's real location only.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tasknotes_mcp_core import (
    CoreError,
    DAILY_LINKS_RECONCILE_CURSOR_PATH,
    DAILY_LINKS_RECONCILE_PENDING_PATH,
    DailyNotesConfig,
    apply_daily_links_reconciliation,
    finalize_daily_links_reconciliation,
    load_daily_links_reconcile_pending,
    load_daily_notes_config,
    load_profile,
    prepare_daily_links_reconciliation,
)

# Fixed deployment locations (issue #110): never env-overridable. The
# cursor/pending paths are aliased verbatim from the core so the fixed
# state locations have exactly one source of truth.
VAULT = Path("/opt/data/obsidian")
LOCK_PATH = Path("/opt/data/.locks/tasknotes.lock")
RECONCILE_CURSOR_PATH = DAILY_LINKS_RECONCILE_CURSOR_PATH
RECONCILE_PENDING_PATH = DAILY_LINKS_RECONCILE_PENDING_PATH

# Master reconcile flag (issue #139). Same strict semantics as the MCP
# server's boolean parsing: missing/empty disables; anything nonempty must
# be exactly true/false.
MASTER_FLAG = "TASKNOTES_DAILY_LINKS_ENABLED"

# Slave reconcile flag (issue #139 W3). Same strict semantics as the master
# flag. Reconciliation is fully inert unless BOTH the master and slave flags
# are enabled; the slave is parsed before any lock/cursor/pending/vault
# access so an invalid slave fails closed before lifecycle access.
SLAVE_FLAG = "TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED"

# The only verbs this fixed-purpose CLI accepts.
RECONCILE_VERB = "reconcile"
FINALIZE_VERB = "finalize"

# Shared lock fd handed down by the josemar-gbrain wrapper chain
# (tasknotes_lock_run.py / run_under_lock).
LOCK_FD_ENV = "TASKNOTES_LOCK_FD"

MAX_MESSAGE = 500


def _runtime_uid() -> int:
    """Effective UID of the running process."""
    return os.geteuid()


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _fail(action: str, error: str, message: object) -> NoReturn:
    _emit({
        "success": False,
        "action": action,
        "error": error,
        "message": str(message)[:MAX_MESSAGE],
    })
    raise SystemExit(1)


def _master_flag_enabled() -> bool:
    """Strictly parse the master reconcile flag (fail closed, never coerce)."""
    raw = os.environ.get(MASTER_FLAG)
    if raw is None or raw.strip() == "":
        return False
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise CoreError(f"{MASTER_FLAG} must be 'true' or 'false'")


def _slave_flag_enabled() -> bool:
    """Strictly parse the slave reconcile flag (fail closed, never coerce).

    Same semantics as the master flag: missing/empty disables; a nonempty
    value must be exactly ``true`` or ``false`` (case-insensitive); anything
    else is a structured failure. Reconciliation is fully inert unless both
    the master and slave flags are enabled.
    """
    raw = os.environ.get(SLAVE_FLAG)
    if raw is None or raw.strip() == "":
        return False
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise CoreError(f"{SLAVE_FLAG} must be 'true' or 'false'")


def _inherited_lock_held() -> bool:
    """True only when the inherited fd is an exclusively flocked descriptor
    of the EXACT configured lock file (same check as the wrapper's
    ``lock_held_by_runner``): inode identity via fstat/stat plus an
    exclusive (``FLOCK ... WRITE``) lock in /proc/self/fdinfo. A missing,
    forged, shared, or other-file fd never satisfies this check."""
    raw = os.environ.get(LOCK_FD_ENV)
    if not raw:
        return False
    try:
        fd = int(raw)
    except ValueError:
        return False
    try:
        st_fd = os.fstat(fd)
        st_path = os.stat(LOCK_PATH)
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
        # Exclusive flock: "FLOCK ... WRITE". A shared (LOCK_SH) lock shows
        # READ and must not satisfy the check.
        if line.startswith("lock:") and "FLOCK" in line and "WRITE" in line:
            return True
    return False


def _run_reconcile() -> dict:
    """Prepare and apply one reconciliation cycle (targeted commit inside).

    Loads the TaskNotes profile and the one validated Daily Notes config
    snapshot, then hands off entirely to the approved W2 core API. Any
    core failure propagates and leaves the cursor/pending state replayable.
    """
    profile = load_profile(VAULT, VAULT)
    config = load_daily_notes_config(VAULT)
    plan = prepare_daily_links_reconciliation(
        VAULT,
        profile,
        config,
        cursor_path=RECONCILE_CURSOR_PATH,
        pending_path=RECONCILE_PENDING_PATH,
    )
    outcome = apply_daily_links_reconciliation(
        VAULT,
        profile,
        config,
        plan,
        pending_path=RECONCILE_PENDING_PATH,
    )
    return {
        "action": RECONCILE_VERB,
        "status": "applied",
        "applied": outcome.applied,
        "commit_created": outcome.commit_created,
    }


def _run_finalize() -> dict:
    """Advance the cursor (the wrapper calls this only after its native
    sync returned success, so the core's sync-success requirement is
    satisfied by construction).

    The applied config snapshot is NOT re-read from vault here: finalize
    loads the validated private pending record (the W2 public core
    pending reader) and reconstructs the exact ``DailyNotesConfig`` the
    reconcile phase applied, from that record's pinned ``daily_folder`` /
    ``daily_format``. This preserves the applied snapshot across the
    separate CLI phases and avoids any mutable vault config read after
    native sync. An absent or invalid pending record fails closed below
    (the core's pending loader raises), leaving the cursor untouched."""
    pending = load_daily_links_reconcile_pending(RECONCILE_PENDING_PATH)
    if pending is None:
        raise CoreError("reconcile finalize requires a pending record")
    config = DailyNotesConfig(
        folder=pending.daily_folder,
        format=pending.daily_format,
    )
    finalize_daily_links_reconciliation(
        VAULT,
        config,
        sync_succeeded=True,
        cursor_path=RECONCILE_CURSOR_PATH,
        pending_path=RECONCILE_PENDING_PATH,
    )
    return {"action": FINALIZE_VERB, "status": "finalized"}


def _run_step(action: str) -> None:
    """Run one validated verb under the identity/lock preconditions."""
    # 1. Master and slave flags first and strictly, BOTH parsed and
    # validated unconditionally: when either is disabled this CLI is
    # completely inert (no state reads, no writes, no vault access, no
    # lock requirement) and refresh proceeds unchanged. An invalid slave
    # fails closed here even when the master is false — the values are
    # only combined (AND) after each is strictly parsed, so a malformed
    # flag never short-circuits into a silent disabled run.
    try:
        master_enabled = _master_flag_enabled()
        slave_enabled = _slave_flag_enabled()
        enabled = master_enabled and slave_enabled
    except CoreError as exc:
        _fail(action, "daily_links_flag_invalid", exc)
    if not enabled:
        _emit({"success": True, "action": action, "status": "disabled"})
        return

    # 2. Runtime identity: never root.
    if _runtime_uid() == 0:
        _fail(
            action,
            "runtime_identity_refused",
            "tasknotes_daily_links_reconcile refuses to run as root; "
            "run as the hermes runtime user under the refresh wrapper",
        )

    # 3. Inherited shared tasknotes flock is required (held by the refresh
    # wrapper chain); this CLI never acquires its own lock.
    if not _inherited_lock_held():
        _fail(
            action,
            "tasknotes_lock_not_held",
            "the shared tasknotes lock is not held by the inherited fd; "
            "invoke only through the josemar-gbrain refresh wrapper",
        )

    # 4. Run the fixed W2 lifecycle step.
    try:
        if action == RECONCILE_VERB:
            _emit({"success": True, **_run_reconcile()})
        else:
            _emit({"success": True, **_run_finalize()})
    except CoreError as exc:
        if action == RECONCILE_VERB:
            _fail(action, "daily_links_reconcile_failed", exc)
        _fail(action, "daily_links_finalize_failed", exc)
    except Exception:
        # Never leak unexpected exception content; the failure still
        # fails refresh without advancing the cursor.
        if action == RECONCILE_VERB:
            _fail(action, "daily_links_reconcile_failed",
                  "unexpected failure during reconciliation")
        _fail(action, "daily_links_finalize_failed",
              "unexpected failure during finalize")


def main(argv: list) -> int:
    action = argv[1] if len(argv) == 2 else None
    if action == RECONCILE_VERB:
        _run_step(RECONCILE_VERB)
    elif action == FINALIZE_VERB:
        _run_step(FINALIZE_VERB)
    else:
        _fail(
            "usage",
            "daily_links_usage",
            "Usage: tasknotes_daily_links_reconcile.py reconcile|finalize",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
