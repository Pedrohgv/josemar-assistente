"""Behavior + contract tests for the W3 refresh-only Daily-links
reconciliation integration (issue #139 revision 3).

Covered layers:

  1. Wrapper order and failure discipline (fixture copy of
     scripts/josemar-gbrain + scripted fake native gbrain + a recording
     stub reconciliation CLI sharing one unified call log): refresh must
     run prepare/apply (``reconcile``) BEFORE the native sync/extract and
     ``finalize`` strictly AFTER a successful sync; a reconcile or sync
     failure must skip finalize and fail refresh without advancing the
     cursor.
  2. Real CLI end-to-end (fixture-patched fixed constants, real temporary
     Git vault, real W2 core): a full refresh with the flags enabled
     projects the scheduled task link and advances the cursor only via the
     post-sync finalize; with either flag explicitly false the CLI is
     completely inert (garbage cursor/pending state is neither read nor
     written) and refresh still succeeds; missing flags resolve to the
     enabled default; a hostile flag value fails refresh before any
     gbrain call.
  3. Static refresh-flow contract: the fixed CLI constant, the isolated
     interpreter, and the required order inside do_refresh.

The wrapper hardcodes its production paths (binary, lock, interpreter,
lock runner, reconciliation CLI), so tests run fixture copies with those
literals substituted for local equivalents — the same pattern as the other
josemar-gbrain suites. A real flock on a temp lock file provides the
locking substrate (no Docker, no gbrain needed).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "josemar-gbrain"
RUNNER = REPO_ROOT / "scripts" / "tasknotes_lock_run.py"
RECONCILE_CLI = REPO_ROOT / "scripts" / "tasknotes_daily_links_reconcile.py"
SCRIPTS_DIR = REPO_ROOT / "scripts"

_D1 = "2026-09-01"

# Exact literals the wrapper fixture must substitute (production paths).
_WRAPPER_LITERALS = {
    "GBRAIN_BIN": "/opt/josemar/libexec/gbrain-native",
    "GBRAIN_NATIVE_CWD": "/opt/gbrain",
    "GBRAIN_TASKNOTES_LOCK": "/opt/data/.locks/tasknotes.lock",
    "GBRAIN_HOME": "/opt/data",
    "GBRAIN_BRAIN_REPO": "/opt/data/obsidian",
    "GBRAIN_SCHEMA_SOURCE_ROOT": "/opt/data/.gbrain/schema-packs",
    "PYTHON_BIN": "/opt/hermes/.venv/bin/python3",
    "TASKNOTES_LOCK_RUNNER": "/opt/josemar/scripts/tasknotes_lock_run.py",
    "TASKNOTES_RECONCILE_CLI": (
        "/opt/josemar/scripts/tasknotes_daily_links_reconcile.py"
    ),
}

# Exact literals the reconciliation CLI fixture must substitute.
_CLI_LITERALS = {
    "vault": 'VAULT = Path("/opt/data/obsidian")',
    "lock": 'LOCK_PATH = Path("/opt/data/.locks/tasknotes.lock")',
    "cursor": "RECONCILE_CURSOR_PATH = DAILY_LINKS_RECONCILE_CURSOR_PATH",
    "pending": "RECONCILE_PENDING_PATH = DAILY_LINKS_RECONCILE_PENDING_PATH",
    "sys_path": (
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))"
    ),
}


def _extract_function(src: str, name: str) -> str:
    header = re.compile(rf"^{name}\(\)\s*\{{", re.MULTILINE)
    start = header.search(src)
    assert start is not None, f"Could not find function {name}"
    body_start = start.end()
    close = re.compile(r"^}$", re.MULTILINE)
    match = close.search(src, body_start)
    assert match is not None, f"Could not find end of function {name}"
    return src[body_start:match.start()]


def patched_wrapper(
    tmp: Path,
    fake_gbrain: Path,
    lock_path: Path,
    reconcile_cli: Path,
) -> Path:
    """Fixture copy of the production wrapper with the fixed production
    literals substituted for local equivalents (no production env seam)."""
    src = WRAPPER.read_text(encoding="utf-8")
    for name, literal in _WRAPPER_LITERALS.items():
        needle = f'{name}="{literal}"'
        assert needle in src, f"wrapper literal drifted: {needle}"
    patched = (
        src.replace(
            f'GBRAIN_BIN="{_WRAPPER_LITERALS["GBRAIN_BIN"]}"',
            f'GBRAIN_BIN="{fake_gbrain}"',
        )
        .replace(
            f'GBRAIN_NATIVE_CWD="{_WRAPPER_LITERALS["GBRAIN_NATIVE_CWD"]}"',
            f'GBRAIN_NATIVE_CWD="{tmp / "native-cwd"}"',
        )
        .replace(
            'GBRAIN_TASKNOTES_LOCK='
            f'"{_WRAPPER_LITERALS["GBRAIN_TASKNOTES_LOCK"]}"',
            f'GBRAIN_TASKNOTES_LOCK="{lock_path}"',
        )
        .replace(
            f'GBRAIN_HOME="{_WRAPPER_LITERALS["GBRAIN_HOME"]}"',
            f'GBRAIN_HOME="{tmp / "state"}"',
        )
        .replace(
            'GBRAIN_BRAIN_REPO='
            f'"{_WRAPPER_LITERALS["GBRAIN_BRAIN_REPO"]}"',
            f'GBRAIN_BRAIN_REPO="{tmp / "brain"}"',
        )
        .replace(
            'GBRAIN_SCHEMA_SOURCE_ROOT='
            f'"{_WRAPPER_LITERALS["GBRAIN_SCHEMA_SOURCE_ROOT"]}"',
            f'GBRAIN_SCHEMA_SOURCE_ROOT='
            f'"{tmp / "state" / ".gbrain" / "schema-packs"}"',
        )
        .replace(
            f'PYTHON_BIN="{_WRAPPER_LITERALS["PYTHON_BIN"]}"',
            f'PYTHON_BIN="{sys.executable}"',
        )
        .replace(
            'TASKNOTES_LOCK_RUNNER='
            f'"{_WRAPPER_LITERALS["TASKNOTES_LOCK_RUNNER"]}"',
            f'TASKNOTES_LOCK_RUNNER="{RUNNER}"',
        )
        .replace(
            'TASKNOTES_RECONCILE_CLI='
            f'"{_WRAPPER_LITERALS["TASKNOTES_RECONCILE_CLI"]}"',
            f'TASKNOTES_RECONCILE_CLI="{reconcile_cli}"',
        )
    )
    script = tmp / "josemar-gbrain"
    script.write_text(patched, encoding="utf-8")
    script.chmod(0o755)
    return script


class UnifiedFakeGbrain:
    """Fake native gbrain logging every invocation to the shared unified
    call log (`gbrain <argv>` lines). Optional fail_patterns make matching
    invocations exit nonzero so wrapper failure paths can be exercised."""

    def __init__(self, tmp: Path, fail_patterns: list[str] | None = None):
        self.log = tmp / "unified-calls.log"
        self.script = tmp / "gbrain"
        fail_guards = "\n".join(
            f'case "$*" in\n  *{pat}*) echo "scripted-failure: {pat}" >&2; exit 5 ;;\nesac'
            for pat in (fail_patterns or [])
        )
        if fail_guards:
            fail_guards += "\n"
        self.script.write_text(
            f"""#!/bin/sh
