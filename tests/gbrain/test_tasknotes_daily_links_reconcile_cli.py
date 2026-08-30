"""Contract + behavior tests for the fixed-purpose Daily-links
reconciliation CLI (scripts/tasknotes_daily_links_reconcile.py, issue #139
revision 3 W3 refresh-only integration).

Layers covered:

  1. Source contract: the CLI is fixed-purpose (exactly two verbs, no
     options, no path arguments, no generic writer interface), reuses the
     approved W2 core API instead of reimplementing reconciliation, never
     spawns any subprocess (so it can never invoke gbrain or the public
     wrapper), never acquires a lock, and aliases the core's fixed
     cursor/pending state paths.
  2. Docker install/compile contract: Dockerfile.hermes must COPY the CLI
     to the fixed image path, mark it executable, and compile-check it;
     the josemar-gbrain wiring constant must equal the installed path.
  3. Behavior (subprocess, fixture-patched constants): strict master-flag
     validation, complete inertness when disabled (no cursor/pending read
     or write even with garbage state, no lock requirement), root
     refusal, inherited exclusive-lock requirement (missing/forged/shared
     fds fail closed), and the full reconcile -> finalize lifecycle
     against a real temporary Git vault through the real W2 core.

The CLI hardcodes its production paths, so behavior tests run a fixture
copy with those literals substituted (same pattern as the josemar-gbrain
suites). A real flock on a temp lock file provides the locking substrate.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "tasknotes_daily_links_reconcile.py"
CORE_PATH = REPO_ROOT / "scripts" / "tasknotes_mcp_core.py"
WRAPPER_PATH = REPO_ROOT / "scripts" / "josemar-gbrain"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile.hermes"
SCRIPTS_DIR = REPO_ROOT / "scripts"

INSTALLED_PATH = "/opt/josemar/scripts/tasknotes_daily_links_reconcile.py"
MASTER_FLAG = "TASKNOTES_DAILY_LINKS_ENABLED"
SLAVE_FLAG = "TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED"

_D1 = "2026-09-01"


def _has_yaml() -> bool:
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        return False


# Exact literals the CLI fixture must substitute (fixture drift guards).
_CLI_LITERALS = {
    "sys_path": "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))",
    "vault": 'VAULT = Path("/opt/data/obsidian")',
    "lock": 'LOCK_PATH = Path("/opt/data/.locks/tasknotes.lock")',
    "cursor": "RECONCILE_CURSOR_PATH = DAILY_LINKS_RECONCILE_CURSOR_PATH",
    "pending": "RECONCILE_PENDING_PATH = DAILY_LINKS_RECONCILE_PENDING_PATH",
    "uid": "    return os.geteuid()",
}


def patched_cli(
    tmp: Path,
    *,
    vault: Path,
    lock_path: Path,
    cursor_path: Path,
    pending_path: Path,
    extra_patch: tuple[str, str] | None = None,
) -> Path:
    """Fixture copy of the real CLI with the fixed production constants
    substituted for local equivalents (no production env seam)."""
    src = CLI_PATH.read_text(encoding="utf-8")
    for needle in _CLI_LITERALS.values():
        assert needle in src, f"CLI literal drifted: {needle!r}"
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


class ReconcileCliSourceContractTests(unittest.TestCase):
    """Static source contract: fixed-purpose, W2 API reuse, no writer, no
    lock acquisition, no subprocess/gbrain capability."""

    def setUp(self) -> None:
        self.src = CLI_PATH.read_text(encoding="utf-8")
        self.core_src = CORE_PATH.read_text(encoding="utf-8")

    def test_reuses_approved_w2_core_api(self) -> None:
        """The CLI must import and call the approved W2 lifecycle functions
        by name — never reimplement reconciliation."""
        for symbol in (
            "prepare_daily_links_reconciliation",
            "apply_daily_links_reconciliation",
            "finalize_daily_links_reconciliation",
            "load_profile",
            "load_daily_notes_config",
            "load_daily_notes_config(VAULT)",
        ):
            self.assertIn(symbol, self.src)

    def test_aliases_core_fixed_state_paths(self) -> None:
        """Cursor/pending locations are aliased from the core constants so
        the fixed state paths have exactly one source of truth."""
        self.assertIn(
            "RECONCILE_CURSOR_PATH = DAILY_LINKS_RECONCILE_CURSOR_PATH",
            self.src,
        )
        self.assertIn(
            "RECONCILE_PENDING_PATH = DAILY_LINKS_RECONCILE_PENDING_PATH",
            self.src,
        )
        for const in ("DAILY_LINKS_RECONCILE_CURSOR_PATH",
                      "DAILY_LINKS_RECONCILE_PENDING_PATH"):
            self.assertIn(f"{const} = Path(", self.core_src)

    def test_no_subprocess_capability_so_no_gbrain_invocation(self) -> None:
        """The CLI imports no subprocess/os-exec surface at all: it cannot
        invoke gbrain (native or public wrapper) by construction."""
        for forbidden in (
            "import subprocess",
            "subprocess.",
            "os.system",
            "os.exec",
            "os.spawn",
            "popen",
            "gbrain-native",
            "gbrain_chat_run",
            "gbrain-chat-run",
        ):
            self.assertNotIn(forbidden, self.src)

    def test_never_acquires_a_lock(self) -> None:
        """No flock acquisition and no Lock class use: the CLI only VERIFIES
        the inherited exclusive flock via /proc/self/fdinfo."""
        self.assertNotIn("import fcntl", self.src)
        self.assertNotIn("flock(", self.src)
        self.assertNotIn("fcntl.flock", self.src)
        self.assertNotIn("Lock(", self.src)
        # Verification is fdinfo-based, exactly like the wrapper check.
        self.assertIn("/proc/self/fdinfo/", self.src)
        self.assertIn('"FLOCK" in line and "WRITE" in line', self.src)

    def test_exactly_two_fixed_verbs(self) -> None:
        self.assertIn('RECONCILE_VERB = "reconcile"', self.src)
        self.assertIn('FINALIZE_VERB = "finalize"', self.src)

    def test_fixed_locations_and_master_flag_constants(self) -> None:
        self.assertIn('VAULT = Path("/opt/data/obsidian")', self.src)
        self.assertIn('LOCK_PATH = Path("/opt/data/.locks/tasknotes.lock")', self.src)
        self.assertIn(f'MASTER_FLAG = "{MASTER_FLAG}"', self.src)

    def test_refuses_root_execution(self) -> None:
        self.assertIn("os.geteuid()", self.src)
        self.assertIn("runtime_identity_refused", self.src)

    def test_strict_master_flag_parsing_is_fail_closed(self) -> None:
        """Missing/empty = enabled (provided default); only exact true/false
        accepted for nonempty values; the disabled short-circuit must
        precede every lifecycle call inside the step runner."""
        self.assertIn('normalized == "true"', self.src)
        self.assertIn('normalized == "false"', self.src)
        self.assertIn("daily_links_flag_invalid", self.src)
        self.assertIn('"status": "disabled"', self.src)
        step_start = self.src.find("def _run_step")
        step_end = self.src.find("def main")
        assert step_start != -1 and step_end != -1
        step = self.src[step_start:step_end]
        disabled_pos = step.find('"status": "disabled"')
        lifecycle_pos = step.find("_run_reconcile()")
        self.assertGreater(lifecycle_pos, -1)
        self.assertLess(disabled_pos, lifecycle_pos,
                        "the disabled short-circuit must precede the lifecycle")

    def test_failure_envelopes_are_content_capped(self) -> None:
        self.assertIn("MAX_MESSAGE", self.src)
        self.assertIn("daily_links_reconcile_failed", self.src)
        self.assertIn("daily_links_finalize_failed", self.src)
        self.assertIn("tasknotes_lock_not_held", self.src)


class ReconcileCliDockerInstallContractTests(unittest.TestCase):
    """Dockerfile.hermes must install, chmod, and compile-check the CLI at
    the exact path the wrapper invokes."""

    def setUp(self) -> None:
        self.dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

    def test_dockerfile_copies_cli_to_fixed_path_once(self) -> None:
        line = f"COPY scripts/tasknotes_daily_links_reconcile.py {INSTALLED_PATH}"
        self.assertEqual(self.dockerfile.count(line), 1,
                         "exactly one COPY of the reconciliation CLI expected")

    def test_dockerfile_chmods_installed_cli_once(self) -> None:
        self.assertEqual(
            self.dockerfile.count(f"\n    {INSTALLED_PATH} \\"), 1,
            "the chmod list must include the installed CLI exactly once",
        )

    def test_dockerfile_compile_checks_installed_cli_once(self) -> None:
        self.assertEqual(
            self.dockerfile.count(f"\n        {INSTALLED_PATH} \\"), 1,
            "the compileall list must include the installed CLI exactly once",
        )

    def test_wrapper_constant_matches_installed_path(self) -> None:
        self.assertIn(f'TASKNOTES_RECONCILE_CLI="{INSTALLED_PATH}"', self.wrapper)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class ReconcileCliBehaviorTests(unittest.TestCase):
    """Subprocess behavior of the real CLI (fixture constants, real W2
    core, real Git vault, real flock substrate)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix="tasknotes-reconcile-cli-"
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
        self.vault = make_vault(self.tmp, "vault")
        self.cli = patched_cli(
            self.tmp,
            vault=self.vault,
            lock_path=self.lock_path,
            cursor_path=self.cursor_path,
            pending_path=self.pending_path,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def env(self, **extra) -> dict:
        env = os.environ.copy()
        env.update({"HOME": str(self.tmp)})
        env.pop(MASTER_FLAG, None)
        env.pop(SLAVE_FLAG, None)
        env.pop("TASKNOTES_LOCK_FD", None)
        env.update(extra)
        return env

    def run_cli(
        self,
        verb: str,
        *,
        flag: str | None = None,
        slave: str | None = "true",
        lock_fd: int | None = None,
        argv: list[str] | None = None,
    ) -> subprocess.CompletedProcess:
        env = self.env()
        if flag is not None:
            env[MASTER_FLAG] = flag
        if slave is not None:
            # Strict dual flags: default the reconcile-enabled slave to
            # enabled so single-flag tests exercise the master alone.
            env[SLAVE_FLAG] = slave
        kwargs: dict = {}
        if lock_fd is not None:
            env["TASKNOTES_LOCK_FD"] = str(lock_fd)
            kwargs["pass_fds"] = [lock_fd]
        return subprocess.run(
            [sys.executable, "-I", str(self.cli), *(argv or [verb])],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=env,
            **kwargs,
        )

    def out(self, result: subprocess.CompletedProcess) -> dict:
        return json.loads(result.stdout)

    def _hold_lock(self) -> int:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def _write_garbage_state(self) -> tuple[bytes, bytes]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_bytes(b"{not-a-cursor")
        self.pending_path.write_bytes(b"{not-a-pending")
        return b"{not-a-cursor", b"{not-a-pending"

    # -- usage -----------------------------------------------------------

    def test_usage_errors_are_structured_failures(self) -> None:
        for argv in ([], ["bogus"], ["reconcile", "finalize"]):
            with self.subTest(argv=argv):
                result = self.run_cli("", argv=argv, flag="true")
                self.assertNotEqual(result.returncode, 0)
                payload = self.out(result)
                self.assertFalse(payload["success"])
                self.assertEqual(payload["error"], "daily_links_usage")

    # -- master flag -----------------------------------------------------

    def test_explicit_false_values_are_inert_successes(self) -> None:
        """Explicit false variants (any case/spacing) exit 0 as inert
        without the lock fd, the vault, or any state access."""
        garbage = self._write_garbage_state()
        vault_before = sorted(p.name for p in self.vault.rglob("*"))
        for flag in ("false", "FALSE", " False "):
            for verb in ("reconcile", "finalize"):
                with self.subTest(flag=flag, verb=verb):
                    result = self.run_cli(verb, flag=flag)
                    self.assertEqual(result.returncode, 0,
                                     result.stdout + result.stderr)
                    payload = self.out(result)
                    self.assertTrue(payload["success"])
                    self.assertEqual(payload["status"], "disabled")
                    self.assertEqual(payload["action"], verb)
        self.assertEqual(self.cursor_path.read_bytes(), garbage[0])
        self.assertEqual(self.pending_path.read_bytes(), garbage[1])
        self.assertEqual(sorted(p.name for p in self.vault.rglob("*")),
                         vault_before, "the vault must be untouched")

    def test_missing_or_empty_flag_defaults_enabled(self) -> None:
        """Missing and empty master values resolve to the enabled default
        (slave explicitly true): the CLI proceeds past the flag gate and
        fails at the next precondition (no inherited lock), proving no
        disabled short-circuit."""
        garbage = self._write_garbage_state()
        for flag in (None, "", "   "):
            with self.subTest(flag=repr(flag)):
                result = self.run_cli("reconcile", flag=flag)
                self.assertNotEqual(result.returncode, 0,
                                    result.stdout + result.stderr)
                payload = self.out(result)
                self.assertEqual(payload["error"], "tasknotes_lock_not_held")
                self.assertNotIn('"status": "disabled"', result.stdout)
        self.assertEqual(self.cursor_path.read_bytes(), garbage[0])
        self.assertEqual(self.pending_path.read_bytes(), garbage[1])

    def test_flag_matrix_missing_empty_false_and_mixed_pairs(self) -> None:
        """Strict dual flags matrix: missing and empty on each flag resolve
        to enabled (default), each explicit false disables, and mixed pairs
        AND-combine. Outcomes: inert success only when at least one explicit
        false; otherwise the run proceeds to the lock precondition. Invalid
        nonempty values fail structured before anything else."""
        inert = (False, "disabled")
        matrix = (
            # (master, slave, expected_error_or_inert)
            (None, None, "tasknotes_lock_not_held"),   # both missing: enabled
            ("", "", "tasknotes_lock_not_held"),       # both empty: enabled
            ("true", None, "tasknotes_lock_not_held"), # slave missing: enabled
            (None, "true", "tasknotes_lock_not_held"), # master missing: enabled
            ("false", None, inert),                    # explicit false wins
            (None, "false", inert),                    # explicit false wins
            ("false", "", inert),                      # mixed false/empty
            ("", "false", inert),                      # mixed empty/false
            ("FALSE", "false", inert),                 # case-insensitive false
            ("True", "TRUE", "tasknotes_lock_not_held"),
            ("false", "true", inert),
            ("true", "false", inert),
        )
        for master, slave, expected in matrix:
            with self.subTest(master=repr(master), slave=repr(slave)):
                result = self.run_cli("reconcile", flag=master, slave=slave)
                payload = self.out(result)
                if expected == inert:
                    self.assertEqual(result.returncode, 0,
                                     result.stdout + result.stderr)
                    self.assertTrue(payload["success"])
                    self.assertEqual(payload["status"], "disabled")
                else:
                    self.assertNotEqual(result.returncode, 0,
                                        result.stdout + result.stderr)
                    self.assertEqual(payload["error"], expected)
                    self.assertNotIn('"status": "disabled"', result.stdout)

    def test_invalid_flag_values_fail_closed_without_state_access(self) -> None:
        garbage = self._write_garbage_state()
        for flag in ("yes", "1", "truefalse", "tru e"):
            with self.subTest(flag=flag):
                result = self.run_cli("reconcile", flag=flag)
                self.assertNotEqual(result.returncode, 0,
                                    result.stdout + result.stderr)
                payload = self.out(result)
                self.assertFalse(payload["success"])
                self.assertEqual(payload["error"], "daily_links_flag_invalid")
        self.assertEqual(self.cursor_path.read_bytes(), garbage[0])
        self.assertEqual(self.pending_path.read_bytes(), garbage[1])

    def test_enabled_accepts_true_variants(self) -> None:
        """true variants proceed past the flag (they then hit the lock
        precondition, proving the flag check alone passed)."""
        for flag in ("true", "TRUE", " True "):
            with self.subTest(flag=flag):
                result = self.run_cli("reconcile", flag=flag)
                self.assertNotEqual(result.returncode, 0)
                payload = self.out(result)
                self.assertEqual(payload["error"], "tasknotes_lock_not_held")

    # -- runtime identity ------------------------------------------------

    def test_root_refused_when_enabled(self) -> None:
        root_cli = patched_cli(
            self.tmp,
            vault=self.vault,
            lock_path=self.lock_path,
            cursor_path=self.cursor_path,
            pending_path=self.pending_path,
            extra_patch=(_CLI_LITERALS["uid"], "    return 0"),
        )
        fd = self._hold_lock()
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(root_cli), "reconcile"],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
                env=self.env(**{MASTER_FLAG: "true",
                                SLAVE_FLAG: "true",
                                "TASKNOTES_LOCK_FD": str(fd)}),
                pass_fds=[fd],
            )
        finally:
            os.close(fd)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.out(result)
        self.assertEqual(payload["error"], "runtime_identity_refused")
        self.assertFalse(self.cursor_path.exists(), "no cursor write as root")
        self.assertFalse(self.pending_path.exists(), "no pending write as root")

    def test_root_with_disabled_flag_is_still_inert_success(self) -> None:
        """Flag validation precedes identity: disabled stays inert even for
        a (simulated) root caller."""
        root_cli = patched_cli(
            self.tmp,
            vault=self.vault,
            lock_path=self.lock_path,
            cursor_path=self.cursor_path,
            pending_path=self.pending_path,
            extra_patch=(_CLI_LITERALS["uid"], "    return 0"),
        )
        result = subprocess.run(
            [sys.executable, "-I", str(root_cli), "finalize"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=self.env(**{MASTER_FLAG: "false"}),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.out(result)["status"], "disabled")

    # -- inherited lock requirement ----------------------------------------

    def test_missing_lock_fd_fails_closed(self) -> None:
        result = self.run_cli("reconcile", flag="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.out(result)["error"], "tasknotes_lock_not_held")

    def test_flocked_fd_to_other_file_fails_closed(self) -> None:
        other = self.tmp / "other.lock"
        other.touch()
        forged = os.open(other, os.O_RDWR)
        real = self._hold_lock()
        try:
            fcntl.flock(forged, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_cli("finalize", flag="true", lock_fd=forged)
        finally:
            os.close(forged)
            os.close(real)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.out(result)["error"], "tasknotes_lock_not_held")

    def test_unflocked_fd_to_lock_file_fails_closed(self) -> None:
        forged = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        real = os.open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(real, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_cli("reconcile", flag="true", lock_fd=forged)
        finally:
            os.close(forged)
            os.close(real)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.out(result)["error"], "tasknotes_lock_not_held")

    def test_shared_lock_fd_fails_closed(self) -> None:
        """A SHARED (LOCK_SH) flock on the exact lock file is not an
        exclusive writer lock (fdinfo shows READ, not WRITE), so it must
        not satisfy the inherited-lock check."""
        forged = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(forged, fcntl.LOCK_SH | fcntl.LOCK_NB)
            result = self.run_cli("reconcile", flag="true", lock_fd=forged)
        finally:
            os.close(forged)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.out(result)["error"], "tasknotes_lock_not_held")

    def test_lock_failures_never_touch_state(self) -> None:
        self._write_garbage_state()
        result = self.run_cli("reconcile", flag="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.cursor_path.read_bytes(), b"{not-a-cursor")
        self.assertEqual(self.pending_path.read_bytes(), b"{not-a-pending")

    # -- full lifecycle against the real W2 core -------------------------

    def test_reconcile_then_finalize_full_cycle(self) -> None:
        """Enabled + inherited exclusive lock: reconcile prepares/applies
        with a targeted commit and a replayable pending; finalize (only
        ever invoked by refresh after sync success) advances the cursor and
        clears the pending."""
        head_before = _git(self.vault, "rev-parse", "HEAD")
        fd = self._hold_lock()
        try:
            result = self.run_cli("reconcile", flag="true", lock_fd=fd)
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.out(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["action"], "reconcile")
        self.assertEqual(payload["status"], "applied")
        self.assertEqual(payload["applied"], 1)
        self.assertTrue(payload["commit_created"])

        note = (self.vault / "journal" / f"{_D1}.md").read_text(encoding="utf-8")
        self.assertIn("- [[t1]]", note)
        self.assertIn("scheduled: '2026-09-01'",
                      (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8"),
                      "task markdown must never be rewritten")

        head_after = _git(self.vault, "rev-parse", "HEAD")
        self.assertNotEqual(head_before, head_after)
        self.assertTrue(self.pending_path.exists())
        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertEqual(pending["from_head"], head_before)
        self.assertEqual(pending["to_head"], head_after)
        self.assertEqual(pending["daily_folder"], "journal")
        self.assertFalse(self.cursor_path.exists(),
                         "reconcile must never advance the cursor")

        fd = self._hold_lock()
        try:
            result = self.run_cli("finalize", flag="true", lock_fd=fd)
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.out(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "finalized")
        self.assertFalse(self.pending_path.exists(),
                         "finalize must clear the pending sibling")
        self.assertTrue(self.cursor_path.exists())
        cursor = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["reconciled_head"], head_after)
        self.assertEqual(cursor["daily_folder"], "journal")

    def test_second_unchanged_cycle_is_idempotent(self) -> None:
        """A steady-state refresh cycle (no external changes) applies
        nothing and keeps the cursor stable — replay never duplicates."""
        fd = self._hold_lock()
        try:
            self.run_cli("reconcile", flag="true", lock_fd=fd)
            self.run_cli("finalize", flag="true", lock_fd=fd)
        finally:
            os.close(fd)
        head = _git(self.vault, "rev-parse", "HEAD")

        fd = self._hold_lock()
        try:
            result = self.run_cli("reconcile", flag="true", lock_fd=fd)
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.out(result)
        self.assertEqual(payload["applied"], 0)
        self.assertFalse(payload["commit_created"])
        self.assertEqual(_git(self.vault, "rev-parse", "HEAD"), head)

        fd = self._hold_lock()
        try:
            result = self.run_cli("finalize", flag="true", lock_fd=fd)
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.pending_path.exists())
        cursor = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["reconciled_head"], head)

    def test_corrupt_cursor_fails_closed_without_writes(self) -> None:
        """An established-but-corrupt cursor fails closed in prepare; the
        garbage state stays untouched (replayable, never clobbered)."""
        self._write_garbage_state()
        fd = self._hold_lock()
        try:
            result = self.run_cli("reconcile", flag="true", lock_fd=fd)
        finally:
            os.close(fd)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.out(result)
        self.assertEqual(payload["error"], "daily_links_reconcile_failed")
        self.assertEqual(self.cursor_path.read_bytes(), b"{not-a-cursor")
        self.assertEqual(self.pending_path.read_bytes(), b"{not-a-pending")


class MakeVaultIdentityTests(unittest.TestCase):
    """CI-portability regression (fast-tests run 33278429405): the
    ``make_vault`` fixture must commit with a deterministic repo-local
    identity so environments without a global git identity (GitHub
    Actions runners) do not fail with ``git commit`` exit 128. The
    global/system git config must remain untouched."""

    def test_make_vault_commits_without_global_git_identity(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="reconcile-cli-vault-identity-"
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
