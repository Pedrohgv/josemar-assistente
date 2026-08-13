#!/opt/hermes/.venv/bin/python3 -I
"""Run the embedding refresh inside its own process group so a timeout can
terminate the whole tree (wrapper, gbrain, and any gbrain child) and thereby
release the tasknotes flock instead of orphaning a lock holder.

The wrapper binary is spawned with start_new_session=True (its own process
group). On timeout the entire group receives SIGTERM, is given a bounded grace
period, and then SIGKILL. Cleanup is driven by the liveness of the process
GROUP, not the leader: even if the leader exits on SIGTERM, a grandchild that
ignores SIGTERM and still holds the flock keeps the group alive, so the
SIGKILL escalation still fires and the flock is released. The helper only
reports completion after the group has demonstrably cleared; a survivor past
SIGKILL is warned about rather than silently claimed clean.

Outer-timeout protection: the cron entrypoint constrains
GBRAIN_EMBED_REFRESH_TIMEOUT below HERMES_CRON_SCRIPT_TIMEOUT after grace,
group drain, and safety margin. The helper repeats that validation when
invoked directly. SIGTERM/SIGINT are blocked before the child can exist, the
handlers are installed, and only then the mask is restored: a signal arriving
during spawn is delivered to the handler, never to the default disposition
(which would orphan the group and release the lock while the child still
runs). The child's own signal mask is reset before exec so the spawned tree
keeps normal termination semantics. The incoming signal is forwarded to the
whole group with the same bounded TERM->KILL cleanup.

The command is an immutable constant (/usr/local/bin/josemar-gbrain
refresh-embeddings): the environment cannot redirect the helper to execute
an uncooperative command. Tests inject a fake command through the main(cmd=)
keyword seam, which the CLI entrypoint never exercises.

Duration knobs are env-driven so the cron can hand down bounded values; all
are validated finite (NaN/inf are rejected to defaults) and the total budget
is capped below the Hermes outer deadline:
  GBRAIN_EMBED_REFRESH_TIMEOUT    seconds before the group is killed
                                  (default 240)
  GBRAIN_EMBED_REFRESH_KILL_GRACE seconds to wait after SIGTERM before
                                  SIGKILL (default 5)
Exit code: the child's return code on success/failure, or 124 on timeout.
"""
import math
import os
import shlex
import signal
import subprocess
import sys
import time
from typing import Optional


REFRESH_CMD = "/usr/local/bin/josemar-gbrain refresh-embeddings"
TERM_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _env_float(name: str, default: float) -> float:
    """Finite nonnegative float from the environment, else the default.

    NaN/inf/-inf are never accepted as durations: they would make the
    timeout arithmetic unbounded or nonsensical."""
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return value


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_cleared(proc: subprocess.Popen) -> bool:
    """True when the leader is reaped and no process remains in its group.

    Reaps the leader first (a dead-but-unreaped leader is still a group
    member), then probes the group with killpg(..., 0). A surviving grandchild
    keeps the group alive regardless of the leader's own exit state.
    """
    proc.poll()
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _shutdown_group(proc: subprocess.Popen, grace: float, drain: float) -> bool:
    """TERM the whole group, escalate to SIGKILL if it survives the grace
    window, then wait (bounded) for the group to clear.

    Independent of the leader's proc.poll() state, so a grandchild that
    ignores SIGTERM and holds the flock cannot be orphaned. Returns True only
    when the group has demonstrably cleared.
    """
    _signal_group(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and not _group_cleared(proc):
        time.sleep(0.05)
    if not _group_cleared(proc):
        _signal_group(proc.pid, signal.SIGKILL)
    deadline = time.monotonic() + drain
    while time.monotonic() < deadline and not _group_cleared(proc):
        time.sleep(0.05)
    return _group_cleared(proc)


def _reset_child_sigmask() -> None:
    """Run in the forked child before exec (preexec_fn): unblock every
    signal so the spawned tree keeps normal termination semantics instead of
    inheriting the parent's temporarily blocked mask. Safe here: the helper
    is single-threaded at the point of the spawn."""
    signal.pthread_sigmask(signal.SIG_SETMASK, [])


def _warn_survivors() -> None:
    print(
        "gbrain embedding refresh: WARNING: process group survived SIGKILL; "
        "the flock may be released while a survivor still runs",
        file=sys.stderr,
    )


def main(cmd: Optional[list[str]] = None) -> int:
    command = cmd if cmd is not None else shlex.split(REFRESH_CMD)
    timeout = _env_float("GBRAIN_EMBED_REFRESH_TIMEOUT", 240.0)
    grace = _env_float("GBRAIN_EMBED_REFRESH_KILL_GRACE", 5.0)
    drain = _env_float("GBRAIN_EMBED_REFRESH_GROUP_DRAIN", 2.0)
    margin = _env_float("GBRAIN_EMBED_REFRESH_TIMEOUT_MARGIN", 10.0)
    outer = _env_float("HERMES_CRON_SCRIPT_TIMEOUT", 300.0)
    maximum = outer - grace - drain - margin - 1.0
    # Accept timeout == maximum (the value the cron entrypoint caps to with
    # `-ge safe_timeout`); reject anything strictly above so a direct
    # invocation cannot exceed the outer deadline.
    if maximum < 0.1 or timeout > maximum:
        print(
            "daily embedding timeout is not strictly below the Hermes outer timeout",
            file=sys.stderr,
        )
        return 2

    # Close the race between the child existing and the termination handlers
    # being installed: block the signals, spawn, install the handlers, then
    # restore the mask. Any signal arriving during spawn stays pending and is
    # delivered to the handler at unblock — never to the default disposition,
    # which would orphan the group and release the flock while the child
    # still runs.
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, TERM_SIGNALS)
    try:
        proc = subprocess.Popen(
            command,
            start_new_session=True,
            preexec_fn=_reset_child_sigmask,
        )
    except OSError as exc:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        print(f"gbrain embedding refresh: cannot start command: {exc}", file=sys.stderr)
        return 127

    def _on_signal(signum: int, _frame: object) -> None:
        # An outer timeout (Hermes scheduler) or Ctrl-C signaled the helper
        # itself. Clean up the whole group before exiting so a gbrain child
        # cannot be orphaned holding the tasknotes flock; exit only after the
        # group has demonstrably cleared (or warn that it has not).
        if not _shutdown_group(proc, grace, drain):
            _warn_survivors()
        print(
            "gbrain embedding refresh interrupted; process group terminated",
            file=sys.stderr,
        )
        os._exit(128 + signum)

    previous = {
        signum: signal.signal(signum, _on_signal) for signum in TERM_SIGNALS
    }
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    except BaseException:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if not _shutdown_group(proc, grace, drain):
                _warn_survivors()
            print(
                "gbrain embedding refresh timed out; process group terminated",
                file=sys.stderr,
            )
            return 124
        if proc.returncode is None:
            proc.wait()
        return int(proc.returncode or 0)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
