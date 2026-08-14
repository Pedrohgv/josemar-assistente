from __future__ import annotations

import os
import subprocess
import unittest

from .helpers import (
    ComposeRuntime,
    TEST_ISOLATION_OVERLAY,
    docker_available,
    REPO_ROOT,
)


VAULT_RECOVERY_OVERLAY = REPO_ROOT / "docker-compose.vault-recovery.yml"


@unittest.skipUnless(os.getenv("RUN_DOCKER_TESTS") == "1", "set RUN_DOCKER_TESTS=1 to run Docker runtime tests")
@unittest.skipUnless(docker_available(), "docker CLI is not available")
class DockerPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = ComposeRuntime()

    def tearDown(self) -> None:
        self.runtime.down()

    def test_hermes_runtime_user_can_write_required_volumes_and_run_gbrain(self) -> None:
        self.runtime.up("hermes")

        script = (
            "set -eu; "
            "su -s /bin/sh hermes -c '"
            "touch /opt/data/runtime-permission-test "
            "&& touch /shared/runtime-permission-test "
            "&& mkdir -p /opt/data/obsidian "
            "&& touch /opt/data/obsidian/runtime-permission-test"
            "'"
        )
        self.runtime.exec("hermes", "sh", "-lc", script)

        # Verify the gbrain CLI is installed and executable.
        process = self.runtime.exec(
            "hermes",
            "sh",
            "-lc",
            "su -s /bin/sh hermes -c 'gbrain status'",
        )
        self.assertIn('"success"', process.stdout)

        logs = self.runtime.logs("hermes")
        self.assertNotIn("cannot write to /opt/data", logs)
        self.assertNotIn("cannot write to /shared", logs)
        self.assertNotIn("cannot write to /opt/data/obsidian", logs)

    def _uploader_command(self, *args: str) -> list[str]:
        """Compose command with base + vault-recovery overlay + test isolation.

        compose_command() returns [docker, compose, -f, base, -f, isolation,
        -p, project]; the vault-recovery overlay is inserted between the
        base file and the test-isolation overlay (isolation must stay LAST
        so its disposable bind mounts win)."""
        command = self.runtime.compose_command()
        command.insert(4, "-f")
        command.insert(5, str(VAULT_RECOVERY_OVERLAY))
        command.extend(args)
        return command

    def test_uploader_refuses_to_run_without_crypt_remote(self) -> None:
        # Phase 3 fail-closed contract: the default-lane uploader exits 2
        # (config error) when VAULT_RECOVERY_RCLONE_REMOTE is missing or the
        # crypt config is unavailable — it never degrades to unencrypted or
        # silently skips uploads.
        proc = subprocess.run(
            self._uploader_command(
                "run", "--rm", "--no-deps", "-e", "VAULT_RECOVERY_ONCE=true",
                "vault-recovery-uploader",
            ),
            cwd=REPO_ROOT, env=self.runtime.env, capture_output=True, text=True,
            check=False, timeout=300,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("VAULT_RECOVERY_RCLONE_REMOTE is required", proc.stderr)

    def test_uploader_mount_boundaries(self) -> None:
        # Staging and the rclone config are READ-ONLY; only the uploader's own
        # state volume is writable — plus ONE strictly-bounded exception: the
        # SAME staging volume mounted read-WRITE at /staging-prune for
        # ack-based local retention. No hermes-data / opt/data mounts at all.
        read_only_staging = subprocess.run(
            self._uploader_command(
                "run", "--rm", "--no-deps", "--entrypoint", "sh",
                "vault-recovery-uploader",
                "-lc", "touch /staging/should-not-write && exit 99",
            ),
            cwd=REPO_ROOT, env=self.runtime.env, capture_output=True, text=True,
            check=False, timeout=300,
        )
        self.assertNotEqual(read_only_staging.returncode, 0)

        read_only_config = subprocess.run(
            self._uploader_command(
                "run", "--rm", "--no-deps", "--entrypoint", "sh",
                "vault-recovery-uploader",
                "-lc", "touch /config/rclone/should-not-write && exit 99",
            ),
            cwd=REPO_ROOT, env=self.runtime.env, capture_output=True, text=True,
            check=False, timeout=300,
        )
        self.assertNotEqual(read_only_config.returncode, 0)

        writable_state = subprocess.run(
            self._uploader_command(
                "run", "--rm", "--no-deps", "--entrypoint", "sh",
                "vault-recovery-uploader",
                "-lc", "touch /state/runtime-permission-test && test -f /state/runtime-permission-test",
            ),
            cwd=REPO_ROOT, env=self.runtime.env, capture_output=True, text=True,
            check=False, timeout=300,
        )
        self.assertEqual(writable_state.returncode, 0, writable_state.stdout + writable_state.stderr)

        # The bounded local-retention mount (/staging-prune) IS writable —
        # the uploader prunes acked staged generations through it.
        writable_prune = subprocess.run(
            self._uploader_command(
                "run", "--rm", "--no-deps", "--entrypoint", "sh",
                "vault-recovery-uploader",
                "-lc", "touch /staging-prune/runtime-permission-test && rm -f /staging-prune/runtime-permission-test",
            ),
            cwd=REPO_ROOT, env=self.runtime.env, capture_output=True, text=True,
            check=False, timeout=300,
        )
        self.assertEqual(writable_prune.returncode, 0, writable_prune.stdout + writable_prune.stderr)


if __name__ == "__main__":
    unittest.main()
