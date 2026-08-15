"""Regression tests for the rclone OAuth-refresh runtime fix.

The published `obsidian-rclone-config` seed stays READ-ONLY; every rclone
consumer runs against a PRIVATE WRITABLE ACTIVE copy of the config
(scripts/rclone-active-config.sh):

  - long-running uploaders seed the active copy into their own state volume
    (persistent across container restarts),
  - short-lived recover steps use an ephemeral private temp dir (never the
    recovery handoff volume).

The active copy is PRESERVED while the seed is unchanged — rclone may have
rewritten it in place to persist a refreshed OAuth token, and reseeding
would discard that refresh — and atomically RESEEDED when the seed changes
(operator rotation). The seed-unchanged test uses a private recorded
fingerprint of the seed, never the active config and never printed output.

Covers (no Docker, fake rclone on PATH):
  - helper: 0600 atomic seeding, fingerprint sidecar, refresh preservation,
    rotation reseed, active-copy recreation, missing-seed fail-closed,
    no content/hash leakage, no temp leftovers;
  - uploader: seeds the active copy and runs EVERY rclone call against it,
    restart retention of a simulated rclone config rewrite, reseed on seed
    rotation, seed immutability;
  - recover: ephemeral private config (never the recovery handoff volume),
    cleanup on exit, seed immutability.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from .phase2_helpers import (
        FakeRcloneFixture,
        RECOVER_SCRIPT,
        UPLOADER_SCRIPT,
        make_generation,
        recover_env_for,
        seed_remote_committed,
        uploader_env_for,
    )
except ImportError:  # discover -s tests/vault_recovery imports top-level
    from phase2_helpers import (  # type: ignore
        FakeRcloneFixture,
        RECOVER_SCRIPT,
        UPLOADER_SCRIPT,
        make_generation,
        recover_env_for,
        seed_remote_committed,
        uploader_env_for,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_SCRIPT = REPO_ROOT / "scripts" / "rclone-active-config.sh"
ACTIVE_NAME = "rclone.active.conf"
FP_SUFFIX = ".seed-fp"

# Secret-shaped markers: tests assert these NEVER appear in script output
# (the helper must not print config content or hashes) and use them to
# detect simulated refreshes/rotations in the config files.
ORIGINAL_SECRET = "ORIGINAL_SECRET_9f2c1a"
REFRESHED_SECRET = "REFRESHED_TOKEN_9f2c1a"
ROTATED_SECRET = "rotated-fixture"


def _seed_config(secret: str = ORIGINAL_SECRET) -> str:
    return (
        "[vault-crypt]\n"
        "type = crypt\n"
        "remote = local:/underlying\n"
        f"password = {secret}\n"
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _run_helper(
    seed: Path,
    active: Path,
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the helper in a /bin/sh subprocess (the scripts source it the
    same way) and print the exported RCLONE_CONFIG* variables."""
    env = {
        **os.environ,
        "HELPER": str(HELPER_SCRIPT),
        "SEED": str(seed),
        "ACTIVE": str(active),
        **(env_extra or {}),
    }
    script = (
        '. "$HELPER"\n'
        'log_info() { echo "helper-info: $1"; }\n'
        'log_error() { echo "helper-error: $1" >&2; }\n'
        'rclone_active_config_ensure "$SEED" "$ACTIVE"\n'
        'printf "RCLONE_CONFIG=%s\\n" "$RCLONE_CONFIG"\n'
        'printf "RCLONE_CONFIG_FILE=%s\\n" "$RCLONE_CONFIG_FILE"\n'
    )
    return subprocess.run(
        ["/bin/sh", "-c", script],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _assert_no_leak(proc: subprocess.CompletedProcess[str], *markers: str) -> None:
    """The helper/scripts must never print config content (secret markers).

    NOTE: this intentionally checks only the secret markers, not generic
    64-hex hashes: the uploader/recover scripts' own pre-existing logs print
    the MANIFEST sha256 (not config hashes). The helper-level tests assert
    the recorded seed fingerprint hash is never printed separately."""
    output = f"{proc.stdout}\n{proc.stderr}"
    for marker in markers:
        assert marker not in output, f"secret marker leaked into output: {marker}"


class RcloneActiveConfigHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-active-cfg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.seed = self.tmp / "seed" / "rclone.conf"
        self.seed.parent.mkdir()
        self.seed.write_text(_seed_config(), encoding="utf-8")
        os.chmod(self.seed, 0o600)
        self.active = self.tmp / "state" / ACTIVE_NAME

    def _run(self, **env_extra) -> subprocess.CompletedProcess[str]:
        return _run_helper(self.seed, self.active, env_extra)

    def test_seeds_private_0600_active_copy(self) -> None:
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.active.exists(), "active config not created")
        self.assertEqual(self.active.read_text("utf-8"), _seed_config())
        self.assertEqual(_mode(self.active), 0o600, "active config must be 0600")
        # The exported RCLONE_CONFIG* point at the active copy, not the seed.
        self.assertIn(f"RCLONE_CONFIG={self.active}", proc.stdout)
        self.assertIn(f"RCLONE_CONFIG_FILE={self.active}", proc.stdout)
        # Seed fingerprint sidecar recorded privately (0600, not printed).
        fp = Path(str(self.active) + FP_SUFFIX)
        self.assertTrue(fp.exists(), "seed fingerprint sidecar not recorded")
        self.assertEqual(_mode(fp), 0o600)
        self.assertEqual(fp.read_text("utf-8").strip(), _sha256_file(self.seed))
        self.assertNotIn(_sha256_file(self.seed), proc.stdout)
        self.assertNotIn(_sha256_file(self.seed), proc.stderr)
        self.assertEqual(self.seed.read_text("utf-8"), _seed_config(),
                         "seed must never be modified")
        _assert_no_leak(proc, ORIGINAL_SECRET)

    def test_preserves_refreshed_active_when_seed_unchanged(self) -> None:
        # First run seeds the active copy.
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        # Simulate rclone persisting an OAuth token refresh: it rewrites the
        # ACTIVE config in place (the whole point of the fix).
        self.active.write_text(
            _seed_config(REFRESHED_SECRET) + 'token = {"refresh_token": "r"}\n',
            encoding="utf-8",
        )
        os.chmod(self.active, 0o600)
        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        # Seed unchanged -> the refreshed active copy MUST be preserved.
        self.assertIn(REFRESHED_SECRET, self.active.read_text("utf-8"),
                      "refresh must survive while the seed is unchanged")
        self.assertIn("preserved", second.stdout)
        self.assertEqual(self.seed.read_text("utf-8"), _seed_config(),
                         "seed must never be modified")
        _assert_no_leak(second, REFRESHED_SECRET, ORIGINAL_SECRET)

    def test_reseeds_atomically_when_seed_rotated(self) -> None:
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        # Simulate a refresh so the active copy differs from the OLD seed.
        self.active.write_text(_seed_config(REFRESHED_SECRET), encoding="utf-8")
        os.chmod(self.active, 0o600)
        # Operator rotates the published seed.
        self.seed.write_text(_seed_config(ROTATED_SECRET), encoding="utf-8")
        os.chmod(self.seed, 0o600)
        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        # Seed changed -> the active copy is reseeded with the NEW seed; the
        # stale refreshed token must be gone.
        self.assertEqual(self.active.read_text("utf-8"), _seed_config(ROTATED_SECRET))
        self.assertNotIn(REFRESHED_SECRET, self.active.read_text("utf-8"))
        fp = Path(str(self.active) + FP_SUFFIX)
        self.assertEqual(fp.read_text("utf-8").strip(), _sha256_file(self.seed))
        # Atomicity: no temp/staging leftovers in the active directory.
        leftovers = [p.name for p in self.active.parent.iterdir() if p != self.active and p != fp]
        self.assertEqual(leftovers, [], f"staging leftovers after reseed: {leftovers}")
        # The rotated seed itself is never modified.
        self.assertEqual(self.seed.read_text("utf-8"), _seed_config(ROTATED_SECRET))
        _assert_no_leak(second, REFRESHED_SECRET, ROTATED_SECRET, ORIGINAL_SECRET)

    def test_recreates_active_copy_when_deleted_but_seed_unchanged(self) -> None:
        self._run()
        # The active copy disappears (e.g. manual tampering); the seed is
        # unchanged. The next run must recreate the active copy from the seed.
        self.active.unlink()
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.active.exists())
        self.assertEqual(self.active.read_text("utf-8"), _seed_config())
        self.assertEqual(self.seed.read_text("utf-8"), _seed_config())

    def test_missing_seed_fails_closed(self) -> None:
        missing = self.tmp / "missing" / "rclone.conf"
        proc = _run_helper(missing, self.active)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("seed not found", proc.stderr)
        self.assertFalse(self.active.exists(), "no active copy on missing seed")
        self.assertFalse(Path(str(self.active) + FP_SUFFIX).exists())

    def test_unchanged_seed_then_rotation_is_stable_across_many_runs(self) -> None:
        # A refresh must survive ANY number of unchanged-seed runs, and a
        # rotation must stick (no oscillation).
        for _ in range(3):
            proc = self._run()
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.active.write_text(_seed_config(REFRESHED_SECRET), encoding="utf-8")
        os.chmod(self.active, 0o600)
        for _ in range(2):
            proc = self._run()
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(REFRESHED_SECRET, self.active.read_text("utf-8"))
        self.seed.write_text(_seed_config(ROTATED_SECRET), encoding="utf-8")
        os.chmod(self.seed, 0o600)
        self._run()
        self.assertEqual(self.active.read_text("utf-8"), _seed_config(ROTATED_SECRET))
        self._run()
        self.assertEqual(self.active.read_text("utf-8"), _seed_config(ROTATED_SECRET))


