#!/usr/bin/env python3
"""Fake `rclone` binary for vault-recovery uploader/recover unit tests.

Emulates the rclone subset the Phase-2 scripts use over a LOCAL directory
"remote" (no real crypt): config show, copy, move, lsjson, ls, cat,
purge. Remote paths `<name>:/<path>` are mapped under
$FAKE_RCLONE_BASE/<name>/<path>.

The scripts MUST read machine inventories through `lsjson` only — the
human-readable `lsd` command is deliberately NOT implemented, so any
regression back to lsd column parsing fails the fake loudly.

`lsjson` emits the machine JSON inventory the scripts parse (see
vault-recovery-lsjson.awk): a top-level array of objects with the real
rclone field set (Path/Name/Size/MimeType/ModTime/IsDir/ID), `--dirs-only`
restricting to directories, and exit 3 ("directory not found") for a
CONFIRMED absent directory — mirroring the real rclone behavior the
fail-closed listing paths rely on.

Every invocation is appended as one JSON line to $FAKE_RCLONE_LOG so tests
can assert ordering (e.g. remote verify BEFORE commit, commit BEFORE ack).
Each log line carries `"config"`: the `--config <path>` argument of the
invocation (null when absent) so tests can assert which config file rclone
was pointed at (e.g. the private ACTIVE copy of the OAuth-refresh fix, never
the read-only seed). `config show` reads the remote section from the
`--config` argument when present, falling back to $FAKE_RCLONE_CONFIG.

Failure injection (deterministic, no sleeps):
  $FAKE_RCLONE_FAIL_CMDS     - comma-separated command names that exit 1
                               before doing anything.
  $FAKE_RCLONE_FAIL_CMD_AFTER - comma-separated "cmd:N" entries: the Nth
                               invocation of that command exits 1 before
                               doing anything (e.g. "copy:3" fails only the
                               third copy). Used to prove ordering (e.g. the
                               committed-payload verification runs before
                               the READY publication).
  $FAKE_RCLONE_PARTIAL_COPY_TO - when a copy DESTINATION contains this
                               substring, only the first child entry is
                               copied and the command exits 1 (simulates a
                               partial inbound object).
  $FAKE_RCLONE_TAMPER_AFTER_COPY_TO - when a copy DESTINATION contains this
                               substring, one deep file in the destination is
                               appended to after a successful copy (simulates
                               remote content that differs from what the
                               uploader validated locally).
  $FAKE_RCLONE_FAIL_MOVE_SRC_SUBSTR - when a move SOURCE contains this
                               substring, the move exits 1 before copying
                               anything (simulates a failed commit step,
                               e.g. the final READY publication).
  $FAKE_RCLONE_FAIL_CAT_SUBSTR - when a cat PATH contains this substring,
                               the cat exits with $FAKE_RCLONE_FAIL_CAT_EXIT
                               (default 5, rclone's "temporary error")
                               before reading (simulates a transport/auth/
                               backend failure — distinct from a CONFIRMED
                               missing file, which exits 4 like the real
                               rclone "file not found"). Use "READY" to fail
                               the READY-marker read and "manifest.json" to
                               fail the manifest binding read.
  $FAKE_RCLONE_LSJSON_ZERO_BYTES - when set, `lsjson` prints NOTHING and
                               exits 0 (simulates a backend that returns a
                               zero-byte response — a PROTOCOL failure that
                               must never be mistaken for an empty
                               inventory; a real empty namespace emits the
                               valid JSON array `[]`).
  $FAKE_RCLONE_STDERR_STATUS   - when set, this text is printed to STDERR
                               before every invocation (or only the
                               comma-separated commands named in
                               $FAKE_RCLONE_STDERR_STATUS_CMDS),
                               simulating the human-oriented status text
                               real rclone writes to stderr (periodic
                               transfer stats under RCLONE_STATS=30s /
                               RCLONE_STATS_ONE_LINE=true /
                               RCLONE_STATS_LOG_LEVEL=NOTICE). The machine
                               parsers (README/manifest cat, lsjson) must
                               ignore it: parser input is stdout ONLY.
  $FAKE_RCLONE_STDERR_STATUS_CMDS - comma-separated command names that
                               receive the FAKE_RCLONE_STDERR_STATUS text
                               (default: every command).

Cat semantics: a missing file exits 4 ("file not found") exactly like the
real rclone, so the scripts can distinguish a CONFIRMED absent marker from
a failed read (any other non-zero exit).

`--exclude <pattern>` is honored on copy/move (uploader READY-last commit):
anchored patterns ("/READY") match only that top-level entry of the source
root; bare patterns match any path component of that name.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def _log(args: list[str], config: str | None = None) -> None:
    log = os.environ.get("FAKE_RCLONE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"cmd": args[0], "args": args, "config": config}) + "\n"
            )


def _log_count(cmd: str) -> int:
    """Number of times ``cmd`` appears in the (persistent) invocation log,
    including the just-logged current invocation. Each fake-rclone process
    exits after one command, so the count must come from the log file."""
    log = os.environ.get("FAKE_RCLONE_LOG")
    if not log or not os.path.exists(log):
        return 0
    with open(log, "r", encoding="utf-8") as fh:
        return sum(1 for line in fh if f'"cmd": "{cmd}"' in line)


def _fail_cmds() -> set[str]:
    raw = os.environ.get("FAKE_RCLONE_FAIL_CMDS", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def _fail_after() -> dict[str, int]:
    """Map command name -> invocation number at which it must fail (1-based),
    from FAKE_RCLONE_FAIL_CMD_AFTER entries like "copy:3,lsjson:1"."""
    limits: dict[str, int] = {}
    for part in os.environ.get("FAKE_RCLONE_FAIL_CMD_AFTER", "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        cmd, _, raw_n = part.partition(":")
        try:
            n = int(raw_n)
        except ValueError:
            continue
        if n >= 1:
            limits[cmd] = n
    return limits


def _split_remote(path: str) -> tuple[str | None, str]:
    """Return (remote_name_or_None, local_path_under_base_or_None)."""
    if ":" not in path:
        return None, path
    name, _, rest = path.partition(":")
    return name, rest.lstrip("/")


def _resolve(path: str) -> Path:
    name, rest = _split_remote(path)
    if name is None:
        return Path(path)
    base = Path(os.environ.get("FAKE_RCLONE_BASE", "/tmp/fake-rclone"))
    return base / name / rest


def _exclude_patterns(args: list[str]) -> list[str]:
    """Extract `--exclude <pattern>` pairs from an rclone command line."""
    patterns: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--exclude" and i + 1 < len(args):
            patterns.append(args[i + 1])
            i += 2
        else:
            i += 1
    return patterns


def _is_excluded(rel: str, patterns: list[str]) -> bool:
    """rclone exclude semantics for the subset we need: "/name" anchors to
    the source root; a bare "name" matches any component of that name."""
    for raw in patterns:
        pat = raw.rstrip("/")
        if not pat:
            continue
        if pat.startswith("/"):
            if rel == pat[1:]:
                return True
        elif rel == pat or rel.endswith("/" + pat):
            return True
    return False


def _copy_contents(
    src: Path,
    dst: Path,
    create_empty_src_dirs: bool = True,
    exclude: list[str] | None = None,
) -> None:
    """rclone copy semantics: the CONTENTS of a source dir land in dst.

    Like a real crypt remote, POSIX modes do NOT survive the round trip:
    every copied file is flattened to the destination umask default (0644
    & ~umask) so tests exercise the mode-relaxed remote validation instead
    of accidentally passing on mode preservation.

    Without ``--create-empty-src-dirs``, EMPTY directories are NOT copied
    (rclone only creates dirs that hold content) — the caller must pass the
    flag or the entries-index count check fails. ``exclude`` entries (rclone
    ``--exclude`` semantics, see ``_is_excluded``) are skipped.
    """
    default_mode = 0o644 & ~_umask()
    exclude = exclude or []

    def _dir_has_content(entry: Path) -> bool:
        for child in entry.rglob("*"):
            if child.is_file():
                return True
        return False

    def _copy_one(entry: Path, target: Path, create_empty: bool, rel: str) -> None:
        if _is_excluded(rel, exclude):
            return
        if entry.is_dir():
            if not create_empty and not _dir_has_content(entry):
                return
            target.mkdir(parents=True, exist_ok=True)
            for child in entry.iterdir():
                _copy_one(child, target / child.name, create_empty, f"{rel}/{child.name}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
            os.chmod(target, default_mode)

    if src.is_file():
        if _is_excluded(src.name, exclude):
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        target = dst / src.name if dst.is_dir() else dst
        shutil.copy2(src, target)
        os.chmod(target, default_mode)
        return
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        _copy_one(entry, dst / entry.name, create_empty_src_dirs, entry.name)


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _cmd_config_show(args: list[str], config_path: str | None) -> int:
    remote = args[0].rstrip(":")
    cfg = Path(config_path or os.environ.get("FAKE_RCLONE_CONFIG", "/nonexistent"))
    if not cfg.exists():
        print(f"error: config file not found: {cfg}", file=sys.stderr)
        return 1
    lines = cfg.read_text("utf-8").splitlines()
    section = None
    out: list[str] = []
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == remote:
            out.append(line)
    if not out:
        print(f"error: remote not found: {remote}", file=sys.stderr)
        return 1
    print("\n".join(out))
    return 0


def _cmd_copy(args: list[str]) -> int:
    src, dst = args[0], args[1]
    partial_to = os.environ.get("FAKE_RCLONE_PARTIAL_COPY_TO", "")
    if partial_to and partial_to in dst:
        # Simulate a partial inbound object: copy only the first child, then
        # fail.
        src_path = _resolve(src)
        dst_path = _resolve(dst)
        dst_path.mkdir(parents=True, exist_ok=True)
        entries = sorted(src_path.iterdir())
        if entries:
            entry = entries[0]
            target = dst_path / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target)
        print("error: simulated partial copy failure", file=sys.stderr)
        return 1
    create_empty = "--create-empty-src-dirs" in args
    try:
        _copy_contents(_resolve(src), _resolve(dst), create_empty)
    except OSError as exc:
        print(f"error: copy failed: {exc}", file=sys.stderr)
        return 1
    tamper_to = os.environ.get("FAKE_RCLONE_TAMPER_AFTER_COPY_TO", "")
    if tamper_to and tamper_to in dst:
        dst_path = _resolve(dst)
        victim = next(
            (p for p in sorted(dst_path.rglob("*")) if p.is_file()), None
        )
        if victim is not None:
            with open(victim, "a", encoding="utf-8") as fh:
                fh.write("TAMPERED\n")
    return 0


def _prune_empty_parents(path: Path) -> None:
    """Remove source directories left empty by a move, walking up while
    empty (rclone `--delete-empty-src-dirs` behavior)."""
    parent = path if path.is_dir() else path.parent
    while parent.is_dir() and not any(parent.iterdir()):
        shutil.rmtree(parent)
        parent = parent.parent


def _cmd_move(args: list[str]) -> int:
    src, dst = args[0], args[1]
    create_empty = "--create-empty-src-dirs" in args
    exclude = _exclude_patterns(args)
    fail_src_substr = os.environ.get("FAKE_RCLONE_FAIL_MOVE_SRC_SUBSTR", "")
    if fail_src_substr and fail_src_substr in src:
        print(f"error: simulated move failure for source {src}", file=sys.stderr)
        return 1
    try:
        _copy_contents(_resolve(src), _resolve(dst), create_empty, exclude)
        src_path = _resolve(src)
        if src_path.is_dir():
            # rclone move deletes every moved source FILE (deepest first);
            # with --create-empty-src-dirs the source directories are kept
            # (empty or not), otherwise emptied source dirs are removed too
            # (--delete-empty-src-dirs, default for move). Excluded entries
            # stay behind.
            for entry in sorted(src_path.rglob("*"), reverse=True):
                rel = entry.relative_to(src_path).as_posix()
                if _is_excluded(rel, exclude):
                    continue
                if entry.is_file() or entry.is_symlink():
                    entry.unlink()
                elif entry.is_dir() and not create_empty:
                    shutil.rmtree(entry)
            if not create_empty and not any(src_path.iterdir()):
                _prune_empty_parents(src_path)
        else:
            src_path.unlink()
            _prune_empty_parents(src_path)
    except OSError as exc:
        print(f"error: move failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_lsjson(args: list[str]) -> int:
    """rclone lsjson semantics: machine JSON array of per-entry objects with
    the fields the real rclone emits (Path, Name, Size, MimeType, ModTime,
    IsDir, plus backend-specific ID/OrigID), pretty-printed with 2-space
    indentation like the real output. `--dirs-only` restricts to directories
    (the committed-namespace inventory the scripts use). A CONFIRMED absent
    directory exits 3 ("directory not found") like the real rclone."""
    root = _resolve(args[0])
    dirs_only = "--dirs-only" in args
    if os.environ.get("FAKE_RCLONE_LSJSON_ZERO_BYTES"):
        return 0
    if not root.is_dir():
        print(f"error: directory not found: {args[0]}", file=sys.stderr)
        return 3
    entries = []
    for entry in sorted(root.iterdir()):
        if dirs_only and not entry.is_dir():
            continue
        entries.append(entry)
    print("[")
    for i, entry in enumerate(entries):
        comma = "," if i < len(entries) - 1 else ""
        is_dir = entry.is_dir()
        print("  {")
        print(f'    "Path": "{entry.name}",')
        print(f'    "Name": "{entry.name}",')
        if is_dir:
            print('    "Size": -1,')
            print('    "MimeType": "inode/directory",')
        else:
            print(f'    "Size": {entry.stat().st_size},')
            print('    "MimeType": "",')
        print('    "ModTime": "2026-01-01T00:00:00.000000000Z",')
        print(f'    "IsDir": {"true" if is_dir else "false"},')
        print(f'    "ID": "{entry.name}"')
        print("  }" + comma)
    print("]")
    return 0


def _cmd_ls(args: list[str]) -> int:
    root = _resolve(args[0])
    if not root.exists():
        return 0
    for entry in sorted(root.iterdir()):
        if entry.is_file():
            print(f"{entry.stat().st_size:12d} 2026-01-01 00:00:00 {entry.name}")
    return 0


def _cmd_cat(args: list[str]) -> int:
    """Print the content of a single remote file (used for READY-marker and
    manifest binding checks on committed generations). A missing file exits
    4 ("file not found") like the real rclone, so the scripts can tell a
    CONFIRMED absent marker from a failed read. FAKE_RCLONE_FAIL_CAT_SUBSTR
    simulates a transport/auth/backend read failure (default exit 5)."""
    fail_substr = os.environ.get("FAKE_RCLONE_FAIL_CAT_SUBSTR", "")
    if fail_substr and fail_substr in args[0]:
        code = int(os.environ.get("FAKE_RCLONE_FAIL_CAT_EXIT", "5"))
        print(
            f"error: simulated cat failure (exit {code}) for {args[0]}",
            file=sys.stderr,
        )
        return code
    target = _resolve(args[0])
    if not target.is_file():
        print(f"error: file not found: {args[0]}", file=sys.stderr)
        return 4
    with open(target, "rb") as fh:
        sys.stdout.buffer.write(fh.read())
    return 0


def _cmd_purge(args: list[str]) -> int:
    target = _resolve(args[0])
    if target.exists():
        shutil.rmtree(target)
    return 0


def main(argv: list[str]) -> int:
    # Capture the `--config <path>` argument (the config file rclone was
    # pointed at) BEFORE stripping it; the scripts run rclone against the
    # private ACTIVE config copy (OAuth-refresh fix), and `config show`
    # must read that same file. Keep `--create-empty-src-dirs` so the
    # handlers can honor it (empty dirs are dropped without the flag).
    config_path: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            config_path = argv[i + 1]
            break
    args: list[str] = []
    for i, arg in enumerate(argv):
        if arg == "--config":
            continue
        if i > 0 and argv[i - 1] == "--config":
            continue
        args.append(arg)
    if not args:
        print("fake rclone: missing command", file=sys.stderr)
        return 2
    cmd = args[0]
    rest = args[1:]
    _log([cmd, *rest], config=config_path)
    # Benign stderr status text (simulated RCLONE_STATS output): printed to
    # stderr BEFORE any failure injection, like real rclone emitting
    # periodic status while a transfer/read is in progress. The recovery
    # script must parse stdout only, so this text must never contaminate
    # the READY/manifest/lsjson payloads.
    status_text = os.environ.get("FAKE_RCLONE_STDERR_STATUS", "")
    if status_text:
        status_cmds = {
            c.strip()
            for c in os.environ.get("FAKE_RCLONE_STDERR_STATUS_CMDS", "").split(",")
            if c.strip()
        }
        if not status_cmds or cmd in status_cmds:
            print(status_text, file=sys.stderr)
    if cmd in _fail_cmds():
        print(f"error: simulated failure for command {cmd}", file=sys.stderr)
        return 1
    fail_after = _fail_after()
    if cmd in fail_after:
        # 1-based: the Nth invocation of this command fails. The count is
        # read from the persistent log (the current invocation is included).
        if _log_count(cmd) == fail_after[cmd]:
            print(
                f"error: simulated failure for invocation {fail_after[cmd]} of command {cmd}",
                file=sys.stderr,
            )
            return 1
    if cmd == "config":
        if rest and rest[0] == "show":
            return _cmd_config_show(rest[1:], config_path)
        print("fake rclone: unsupported config subcommand", file=sys.stderr)
        return 2
    handler = {
        "copy": _cmd_copy,
        "move": _cmd_move,
        "lsjson": _cmd_lsjson,
        "ls": _cmd_ls,
        "cat": _cmd_cat,
        "purge": _cmd_purge,
    }.get(cmd)
    if handler is None:
        print(f"fake rclone: unsupported command {cmd}", file=sys.stderr)
        return 2
    return handler(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
