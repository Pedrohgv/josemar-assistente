#!/opt/hermes/.venv/bin/python3 -I
"""gbrain: public safe adapter for chat/external native gbrain commands
(issue #110). This script is installed AS /usr/local/bin/gbrain; the native
CLI lives at the private non-PATH path /opt/josemar/libexec/gbrain-native.

The native gbrain CLI must never open the PGLite data directory while another
gbrain process is active, and must never run as root. This adapter is the
single safe path for chat/external gbrain invocations:

  - executes ONLY the fixed pinned native binary
    /opt/josemar/libexec/gbrain-native; the remaining argv is passed through
    unchanged. There is no runtime override: the native binary path, the
    lock runner path, the lock path, and the Python interpreter are
    module-level constants, exposed only as keyword parameters of main() so
    tests can inject fakes without touching the production path (the CLI
    entrypoint always uses the constants).
  - enforces an explicit native subcommand allowlist matching the documented
    chat surface (search, get, capture, put, link, backlinks, status, query,
    chronicle queries, jobs, ...). Maintenance/admin commands (init, config,
    sync, extract, embed, migrate, schema, ...) are rejected: they are
    operator-only and run through josemar-gbrain instead.
  - runs the lock runner with the fixed image interpreter in isolated mode
    (python3 -I): PYTHONPATH/sitecustomize from the caller's environment
    cannot execute code in the runner before the lock is taken
  - drops root privileges to the `hermes` runtime user BEFORE the shared lock
    is touched; the drop is unconditional — no environment or CLI flag can
    skip it (the only other way to run non-root is to start as non-root).
  - exports the canonical gbrain env, assigned explicitly so a caller-supplied
    value can never redirect the pinned binary: GBRAIN_HOME, GBRAIN_BRAIN_REPO,
    GBRAIN_SCHEMA_PACK, GBRAIN_SKIP_STARTUP_HOOKS. GBRAIN_SCHEMA_PACK comes
    from the runtime source of truth written by `josemar-gbrain reindex`
    (/opt/data/.gbrain/active-schema-pack), failing closed to the canonical
    "josemar" pack — never from the caller's environment.
  - serializes the call through the shared TaskNotes/gbrain lock
    (/opt/data/.locks/tasknotes.lock) with bounded lock acquisition and a
    bounded process runtime, via tasknotes_lock_run.py
  - preserves stdin/stdout/stderr and the child exit status exactly: the
    adapter exec-replaces itself with the lock runner, so there is no extra
    process layer between the caller and the gbrain command

It is deliberately NOT used by the TaskNotes MCP (already the lock owner) and
not by the operator wrapper josemar-gbrain (which self-locks its own gbrain
access).

Exit codes: 2 usage/validation errors (including allowlist rejections),
75 lock busy after the bounded wait, 124 process runtime timeout,
128+N terminated by signal N, otherwise the gbrain child's own status.
"""

from __future__ import annotations

import argparse
import os
import pwd
import re
import sys
from pathlib import Path
from typing import Optional


PYTHON_BIN = "/opt/hermes/.venv/bin/python3"
GBRAIN_BIN = "/opt/josemar/libexec/gbrain-native"
RUNNER = "/opt/josemar/scripts/tasknotes_lock_run.py"
LOCK_PATH = "/opt/data/.locks/tasknotes.lock"
RUNTIME_USER = "hermes"
SCHEMA_PACK_FILE = "/opt/data/.gbrain/active-schema-pack"
DEFAULT_SCHEMA_PACK = "josemar"

