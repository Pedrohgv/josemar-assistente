from __future__ import annotations

import os
import subprocess
import unittest

from .helpers import (
    ComposeRuntime,
    HERMES_WRITABLE_PROBE_PATHS,
    REPO_ROOT,
    TEST_ISOLATION_OVERLAY,
    docker_available,
    hermes_writable_probe_command,
)


VAULT_RECOVERY_OVERLAY = REPO_ROOT / "docker-compose.vault-recovery.yml"


@unittest.skipUnless(os.getenv("RUN_DOCKER_TESTS") == "1", "set RUN_DOCKER_TESTS=1 to run Docker runtime tests")
@unittest.skipUnless(docker_available(), "docker CLI is not available")
class DockerPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = ComposeRuntime()

    def tearDown(self) -> None:
        self.runtime.down()

    def _activate_gbrain_brain(self) -> None:
        """Initialize the gbrain brain exactly like the first production
        activation, as the hermes runtime user.

        The container init does NOT auto-activate gbrain: production relies
        on the agent-state workspace sync seeding the josemar schema pack
        into /opt/data/.gbrain/schema-packs/ and on the operator running
        `josemar-gbrain reindex` once. The disposable test runtime has sync
        disabled and a fresh obsidian-vault volume, so this method seeds the
        same prerequisites (a minimal VALID josemar pack — the repo's
        checked-in packs predate the gbrain manifest v1 api_version/name/
        version contract — plus a git-initialized vault with one commit, as
        native gbrain sync requires) and then runs the operator activation,
        which exercises the wrapper lock, native init/config/sync and the
        volume permissions exactly like the production first activation.
        """
        seed = (
            "set -eu; "
            "su -s /bin/sh hermes -c '"
            "mkdir -p /opt/data/.gbrain/schema-packs/josemar && "
            "printf \"api_version: gbrain-schema-pack-v1\\n"
            "name: josemar\\n"
            "version: 1.0.0\\n"
            "extends: gbrain-base-v2\\n"
            "page_types: []\\n\" "
            "> /opt/data/.gbrain/schema-packs/josemar/pack.yaml && "
            "git -C /opt/data/obsidian init -q -b main && "
            "git -C /opt/data/obsidian config user.email gbrain-test && "
            "git -C /opt/data/obsidian config user.name gbrain-test && "
            "touch /opt/data/obsidian/.gitkeep && "
            "git -C /opt/data/obsidian add -A && "
            "git -C /opt/data/obsidian commit -q -m init"
            "'"
        )
        self.runtime.exec("hermes", "sh", "-lc", seed, timeout=120)
        activation = self.runtime.exec(
            "hermes",
            "sh",
            "-lc",
            "su -s /bin/sh hermes -c 'josemar-gbrain reindex'",
            timeout=300,
        )
        self.assertIn(
            '"success": true', activation.stdout, activation.stderr
        )

    def test_hermes_runtime_user_can_write_required_volumes_and_run_gbrain(self) -> None:
        self.runtime.up("hermes")
        # `up -d` returns when the container STARTS; the init chown of the
        # root-owned named volumes (HERMES_HOME, /shared, vault-recovery
        # staging) runs asynchronously during cont-init, and the obsidian
        # vault's ownership comes from the image copy, not the init. Wait
        # for the exact writable state of EVERY path this test writes
        # before probing, so the exec below never races the init
        # (Permission denied on /shared or /opt/data/obsidian).
        self.runtime.wait_until_hermes_writable()

        # The gbrain brain is not part of the container init; activate it
        # first (seed + operator `josemar-gbrain reindex`) so the CLI
        # checks below run against a real initialized brain.
        self._activate_gbrain_brain()

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

        # Verify the gbrain CLI is installed and executable: the public
        # issue #110 adapter (root drop + shared lock) must run as the
        # hermes runtime user against the activated brain. `gbrain status`
        # prints the human-readable health dashboard (exit 0 proves the
        # adapter + PGLite brain are reachable; exec raises on non-zero).
        process = self.runtime.exec(
            "hermes",
            "sh",
            "-lc",
            "su -s /bin/sh hermes -c 'gbrain status'",
        )
        self.assertIn("GBrain Status", process.stdout)

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
        # NOTE: `docker compose run` does NOT pass the CLI environment into
        # the container (service env only, -e overrides it), and the
        # vault-recovery overlay pins a LITERAL remote name; the `-e
        # VAULT_RECOVERY_RCLONE_REMOTE=` flag therefore forces the
        # missing-remote branch the contract asserts.
        proc = subprocess.run(
            self._uploader_command(
                "run", "--rm", "--no-deps",
                "-e", "VAULT_RECOVERY_ONCE=true",
                "-e", "VAULT_RECOVERY_RCLONE_REMOTE=",
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


class HermesWritableReadinessTests(unittest.TestCase):
    """No-docker regression for the readiness gate: the probe must cover
    EVERY path the runtime contract depends on being hermes-writable —
    HERMES_HOME, the /shared handoff, the obsidian vault (written by the
    permission test immediately after the wait; a fresh named volume's
    ownership is inherited from the image copy, never repaired by the init
    allowlist) and the vault-recovery staging dir. A narrower probe would
    let the permission test race into a Permission-denied exec."""

    def test_probe_paths_match_the_runtime_contract(self) -> None:
        self.assertEqual(
            HERMES_WRITABLE_PROBE_PATHS,
            (
                "/opt/data",
                "/shared",
                "/opt/data/obsidian",
                "/opt/data/vault-recovery/staging",
            ),
        )

    def test_probe_command_touches_and_cleans_every_contract_path(self) -> None:
        probe = hermes_writable_probe_command()
        for path in HERMES_WRITABLE_PROBE_PATHS:
            # Each path must be write-probed ...
            self.assertIn(f"{path}/.runtime-perm-probe", probe, path)
            # ... and each probe file must be removed in the same command.
        self.assertIn(
            "rm -f "
            + " ".join(
                f"{path}/.runtime-perm-probe" for path in HERMES_WRITABLE_PROBE_PATHS
            ),
            probe,
        )
        # The probe must run as the hermes runtime user, never root.
        self.assertIn("su -s /bin/sh hermes -c", probe)

    def test_permission_test_write_paths_are_all_probed(self) -> None:
        # Every path the permission test writes right after the wait must
        # be covered by the readiness probe.
        for path in ("/opt/data", "/shared", "/opt/data/obsidian"):
            self.assertIn(path, HERMES_WRITABLE_PROBE_PATHS)


if __name__ == "__main__":
    unittest.main()
