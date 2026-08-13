"""Behavior and source-contract tests for the gbrain-chat-run adapter (issue #110).

The adapter is the guarded entrypoint for chat/external native gbrain
commands: it must run only the pinned gbrain binary, preserve argv / stdin /
stdout / stderr / exit status, set the canonical gbrain env, serialize through
the shared tasknotes lock with bounded lock acquisition and runtime, and drop
root to the hermes runtime user before touching the lock.

Behavior tests run the real adapter against a fake gbrain binary and the real
lock runner (no Docker, no root needed: the tests run as a non-root user, so
the privilege drop is a no-op and is covered by source-contract assertions).
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "scripts" / "gbrain_chat_run.py"
RUNNER = REPO_ROOT / "scripts" / "tasknotes_lock_run.py"


class FakeGbrain:
    """A fake `gbrain` binary that logs argv and env and mirrors streams."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.argv_log = tmp / "gbrain-argv.log"
        self.env_log = tmp / "gbrain-env.log"
        self.stdin_file = tmp / "gbrain-stdin.txt"
        self.stdout_file = tmp / "gbrain-stdout.txt"
        self.stderr_file = tmp / "gbrain-stderr.txt"
        self.script = tmp / "gbrain"
        self.script.write_text(
            f"""#!/bin/sh
printf '%s\\n' "$@" >> "{self.argv_log}"
printf 'GBRAIN_SKIP_STARTUP_HOOKS=%s\\n' "${{GBRAIN_SKIP_STARTUP_HOOKS:-}}" >> "{self.env_log}"
printf 'GBRAIN_HOME=%s\\n' "${{GBRAIN_HOME:-}}" >> "{self.env_log}"
printf 'GBRAIN_BRAIN_REPO=%s\\n' "${{GBRAIN_BRAIN_REPO:-}}" >> "{self.env_log}"
printf 'GBRAIN_SCHEMA_PACK=%s\\n' "${{GBRAIN_SCHEMA_PACK:-}}" >> "{self.env_log}"
printf 'TASKNOTES_LOCK_FD=%s\\n' "${{TASKNOTES_LOCK_FD:-}}" >> "{self.env_log}"
cat > "{self.stdin_file}"
printf 'fake stdout: %s\\n' "$*" > "{self.stdout_file}"
printf 'fake stderr: %s\\n' "$*" > "{self.stderr_file}"
case "$1" in
  backlinks) exit 7 ;;
esac
exit 0
""",
            encoding="utf-8",
        )
        self.script.chmod(0o755)

    def argv(self) -> list[str]:
        if not self.argv_log.exists():
            return []
        return [ln for ln in self.argv_log.read_text(encoding="utf-8").splitlines() if ln]

    def env_lines(self) -> list[str]:
        if not self.env_log.exists():
            return []
        return [ln for ln in self.env_log.read_text(encoding="utf-8").splitlines() if ln]


class GbrainChatRunBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gbrain-chat-run-")
        self.tmp = Path(self._tmp.name)
        self.lock_path = self.tmp / "locks" / "tasknotes.lock"
        self.fake = FakeGbrain(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(
        self, *gbrain_args: str, env_extra: dict | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run main() in a fresh interpreter with fakes injected via the
        module-level test parameters (no production executable override: the
        CLI entrypoint always uses the fixed private native binary
        /opt/josemar/libexec/gbrain-native, the fixed lock runner, and the
        fixed lock path."""
        boot = (
            "import sys\n"
            f"sys.path.insert(0, {str(ADAPTER.parent)!r})\n"
            "import gbrain_chat_run\n"
            "argv = ['--', *{args!r}]\n"
            "sys.exit(gbrain_chat_run.main(argv, gbrain_bin={bin!r}, "
            "runner={runner!r}, lock_path={lock!r}, "
            "interpreter=sys.executable))\n"
        ).format(
            args=list(gbrain_args),
            bin=str(self.fake.script),
            runner=str(RUNNER),
            lock=str(self.lock_path),
        )
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-c", boot],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=env,
            input="hello-stdin",
        )

    def test_usage_error_without_subcommand(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("subcommand is required", result.stderr)

    def test_public_timeout_knobs_removed_and_rejected(self) -> None:
        """The lock wait / runtime / kill-grace knobs are no longer public
        CLI options: passing them must be rejected (argparse unrecognized
        arguments) and gbrain must never run."""
        for knob in ("--timeout=0.5", "--lock-timeout=nan", "--kill-grace=inf",
                     "--timeout", "5"):
            with self.subTest(knob=knob):
                result = self._run(knob, "status")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(
                    self.fake.argv(), [], f"gbrain must not run for {knob}"
                )

    def test_child_exit_status_preserved(self) -> None:
        result = self._run("backlinks")
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_argv_passed_through_to_gbrain(self) -> None:
        result = self._run("get", "--slug", "foo/bar")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.argv(), ["get", "--slug", "foo/bar"])

    def test_stdin_stdout_stderr_preserved(self) -> None:
        result = self._run("search", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.stdin_file.read_text(encoding="utf-8"), "hello-stdin")
        self.assertEqual(
            self.fake.stdout_file.read_text(encoding="utf-8"),
            "fake stdout: search alpha\n",
        )
        self.assertEqual(
            self.fake.stderr_file.read_text(encoding="utf-8"),
            "fake stderr: search alpha\n",
        )

    def test_canonical_gbrain_env_set(self) -> None:
        result = self._run("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        env_lines = self.fake.env_lines()
        for expected in (
            "GBRAIN_SKIP_STARTUP_HOOKS=1",
            "GBRAIN_HOME=/opt/data",
            "GBRAIN_BRAIN_REPO=/opt/data/obsidian",
            "GBRAIN_SCHEMA_PACK=josemar",
        ):
            self.assertIn(expected, env_lines)
        # The lock runner passes the actual lock fd number to the chain so
        # downstream wrappers can validate it; no boolean marker is used.
        fd_lines = [ln for ln in env_lines if ln.startswith("TASKNOTES_LOCK_FD=")]
        self.assertEqual(len(fd_lines), 1, env_lines)
        self.assertTrue(fd_lines[0].split("=", 1)[1].isdigit(), env_lines)
        self.assertNotIn(
            "TASKNOTES_LOCK_HELD", "\n".join(env_lines), "no forgeable boolean marker"
        )

    def test_skip_startup_hooks_enforced_regardless_of_caller_env(self) -> None:
        """Issue #112: GBRAIN_SKIP_STARTUP_HOOKS must be assigned (never
        inherited), so a hostile caller environment cannot re-enable gbrain
        startup hooks through the private launcher chain."""
        for hostile in ("0", ""):
            with self.subTest(caller_value=hostile):
                if self.fake.env_log.exists():
                    self.fake.env_log.unlink()
                result = self._run(
                    "status", env_extra={"GBRAIN_SKIP_STARTUP_HOOKS": hostile}
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("GBRAIN_SKIP_STARTUP_HOOKS=1", self.fake.env_lines())

    def test_waits_for_fixed_lock_wait_then_runs(self) -> None:
        """With the public lock-wait knob gone, the adapter uses the fixed
        30s bound: a short-lived holder must be waited out, then gbrain
        runs. (The 75-busy path with a short timeout is covered in the
        lock-runner suite; through the adapter the wait is fixed at 30s.)"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, os, time\n"
                f"fd = os.open({str(self.lock_path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "time.sleep(0.4)\n",
            ]
        )
        try:
            result = self._run("status")
        finally:
            holder.wait(timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.argv(), ["status"])

    def test_runtime_timeout_is_fixed_constant(self) -> None:
        """The public timeout knobs are gone; the runner receives the fixed
        conservative constants (source contract for the exact values)."""
        src = ADAPTER.read_text(encoding="utf-8")
        self.assertIn("LOCK_WAIT_TIMEOUT = 30.0", src)
        self.assertIn("RUN_TIMEOUT = 300.0", src)
        self.assertIn("KILL_GRACE = 5.0", src)
        self.assertIn('"--lock-timeout", str(LOCK_WAIT_TIMEOUT)', src)
        self.assertIn('"--timeout", str(RUN_TIMEOUT)', src)
        self.assertIn('"--kill-grace", str(KILL_GRACE)', src)

    def test_admin_subcommands_rejected_before_lock(self) -> None:
        """Maintenance/admin commands are operator-only: the adapter must
        reject them with exit 2 before the lock is touched and without
        running gbrain."""
        for admin in ("init", "config", "sync", "extract", "embed", "migrate",
                      "schema", "reindex", "refresh", "import", "export",
                      "jobs", "chronicle-backfill"):
            with self.subTest(admin=admin):
                result = self._run(admin, "--yes")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("allowlist", result.stderr)
                self.assertEqual(
                    self.fake.argv(), [], f"gbrain must not run for {admin}"
                )

    def test_restore_allowed_publicly(self) -> None:
        result = self._run("restore", "--slug", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.argv(), ["restore", "--slug", "x"])

    def test_put_stdin_rejected_but_content_put_allowed(self) -> None:
        """`put --stdin` (bulk/scripted ingestion) is operator-only; agents
        write via `put <slug> --content ...` or capture. Both plain and
        =variants of --stdin must be rejected before the lock."""
        for args in (
            ("put", "--stdin"),
            ("put", "--slug", "x", "--stdin"),
            ("put", "--stdin", "--content", "hello"),
            ("put", "--slug", "x", "--stdin=true"),
        ):
            with self.subTest(args=args):
                result = self._run(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("allowlist", result.stderr)
                self.assertEqual(
                    self.fake.argv(), [], f"gbrain must not run for {args}"
                )
        ok = self._run("put", "people/x", "--content", "hello")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(self.fake.argv(), ["put", "people/x", "--content", "hello"])

    def test_capture_stdin_preserved(self) -> None:
        """capture keeps its stdin ingestion form (TaskNotes/agent file
        capture); only put --stdin is restricted."""
        result = self._run("capture", "--stdin", "--slug", "tasks/x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.argv(), ["capture", "--stdin", "--slug", "tasks/x"])

    def test_documented_chat_subcommands_accepted(self) -> None:
        """Every documented agent-facing subcommand must pass the allowlist
        (never the rc=2 rejection). backlinks exits 7 (status preserved);
        capture sleeps 60s so it is exercised in the runner suite, not here."""
        cases = [
            "search", "get", "put", "link", "backlinks", "status",
            "query", "day", "since", "last-seen", "on-this-day", "orient",
            "ontology", "timeline", "graph", "tags", "history", "delete",
            "revert", "doctor", "restore", "schema-status",
        ]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                result = self._run(cmd)
                self.assertNotEqual(
                    result.returncode, 2, f"{cmd} must pass the allowlist"
                )
                expected = 7 if cmd == "backlinks" else 0
                self.assertEqual(result.returncode, expected, result.stderr)
        self.assertTrue(self.fake.argv(), "gbrain must have run")

    def test_sources_list_argv_passed_through(self) -> None:
        result = self._run("sources", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.argv(), ["sources", "list", "--json"])

    def test_sources_mutations_rejected(self) -> None:
        """Only read-only `sources list` is agent-facing; every other sources
        subcommand (and a bare `sources`) is operator-only and rejected
        before the lock."""
        for args in (
            ("sources",),
            ("sources", "add", "--name", "x"),
            ("sources", "remove", "x"),
            ("sources", "harden"),
            ("sources", "set"),
        ):
            with self.subTest(args=args):
                result = self._run(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("allowlist", result.stderr)
                self.assertEqual(
                    self.fake.argv(), [], f"gbrain must not run for {args}"
                )

    def test_schema_pack_read_from_runtime_marker(self) -> None:
        """The schema pack must come from the runtime source of truth written
        by reindex, not from a hardcoded guess or the caller env."""
        marker = self.tmp / "active-schema-pack"
        marker.write_text("mypack\n", encoding="utf-8")
        boot = (
            "import sys\n"
            f"sys.path.insert(0, {str(ADAPTER.parent)!r})\n"
            "import gbrain_chat_run\n"
            "argv = ['--', 'status']\n"
            "sys.exit(gbrain_chat_run.main(argv, gbrain_bin={bin!r}, "
            "runner={runner!r}, lock_path={lock!r}, "
            "interpreter=sys.executable, schema_pack_file={marker!r}))\n"
        ).format(
            bin=str(self.fake.script),
            runner=str(RUNNER),
            lock=str(self.lock_path),
            marker=str(marker),
        )
        result = subprocess.run(
            [sys.executable, "-c", boot],
            capture_output=True, text=True, check=False, timeout=15,
            env=os.environ.copy(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GBRAIN_SCHEMA_PACK=mypack", self.fake.env_lines())

    def test_invalid_schema_pack_marker_fails_closed_to_default(self) -> None:
        marker = self.tmp / "active-schema-pack"
        marker.write_text("INVALID PACK!\n", encoding="utf-8")
        boot = (
            "import sys\n"
            f"sys.path.insert(0, {str(ADAPTER.parent)!r})\n"
            "import gbrain_chat_run\n"
            "argv = ['--', 'status']\n"
            "sys.exit(gbrain_chat_run.main(argv, gbrain_bin={bin!r}, "
            "runner={runner!r}, lock_path={lock!r}, "
            "interpreter=sys.executable, schema_pack_file={marker!r}))\n"
        ).format(
            bin=str(self.fake.script),
            runner=str(RUNNER),
            lock=str(self.lock_path),
            marker=str(marker),
        )
        result = subprocess.run(
            [sys.executable, "-c", boot],
            capture_output=True, text=True, check=False, timeout=15,
            env=os.environ.copy(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GBRAIN_SCHEMA_PACK=josemar", self.fake.env_lines())

    def test_no_public_timeout_knobs(self) -> None:
        """The adapter must expose NO timeout CLI knobs (callers cannot
        weaken the bounds) and must never pass --nonblocking; the runner
        receives the fixed conservative constants."""
        src = ADAPTER.read_text(encoding="utf-8")
        self.assertIn("LOCK_WAIT_TIMEOUT = 30.0", src)
        self.assertIn("RUN_TIMEOUT = 300.0", src)
        self.assertIn("KILL_GRACE = 5.0", src)
        self.assertNotIn("parser.add_argument(", src.split("gbrain_args")[0])
        self.assertNotIn("--nonblocking", src)
        self.assertNotIn("args.lock_timeout", src)
        self.assertNotIn("args.timeout", src)
        self.assertNotIn("args.kill_grace", src)


class GbrainChatRunRootDropContractTests(unittest.TestCase):
    """The adapter must drop root to hermes BEFORE touching the shared lock."""

    def setUp(self) -> None:
        self.src = ADAPTER.read_text(encoding="utf-8")

    def test_fixed_binary_is_pinned_path(self) -> None:
        self.assertIn('GBRAIN_BIN = "/opt/josemar/libexec/gbrain-native"', self.src)
        self.assertNotIn('GBRAIN_BIN = "/usr/local/bin/gbrain"', self.src)

    def test_no_production_binary_override(self) -> None:
        """The adapter must never take the binary path from the environment or
        the command line; the only seam is the main() keyword parameter used
        by module-import tests, which the CLI entrypoint never exercises."""
        self.assertIn("gbrain_bin: str = GBRAIN_BIN", self.src)
        self.assertNotIn('environ.get("GBRAIN_BIN"', self.src)
        self.assertNotIn('GBRAIN_BIN")', self.src)

    def test_no_lock_runner_or_lock_path_override(self) -> None:
        """The lock runner and the lock path are fixed production constants,
        reachable only through the main() keyword test seams. There is no
        --lock-path flag and no TASKNOTES_LOCK_* environment override."""
        self.assertIn("runner: str = RUNNER", self.src)
        self.assertIn("lock_path: str = LOCK_PATH", self.src)
        self.assertNotIn(
            'parser.add_argument(\n        "--lock-path"', self.src
        )
        self.assertNotIn("TASKNOTES_LOCK_RUNNER", self.src)
        self.assertNotIn("TASKNOTES_LOCK_PATH", self.src)
        self.assertNotIn("TASKNOTES_LOCK_HELD", self.src)

    def test_shebang_uses_fixed_isolated_interpreter(self) -> None:
        """The adapter shebang must not resolve python through PATH (env):
        it must use the fixed image interpreter in isolated mode so a
        hostile PYTHONPATH/sitecustomize cannot run code before the lock."""
        first = self.src.splitlines()[0]
        self.assertEqual(first, "#!/opt/hermes/.venv/bin/python3 -I")
        self.assertNotIn("/usr/bin/env", first)

    def test_runner_invoked_with_fixed_isolated_interpreter(self) -> None:
        """The runner must be exec'd with the fixed image interpreter in
        isolated mode (-I), never with sys.executable (which would follow a
        caller-chosen interpreter)."""
        self.assertIn('PYTHON_BIN = "/opt/hermes/.venv/bin/python3"', self.src)
        self.assertIn("interpreter: str = PYTHON_BIN", self.src)
        self.assertIn('"-I",', self.src)
        self.assertNotIn("sys.executable", self.src)

    def test_cli_entrypoint_uses_only_fixed_paths(self) -> None:
        """The CLI entrypoint calls main() with no arguments, so it always
        runs the pinned binary, the fixed lock runner, and the fixed lock
        path."""
        self.assertIn("\n    raise SystemExit(main())\n", self.src)
        self.assertIn('RUNNER = "/opt/josemar/scripts/tasknotes_lock_run.py"', self.src)
        self.assertIn('LOCK_PATH = "/opt/data/.locks/tasknotes.lock"', self.src)
        self.assertIn('"--lock-path", str(lock_path)', self.src)

    def test_canonical_gbrain_env_assigned_not_inherited(self) -> None:
        """GBRAIN_HOME / GBRAIN_BRAIN_REPO must be assigned explicitly (never
        setdefault) so a caller-supplied value cannot redirect the pinned
        binary at a different brain repo; GBRAIN_SCHEMA_PACK must come from
        the runtime source of truth (never the caller env or a guess)."""
        self.assertIn('os.environ["GBRAIN_HOME"] = "/opt/data"', self.src)
        self.assertIn('os.environ["GBRAIN_BRAIN_REPO"] = "/opt/data/obsidian"', self.src)
        self.assertIn(
            'os.environ["GBRAIN_SCHEMA_PACK"] = _active_schema_pack(schema_pack_file)',
            self.src,
        )
        self.assertNotIn('setdefault("GBRAIN_', self.src)
        self.assertIn('SCHEMA_PACK_FILE = "/opt/data/.gbrain/active-schema-pack"', self.src)
        self.assertIn("schema_pack_file: str = SCHEMA_PACK_FILE", self.src)

    def test_skip_startup_hooks_assigned_not_inherited(self) -> None:
        """Issue #112: GBRAIN_SKIP_STARTUP_HOOKS must be assigned explicitly
        (never setdefault), so a caller-supplied value cannot re-enable
        gbrain startup hooks through the private launcher."""
        self.assertIn('os.environ["GBRAIN_SKIP_STARTUP_HOOKS"] = "1"', self.src)
        self.assertNotIn('setdefault("GBRAIN_SKIP_STARTUP_HOOKS"', self.src)

    def test_explicit_chat_subcommand_allowlist(self) -> None:
        """The adapter must define an explicit allowlist of the documented
        agent-facing subcommands and reject everything else before the lock.
        jobs/chronicle-backfill are operator-only; restore is public."""
        self.assertIn("CHAT_SUBCOMMANDS = frozenset(", self.src)
        for cmd in ("search", "get", "capture", "put", "link", "backlinks",
                    "status", "schema-status", "sources", "restore"):
            self.assertIn(f'"{cmd}"', self.src)
        for cmd in ("jobs", "chronicle-backfill"):
            self.assertNotIn(f'"{cmd}"', self.src.split("CHAT_SUBSUBCOMMANDS")[0])
        self.assertIn("_chat_subcommand_allowed", self.src)
        self.assertNotIn("os.environ.get", self.src.split("_parser")[0])

    def test_command_inventory_export_matches_allowlist(self) -> None:
        """The mechanical inventory export must match the runtime
        allowlist exactly (single source of truth for policy tests)."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("gbrain_chat_run_inv", ADAPTER)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        self.assertEqual(
            sorted(mod.CHAT_COMMAND_INVENTORY["subcommands"]),
            sorted(mod.CHAT_SUBCOMMANDS),
        )
        self.assertEqual(
            mod.CHAT_COMMAND_INVENTORY["subsubcommands"]["sources"],
            ["list"],
        )
        self.assertEqual(
            mod.CHAT_COMMAND_INVENTORY["rejected_arguments"]["put"],
            ["--stdin"],
        )
        for op in mod.CHAT_COMMAND_INVENTORY["operator_only"]:
            self.assertNotIn(op, mod.CHAT_SUBCOMMANDS)

    def test_sources_allowlist_is_read_only_list_only(self) -> None:
        """`sources` must be validated argument-aware: only the `list`
        sub-subcommand is agent-facing; mutations stay operator-only."""
        self.assertIn('"sources": frozenset({"list"})', self.src)
        self.assertIn("CHAT_SUBSUBCOMMANDS", self.src)
        self.assertIn('gbrain_args[1] not in allowed_subs', self.src)
        self.assertNotIn('"add"', self.src.split("CHAT_SUBSUBCOMMANDS")[1].split("}")[0])
        self.assertNotIn('"remove"', self.src.split("CHAT_SUBSUBCOMMANDS")[1].split("}")[0])
        self.assertNotIn('"harden"', self.src.split("CHAT_SUBSUBCOMMANDS")[1].split("}")[0])

    def test_no_dropped_privs_env_sentinel(self) -> None:
        """The privilege drop must be unconditional: no environment flag can
        claim the drop already happened and skip it."""
        self.assertNotIn("DROPPED_PRIVS", self.src)

    def test_drops_to_hermes_before_lock_runner_exec(self) -> None:
        self.assertIn('RUNTIME_USER = "hermes"', self.src)
        self.assertIn("pwd.getpwnam(RUNTIME_USER)", self.src)
        self.assertIn("os.initgroups", self.src)
        self.assertIn("os.setgid", self.src)
        self.assertIn("os.setuid", self.src)
        drop_pos = self.src.find("def _drop_root")
        exec_pos = self.src.find("os.execv")
        self.assertGreater(drop_pos, -1, "privilege drop function must exist")
        self.assertGreater(exec_pos, -1, "lock runner exec must exist")
        self.assertLess(
            drop_pos,
            exec_pos,
            "privilege drop must precede the lock runner exec",
        )

    def test_adapter_never_opens_the_lock_itself(self) -> None:
        """All lock handling lives in tasknotes_lock_run.py; the adapter must
        not open or flock the lock file on its own."""
        self.assertNotIn("fcntl", self.src)
        self.assertNotIn("os.open", self.src)
        self.assertIn("tasknotes_lock_run.py", self.src)


if __name__ == "__main__":
    unittest.main()