# The documented user-facing chat surface (skills-factory gbrain SKILL.md and
# its references/): retrieval, authoring (put/capture/link/delete/revert),
# linking, chronicle queries, `restore`, the read-only `schema-status`
# diagnostic, and read-only `sources list`. Operator-only maintenance (init,
# config, sync, extract, embed, migrate, schema, jobs, chronicle-backfill,
# ...) is rejected before the lock is ever touched.
CHAT_SUBCOMMANDS = frozenset(
    {
        "search",
        "get",
        "capture",
        "put",
        "link",
        "backlinks",
        "status",
        "query",
        "day",
        "since",
        "last-seen",
        "on-this-day",
        "orient",
        "ontology",
        "timeline",
        "graph",
        "tags",
        "history",
        "delete",
        "revert",
        "doctor",
        "restore",
        "schema-status",
        "sources",
    }
)

# Subcommands that take a further subcommand token: only the listed
# sub-subcommands are agent-facing (all read-only). Anything else under these
# parents (e.g. `sources add|remove|harden`) is operator-only and rejected.
CHAT_SUBSUBCOMMANDS = {
    "sources": frozenset({"list"}),
}

# Argument forms that are operator-only even under an allowlisted subcommand.
# `put --stdin` is the bulk/scripted ingestion path (TaskNotes-style writes);
# agents write via `put <slug> --content ...` or `capture --file/--stdin`.
CHAT_REJECTED_ARGUMENTS = {
    "put": ("--stdin",),
}


def _chat_subcommand_allowed(gbrain_args: list[str]) -> bool:
    """True when the argv is on the documented agent-facing surface.

    Top-level subcommands are allowlisted wholesale; subcommands that take a
    further subcommand token are allowlisted per sub-subcommand so only the
    read-only variants (sources list) pass while mutations stay
    operator-only. Allowlisted subcommands may still carry rejected argument
    forms (put --stdin).
    """
    head = gbrain_args[0]
    if head not in CHAT_SUBCOMMANDS:
        return False
    allowed_subs = CHAT_SUBSUBCOMMANDS.get(head)
    if allowed_subs is not None:
        if len(gbrain_args) < 2 or gbrain_args[1] not in allowed_subs:
            return False
    rejected = CHAT_REJECTED_ARGUMENTS.get(head, ())
    if any(
        arg == form or arg.startswith(form + "=")
        for arg in gbrain_args[1:]
        for form in rejected
    ):
        return False
    return True


# Mechanical command inventory for policy tests: the docs-policy suite
# asserts the documented user-facing surface against this single export
# instead of maintaining a fragile duplicate list.
CHAT_COMMAND_INVENTORY = {
    "subcommands": sorted(CHAT_SUBCOMMANDS),
    "subsubcommands": {name: sorted(subs) for name, subs in CHAT_SUBSUBCOMMANDS.items()},
    "rejected_arguments": {name: list(forms) for name, forms in CHAT_REJECTED_ARGUMENTS.items()},
    "operator_only": sorted(
        {
            "init", "config", "sync", "extract", "embed", "migrate", "schema",
            "reindex", "refresh", "import", "export", "jobs",
            "chronicle-backfill",
        }
    ),
}


# Conservative fixed bounds for the shared-lock wait, the native process
# runtime, and the TERM->KILL grace. These are deliberately NOT public CLI
# knobs: agent-facing callers cannot weaken them (a hostile caller must not
# be able to hold the lock forever or run gbrain unbounded).
LOCK_WAIT_TIMEOUT = 30.0
RUN_TIMEOUT = 300.0
KILL_GRACE = 5.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gbrain",
        description=(
            "Run a native gbrain command as the hermes runtime user under the "
            "shared TaskNotes/gbrain lock."
        ),
    )
    parser.add_argument(
        "gbrain_args",
        nargs=argparse.REMAINDER,
        help="gbrain subcommand and its arguments",
    )
    return parser


def _drop_root() -> None:
    """Become the hermes runtime user before the shared lock is touched.

    The drop is unconditional: there is deliberately no environment flag that
    could skip it. A process already running as hermes is detected via the
    effective UID and simply continues.
    """
    if os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam(RUNTIME_USER)
    except KeyError:
        print(
            "gbrain-chat-run: root execution refused: the hermes runtime user does not exist",
            file=sys.stderr,
        )
        raise SystemExit(1)
    os.environ["HOME"] = os.environ.get("HERMES_HOME", "/opt/data")
    os.environ.setdefault("XDG_CONFIG_HOME", "/opt/data/.config")
    os.initgroups(pw.pw_name, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)


