#!/usr/bin/env python3
"""Run one command while holding the shared TaskNotes/gbrain process lock."""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional


LOCK_BUSY_EXIT = 75
TIMEOUT_EXIT = 124


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-path", required=True, type=Path)
    parser.add_argument("--nonblocking", action="store_true")
    parser.add_argument("--timeout", type=float, default=0.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("tasknotes-lock-run: command is required", file=sys.stderr)
        return 2
    if args.timeout < 0:
        print("tasknotes-lock-run: timeout must not be negative", file=sys.stderr)
        return 2

    args.lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = os.open(
            args.lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        print(f"tasknotes-lock-run: cannot open lock safely: {exc}", file=sys.stderr)
        return 1

    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if args.nonblocking else 0)
        try:
            fcntl.flock(lock_fd, flags)
        except OSError as exc:
            if args.nonblocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                print("tasknotes-lock-run: lock busy", file=sys.stderr)
                return LOCK_BUSY_EXIT
            print(f"tasknotes-lock-run: lock failed: {exc}", file=sys.stderr)
            return 1

        try:
            process = subprocess.Popen(command, start_new_session=True)
        except OSError as exc:
            print(f"tasknotes-lock-run: cannot start command: {exc}", file=sys.stderr)
            return 127

        forwarded_signal: Optional[int] = None

        def forward(signum: int, _frame: object) -> None:
            nonlocal forwarded_signal
            forwarded_signal = signum
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

        previous = {
            signum: signal.signal(signum, forward)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            try:
                returncode = process.wait(timeout=args.timeout or None)
            except subprocess.TimeoutExpired:
                _kill_group(process)
                process.wait()
                print("tasknotes-lock-run: command timed out", file=sys.stderr)
                return TIMEOUT_EXIT
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

        if forwarded_signal is not None:
            return 128 + forwarded_signal
        return returncode
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(run())
