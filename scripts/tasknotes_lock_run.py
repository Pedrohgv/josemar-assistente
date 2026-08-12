#!/opt/hermes/.venv/bin/python3 -I
"""Run one command while holding the shared TaskNotes/gbrain process lock.

Holds an exclusive flock on the lock file for the whole lifetime of the
child. The child is spawned in its own process group (start_new_session) so a
timeout or an incoming signal can terminate the entire group — TERM grace,
then KILL — and the lock is released only after the group has been reaped.

The child inherits the lock fd itself (pass_fds) and is told its number via
TASKNOTES_LOCK_FD, so downstream wrappers (josemar-gbrain refresh/reindex)
can verify they really run under this lock and skip self-acquisition without
any forgeable env boolean (issue #110). Conversely, when this runner is
itself re-entered by the wrapper with an already-validated inherited lock fd
(the wrapper acquired the flock and exec'd the runner to gain process-group
and timeout protection), the runner skips its own acquisition and manages
that same fd for the whole chain.

Termination signals (SIGINT/SIGTERM/SIGHUP/SIGQUIT) are blocked before the
child can exist, the handlers are installed, and only then the mask is
restored: a signal arriving during spawn is delivered to the handler, never
to the default disposition (which would orphan the group and release the
lock while the child still runs). The child's own signal mask is reset before
exec so the spawned tree keeps normal termination semantics. The incoming
signal is forwarded to the whole group (TERM grace, then KILL) and the runner
exits with 128+signal only after the group has demonstrably cleared; a
survivor past SIGKILL is reported rather than silently claimed clean.

Exit codes:
  75      lock busy (nonblocking miss, or acquisition timeout)
  124     child/group runtime timeout
  128+N   terminated by signal N (runner or child, independently)
  other   the child's own exit status
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


LOCK_BUSY_EXIT = 75
TIMEOUT_EXIT = 124
LOCK_FD_ENV = "TASKNOTES_LOCK_FD"
TERM_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
GROUP_DRAIN = 2.0  # post-KILL wait for the process group to clear


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-path", required=True, type=Path)
    parser.add_argument("--nonblocking", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=0.0)
    parser.add_argument("--kill-grace", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _group_cleared(process: subprocess.Popen[bytes]) -> bool:
    """True when the leader is reaped and no process remains in its group.

    Reaps the leader first (a dead-but-unreaped leader is still a group
    member), then probes the group with killpg(..., 0). A surviving grandchild
    keeps the group alive regardless of the leader's own exit state.
    """
    process.poll()
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _terminate_group(
    process: subprocess.Popen[bytes], grace: float, sig: int
) -> None:
    """Signal the whole group, escalate to SIGKILL after the grace window,
    then wait (bounded) for the group to clear so the lock is only released
    after every child is gone.

    Never calls process.wait(): that takes Popen's _waitpid_lock, which the
    main flow already holds when this runs inside a signal handler that
    interrupted a blocking wait (same-thread re-acquire would deadlock).
    poll() is lock-free and reaps the leader."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and not _group_cleared(process):
        time.sleep(0.05)
    if not _group_cleared(process):
        _kill_group(process)
    deadline = time.monotonic() + GROUP_DRAIN
    while time.monotonic() < deadline and not _group_cleared(process):
        time.sleep(0.05)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.05)


def _warn_survivors() -> None:
    print(
        "tasknotes-lock-run: WARNING: process group survived SIGKILL; the "
        "lock may be released while a survivor still runs",
        file=sys.stderr,
    )


def _acquire_lock(
    lock_fd: int, nonblocking: bool, lock_timeout: float
) -> Optional[int]:
    """Acquire the flock. Returns None on success, or an exit code on failure."""
    if nonblocking:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                print("tasknotes-lock-run: lock busy", file=sys.stderr)
                return LOCK_BUSY_EXIT
            print(f"tasknotes-lock-run: lock failed: {exc}", file=sys.stderr)
            return 1
        return None
    if lock_timeout <= 0:
        # Unbounded blocking acquisition (legacy behavior).
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError as exc:
            print(f"tasknotes-lock-run: lock failed: {exc}", file=sys.stderr)
            return 1
        return None
    deadline = time.monotonic() + lock_timeout
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                print(f"tasknotes-lock-run: lock failed: {exc}", file=sys.stderr)
                return 1
            if time.monotonic() >= deadline:
                print(
                    f"tasknotes-lock-run: lock acquisition timed out after "
                    f"{lock_timeout:.1f}s",
                    file=sys.stderr,
                )
                return LOCK_BUSY_EXIT
            time.sleep(0.1)