echo "gbrain $*" >> "{self.log}"
{fail_guards}case "$1" in
  sync) echo '{{"status":"ok"}}' ;;
  *) echo '{{"status":"ok"}}' ;;
esac
""",
            encoding="utf-8",
        )
        self.script.chmod(0o755)

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [
            ln
            for ln in self.log.read_text(encoding="utf-8").splitlines()
            if ln.startswith("gbrain ")
        ]


def stub_reconcile_cli(
    tmp: Path,
    log_path: Path,
    *,
    reconcile_rc: int = 0,
    finalize_rc: int = 0,
) -> Path:
    """Recording stub for the fixed reconciliation CLI: appends
    `cli <verb>` to the unified log and exits with the configured code.
    Must be a Python script because the wrapper invokes it through the
    fixed isolated interpreter."""
    script = tmp / "reconcile-cli-stub"
    script.write_text(
        f"""#!{sys.executable}
import sys
verb = sys.argv[1] if len(sys.argv) > 1 else ""
with open({str(log_path)!r}, "a", encoding="utf-8") as fh:
    fh.write("cli " + verb + "\\n")
sys.exit({{"reconcile": {reconcile_rc}, "finalize": {finalize_rc}}}.get(verb, 1))
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def patched_reconcile_cli(
    tmp: Path,
    *,
    vault: Path,
    lock_path: Path,
    cursor_path: Path,
    pending_path: Path,
    extra_patch: tuple[str, str] | None = None,
) -> Path:
    """Fixture copy of the real reconciliation CLI with the fixed
    production constants substituted for local equivalents."""
    src = RECONCILE_CLI.read_text(encoding="utf-8")
    for needle in _CLI_LITERALS.values():
        assert needle in src, f"CLI literal drifted: {needle}"
    patched = (
        src.replace(
            _CLI_LITERALS["sys_path"],
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})",
        )
        .replace(_CLI_LITERALS["vault"], f"VAULT = Path({str(vault)!r})")
        .replace(_CLI_LITERALS["lock"], f"LOCK_PATH = Path({str(lock_path)!r})")
        .replace(
            _CLI_LITERALS["cursor"],
            f"RECONCILE_CURSOR_PATH = Path({str(cursor_path)!r})",
        )
        .replace(
            _CLI_LITERALS["pending"],
            f"RECONCILE_PENDING_PATH = Path({str(pending_path)!r})",
        )
    )
    if extra_patch is not None:
        needle, replacement = extra_patch
        assert needle in patched, "CLI extra patch needle not found"
        patched = patched.replace(needle, replacement)
    script = tmp / "tasknotes_daily_links_reconcile"
    script.write_text(patched, encoding="utf-8")
    script.chmod(0o755)
    return script


