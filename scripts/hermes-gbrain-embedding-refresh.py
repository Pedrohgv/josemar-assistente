#!/usr/bin/env python3
"""Run the embedding refresh inside its own process group so a timeout can
terminate the whole tree (wrapper, gbrain, and any gbrain child) and thereby
release the tasknotes flock instead of orphaning a lock holder.

The wrapper binary is spawned with start_new_session=True (its own process
group). On timeout the entire group receives SIGTERM, is given a bounded grace
period, and then SIGKILL. Cleanup is driven by the liveness of the process
GROUP, not the leader: even if the leader exits on SIGTERM, a grandchild that
ignores SIGTERM and still holds the flock keeps the group alive, so the
SIGKILL escalation still fires and the flock is released.

Outer-timeout protection: the cron entrypoint constrains
GBRAIN_EMBED_REFRESH_TIMEOUT below HERMES_CRON_SCRIPT_TIMEOUT after grace,
group drain, and safety margin. The helper repeats that validation when
invoked directly. SIGTERM/SIGINT are forwarded to the group with the same
bounded TERM->KILL cleanup, so an outer timeout that signals the helper still
leaves no orphaned lock holder behind.

All knobs are env-driven so the script is behavior-testable without root:
  GBRAIN_EMBED_REFRESH_CMD       command to run (default josemar-gbrain
                                 refresh-embeddings)
  GBRAIN_EMBED_REFRESH_TIMEOUT   seconds before the group is killed
                                 (default 240)
  GBRAIN_EMBED_REFRESH_KILL_GRACE seconds to wait after SIGTERM before
                                 SIGKILL (default 5)
Exit code: the child's return code on success/failure, or 124 on timeout.
"""
import os
import shlex
import signal
import subprocess
import sys
import time


def _env_float(name: str, default: float) -> float:
    try:
        return max(float(os.environ.get(name, "")), 0.1)
    except ValueError:
        return default


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


def _shutdown_group(proc: subprocess.Popen, grace: float, drain: float) -> None:
    """TERM the whole group, escalate to SIGKILL if it survives the grace
    window, then wait (bounded) for the group to clear.

    Independent of the leader's proc.poll() state, so a grandchild that
    ignores SIGTERM and holds the flock cannot be orphaned.
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


def main() -> int:
    cmd = shlex.split(
        os.environ.get(
            "GBRAIN_EMBED_REFRESH_CMD",
            "/usr/local/bin/josemar-gbrain refresh-embeddings",
        )
    )
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

    proc = subprocess.Popen(cmd, start_new_session=True)

    def _on_signal(signum: int, _frame: object) -> None:
        # An outer timeout (Hermes scheduler) or Ctrl-C signaled the helper
        # itself. Clean up the whole group before exiting so a gbrain child
        # cannot be orphaned holding the tasknotes flock.
        _shutdown_group(proc, grace, drain)
        print(
            "gbrain embedding refresh interrupted; process group terminated",
            file=sys.stderr,
        )
        os._exit(128 + signum)

    previous = {
        signum: signal.signal(signum, _on_signal)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _shutdown_group(proc, grace, drain)
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