class UploaderActiveConfigTests(unittest.TestCase):
    """The vault-recovery uploader seeds the active copy into its state
    volume and runs EVERY rclone invocation against it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-uploader-active-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gen_id, self.staging = make_generation(self.tmp)
        self.state = self.tmp / "state"
        self.fixture = FakeRcloneFixture(self.tmp, {
            "vault-crypt": {
                "type": "crypt",
                "remote": "local:/underlying",
                "password": ORIGINAL_SECRET,
            }
        })
        self.active = self.state / ACTIVE_NAME

    def _run(self, **over) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(UPLOADER_SCRIPT)],
            env=uploader_env_for(self.fixture, self.staging, self.state, **over),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _assert_all_rclone_calls_use_active(self) -> None:
        entries = self.fixture.log_entries()
        self.assertTrue(entries, "no rclone calls recorded")
        for entry in entries:
            self.assertEqual(
                entry["config"], str(self.active),
                f"rclone {entry['cmd']} must run against the ACTIVE config, "
                f"not the read-only seed: {entry}",
            )

    def test_seeds_active_config_and_runs_rclone_against_it(self) -> None:
        seed_before = self.fixture.config_file.read_bytes()
        proc = self._run()
        self.assertEqual(proc.returncode, 0,
                         f"uploader failed:\n{proc.stdout}\n{proc.stderr}")
        # Active copy seeded in the state volume with the seed content, 0600.
        self.assertTrue(self.active.exists())
        self.assertEqual(self.active.read_bytes(), seed_before)
        self.assertEqual(_mode(self.active), 0o600)
        fp = Path(str(self.active) + FP_SUFFIX)
        self.assertTrue(fp.exists())
        # Every rclone call (config show, copy, move, lsjson...) used the
        # active copy — the seed was never handed to rclone.
        self._assert_all_rclone_calls_use_active()
        # Seed immutability.
        self.assertEqual(self.fixture.config_file.read_bytes(), seed_before)
        _assert_no_leak(proc, ORIGINAL_SECRET)

    def test_restart_retention_preserves_simulated_rclone_rewrite(self) -> None:
        first = self._run()
        self.assertEqual(first.returncode, 0,
                         f"first run failed:\n{first.stdout}\n{first.stderr}")
        # Simulate rclone rewriting the ACTIVE config to persist an OAuth
        # token refresh (in-place, exactly what rclone does on refresh).
        refreshed = (
            "[vault-crypt]\n"
            "type = crypt\n"
            "remote = local:/underlying\n"
            f"password = {REFRESHED_SECRET}\n"
            'token = {"access_token": "a", "refresh_token": "r"}\n'
        )
        self.active.write_text(refreshed, encoding="utf-8")
        os.chmod(self.active, 0o600)
        # "Restart": a fresh uploader process over the same state volume.
        second = self._run()
        self.assertEqual(second.returncode, 0,
                         f"restart run failed:\n{second.stdout}\n{second.stderr}")
        # The seed is unchanged -> the refreshed active copy must survive.
        self.assertEqual(self.active.read_text("utf-8"), refreshed,
                         "refreshed active config must survive the restart")
        self.assertIn(REFRESHED_SECRET, self.active.read_text("utf-8"))
        # rclone kept using the ACTIVE copy (which now holds the refresh).
        self._assert_all_rclone_calls_use_active()
        # Seed immutability: the published seed bytes never changed.
        self.assertEqual(self.fixture.config_file.read_text("utf-8"),
                         _seed_config(ORIGINAL_SECRET))
        _assert_no_leak(second, REFRESHED_SECRET, ORIGINAL_SECRET)

    def test_reseeds_active_config_when_seed_rotated(self) -> None:
        first = self._run()
        self.assertEqual(first.returncode, 0,
                         f"first run failed:\n{first.stdout}\n{first.stderr}")
        # Operator rotates the published seed (new secret, still crypt).
        self.fixture._write_config({
            "vault-crypt": {
                "type": "crypt",
                "remote": "local:/underlying",
                "password": ROTATED_SECRET,
            }
        })
        second = self._run()
        self.assertEqual(second.returncode, 0,
                         f"rotated run failed:\n{second.stdout}\n{second.stderr}")
        # Active copy must now be the rotated seed; the old secret is gone.
        self.assertEqual(self.active.read_text("utf-8"),
                         _seed_config(ROTATED_SECRET))
        self.assertNotIn(ORIGINAL_SECRET, self.active.read_text("utf-8"))
        fp = Path(str(self.active) + FP_SUFFIX)
        self.assertEqual(fp.read_text("utf-8").strip(), _sha256_file(self.fixture.config_file))
        # rclone ran against the reseeded active copy throughout.
        self._assert_all_rclone_calls_use_active()
        _assert_no_leak(second, ORIGINAL_SECRET, ROTATED_SECRET)


class RecoverActiveConfigTests(unittest.TestCase):
    """The recover step uses an EPHEMERAL PRIVATE writable config in a fresh
    temp dir — never the recovery handoff volume — removed on exit."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-recover-active-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gen_id, self.staging = make_generation(self.tmp)
        self.recovery = self.tmp / "recovery"
        self.fixture = FakeRcloneFixture(self.tmp, {
            "vault-crypt": {
                "type": "crypt",
                "remote": "local:/underlying",
                "password": ORIGINAL_SECRET,
            }
        })
        self.ephemeral_root = self.tmp / "ephemeral-root"
        self.ephemeral_root.mkdir()
        seed_remote_committed(self.fixture, self.gen_id, self.staging)

    def _run(self, *args, **over) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(RECOVER_SCRIPT), *args],
            env=recover_env_for(
                self.fixture, self.recovery,
                TMPDIR=str(self.ephemeral_root), **over,
            ),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _ephemeral_leftovers(self) -> list[Path]:
        if not self.ephemeral_root.exists():
            return []
        return list(self.ephemeral_root.glob("vault-recovery-rclone.*"))

    def _assert_ephemeral_config_use(self) -> None:
        entries = self.fixture.log_entries()
        self.assertTrue(entries)
        for entry in entries:
            cfg = entry["config"]
            self.assertIsNotNone(cfg, f"rclone call without --config: {entry}")
            assert cfg is not None
            cfg_path = Path(cfg)
            self.assertTrue(
                str(cfg_path).startswith(str(self.ephemeral_root / "vault-recovery-rclone.")),
                f"rclone config must be the ephemeral private copy: {entry}",
            )
            self.assertEqual(cfg_path.name, "rclone.conf")
            self.assertNotIn(str(self.recovery), str(cfg_path),
                             "config must never live in the recovery handoff volume")
        # No config file anywhere in the handoff volume.
        self.assertEqual(
            [p for p in self.recovery.rglob("rclone.conf")], [],
            "no rclone config may leak into the recovery handoff volume",
        )

    def test_download_uses_ephemeral_private_config_and_cleans_up(self) -> None:
        seed_before = self.fixture.config_file.read_bytes()
        proc = self._run("download", self.gen_id)
        self.assertEqual(proc.returncode, 0,
                         f"recover failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertTrue((self.recovery / "RECOVERY_READY").exists())
        self._assert_ephemeral_config_use()
        # The ephemeral private config dir is removed on exit (trap).
        self.assertEqual(self._ephemeral_leftovers(), [],
                         "ephemeral config dir must be removed on exit")
        # Seed immutability.
        self.assertEqual(self.fixture.config_file.read_bytes(), seed_before)
        _assert_no_leak(proc, ORIGINAL_SECRET)

    def test_list_remote_uses_ephemeral_private_config_and_cleans_up(self) -> None:
        seed_before = self.fixture.config_file.read_bytes()
        proc = self._run("list-remote")
        self.assertEqual(proc.returncode, 0,
                         f"list-remote failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn(self.gen_id, proc.stdout)
        self._assert_ephemeral_config_use()
        self.assertEqual(self._ephemeral_leftovers(), [])
        self.assertEqual(self.fixture.config_file.read_bytes(), seed_before)
        _assert_no_leak(proc, ORIGINAL_SECRET)


if __name__ == "__main__":
    unittest.main()