def _active_schema_pack(schema_pack_file: str) -> str:
    """Runtime source of truth for the schema pack.

    `josemar-gbrain reindex` persists the activated pack at the fixed path
    /opt/data/.gbrain/active-schema-pack. The adapter reads it so agent-facing
    calls use the same pack the operator activated, instead of a hardcoded
    guess that drifts from the runtime. Any unreadable/invalid value fails
    closed to the canonical default; the path is fixed and never
    caller-controlled.
    """
    try:
        pack = Path(schema_pack_file).read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_SCHEMA_PACK
    if re.fullmatch(r"[a-z0-9._-]+", pack):
        return pack
    return DEFAULT_SCHEMA_PACK


def main(
    argv: Optional[list[str]] = None,
    *,
    gbrain_bin: str = GBRAIN_BIN,
    runner: str = RUNNER,
    lock_path: str = LOCK_PATH,
    interpreter: str = PYTHON_BIN,
    schema_pack_file: str = SCHEMA_PACK_FILE,
) -> int:
    """Run the lock runner against the pinned gbrain binary.

    The keyword parameters exist only as test seams for the module-level
    import; the CLI entrypoint (__main__) always uses the fixed constants,
    so no environment or command line can override the production paths.
    The runner is always started with the fixed interpreter in isolated mode
    (-I): PYTHONPATH/sitecustomize from the caller's environment cannot run
    code in the runner before the lock is taken.
    """
    args = _parser().parse_args(argv)
    gbrain_args = list(args.gbrain_args)
    if gbrain_args and gbrain_args[0] == "--":
        gbrain_args = gbrain_args[1:]
    if not gbrain_args:
        print("gbrain: a gbrain subcommand is required", file=sys.stderr)
        return 2
    if not _chat_subcommand_allowed(gbrain_args):
        if len(gbrain_args) > 1 and gbrain_args[0] in CHAT_SUBSUBCOMMANDS:
            label = f"{gbrain_args[0]} {gbrain_args[1]}"
        elif len(gbrain_args) > 1 and gbrain_args[0] in CHAT_REJECTED_ARGUMENTS:
            label = f"{gbrain_args[0]} {gbrain_args[1]}"
        else:
            label = gbrain_args[0]
        print(
            f"gbrain: subcommand {label!r} is not on the agent-facing "
            "allowlist; operator-only maintenance runs through josemar-gbrain",
            file=sys.stderr,
        )
        return 2

    # Canonical env, assigned explicitly (never setdefault) so a
    # caller-supplied value cannot redirect the pinned binary at a different
    # brain repo or schema pack. The schema pack comes from the runtime
    # source of truth written at activation, not from the caller.
    os.environ["GBRAIN_HOME"] = "/opt/data"
    os.environ["GBRAIN_BRAIN_REPO"] = "/opt/data/obsidian"
    os.environ["GBRAIN_SCHEMA_PACK"] = _active_schema_pack(schema_pack_file)
    os.environ["GBRAIN_SKIP_STARTUP_HOOKS"] = "1"

    _drop_root()

    try:
        os.execv(
            interpreter,
            [
                interpreter,
                "-I",
                runner,
                "--lock-path", str(lock_path),
                "--lock-timeout", str(LOCK_WAIT_TIMEOUT),
                "--timeout", str(RUN_TIMEOUT),
                "--kill-grace", str(KILL_GRACE),
                "--",
                gbrain_bin,
                *gbrain_args,
            ],
        )
    except OSError as exc:
        print(f"gbrain-chat-run: cannot exec lock runner: {exc}", file=sys.stderr)
        return 127
    return 127  # unreachable: execv only returns on failure


if __name__ == "__main__":
    raise SystemExit(main())
