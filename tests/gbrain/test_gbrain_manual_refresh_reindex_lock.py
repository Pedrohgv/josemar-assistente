"""Behavior tests: direct manual `josemar-gbrain refresh`/`reindex` must not
open PGLite unprotected (issue #110), and cron chains (where the lock runner
already holds the lock and passes the lock fd via TASKNOTES_LOCK_FD) must not
nest acquisition. Forged booleans or forged fds must never bypass the lock.
Fresh activation must create the lock file safely; symlinked lock paths must
be rejected with no check→open TOCTOU.

The wrapper hardcodes its production paths (binary, lock, interpreter, lock
runner) with no env overrides, so these tests run a fixture copy of the real
wrapper with those literals substituted for local equivalents — the same
pattern as the cron fixtures. A real flock on a temp lock file provides the
locking substrate (no Docker, no gbrain needed).
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


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "josemar-gbrain"
RUNNER = REPO_ROOT / "scripts" / "tasknotes_lock_run.py"


def patched_wrapper(tmp: Path, fake_gbrain: Path, lock_path: Path) -> Path:
    """Fixture copy of the production wrapper with the fixed production
    literals substituted for local equivalents (no production env seam)."""
    src = WRAPPER.read_text(encoding="utf-8")
    patched = (
        src.replace('GBRAIN_BIN="/opt/josemar/libexec/gbrain-native"', f'GBRAIN_BIN="{fake_gbrain}"')
        .replace(
            'GBRAIN_TASKNOTES_LOCK="/opt/data/.locks/tasknotes.lock"',
            f'GBRAIN_TASKNOTES_LOCK="{lock_path}"',
        )
        .replace('GBRAIN_HOME="/opt/data"', f'GBRAIN_HOME="{tmp / "state"}"')
        .replace(
            'GBRAIN_BRAIN_REPO="/opt/data/obsidian"',
            f'GBRAIN_BRAIN_REPO="{tmp / "brain"}"',
        )
        .replace(
            'GBRAIN_SCHEMA_SOURCE_ROOT="/opt/data/.gbrain/schema-packs"',
            f'GBRAIN_SCHEMA_SOURCE_ROOT="{tmp / "state" / ".gbrain" / "schema-packs"}"',
        )
        .replace(
            'PYTHON_BIN="/opt/hermes/.venv/bin/python3"',
            f'PYTHON_BIN="{sys.executable}"',
        )
        .replace(
            'TASKNOTES_LOCK_RUNNER="/opt/josemar/scripts/tasknotes_lock_run.py"',
            f'TASKNOTES_LOCK_RUNNER="{RUNNER}"',
        )
    )
    script = tmp / "josemar-gbrain"
    script.write_text(patched, encoding="utf-8")
    script.chmod(0o755)
    return script


class FakeGbrain:
    """A fake `gbrain` binary that logs every invocation and the enforced
    startup-hook env value. Optional `fail_patterns` cause any invocation
    whose full command line contains the pattern to exit nonzero with a
    stderr note, so failure paths (e.g. a failing `init --migrate-only`) can
    be exercised. Optional `on_schema_validate` is a shell snippet executed
    when `$1` is `schema` (e.g. the native schema-pack validation that runs
    between the reindex preflight and the init choice), letting a test
    mutate the filesystem at that exact instant."""

    def __init__(
        self,
        tmp: Path,
        fail_patterns: list[str] | None = None,
        on_schema_validate: str | None = None,
    ):
        self.log = tmp / "gbrain-calls.log"
        self.env_log = tmp / "gbrain-env.log"
        self.script = tmp / "gbrain"
        fail_guards = "\n".join(
            f'case "$*" in\n  *{pat}*) echo "scripted-failure: {pat}" >&2; exit 5 ;;\nesac'
            for pat in (fail_patterns or [])
        )
        if fail_guards:
            fail_guards += "\n"
        schema_arm = ""
        if on_schema_validate:
            schema_arm = f"  schema) {on_schema_validate} ;;\n"
        self.script.write_text(
            f"""#!/bin/sh
echo "$*" >> "{self.log}"
printf 'GBRAIN_SKIP_STARTUP_HOOKS=%s\\n' "${{GBRAIN_SKIP_STARTUP_HOOKS:-}}" >> "{self.env_log}"
{fail_guards}case "$1" in
{schema_arm}  config) echo "ok" ;;
  sync) echo '{{"status":"ok"}}' ;;
  extract) echo '{{"status":"ok"}}' ;;
  *) echo '{{"status":"ok"}}' ;;