# ---------------------------------------------------------------------------
# Minimal real TaskNotes vault fixture (same shape as the W2 lifecycle
# fixtures in tests/tasknotes_mcp/test_core.py, kept local and compact).
# ---------------------------------------------------------------------------

MANIFEST = {
    "id": "tasknotes",
    "name": "TaskNotes",
    "version": "4.11.1",
}

PROFILE_DATA = {
    "tasksFolder": "tasks",
    "taskTag": "task",
    "taskIdentificationMethod": "tag",
    "defaultTaskStatus": "open",
    "defaultTaskPriority": "normal",
    "fieldMapping": {
        "title": "title",
        "status": "status",
        "priority": "priority",
        "due": "due",
        "scheduled": "scheduled",
        "projects": "projects",
        "completedDate": "completedDate",
        "archiveTag": "archived",
    },
    "customStatuses": [
        {"id": "open", "value": "open", "isCompleted": False},
        {"id": "done", "value": "done", "isCompleted": True},
    ],
    "customPriorities": [
        {"id": "normal", "value": "normal"},
    ],
}


def _write_profile(vault: Path) -> None:
    plugin_dir = vault / ".obsidian" / "plugins" / "tasknotes"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "manifest.json").write_text(
        json.dumps(MANIFEST), encoding="utf-8"
    )
    (plugin_dir / "data.json").write_text(
        json.dumps(PROFILE_DATA), encoding="utf-8"
    )