def _reset_child_sigmask() -> None:
    """Run in the forked child before exec (preexec_fn): unblock every
    signal so the spawned tree keeps normal termination semantics instead of
    inheriting the parent's temporarily blocked mask. Safe here: the runner
    is single-threaded at the point of the spawn."""
    signal.pthread_sigmask(signal.SIG_SETMASK, [])


def _inherited_lock_fd(lock_path: Path) -> Optional[int]:
    """Return the inherited lock fd only when it is a validated, actually-held
    EXCLUSIVE flock on the exact configured lock file (same verification as
    josemar-gbrain's lock_held_by_runner); None otherwise.

    Used when the wrapper already holds the flock and re-enters through this
    runner to gain process-group/timeout protection: the runner must not try
    to acquire the lock again (its own fd would conflict with the held one).
    An attacker-supplied fd number, shared lock, or fd to another file fails
    closed here and the runner falls back to its own safe acquisition.
    """
    raw = os.environ.get(LOCK_FD_ENV)
    if not raw:
        return None
    try:
        fd = int(raw)
    except ValueError:
        return None
    try:
        st_fd = os.fstat(fd)
        st_path = os.stat(lock_path)
    except OSError:
        return None
    if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
        return None
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="utf-8") as fh:
            info = fh.read()
    except OSError:
        return None
    for line in info.splitlines():
        # Exclusive flock: "FLOCK ... WRITE"; shared (LOCK_SH) shows READ.
        if line.startswith("lock:") and "FLOCK" in line and "WRITE" in line:
            return fd
    return None


def run(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("tasknotes-lock-run: command is required", file=sys.stderr)
        return 2
    if (
        not math.isfinite(args.timeout)
        or not math.isfinite(args.lock_timeout)
        or not math.isfinite(args.kill_grace)
        or args.timeout < 0
        or args.lock_timeout < 0
        or args.kill_grace < 0
    ):
        print(
            "tasknotes-lock-run: timeouts and kill grace must be finite and not negative",
            file=sys.stderr,
        )
        return 2
    if args.nonblocking and args.lock_timeout > 0:
        print(
            "tasknotes-lock-run: --nonblocking and --lock-timeout are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    lock_fd = -1
    try:
        inherited_fd = _inherited_lock_fd(args.lock_path)
        if inherited_fd is None:
            args.lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                lock_fd = os.open(
                    args.lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                )
            except OSError as exc:
                print(
                    f"tasknotes-lock-run: cannot open lock safely: {exc}",
                    file=sys.stderr,
                )
                return 1
            exit_code = _acquire_lock(lock_fd, args.nonblocking, args.lock_timeout)
            if exit_code is not None:
                return exit_code  # the finally below closes lock_fd
        else:
            # The caller (josemar-gbrain) already holds the flock on this fd
            # and re-entered through the runner for group/timeout protection.
            lock_fd = inherited_fd

        env = os.environ.copy()
        env[LOCK_FD_ENV] = str(lock_fd)

        # Close the race between the child existing and the termination
        # handlers being installed: block the signals, spawn, install the
        # handlers, then restore the mask. Any signal arriving during spawn
        # stays pending and is delivered to the handler at unblock — never
        # to the default disposition, which would orphan the group and
        # release the flock while the child still runs.
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, TERM_SIGNALS)
        try:
            process = subprocess.Popen(
                command,
                start_new_session=True,
                env=env,
                pass_fds=[lock_fd],
                preexec_fn=_reset_child_sigmask,
            )
        except OSError as exc:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            print(f"tasknotes-lock-run: cannot start command: {exc}", file=sys.stderr)
            return 127

        def forward(signum: int, _frame: object) -> None:
            # Clean up the whole group (TERM grace, then KILL); the kernel
            # only releases the flock once this process is gone, so exit
            # only after the group has demonstrably cleared.
            _terminate_group(process, args.kill_grace, signum)
            if not _group_cleared(process):
                _warn_survivors()
            os._exit(128 + signum)

        previous = {
            signum: signal.signal(signum, forward) for signum in TERM_SIGNALS
        }
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        except BaseException:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            raise
        try:
            try:
                returncode = process.wait(timeout=args.timeout or None)
            except subprocess.TimeoutExpired:
                _terminate_group(process, args.kill_grace, signal.SIGTERM)
                if not _group_cleared(process):
                    _warn_survivors()
                print("tasknotes-lock-run: command timed out", file=sys.stderr)
                return TIMEOUT_EXIT
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

        if returncode is None:
            returncode = process.wait()
        if returncode < 0:
            # The child died of an independent signal; map it like a shell.
            return 128 + (-returncode)
        return returncode
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(run())