esac
""",
            encoding="utf-8",
        )
        self.script.chmod(0o755)

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln]

    def env_lines(self) -> list[str]:
        if not self.env_log.exists():
            return []
        return [ln for ln in self.env_log.read_text(encoding="utf-8").splitlines() if ln]


class ManualRefreshReindexLockBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gbrain-manual-lock-")
        self.tmp = Path(self._tmp.name)
        self.lock_path = self.tmp / "locks" / "tasknotes.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch()
        self.fake = FakeGbrain(self.tmp)
        self.wrapper = patched_wrapper(self.tmp, self.fake.script, self.lock_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def env(self, **extra) -> dict:
        env = os.environ.copy()
        env.update({"HOME": str(self.tmp)})
        env.update(extra)
        return env

    def run_wrapper(self, subcommand: str, **env_extra) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.wrapper), subcommand],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=self.env(**env_extra),
        )

    def _hold_lock(self) -> int:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def test_refresh_succeeds_when_lock_free(self) -> None:
        result = self.run_wrapper("refresh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        self.assertTrue(self.fake.calls(), "gbrain must run when the lock is free")

    def test_skip_startup_hooks_enforced_regardless_of_caller_env(self) -> None:
        """Issue #112: the wrapper exports GBRAIN_SKIP_STARTUP_HOOKS=1
        (export assignment overrides the caller), so every gbrain invocation
        through the private launcher skips startup hooks even under a hostile
        caller environment."""
        for hostile in ("0", ""):
            with self.subTest(caller_value=hostile):
                if self.fake.env_log.exists():
                    self.fake.env_log.unlink()
                result = self.run_wrapper("refresh", GBRAIN_SKIP_STARTUP_HOOKS=hostile)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                lines = self.fake.env_lines()
                self.assertTrue(lines, "gbrain must run under refresh")
                for line in lines:
                    self.assertEqual(line, "GBRAIN_SKIP_STARTUP_HOOKS=1")

    def test_refresh_refuses_when_lock_held(self) -> None:
        fd = self._hold_lock()
        try:
            result = self.run_wrapper("refresh")
        finally:
            os.close(fd)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh_lock_busy", result.stdout)
        self.assertEqual(
            self.fake.calls(), [], "gbrain must not run while the lock is busy"
        )

    def test_reindex_refuses_when_lock_held(self) -> None:
        fd = self._hold_lock()
        try:
            result = self.run_wrapper("reindex")
        finally:
            os.close(fd)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reindex_lock_busy", result.stdout)
        self.assertEqual(
            self.fake.calls(), [], "gbrain must not run while the lock is busy"
        )

    def test_refresh_skips_self_lock_when_runner_holds_lock(self) -> None:
        """Cron chain: the lock runner already holds the flock and passes the
        lock fd to its child (TASKNOTES_LOCK_FD); refresh must proceed
        without nested acquisition."""
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [str(self.wrapper), "refresh"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                env=self.env(TASKNOTES_LOCK_FD=str(fd)),
                pass_fds=[fd],
            )
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        self.assertTrue(self.fake.calls(), "refresh must run under the runner's lock")

    def test_boolean_forgery_cannot_bypass_lock(self) -> None:
        """The old TASKNOTES_LOCK_HELD boolean is ignored: with the lock held
        elsewhere, refresh must refuse instead of running unlocked."""
        fd = self._hold_lock()
        try:
            result = self.run_wrapper("refresh", TASKNOTES_LOCK_HELD="1")
        finally:
            os.close(fd)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh_lock_busy", result.stdout)
        self.assertEqual(
            self.fake.calls(), [], "gbrain must not run while the lock is busy"
        )

    def test_forged_flocked_fd_to_other_file_cannot_bypass_lock(self) -> None:
        """A flocked fd to a DIFFERENT file is not the configured lock: the
        wrapper must not skip self-acquisition, so with the real lock held
        elsewhere refresh must refuse."""
        other = self.tmp / "other.lock"
        other.touch()
        forged = os.open(other, os.O_RDWR)
        real = self._hold_lock()
        try:
            fcntl.flock(forged, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_wrapper("refresh", TASKNOTES_LOCK_FD=str(forged))
        finally:
            os.close(forged)
            os.close(real)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh_lock_busy", result.stdout)
        self.assertEqual(
            self.fake.calls(), [], "gbrain must not run while the lock is busy"
        )

    def test_forged_unflocked_fd_cannot_skip_acquisition(self) -> None:
        """An inherited fd to the lock file that does NOT actually hold a
        flock must not skip self-acquisition (fdinfo shows no FLOCK for it):
        with the lock held via a different fd, refresh must refuse."""
        forged = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)  # not flocked
        real = os.open(self.lock_path, os.O_RDWR)  # separate fd; holds the flock
        try:
            fcntl.flock(real, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_wrapper("refresh", TASKNOTES_LOCK_FD=str(forged))
        finally:
            os.close(forged)
            os.close(real)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh_lock_busy", result.stdout)
        self.assertEqual(
            self.fake.calls(), [], "gbrain must not run while the lock is busy"
        )

    def test_forged_shared_lock_fd_cannot_skip_acquisition(self) -> None:
        """A SHARED (LOCK_SH) flock on the exact lock file is not an
        exclusive writer lock (fdinfo shows READ, not WRITE), so it must not
        skip self-acquisition: with the exclusive lock held by another
        process, refresh must refuse."""
        forged = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(forged, fcntl.LOCK_SH | fcntl.LOCK_NB)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, os, time\n"
                f"fd = os.open({str(self.lock_path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "time.sleep(10)\n",
            ]
        )
        try:
            result = self.run_wrapper("refresh", TASKNOTES_LOCK_FD=str(forged))
        finally:
            holder.kill()
            holder.wait(timeout=10)
            os.close(forged)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh_lock_busy", result.stdout)
        self.assertEqual(
            self.fake.calls(), [], "gbrain must not run while the lock is busy"
        )

    def test_fresh_install_creates_lock_file_safely(self) -> None:
        """Fresh deployment: no locks dir and no lock file exist before
        reindex; the wrapper must create them safely (regular file, not a
        symlink) and complete activation."""
        self.lock_path.unlink()
        self.lock_path.parent.rmdir()
        result = self.run_wrapper("reindex", GBRAIN_SCHEMA_PACK="gbrain-base")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        self.assertTrue(self.lock_path.is_file(), "lock file must be created")
        self.assertFalse(self.lock_path.is_symlink(), "lock file must be regular")
        self.assertTrue(self.fake.calls(), "gbrain must run after lock creation")
        # The runtime schema-pack marker must be persisted for the adapter.
        marker = self.tmp / "state" / ".gbrain" / "active-schema-pack"
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "gbrain-base")

    def test_reindex_atomically_replaces_existing_marker(self) -> None:
        """A stale marker must be atomically replaced (same-directory temp +
        rename): after a successful reindex with a different pack, the marker
        holds exactly the new pack and no temp files remain."""
        marker = self.tmp / "state" / ".gbrain" / "active-schema-pack"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("oldpack\n", encoding="utf-8")
        result = self.run_wrapper("reindex", GBRAIN_SCHEMA_PACK="gbrain-base")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "gbrain-base")
        self.assertFalse(marker.is_symlink())
        leftovers = [
            p for p in marker.parent.iterdir()
            if p.name.startswith("active-schema-pack.") and p.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [], "no temp files may remain after the rename")

    def test_reindex_fails_closed_when_marker_cannot_be_replaced(self) -> None:
        """A marker path that cannot be replaced (here: a directory) must be a
        structured nonzero error: reindex must NOT report success without the
        marker."""
        marker = self.tmp / "state" / ".gbrain" / "active-schema-pack"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.mkdir()
        result = self.run_wrapper("reindex", GBRAIN_SCHEMA_PACK="gbrain-base")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("schema_pack_marker_write_failed", result.stdout)
        self.assertNotIn('"success": true', result.stdout)

    def test_reindex_fails_closed_when_marker_is_symlink(self) -> None:
        """A pre-placed symlink at the marker path is fail-closed: reindex
        must refuse to report success and must NOT clobber the symlink
        target."""
        marker = self.tmp / "state" / ".gbrain" / "active-schema-pack"
        marker.parent.mkdir(parents=True, exist_ok=True)
        decoy = self.tmp / "decoy-marker"
        decoy.write_text("precious\n", encoding="utf-8")
        marker.symlink_to(decoy)
        result = self.run_wrapper("reindex", GBRAIN_SCHEMA_PACK="gbrain-base")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("schema_pack_marker_write_failed", result.stdout)
        self.assertNotIn('"success": true', result.stdout)
        self.assertTrue(marker.is_symlink(), "symlink must not be clobbered")
        self.assertEqual(
            decoy.read_text(encoding="utf-8").strip(), "precious",
            "decoy target must be untouched",
        )

    def test_symlinked_lock_path_is_rejected(self) -> None:
        """A pre-placed symlink at the lock path must be refused (the
        no-follow verification fails closed): refresh must not run against a
        decoy lock and must not clobber the symlink target."""
        self.lock_path.unlink()
        decoy = self.tmp / "decoy.lock"
        decoy.touch()
        self.lock_path.symlink_to(decoy)
        result = self.run_wrapper("refresh")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh_lock_unavailable", result.stdout)
        self.assertEqual(
            self.fake.calls(), [], "gbrain must not run through a symlinked lock"
        )
        self.assertTrue(self.lock_path.is_symlink(), "symlink must not be clobbered")

    def test_symlinked_lock_to_missing_target_is_rejected(self) -> None:
        """A symlink to a missing target fails the openability probe in-shell
        (no shell death) and must be refused with the structured error."""
        self.lock_path.unlink()
        self.lock_path.symlink_to(self.tmp / "missing-target")
        result = self.run_wrapper("refresh")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh_lock_unavailable", result.stdout)
        self.assertEqual(self.fake.calls(), [])

    def test_refresh_fails_closed_when_lock_dir_unwritable(self) -> None:
        """When the lock file cannot be opened (unwritable directory), the
        wrapper must emit the structured unavailable error and never run
        gbrain — no shell death, no silent skip."""
        self.lock_path.unlink()
        self.lock_path.parent.chmod(0o500)
        try:
            result = self.run_wrapper("refresh")
        finally:
            self.lock_path.parent.chmod(0o700)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh_lock_unavailable", result.stdout)
        self.assertEqual(
            self.fake.calls(), [], "gbrain must not run without a usable lock"
        )

    def test_lock_released_after_refresh(self) -> None:
        result = self.run_wrapper("refresh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # Nonblocking acquisition must succeed: the wrapper released the lock.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_fixed_locations_ignore_env_overrides(self) -> None:
        """GBRAIN_HOME / GBRAIN_BRAIN_REPO are fixed constants: forged env
        values must not redirect gbrain at a different state dir or repo."""
        marker = self.tmp / "pwned"
        forged = str(self.tmp / "forged-state")
        result = self.run_wrapper(
            "refresh",
            GBRAIN_HOME=forged,
            GBRAIN_BRAIN_REPO=forged,
            GBRAIN_SCHEMA_SOURCE_ROOT=forged,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        calls = "\n".join(self.fake.calls())
        self.assertIn(str(self.tmp / "brain"), calls,
                      "gbrain must use the fixed brain repo, not the forged one")
        self.assertNotIn(forged, calls)
        self.assertFalse(marker.exists())

    def test_hostile_pythonpath_cannot_inject_into_lock_validation(self) -> None:
        """The pre-lock python helpers (lock validation, safe open) run with
        the fixed interpreter in isolated mode (-I): a hostile PYTHONPATH
        sitecustomize must never execute."""
        evil = self.tmp / "evil"
        evil.mkdir()
        marker = evil / "sitecustomize-ran"
        (evil / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        result = self.run_wrapper("refresh", PYTHONPATH=str(evil))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        self.assertFalse(
            marker.exists(),
            "sitecustomize must not execute in the isolated pre-lock helpers",
        )


class ReindexStateAwareBehaviorTests(unittest.TestCase):
    """Issue #132 Findings 1-2: a fail-closed preflight runs under the shared
    lock BEFORE any native gbrain invocation. Healthy existing = regular
    non-symlink config.json (JSON object, engine exactly pglite, database_path
    resolving to the canonical PGLite path, no database_url) + non-symlink
    DIRECTORY brain.pglite; fresh = both artifacts absent; everything else is
    a structured nonzero error with ZERO native gbrain calls. GBRAIN_DATABASE_URL
    and an effective DATABASE_URL (per the exact cwd-dotenv heuristic) fail
    closed too.

    These tests run the patched real wrapper against a scripted fake gbrain
    and assert the recorded native argv (FakeGbrain.calls) plus exit codes.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gbrain-state-aware-")
        self.tmp = Path(self._tmp.name)
        self.lock_path = self.tmp / "locks" / "tasknotes.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch()
        self.state_dir = self.tmp / "state" / ".gbrain"
        self.config_path = self.state_dir / "config.json"
        self.pglite_path = self.state_dir / "brain.pglite"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def env(self, **extra) -> dict:
        env = os.environ.copy()
        # Deterministic database env: the preflight must never see a stray
        # caller value; tests opt in explicitly via extra.
        env.pop("GBRAIN_DATABASE_URL", None)
        env.pop("DATABASE_URL", None)
        env.update({"HOME": str(self.tmp), "GBRAIN_SCHEMA_PACK": "gbrain-base"})
        env.update(extra)
        return env

    def run_reindex(
        self, fake: FakeGbrain, cwd: Path | None = None, **env_extra
    ) -> subprocess.CompletedProcess[str]:
        wrapper = patched_wrapper(self.tmp, fake.script, self.lock_path)
        return subprocess.run(
            [str(wrapper), "reindex"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=self.env(**env_extra),
            cwd=str(cwd or self.tmp),
        )

    def _write_config(self, payload: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")

    def _make_healthy_state(self) -> None:
        """Canonical healthy existing state: regular config.json with the
        exact pglite file-plane shape plus the brain.pglite DIRECTORY."""
        self.pglite_path.mkdir(parents=True, exist_ok=True)
        self._write_config({
            "engine": "pglite",
            "database_path": str(self.pglite_path),
        })

    def _dotenv_fixture(self, files: dict[str, str]) -> Path:
        """Write the given dotenv files into a dedicated cwd directory and
        return it (the wrapper's preflight reads them from the inherited
        cwd)."""
        dotenv_dir = self.tmp / "dotenv"
        dotenv_dir.mkdir(exist_ok=True)
        for name, content in files.items():
            (dotenv_dir / name).write_text(content, encoding="utf-8")
        return dotenv_dir

    def test_genuinely_fresh_state_runs_fresh_init_only(self) -> None:
        """(1) No config and no PGLite artifact: the fresh vector
        `init --pglite --no-embedding` plus the fresh-only keyword write run;
        migrate-only must not."""
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        calls = fake.calls()
        self.assertIn("init --pglite --no-embedding", calls)
        self.assertIn("config set search.mcp_keyword_only true", calls)
        self.assertNotIn("init --migrate-only", calls)

    def test_healthy_existing_state_runs_migrate_only_only(self) -> None:
        """(2) Healthy canonical existing PGLite state: only
        `init --migrate-only` runs; no fresh init and no keyword write."""
        self._make_healthy_state()
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        calls = fake.calls()
        self.assertIn("init --migrate-only", calls)
        joined = " ".join(calls)
        self.assertNotIn("init --pglite", joined,
                         "no fresh init may run for an existing config")
        self.assertNotIn("--no-embedding", joined,
                         "no-embedding must never be passed to migrate-only")
        self.assertFalse(
            any(c.startswith("config set search.mcp_keyword_only") for c in calls),
            "the existing-config branch must not write keyword mode",
        )

    def test_corrupt_config_fails_closed_with_no_native_calls(self) -> None:
        """(5) A corrupt regular config (with the PGLite directory present)
        fails closed before any native call; bytes are left unchanged."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pglite_path.mkdir(exist_ok=True)
        original = b"not-json-{corrupt"
        self.config_path.write_bytes(original)
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_malformed", result.stdout)
        self.assertNotIn('"success": true', result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")
        self.assertEqual(self.config_path.read_bytes(), original,
                         "corrupt config bytes must be left unchanged")

    def test_config_absent_with_pglite_dir_fails_closed_before_init(self) -> None:
        """(4) Config absent but a brain.pglite artifact exists (canonical
        directory form): structured nonzero error before any init, artifact
        untouched."""
        self.pglite_path.mkdir(parents=True, exist_ok=True)
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_pglite_without_config", result.stdout)
        self.assertNotIn('"success": true', result.stdout)
        self.assertEqual(fake.calls(), [], "no native init may run")
        self.assertTrue(self.pglite_path.is_dir(), "artifact must not be clobbered")

    def test_config_absent_with_pglite_file_fails_closed(self) -> None:
        """(4) brain.pglite existing in any form (here: a plain file) with
        no config must also fail closed."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pglite_path.write_text("stray", encoding="utf-8")
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_pglite_without_config", result.stdout)
        self.assertEqual(fake.calls(), [], "no native init may run")

    def test_config_without_pglite_fails_closed_with_no_native_calls(self) -> None:
        """(3) Regular config but no brain.pglite: healthy existing requires
        the PGLite directory, so reindex fails closed with no native calls."""
        self._write_config({"engine": "pglite",
                            "database_path": str(self.pglite_path)})
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_without_pglite", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_pglite_regular_file_with_config_fails_closed(self) -> None:
        """brain.pglite must be a DIRECTORY: a regular file at the path with
        a config present fails closed."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("{}", encoding="utf-8")
        self.pglite_path.write_text("stray", encoding="utf-8")
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_pglite_not_directory", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_directory_config_fails_closed_without_clobber(self) -> None:
        """(12) A directory at the config path is a non-regular existing
        entry: fail closed before init and never clobber it."""
        self.config_path.mkdir(parents=True, exist_ok=True)
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_not_regular", result.stdout)
        self.assertEqual(fake.calls(), [], "no native init may run")
        self.assertTrue(self.config_path.is_dir(), "directory must not be clobbered")

    def test_symlinked_config_fails_closed_without_clobber(self) -> None:
        """(4) A config symlink to a real target fails closed; the symlink
        and its decoy target stay untouched."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        decoy = self.tmp / "decoy-config"
        decoy.write_text("precious\n", encoding="utf-8")
        self.config_path.symlink_to(decoy)
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_symlink", result.stdout)
        self.assertEqual(fake.calls(), [], "no native init may run")
        self.assertTrue(self.config_path.is_symlink(), "symlink must not be clobbered")
        self.assertEqual(decoy.read_text(encoding="utf-8").strip(), "precious",
                         "decoy target must be untouched")

    def test_dangling_symlinked_config_fails_closed(self) -> None:
        """(4) A dangling config symlink also fails closed (no init)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.symlink_to(self.tmp / "missing-config-target")
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_symlink", result.stdout)
        self.assertEqual(fake.calls(), [], "no native init may run")

    def test_symlinked_pglite_fails_closed_without_clobber(self) -> None:
        """(5) A brain.pglite symlink fails closed even with a regular
        config present; the symlink and its target stay untouched."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("{}", encoding="utf-8")
        decoy = self.tmp / "decoy-pglite"
        decoy.mkdir()
        self.pglite_path.symlink_to(decoy)
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_pglite_symlink", result.stdout)
        self.assertEqual(fake.calls(), [], "no native init may run")
        self.assertTrue(self.pglite_path.is_symlink(), "symlink must not be clobbered")

    def test_dangling_symlinked_pglite_fails_closed(self) -> None:
        """(5) A dangling brain.pglite symlink also fails closed (no init)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("{}", encoding="utf-8")
        self.pglite_path.symlink_to(self.tmp / "missing-pglite-target")
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_pglite_symlink", result.stdout)
        self.assertEqual(fake.calls(), [], "no native init may run")

    def test_migrate_only_failure_is_terminal_no_fresh_fallback(self) -> None:
        """(13) A failing `init --migrate-only` on a healthy existing state
        returns nonzero with the structured init error and must NOT fall
        back to fresh init."""
        self._make_healthy_state()
        fake = FakeGbrain(self.tmp, fail_patterns=["init --migrate-only"])
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_init_failed", result.stdout)
        calls = fake.calls()
        self.assertIn("init --migrate-only", calls)
        self.assertNotIn("init --pglite", " ".join(calls),
                         "no fresh-init fallback after a migrate-only failure")

    def test_persisted_engine_postgres_fails_closed(self) -> None:
        """(6) A persisted non-pglite engine (postgres) fails closed with no
        native calls."""
        self.pglite_path.mkdir(parents=True, exist_ok=True)
        self._write_config({"engine": "postgres",
                            "database_path": str(self.pglite_path)})
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_engine_invalid", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_engine_less_config_fails_closed(self) -> None:
        """Engine-less historical config shapes are not permitted: missing
        engine fails closed with no native calls."""
        self.pglite_path.mkdir(parents=True, exist_ok=True)
        self._write_config({"database_path": str(self.pglite_path)})
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_engine_invalid", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_persisted_database_url_fails_closed(self) -> None:
        """(7) A persisted database_url in config.json fails closed with no
        native calls."""
        self.pglite_path.mkdir(parents=True, exist_ok=True)
        self._write_config({"engine": "pglite",
                            "database_path": str(self.pglite_path),
                            "database_url": "postgres://persisted"})
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_database_url_persisted", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_noncanonical_database_path_fails_closed(self) -> None:
        """(8) A database_path not resolving to the canonical PGLite path
        fails closed with no native calls."""
        self.pglite_path.mkdir(parents=True, exist_ok=True)
        self._write_config({"engine": "pglite",
                            "database_path": str(self.tmp / "elsewhere")})
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_database_path_invalid", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_missing_database_path_fails_closed(self) -> None:
        """(8b) A config without database_path fails closed with no native
        calls."""
        self.pglite_path.mkdir(parents=True, exist_ok=True)
        self._write_config({"engine": "pglite"})
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_config_database_path_invalid", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_gbrain_database_url_env_fails_closed(self) -> None:
        """(9) A truthy GBRAIN_DATABASE_URL fails closed before any native
        call (the native CLI would select Postgres), even on fresh state."""
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, GBRAIN_DATABASE_URL="postgres://env")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_gbrain_database_url_env", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_effective_database_url_fails_closed(self) -> None:
        """(10) A truthy DATABASE_URL not matched by any cwd dotenv
        assignment is effective (Postgres) and fails closed with no native
        calls."""
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, DATABASE_URL="postgres://env-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_database_url_effective", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_cwd_dotenv_matched_database_url_accepted_for_healthy_state(self) -> None:
        """(11) Faithful heuristic: a DATABASE_URL equal to a DATABASE_URL
        assignment in a fixed dotenv file of the INHERITED CWD is ignored by
        loadConfig, so a healthy existing state may proceed to migrate-only."""
        self._make_healthy_state()
        dotenv_dir = self.tmp / "dotenv"
        dotenv_dir.mkdir()
        (dotenv_dir / ".env").write_text(
            "DATABASE_URL=postgres://matched\n", encoding="utf-8"
        )
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, cwd=dotenv_dir, DATABASE_URL="postgres://matched")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        calls = fake.calls()
        self.assertIn("init --migrate-only", calls)
        self.assertNotIn("init --pglite", " ".join(calls))

    def test_cwd_dotenv_mismatched_database_url_fails_closed(self) -> None:
        """(10b) The heuristic is value equality, not file presence: a
        DATABASE_URL different from the cwd dotenv value is still effective
        and fails closed with no native calls."""
        self._make_healthy_state()
        dotenv_dir = self._dotenv_fixture({
            ".env": "DATABASE_URL=postgres://file-value\n",
        })
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, cwd=dotenv_dir, DATABASE_URL="postgres://env-value")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_database_url_effective", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    # --- exact pinned dotenv parser semantics (issue #132 Gate 1) ---

    def test_dotenv_lf_only_lone_cr_is_not_a_line_separator(self) -> None:
        """LF-only semantics: a lone CR (no LF) is NOT a line separator, so
        the whole blob is one assignment whose value cannot match the env
        DATABASE_URL (splitlines would have split at the CR and matched)."""
        self._make_healthy_state()
        dotenv_dir = self._dotenv_fixture({
            ".env": "DATABASE_URL=postgres://a\rpostgres://b",
        })
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, cwd=dotenv_dir, DATABASE_URL="postgres://a")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_database_url_effective", result.stdout)
        self.assertEqual(fake.calls(), [],
                         "effective DATABASE_URL must refuse before native calls")

    def test_dotenv_inline_comment_truncation_trims_remainder(self) -> None:
        """Multiple spaces before an inline comment: the remainder after the
        first ' #' is truncated AND trimmed, so the matched value has no
        trailing spaces and the env DATABASE_URL is accepted on healthy
        state."""
        self._make_healthy_state()
        dotenv_dir = self._dotenv_fixture({
            ".env": "DATABASE_URL=postgres://v  # note\n",
        })
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, cwd=dotenv_dir, DATABASE_URL="postgres://v")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        calls = fake.calls()
        self.assertIn("init --migrate-only", calls)
        self.assertNotIn("init --pglite", " ".join(calls))

    def test_dotenv_quoted_value_whole_quote_removal(self) -> None:
        """A whole-value quoted assignment matches the env DATABASE_URL on
        healthy state (quote removal, no comment stripping inside quotes)."""
        self._make_healthy_state()
        dotenv_dir = self._dotenv_fixture({
            ".env": "DATABASE_URL='postgres://q'\n",
        })
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, cwd=dotenv_dir, DATABASE_URL="postgres://q")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = fake.calls()
        self.assertIn("init --migrate-only", calls)
        self.assertNotIn("init --pglite", " ".join(calls))

    def test_dotenv_export_prefix_matched(self) -> None:
        """An `export DATABASE_URL=...` assignment matches the env value on
        healthy state."""
        self._make_healthy_state()
        dotenv_dir = self._dotenv_fixture({
            ".env": "export DATABASE_URL=postgres://e\n",
        })
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, cwd=dotenv_dir, DATABASE_URL="postgres://e")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = fake.calls()
        self.assertIn("init --migrate-only", calls)
        self.assertNotIn("init --pglite", " ".join(calls))

    def test_dotenv_empty_assignment_ignored(self) -> None:
        """Empty assignments are ignored: the union stays empty, so an env
        DATABASE_URL is effective and refuses before native calls."""
        self._make_healthy_state()
        dotenv_dir = self._dotenv_fixture({
            ".env": "DATABASE_URL=\nDATABASE_URL=   \n",
        })
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, cwd=dotenv_dir, DATABASE_URL="postgres://env")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gbrain_state_database_url_effective", result.stdout)
        self.assertEqual(fake.calls(), [], "no native gbrain call may run")

    def test_dotenv_all_five_files_union_matched(self) -> None:
        """All five fixed dotenv files are parsed and their values unioned: a
        value from .env.production matches even when the other files hold
        different values, so the env DATABASE_URL is accepted on healthy
        state."""
        self._make_healthy_state()
        dotenv_dir = self._dotenv_fixture({
            ".env": "DATABASE_URL=postgres://one\n",
            ".env.local": "DATABASE_URL=postgres://two\n",
            ".env.development": "DATABASE_URL=postgres://three\n",
            ".env.production": "DATABASE_URL=postgres://four\n",
            ".env.test": "DATABASE_URL=postgres://five\n",
        })
        fake = FakeGbrain(self.tmp)
        result = self.run_reindex(fake, cwd=dotenv_dir, DATABASE_URL="postgres://four")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = fake.calls()
        self.assertIn("init --migrate-only", calls)
        self.assertNotIn("init --pglite", " ".join(calls))

    # --- trusted classification (issue #132 Gate 1) ---

    def test_init_selection_exclusively_from_validated_preflight_state(self) -> None:
        """The validated preflight state variable alone selects the init
        vector: with a custom schema pack, the native `schema validate` runs
        BETWEEN the preflight and the init choice; the fake gbrain deletes
        config.json at that instant. A filesystem reclassification would now
        see a missing config and choose FRESH; the validated variable must
        still choose EXISTING (migrate-only)."""
        self._make_healthy_state()
        pack_dir = self.tmp / "state" / ".gbrain" / "schema-packs" / "custom"
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "pack.yaml").write_text("name: custom\n", encoding="utf-8")
        fake = FakeGbrain(self.tmp, on_schema_validate=f"rm -f {self.config_path}")
        result = self.run_reindex(fake, GBRAIN_SCHEMA_PACK="custom")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("schema validate custom", fake.calls())
        self.assertFalse(self.config_path.exists(),
                         "the schema-validate hook must have removed config.json")
        calls = fake.calls()
        self.assertIn("init --migrate-only", calls,
                      "init must be chosen from the validated preflight state")
        self.assertNotIn("init --pglite", " ".join(calls),
                         "a filesystem reclassification must not switch to fresh")
        self.assertFalse(
            any(c.startswith("config set search.mcp_keyword_only") for c in calls),
            "the existing branch must not write keyword mode",
        )


if __name__ == "__main__":
    unittest.main()
