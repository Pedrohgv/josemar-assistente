"""Docker-gated regression for the uploader's KERNEL-RELEASED flock.

The upload lock (scripts/vault-recovery-uploader.sh) is a non-blocking
exclusive flock(1) held on a dedicated file descriptor (fd 9) of a regular
file (`.upload.flock`) in the state volume. The kernel releases the lock
automatically when the owning process dies — even on whole-container SIGKILL
(`docker kill -s SIGKILL`, e.g. during a deploy) — so a dead container can
never leave a stale lease that blocks future starts. The legacy mkdir lease
(`.upload.lock` directory) is gone.

This regression proves the flock lifecycle in the ACTUAL pinned rclone
runtime image (digest-pinned, busybox flock — NOT the host's flock), with
the REAL named volumes from the vault-recovery overlay:

  1. FD-based exclusive flock works: the uploader's acquire path
     (`exec 9>... ; flock -n 9`) succeeds in the pinned image, and a live
     holder holding the same kernel lock on the same lock FILE rejects a
     concurrent uploader run with exit 1 and a visible error (fail closed,
     no state advance).
  2. Whole-container SIGKILL releases it: the holder container is killed
     with `docker kill -s SIGKILL` (no cleanup, no traps can run); the
     kernel drops the flock with the dead process.
  3. A restart against the SAME state volume reacquires successfully: the
     very next uploader run succeeds and the lock file (never deleted) is
     still a regular file.
  4. Fail closed without flock(1): a runtime whose PATH cannot resolve
     `flock` exits 2 before opening any lock file, instead of falling back
     to a lease that could survive process death.

Local runs skip unless RUN_DOCKER_TESTS=1 and the docker CLI is available.
Never uses production volumes, credentials, or remotes: the stack renders
with the sanitized test env (helpers.sanitized_test_env) + disposable
env-file, and only the project-prefixed named volumes are touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path

from .helpers import REPO_ROOT, sanitized_test_env, write_disposable_env_file

BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
VAULT_RECOVERY_OVERLAY = REPO_ROOT / "docker-compose.vault-recovery.yml"
TAILSCALE_ISOLATION_OVERLAY = (
    REPO_ROOT / "tests" / "runtime" / "docker-compose.test-tailscale-isolation.yml"
)
RCLONE_IMAGE = (
    "rclone/rclone@sha256:b06aed988cf5967de7c25be5925240983981c757f4ed1ac9d2fa659d51d60548"
)

# Tools the uploader needs to REACH acquire_upload_lock (startup validation
# + active-config seeding + crypt remote check), mapped to their absolute
# paths INSIDE the pinned image. The fail-closed run mounts a farm of these
# symlinks (minus flock) as the whole PATH: `command -v flock` then fails
# and the uploader must exit 2 without ever opening the lock file.
IMAGE_TOOL_TARGETS = {
    "awk": "/bin/busybox",
    "cat": "/bin/busybox",
    "chmod": "/bin/busybox",
    "cp": "/bin/busybox",
    "cut": "/bin/busybox",
    "dirname": "/bin/busybox",
    "mkdir": "/bin/busybox",
    "mv": "/bin/busybox",
    "rm": "/bin/busybox",
    "sha256sum": "/bin/busybox",
    "rclone": "/usr/local/bin/rclone",
}


def _docker_enabled() -> bool:
    if os.getenv("RUN_DOCKER_TESTS") != "1":
        return False
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=30, check=False
        )
        return True
    except Exception:
        return False


@unittest.skipUnless(_docker_enabled(), "set RUN_DOCKER_TESTS=1 with a docker CLI for the flock regression")
class UploaderFlockLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        token = uuid.uuid4().hex[:12]
        self.project = f"josemar-vr-flock-{token}"
        self.holder_name = f"{self.project}-flock-holder"
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-flock-"))
        self.underlying = self.tmp / "underlying"
        self.underlying.mkdir()
        self.volume_names = {
            "obsidian-rclone-config": f"{self.project}-rclone-config",
            "vault-recovery-staging": f"{self.project}-vr-staging",
            "vault-recovery-uploader-state": f"{self.project}-vr-uploader-state",
        }
        # Fail-closed test env (security hardening): the CENTRALIZED
        # sanitizer (helpers.sanitized_test_env) blanks every production-
        # influencing key and removes the Compose selector vars; the
        # disposable env-file (pinned via --env-file) replaces the repo
        # `.env` as a second independent layer. Only deterministic test
        # values are set here (same skeleton as the round-trip test).
        self.env = sanitized_test_env()
        self.env.update(
            {
                "COMPOSE_PROJECT_NAME": self.project,
                "JOSEMAR_CONTAINER_PREFIX": self.project,
                "HERMES_DASHBOARD_SESSION_TOKEN": f"test-session-{token}",
                "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "test-admin",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": f"test-password-{token}",
                "HERMES_DASHBOARD_BASIC_AUTH_SECRET": f"test-secret-{token}",
                "HERMES_DASHBOARD_INSECURE": "1",
                "HERMES_DASHBOARD": "0",
                "WORKSPACE_SYNC_ON_START": "false",
                "WORKSPACE_SYNC_INTERVAL": "0",
                "GBRAIN_REFRESH_INTERVAL": "0",
                "GBRAIN_EMBED_REFRESH_SCHEDULE": "0",
                "VAULT_RECOVERY_EXPORT_ENABLED": "false",
                "VAULT_RECOVERY_RCLONE_REMOTE": "vault-recovery-crypt",
                "VAULT_RECOVERY_RCLONE_PATH": "Josemar/vault-recovery",
            }
        )
        self.env_file = write_disposable_env_file(self.env, self.tmp / "compose.env")
        # Pin the three named volumes this test uses to the exact names the
        # config seeding and holder/state probes address (compose would
        # otherwise prefix them `<project>_<volume>`).
        volumes = "\n".join(
            f"  {key}:\n    name: {value}"
            for key, value in self.volume_names.items()
        )
        self.override = self.tmp / "disposable-compose.yml"
        self.override.write_text(
            textwrap.dedent(f"volumes:\n{volumes}\n"), encoding="utf-8"
        )

    def compose(self, *args: str, timeout: int = 180, check: bool = False) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", "--env-file", str(self.env_file)]
        for path in (BASE_COMPOSE, VAULT_RECOVERY_OVERLAY, TAILSCALE_ISOLATION_OVERLAY, self.override):
            command.extend(("-f", str(path)))
        command.extend(("-p", self.project, *args))
        return subprocess.run(
            command, cwd=REPO_ROOT, env=self.env, capture_output=True, text=True,
            check=check, timeout=timeout,
        )

    def _docker(self, *args: str, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True,
            check=check, timeout=timeout,
        )

    def _init_local_crypt(self) -> None:
        """Put a local-only crypt remote in the disposable config volume."""
        config_vol = self.volume_names["obsidian-rclone-config"]
        for args in (
            ["config", "create", "local", "local"],
            ["config", "create", "vault-recovery-crypt", "crypt",
             "remote", "local:/underlying",
             "password", "test-password", "password2", "test-password2"],
        ):
            proc = self._docker(
                "run", "--rm", "--network", "none",
                "-v", f"{config_vol}:/config/rclone",
                "-v", f"{self.underlying}:/underlying",
                RCLONE_IMAGE, *args,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def _state_probe(self, shell_test: str) -> subprocess.CompletedProcess[str]:
        """Run a shell test INSIDE a container mounting the real state
        volume (the uploader's own state volume from the overlay)."""
        return self._docker(
            "run", "--rm", "--network", "none",
            "-v", f"{self.volume_names['vault-recovery-uploader-state']}:/state",
            "--entrypoint", "sh",
            RCLONE_IMAGE, "-c", shell_test,
        )

    def _no_flock_farm(self) -> Path:
        """A host dir of symlinks to the pinned image's OWN binaries, with
        flock deliberately missing: mounted into the container and used as
        the whole PATH, `command -v flock` fails exactly like a runtime
        without flock(1)."""
        farm = self.tmp / "no-flock-bin"
        farm.mkdir()
        for tool, target in IMAGE_TOOL_TARGETS.items():
            os.symlink(target, farm / tool)
        return farm

    def _start_holder(self) -> None:
        """Start a live lock holder in the pinned image: opens the uploader's
        exact lock file on fd 9 and takes the SAME kernel flock the uploader
        takes (`exec 9>... ; flock -n 9`), then sleeps forever. `--restart
        no` so a SIGKILL stays dead (no restart policy races)."""
        proc = self._docker(
            "run", "-d", "--name", self.holder_name, "--network", "none",
            "--restart", "no",
            "-v", f"{self.volume_names['obsidian-rclone-config']}:/config/rclone:ro",
            "-v", f"{self.volume_names['vault-recovery-staging']}:/staging:ro",
            "-v", f"{self.volume_names['vault-recovery-staging']}:/staging-prune",
            "-v", f"{self.volume_names['vault-recovery-uploader-state']}:/state",
            "-v", f"{REPO_ROOT / 'scripts' / 'vault-recovery-uploader.sh'}:/scripts/vault-recovery-uploader.sh:ro",
            "-v", f"{REPO_ROOT / 'scripts' / 'vault-recovery-json.awk'}:/scripts/vault-recovery-json.awk:ro",
            "-v", f"{REPO_ROOT / 'scripts' / 'vault-recovery-manifest-schema.awk'}:/scripts/vault-recovery-manifest-schema.awk:ro",
            "-v", f"{REPO_ROOT / 'scripts' / 'vault-recovery-lsjson.awk'}:/scripts/vault-recovery-lsjson.awk:ro",
            "-v", f"{REPO_ROOT / 'scripts' / 'rclone-active-config.sh'}:/scripts/rclone-active-config.sh:ro",
            "-e", "RCLONE_CONFIG=/config/rclone/rclone.conf",
            "-e", "VAULT_RECOVERY_UPLOADER_STAGING_DIR=/staging",
            "-e", "VAULT_RECOVERY_PRUNE_DIR=/staging-prune",
            "-e", "VAULT_RECOVERY_UPLOADER_STATE_DIR=/state",
            "-e", "VAULT_RECOVERY_RCLONE_REMOTE=vault-recovery-crypt",
            "-e", "VAULT_RECOVERY_RCLONE_PATH=Josemar/vault-recovery",
            "--entrypoint", "sh",
            RCLONE_IMAGE,
            "-c", "exec 9>/state/.upload.flock; flock -n 9; echo \"holder acquired the upload flock (rc=$?)\"; sleep 3600",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The holder must be RUNNING and must have acquired the flock before
        # the contended run fires (docker run -d returns before the process
        # executes; the one-shot below would otherwise race it).
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = self._docker("inspect", "-f", "{{.State.Status}}", self.holder_name)
            logs = self._docker("logs", self.holder_name)
            if status.returncode == 0 and status.stdout.strip() == "running" \
                    and "holder acquired the upload flock" in logs.stdout:
                return
            time.sleep(0.5)
        self.fail(
            "lock holder never acquired the flock in the pinned image:\n"
            f"{self._docker('logs', self.holder_name).stdout}"
        )

    def _one_shot(self, *extra_run_args: str) -> subprocess.CompletedProcess[str]:
        """One-shot uploader run through the REAL compose overlay (pinned
        image, real named volumes), like production `--once` invocations."""
        return self.compose(
            "run", "--rm", "--no-deps", "-e", "VAULT_RECOVERY_ONCE=true",
            *extra_run_args,
            "vault-recovery-uploader",
        )

    def test_pinned_digest_matches_the_overlay(self) -> None:
        # Guard against drift: this regression must exercise the EXACT
        # pinned rclone digest the vault-recovery overlay deploys.
        overlay = (REPO_ROOT / "docker-compose.vault-recovery.yml").read_text("utf-8")
        self.assertIn("rclone/rclone@" + RCLONE_IMAGE.split("@", 1)[1], overlay)

    def test_flock_lifecycle_in_pinned_image(self) -> None:
        try:
            self._init_local_crypt()

            # --- 0. Fail closed WITHOUT flock(1), on the pristine state
            # volume: exit 2, visible error, and the lock file is NEVER even
            # opened (the flock presence check runs before `exec 9>...`).
            farm = self._no_flock_farm()
            no_flock = self._one_shot(
                "-e", "PATH=/no-flock-bin",
                "-v", f"{farm}:/no-flock-bin:ro",
            )
            self.assertEqual(no_flock.returncode, 2,
                             f"must fail closed without flock:\n{no_flock.stdout}\n{no_flock.stderr}")
            self.assertIn("flock(1) is not available", no_flock.stderr)
            probe = self._state_probe("test ! -e /state/.upload.flock && echo no-lock-file")
            self.assertEqual(probe.returncode, 0,
                             "fail-closed path must never create the lock file: "
                             + probe.stdout + probe.stderr)

            # --- 1. Live holder in the pinned image acquires the kernel
            # flock on the uploader's lock file (same fd, same binary).
            self._start_holder()
            lock_probe = self._state_probe(
                "test -f /state/.upload.flock && test ! -d /state/.upload.flock && echo regular-file"
            )
            self.assertEqual(lock_probe.returncode, 0,
                             "lock must be a regular flock file, not a mkdir lease: "
                             + lock_probe.stdout + lock_probe.stderr)

            # --- 2. LIVE contention is rejected: a concurrent uploader run
            # fails immediately (non-blocking flock) with a visible error.
            contended = self._one_shot()
            self.assertEqual(contended.returncode, 1,
                             f"contended one-shot must fail:\n{contended.stdout}\n{contended.stderr}")
            self.assertIn("Upload already in progress", contended.stderr)

            # --- 3. Whole-container SIGKILL: no cleanup, no traps can run;
            # ONLY the kernel can release the lock. `docker wait` must
            # report the SIGKILL death (137) and the container must NOT
            # restart (--restart no).
            killed = self._docker("kill", "-s", "SIGKILL", self.holder_name)
            self.assertEqual(killed.returncode, 0, killed.stderr)
            waited = self._docker("wait", self.holder_name, timeout=60)
            self.assertEqual(waited.returncode, 0, waited.stderr)
            self.assertEqual(waited.stdout.strip(), "137",
                             "holder must have died from SIGKILL")

            # --- 4. Restart against the SAME state volume: the kernel
            # released the flock with the dead container, so the very next
            # uploader run reacquires successfully and the lock file (never
            # deleted) is still there.
            restarted = self._one_shot()
            self.assertEqual(restarted.returncode, 0,
                             f"post-SIGKILL restart failed:\n{restarted.stdout}\n{restarted.stderr}")
            again = self._one_shot()
            self.assertEqual(again.returncode, 0,
                             f"second post-SIGKILL restart failed:\n{again.stdout}\n{again.stderr}")
            lock_probe = self._state_probe(
                "test -f /state/.upload.flock && test ! -d /state/.upload.flock && echo regular-file"
            )
            self.assertEqual(lock_probe.returncode, 0,
                             "lock file must persist (never deleted): "
                             + lock_probe.stdout + lock_probe.stderr)
        finally:
            self._docker("rm", "-f", self.holder_name, check=False, timeout=60)
            self.compose("down", "-v", "--remove-orphans", timeout=240)
            shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
