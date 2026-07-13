from __future__ import annotations

import os
import unittest

from .helpers import ComposeRuntime, docker_available


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

    def test_backup_vault_mount_is_read_only_but_state_is_writable(self) -> None:
        read_only = self.runtime.run(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "sh",
            "obsidian-backup",
            "-lc",
            "touch /data/obsidian/should-not-write && exit 99",
            check=False,
            timeout=180,
        )
        self.assertNotEqual(read_only.returncode, 0)

        self.runtime.run(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "sh",
            "obsidian-backup",
            "-lc",
            "touch /state/runtime-permission-test",
            timeout=180,
        )


if __name__ == "__main__":
    unittest.main()