def _write_task(vault: Path, slug: str, *, scheduled: str | None) -> None:
    path = vault / "tasks" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "type: note",
        f"title: {slug}",
        "status: open",
        "priority: normal",
        "tags:",
        "  - task",
    ]
    if scheduled is not None:
        lines.append(f"scheduled: '{scheduled}'")
    lines += ["---", f"body {slug}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _git(vault: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(vault), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_all(vault: Path, message: str) -> str:
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", message)
    return _git(vault, "rev-parse", "HEAD")


def make_vault(tmpdir: Path, name: str) -> Path:
    """Build a minimal reconcile-ready vault: TaskNotes profile, Daily
    Notes config (folder ``journal``), one committed scheduled task."""
    vault = tmpdir / name
    vault.mkdir()
    _write_profile(vault)
    (vault / "tasks").mkdir()
    (vault / "journal").mkdir()
    (vault / ".obsidian" / "daily-notes.json").write_text(
        json.dumps({"folder": "journal"}), encoding="utf-8"
    )
    _git(vault, "init", "-q")
    # Deterministic repo-local identity: CI runners (GitHub Actions)
    # have no global git identity, so the fixture's initial commit
    # would otherwise exit 128. Repo-local config only — the global
    # (and system) git config is never touched.
    _git(vault, "config", "user.name", "tasknotes-tests")
    _git(vault, "config", "user.email", "tasknotes-tests@local")
    _write_task(vault, "t1", scheduled=_D1)
    _commit_all(vault, "init")
    return vault


class _RefreshFixtureMixin:
    """Shared fixture plumbing for the refresh behavior suites."""

    # Set by each suite's _setup helper.
    fake: UnifiedFakeGbrain

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix="gbrain-refresh-daily-links-"
        )
        self.tmp = Path(self._tmp.name)
        self.lock_path = self.tmp / "locks" / "tasknotes.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch()
        self.state_dir = self.tmp / "state" / ".gbrain"
        self.cursor_path = (
            self.state_dir / "josemar-tasknotes-daily-links-reconcile.json"
        )
        self.pending_path = (
            self.state_dir
            / "josemar-tasknotes-daily-links-reconcile-pending.json"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def env(self, **extra) -> dict:
        env = os.environ.copy()
        env.update({"HOME": str(self.tmp)})
        # Deterministic feature flags: every test sets them explicitly.
        env.pop("TASKNOTES_DAILY_LINKS_ENABLED", None)
        env.pop("TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED", None)
        env.pop("TASKNOTES_LOCK_FD", None)
        env.update(extra)
        return env

    def unified_lines(self) -> list[str]:
        if not self.fake.log.exists():
            return []
        return [
            ln
            for ln in self.fake.log.read_text(encoding="utf-8").splitlines()
            if ln
        ]

    def _run_wrapper(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            **kwargs,
        )

    def _hold_lock(self) -> int:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd


class RefreshReconcileOrderBehaviorTests(_RefreshFixtureMixin, unittest.TestCase):
    """Wrapper order/failure discipline with a recording stub CLI."""

    def _setup(self, *, reconcile_rc: int = 0, finalize_rc: int = 0,
               gbrain_fail_patterns: list[str] | None = None) -> Path:
        self.fake = UnifiedFakeGbrain(self.tmp, gbrain_fail_patterns)
        cli = stub_reconcile_cli(
            self.tmp,
            self.fake.log,
            reconcile_rc=reconcile_rc,
            finalize_rc=finalize_rc,
        )
        self.wrapper = patched_wrapper(self.tmp, self.fake.script, self.lock_path, cli)
        return self.wrapper

    def test_refresh_runs_reconcile_before_sync_and_finalize_after_sync(self) -> None:
        """Required order: reconcile -> sync -> extract --stale ->
        extract links -> finalize, each exactly once, and refresh succeeds."""
        self._setup()
        result = self._run_wrapper(
            [str(self.wrapper), "refresh"], env=self.env()
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        lines = self.unified_lines()
        self.assertEqual(lines.count("cli reconcile"), 1)
        self.assertEqual(lines.count("cli finalize"), 1)
        reconcile = lines.index("cli reconcile")
        finalize = lines.index("cli finalize")
        syncs = [i for i, ln in enumerate(lines) if ln.startswith("gbrain sync")]
        extracts = [
            i for i, ln in enumerate(lines)
            if ln.startswith("gbrain extract --stale")
        ]
        links = [
            i for i, ln in enumerate(lines)
            if ln.startswith("gbrain extract links")
        ]
        self.assertEqual(len(syncs), 1)
        self.assertEqual(len(extracts), 1)
        self.assertEqual(len(links), 1)
        self.assertLess(reconcile, syncs[0],
                        "prepare/apply must run before the native sync")
        self.assertLess(syncs[0], extracts[0])
        self.assertLess(extracts[0], links[0])
        self.assertLess(links[0], finalize,
                        "finalize must run only after the sync/extract chain")

    def test_refresh_reconcile_failure_blocks_sync_and_finalize(self) -> None:
        """A failing reconcile cycle must fail refresh before any gbrain
        call and never reach finalize (cursor/pending stay replayable)."""
        self._setup(reconcile_rc=1)
        result = self._run_wrapper(
            [str(self.wrapper), "refresh"], env=self.env()
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("daily_links_reconcile_failed", result.stdout)
        self.assertNotIn('"success": true', result.stdout)
        self.assertEqual(
            [ln for ln in self.unified_lines() if ln.startswith("gbrain ")],
            [],
            "no native gbrain call may run after a reconcile failure",
        )
        self.assertNotIn("cli finalize", self.unified_lines())

    def test_refresh_sync_failure_skips_finalize(self) -> None:
        """When the native sync fails, refresh fails and finalize must NOT
        run: the pending record stays replayable and the cursor is not
        advanced."""
        self._setup(gbrain_fail_patterns=["sync --no-embed"])
        result = self._run_wrapper(
            [str(self.wrapper), "refresh"], env=self.env()
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_sync_failed", result.stdout)
        self.assertNotIn('"success": true', result.stdout)
        lines = self.unified_lines()
        self.assertIn("cli reconcile", lines)
        self.assertNotIn("cli finalize", lines,
                         "finalize must never run after a failed sync")

    def test_refresh_finalize_failure_fails_refresh_after_sync(self) -> None:
        """A failing finalize must fail refresh even though the sync
        succeeded (the cursor must not silently lag the applied state)."""
        self._setup(finalize_rc=1)
        result = self._run_wrapper(
            [str(self.wrapper), "refresh"], env=self.env()
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("daily_links_finalize_failed", result.stdout)
        self.assertNotIn('"success": true', result.stdout)
        lines = self.unified_lines()
        self.assertIn("cli reconcile", lines)
        self.assertTrue(
            any(ln.startswith("gbrain extract links") for ln in lines),
            "the sync chain ran before the finalize failure",
        )


class RefreshRealCliEndToEndTests(_RefreshFixtureMixin, unittest.TestCase):
    """Full refresh chain against the REAL reconciliation CLI (fixture
    constants), a real Git vault, and the fake native gbrain."""

    def _setup_real(self, *, extra_cli_patch: tuple[str, str] | None = None) -> Path:
        self.fake = UnifiedFakeGbrain(self.tmp)
        cli = patched_reconcile_cli(
            self.tmp,
            vault=self.tmp / "vault",
            lock_path=self.lock_path,
            cursor_path=self.cursor_path,
            pending_path=self.pending_path,
            extra_patch=extra_cli_patch,
        )
        self.wrapper = patched_wrapper(self.tmp, self.fake.script, self.lock_path, cli)
        return self.wrapper

    def test_enabled_refresh_end_to_end_projects_and_finalizes_cursor(self) -> None:
        """Flag enabled + inherited lock fd: refresh projects the scheduled
        task link (prepare/apply + targeted commit) and advances the cursor
        only via the post-sync finalize (pending cleared)."""
        vault = make_vault(self.tmp, "vault")
        head_before = _git(vault, "rev-parse", "HEAD")
        self._setup_real()
        fd = self._hold_lock()
        try:
            result = self._run_wrapper(
                [str(self.wrapper), "refresh"],
                env=self.env(
                    TASKNOTES_DAILY_LINKS_ENABLED="true",
                    TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED="true",
                    TASKNOTES_LOCK_FD=str(fd),
                ),
                pass_fds=[fd],
            )
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        note = (vault / "journal" / f"{_D1}.md").read_text(encoding="utf-8")
        self.assertIn("- [[t1]]", note)
        head_after = _git(vault, "rev-parse", "HEAD")
        self.assertNotEqual(head_before, head_after,
                            "the reconcile cycle must create a targeted commit")
        self.assertFalse(self.pending_path.exists(),
                         "finalize must clear the pending sibling")
        self.assertTrue(self.cursor_path.exists(), "finalize must write the cursor")
        cursor = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["reconciled_head"], head_after)
        self.assertEqual(cursor["daily_folder"], "journal")
        # The task file itself is never written by reconciliation.
        self.assertIn("scheduled: '2026-09-01'",
                      (vault / "tasks" / "t1.md").read_text(encoding="utf-8"))

    def test_enabled_refresh_full_chain_self_acquired_lock(self) -> None:
        """Manual run without an inherited fd: the wrapper self-acquires the
        lock through the real lock-runner chain, and the real CLI still
        observes the inherited exclusive flock (fd 9) and completes."""
        make_vault(self.tmp, "vault")
        self._setup_real()
        result = self._run_wrapper(
            [str(self.wrapper), "refresh"],
            env=self.env(
                TASKNOTES_DAILY_LINKS_ENABLED="true",
                TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED="true",
            ),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        self.assertTrue(self.cursor_path.exists(), "cursor advanced")
        self.assertFalse(self.pending_path.exists(), "pending cleared")

    def test_disabled_refresh_is_completely_inert_with_garbage_state(self) -> None:
        """Flags explicitly false: the whole refresh succeeds exactly as
        before the feature existed, and the real CLI never reads or writes
        the cursor/pending state (garbage bytes stay garbage) nor touches
        the (absent) vault."""
        garbage_cursor = b"{not-a-cursor"
        garbage_pending = b"{not-a-pending"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_bytes(garbage_cursor)
        self.pending_path.write_bytes(garbage_pending)
        self._setup_real()
        result = self._run_wrapper(
            [str(self.wrapper), "refresh"],
            env=self.env(TASKNOTES_DAILY_LINKS_ENABLED="false",
                         TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED="false"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        self.assertEqual(self.cursor_path.read_bytes(), garbage_cursor)
        self.assertEqual(self.pending_path.read_bytes(), garbage_pending)
        self.assertFalse((self.tmp / "vault").exists(),
                         "an absent vault must stay untouched when disabled")

    def test_missing_flags_refresh_defaults_enabled_end_to_end(self) -> None:
        """Both flags missing resolve to the enabled default: a plain
        refresh (no feature env at all) runs the reconciliation lifecycle
        against the real vault and advances the cursor via the post-sync
        finalize."""
        vault = make_vault(self.tmp, "vault")
        head_before = _git(vault, "rev-parse", "HEAD")
        self._setup_real()
        result = self._run_wrapper(
            [str(self.wrapper), "refresh"], env=self.env()
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        note = (vault / "journal" / f"{_D1}.md").read_text(encoding="utf-8")
        self.assertIn("- [[t1]]", note)
        head_after = _git(vault, "rev-parse", "HEAD")
        self.assertNotEqual(head_before, head_after,
                            "the default-enabled reconcile must commit")
        self.assertFalse(self.pending_path.exists())
        self.assertTrue(self.cursor_path.exists())

    def test_invalid_flag_fails_refresh_before_any_gbrain_call(self) -> None:
        """A hostile master flag value is a strict validation failure: the
        CLI (and therefore refresh) fails before any native gbrain call."""
        make_vault(self.tmp, "vault")
        self._setup_real()
        result = self._run_wrapper(
            [str(self.wrapper), "refresh"],
            env=self.env(TASKNOTES_DAILY_LINKS_ENABLED="sometimes"),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("daily_links_reconcile_failed", result.stdout)
        self.assertIn("daily_links_flag_invalid", result.stdout)
        self.assertNotIn('"success": true', result.stdout)
        self.assertEqual(
            [ln for ln in self.unified_lines() if ln.startswith("gbrain ")],
            [],
            "flag validation must precede the native sync",
        )
        self.assertFalse(self.cursor_path.exists(), "no cursor write on flag failure")


class RefreshReconcileStaticContractTests(unittest.TestCase):
    """Source contract: the refresh flow wires the fixed CLI in the
    required order with structured failure envelopes."""

    def setUp(self) -> None:
        self.src = WRAPPER.read_text(encoding="utf-8")
        self.body = _extract_function(self.src, "do_refresh")

    def test_fixed_cli_constant_present_and_never_env_overridable(self) -> None:
        self.assertIn(
            'TASKNOTES_RECONCILE_CLI='
            '"/opt/josemar/scripts/tasknotes_daily_links_reconcile.py"',
            self.src,
        )

    def test_refresh_invokes_cli_with_isolated_interpreter(self) -> None:
        self.assertIn(
            '"$PYTHON_BIN" -I "$TASKNOTES_RECONCILE_CLI" reconcile 2>&1',
            self.body,
        )
        self.assertIn(
            '"$PYTHON_BIN" -I "$TASKNOTES_RECONCILE_CLI" finalize 2>&1',
            self.body,
        )

    def test_refresh_order_reconcile_before_sync_finalize_after(self) -> None:
        reconcile_pos = self.body.find(
            '"$TASKNOTES_RECONCILE_CLI" reconcile'
        )
        sync_pos = self.body.find("run_sync_extract_links")
        finalize_pos = self.body.find(
            '"$TASKNOTES_RECONCILE_CLI" finalize'
        )
        self.assertGreater(reconcile_pos, -1)
        self.assertGreater(sync_pos, -1)
        self.assertGreater(finalize_pos, -1)
        self.assertLess(reconcile_pos, sync_pos,
                        "prepare/apply must precede the native sync")
        self.assertLess(sync_pos, finalize_pos,
                        "finalize must follow the native sync")

    def test_refresh_reconcile_failure_is_structured_and_terminal(self) -> None:
        self.assertIn("daily_links_reconcile_failed", self.body)
        self.assertIn('"success": False', self.body)
        fail_pos = self.body.find("daily_links_reconcile_failed")
        sync_pos = self.body.find("run_sync_extract_links")
        self.assertLess(fail_pos, sync_pos,
                        "the reconcile failure path must precede the sync")
        self.assertIn("return 1", self.body)

    def test_refresh_finalize_failure_is_structured_and_terminal(self) -> None:
        self.assertIn("daily_links_finalize_failed", self.body)
        fail_pos = self.body.find("daily_links_finalize_failed")
        success_pos = self.body.find('"success": true, "action": "refresh"')
        self.assertGreater(success_pos, -1)
        self.assertLess(fail_pos, success_pos,
                        "the finalize failure path must precede the success envelope")
        self.assertIn("return 1", self.body)

    def test_refresh_keeps_existing_behavior(self) -> None:
        """The pre-existing refresh contract is preserved: shared lock,
        incremental sync via the shared helper, no init/schema/embed work."""
        self.assertIn("acquire_tasknotes_lock refresh", self.body)
        self.assertIn("run_sync_extract_links", self.body)
        self.assertNotIn("run_sync_extract_links full", self.body)
        self.assertNotIn("init --pglite", self.body)
        self.assertNotIn("schema sync --apply", self.body)
        self.assertNotIn("embed --stale", self.body)
        self.assertNotIn("install_source_pack", self.body)

    def test_refresh_never_nests_public_gbrain_or_new_locks(self) -> None:
        """The reconciliation steps must not go through the public gbrain
        adapter or acquire any lock inside do_refresh."""
        self.assertNotIn("gbrain-chat-run", self.body)
        self.assertNotIn('"$GBRAIN_BIN" ', self.body.split("run_sync_extract_links")[0])
        lock_invocations = [
            line for line in self.body.splitlines()
            if "flock" in line and "acquire_tasknotes_lock" not in line
        ]
        self.assertEqual(
            lock_invocations, [],
            "do_refresh must not acquire locks beyond the shared entry",
        )


class MakeVaultIdentityTests(unittest.TestCase):
    """CI-portability regression (fast-tests run 33278429405): the
    ``make_vault`` fixture must commit with a deterministic repo-local
    identity so environments without a global git identity (GitHub
    Actions runners) do not fail with ``git commit`` exit 128. The
    global/system git config must remain untouched."""

    def test_make_vault_commits_without_global_git_identity(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="gbrain-refresh-vault-identity-"
        ) as raw:
            tmp = Path(raw)
            empty_config = tmp / "empty-gitconfig"
            empty_config.write_text("", encoding="utf-8")
            env = {
                "GIT_CONFIG_GLOBAL": str(empty_config),
                "GIT_CONFIG_SYSTEM": str(empty_config),
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            with mock.patch.dict(os.environ, env):
                vault = make_vault(tmp, "identity-vault")
                # Follow-up fixture commits keep working under the same
                # identity-free environment (repo-local config).
                _write_task(vault, "t2", scheduled=_D1)
                head = _commit_all(vault, "second")
            self.assertEqual(len(head), 40)
            # Nothing was written outside the repo: the empty global
            # config file stayed empty.
            self.assertEqual(empty_config.read_text(encoding="utf-8"), "")
            # The identity is repo-local, not ambient.
            self.assertEqual(
                _git(vault, "config", "--local", "user.name"),
                "tasknotes-tests",
            )
            self.assertEqual(
                _git(vault, "config", "--local", "user.email"),
                "tasknotes-tests@local",
            )


if __name__ == "__main__":
    unittest.main()
