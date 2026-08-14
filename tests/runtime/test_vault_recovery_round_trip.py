"""Docker-gated vault-recovery Phase-2 encrypted round trip.

Real rclone crypt over a LOCAL underlying directory, exercising the full
Phase-2 lane end to end on the pinned images:

  1. disposable isolated Hermes runtime (project volumes only);
  2. real gbrain PGLite state + a real vault note in /opt/data;
  3. production export wrapper -> staged generation (READY + manifest +
     per-tree entries index);
  4. production uploader in ONE-SHOT mode -> uncommitted -> remote
     decrypted verification -> committed + local ack ledger;
  5. CIPHERTEXT proof: the underlying (pre-crypt) namespace holds no
     plaintext names/markers, while listing through the crypt remote shows
     the decrypted generation layout;
  6. production recover step (profile-gated rclone service) -> validated
     RECOVERY_READY handoff in the disposable recovery volume;
  7. short-lived hermes verify-recovery (disposable doctor on a copy) ->
     VERIFIED_READY;
  8. short-lived hermes install-recovery into the REAL mount layout: the
     live vault at /opt/data/obsidian is the root of the obsidian-vault
     volume, so rename(2) on the mount root fails EBUSY and the journaled
     per-entry swap must take over while .gbrain gets the atomic rename
     swap; the journal records status=complete;
  9. post-install proofs: doctor opens the RESTORED live .gbrain, the
     restored page/note contents are live, and rollback restores the
     original live content.

Local runs skip unless RUN_DOCKER_TESTS=1 and the docker CLI is available.
Never uses production volumes, credentials, or remotes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
VAULT_RECOVERY_OVERLAY = REPO_ROOT / "docker-compose.vault-recovery.yml"
RCLONE_IMAGE = "rclone/rclone@sha256:b06aed988cf5967de7c25be5925240983981c757f4ed1ac9d2fa659d51d60548"

GBRAIN_ENV = (
    "GBRAIN_HOME=/opt/data GBRAIN_BRAIN_REPO=/opt/data/obsidian "
    "GBRAIN_SCHEMA_PACK=josemar GBRAIN_SKIP_STARTUP_HOOKS=1 "
    "HOME=/opt/data XDG_CONFIG_HOME=/opt/data/.config"
)
NATIVE = "/opt/josemar/libexec/gbrain-native"
STAGING = "/opt/data/vault-recovery/staging"
RESTORE_WRAPPER = "/opt/josemar/scripts/vault-recovery-restore.sh"
PLAINTEXT_MARKER = "ROUND_TRIP_PLAINTEXT_MARKER_7f3d9c"


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


@unittest.skipUnless(_docker_enabled(), "set RUN_DOCKER_TESTS=1 with a docker CLI for the round trip")
class VaultRecoveryRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        token = uuid.uuid4().hex[:12]
        self.project = f"josemar-vr-rt-{token}"
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-round-trip-"))
        self.state = self.tmp / "agent-state"
        self.credentials = self.tmp / "credentials"
        self.state.mkdir()
        self.credentials.mkdir()
        self.underlying = self.tmp / "underlying"
        self.underlying.mkdir()
        self.volume_names = {
            "hermes-data": f"{self.project}-hermes-data",
            "aux-ml-shared": f"{self.project}-aux-shared",
            "obsidian-vault": f"{self.project}-obsidian",
            "obsidian-rclone-config": f"{self.project}-rclone-config",
            "vault-recovery-staging": f"{self.project}-vr-staging",
            "vault-recovery-uploader-state": f"{self.project}-vr-uploader-state",
            "vault-recovery-recovery": f"{self.project}-vr-recovery",
        }
        volumes = "\n".join(
            f"  {key}:\n    name: {value}"
            for key, value in self.volume_names.items()
        )
        self.override = self.tmp / "disposable-compose.yml"
        self.override.write_text(
            textwrap.dedent(
                f"""
                services:
                  hermes:
                    ports: !reset []
                    volumes:
                      - hermes-data:/opt/data
                      - aux-ml-shared:/shared
                      - obsidian-vault:/opt/data/obsidian
                      - vault-recovery-staging:/opt/data/vault-recovery/staging
                      - {self.state}:/opt/josemar/source-agent-state:ro
                      - {self.credentials}:/opt/josemar/credentials-source:ro
                  tailscale:
                    ports: !reset []
                  vault-recovery-uploader:
                    # Keep the overlay's read-only boundary; the LOCAL test
                    # crypt remote needs its underlying dir visible inside
                    # the uploader (writable: it owns the upload namespace).
                    volumes:
                      - {self.underlying}:/underlying
                  vault-recovery-recover:
                    # The recover step reads the same local underlying dir.
                    volumes:
                      - {self.underlying}:/underlying:ro
                volumes:
                __VOLUMES__
                """
            ).lstrip().replace("__VOLUMES__", volumes),
            encoding="utf-8",
        )
        self.env = os.environ.copy()
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
                "WORKSPACE_STATE_REPO": "",
                "WORKSPACE_REPO_TOKEN": "",
                "TELEGRAM_BOT_TOKEN": "",
                "PRIMARY_TELEGRAM_ID": "",
                "TELEGRAM_ALLOWED_USERS": "",
                "TELEGRAM_HOME_CHANNEL": "",
                "GATEWAY_ALLOWED_USERS": "",
                "HERMES_TELEGRAM_BOT_TOKEN": "",
                "HERMES_TELEGRAM_ALLOWED_USERS": "",
                "HERMES_TELEGRAM_HOME_CHANNEL": "",
                "HERMES_GATEWAY_ALLOWED_USERS": "",
                # Disable the three owned Hermes cron jobs for the round trip:
                # the verify/install/rollback steps take the shared
                # TaskNotes/gbrain lock (fix 5), so a cron firing mid-test
                # would refuse them (or race them). The export is run
                # manually by the test, so removing its owned job is safe.
                "GBRAIN_REFRESH_INTERVAL": "0",
                "GBRAIN_EMBED_REFRESH_SCHEDULE": "0",
                "VAULT_RECOVERY_EXPORT_ENABLED": "false",
                "VAULT_RECOVERY_RCLONE_REMOTE": "vault-recovery-crypt",
                "VAULT_RECOVERY_RCLONE_PATH": "Josemar/vault-recovery",
                "COMPOSE_PROFILES": "",
            }
        )

    def compose(self, *args: str, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose"]
        for path in (BASE_COMPOSE, VAULT_RECOVERY_OVERLAY, self.override):
            command.extend(("-f", str(path)))
        command.extend(("-p", self.project, *args))
        return subprocess.run(
            command, cwd=REPO_ROOT, env=self.env, capture_output=True, text=True,
            check=check, timeout=timeout,
        )

    def _exec(self, service: str, *command: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.compose("exec", "-T", service, *command, timeout=timeout, check=check)

    def _init_local_crypt(self) -> None:
        """Put a local-only crypt remote in the disposable config volume."""
        config_vol = self.volume_names["obsidian-rclone-config"]
        for args in (
            ["config", "create", "local", "local"],
            ["config", "create", "vault-recovery-crypt", "crypt",
             "remote", "local:/underlying",
             "password", "test-password", "password2", "test-password2"],
        ):
            proc = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    "-v", f"{config_vol}:/config/rclone",
                    "-v", f"{self.underlying}:/underlying",
                    RCLONE_IMAGE, *args,
                ], capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        inspect = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none",
                "-v", f"{config_vol}:/config/rclone:ro",
                RCLONE_IMAGE, "config", "show", "vault-recovery-crypt",
            ], capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        self.assertIn("type = crypt", inspect.stdout)

    def _wait_for_init_complete(self, timeout: int = 240) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            logs = self.compose("logs", "--no-color", "hermes", timeout=60)
            if "Josemar Hermes setup complete" in (logs.stdout + logs.stderr):
                return
            time.sleep(2)
        logs = self.compose("logs", "--no-color", "hermes", timeout=60)
        self.fail("Hermes init did not reach its completion marker:\n" + logs.stdout + logs.stderr)

    def _doctor_ok(self) -> None:
        """The live doctor must pass the production required-check contract
        (run as the hermes runtime user, issue #110 conventions)."""
        proc = self._hermes(f"{GBRAIN_ENV} {NATIVE} doctor --json", timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        for check in ("connection", "jsonb_integrity", "schema_version", "pgvector"):
            found = [c for c in report["checks"] if c["name"] == check]
            self.assertEqual(len(found), 1, report)
            self.assertEqual(found[0]["status"], "ok", report)

    def _hermes(self, script: str, *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a shell fragment as the hermes runtime user (issue #110)."""
        return self._exec(
            "hermes", "su", "-s", "/bin/sh", "hermes", "-c", script,
            timeout=timeout, check=check,
        )

    def test_encrypted_round_trip_upload_recover_verify_install_rollback(self) -> None:
        try:
            self._init_local_crypt()
            up = self.compose(
                "up", "-d", "--build", "--wait", "--wait-timeout", "600",
                "hermes", timeout=1800,
            )
            self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
            self._wait_for_init_complete()

            # The fresh obsidian-vault volume is root-owned; production
            # ownership (the Syncthing/Hermes runtime uid) is applied before
            # any state is written, so the install/rollback renames work.
            chown = self._exec(
                "hermes", "sh", "-lc",
                "chown -R 10000:10000 /opt/data/obsidian",
                timeout=120, check=False,
            )
            self.assertEqual(chown.returncode, 0, chown.stdout + chown.stderr)
            # The shared tasknotes/gbrain lock dir must be writable by the
            # hermes runtime user (root-created dirs would block the export).
            self._hermes("mkdir -p /opt/data/.locks", timeout=60)

            # --- Real live state: gbrain PGLite + vault note + schema pack.
            # --no-embedding is enough for the round trip: the pinned doctor
            # still reports connection/pgvector/schema_version/jsonb_integrity
            # ok (the real-vector proof is the phase-1 portability gate).
            init = self._hermes(
                f"{GBRAIN_ENV} {NATIVE} init --pglite --no-embedding",
                timeout=180, check=False,
            )
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            self._hermes(f"{GBRAIN_ENV} {NATIVE} config set sync.repo_path /opt/data/obsidian")
            self._hermes(
                f"{GBRAIN_ENV} {NATIVE} put note-a --content '# Note A\\n\\n{PLAINTEXT_MARKER} A'"
            )
            self._hermes(
                "mkdir -p /opt/data/.gbrain/schema-packs/josemar && "
                "printf 'schema: josemar-test\\n' > /opt/data/.gbrain/schema-packs/josemar/pack.yaml && "
                "printf 'josemar\\n' > /opt/data/.gbrain/active-schema-pack"
            )
            self._hermes(
                f"printf '# Vault note\\n{PLAINTEXT_MARKER}\\n' > /opt/data/obsidian/note.md"
            )
            self._doctor_ok()

            # --- Production export (as the hermes runtime user).
            export = self._hermes(
                f"VAULT_RECOVERY_STAGING_DIR={STAGING} "
                "VAULT_RECOVERY_CONVERGENCE_ATTEMPTS=6 "
                "/opt/josemar/scripts/vault-recovery-export.sh",
                timeout=240, check=False,
            )
            self.assertNotEqual(export.returncode, 75, export.stdout + export.stderr)
            self.assertEqual(export.returncode, 0, export.stdout + export.stderr)
            gen = self._hermes(f"cat {STAGING}/latest", timeout=60).stdout.strip()
            self.assertRegex(gen, r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")

            # A pre-install-only sentinel created AFTER the export: it is NOT
            # part of the generation, so the install must move it aside and
            # the rollback must restore it.
            self._hermes(
                "printf 'PRE_INSTALL_ONLY_CONTENT\\n' > /opt/data/obsidian/pre-install.txt"
            )

            # --- Uploader one-shot: uncommitted -> verified -> committed -> ack.
            upload = self.compose(
                "run", "--rm", "--no-deps", "-e", "VAULT_RECOVERY_ONCE=true",
                "vault-recovery-uploader", timeout=300,
            )
            self.assertEqual(upload.returncode, 0, upload.stdout + upload.stderr)
            self._hermes(f"test -f {STAGING}/{gen}/manifest.json", timeout=60)

            # --- CIPHERTEXT proof: the underlying (pre-crypt) namespace
            # carries NO plaintext marker, NO plain generation-id, and NO
            # plaintext file names anywhere (crypt encrypts every path
            # component); listing through the crypt remote shows the
            # decrypted generation layout.
            underlying_ls = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    "-v", f"{self.volume_names['obsidian-rclone-config']}:/config/rclone:ro",
                    "-v", f"{self.underlying}:/underlying:ro",
                    RCLONE_IMAGE, "lsf", "-R", "local:/underlying",
                ], capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(underlying_ls.returncode, 0, underlying_ls.stderr)
            self.assertTrue(underlying_ls.stdout.strip(), "encrypted namespace must exist under the underlying")
            self.assertNotIn(PLAINTEXT_MARKER, underlying_ls.stdout)
            self.assertNotIn(gen, underlying_ls.stdout)
            self.assertNotIn("manifest.json", underlying_ls.stdout)
            self.assertNotIn("vault/", underlying_ls.stdout)
            crypt_ls = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    "-v", f"{self.volume_names['obsidian-rclone-config']}:/config/rclone:ro",
                    "-v", f"{self.underlying}:/underlying:ro",
                    RCLONE_IMAGE, "lsf", "-R",
                    f"vault-recovery-crypt:Josemar/vault-recovery/committed/{gen}",
                ], capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(crypt_ls.returncode, 0, crypt_ls.stderr)
            for expected in ("READY", "manifest.json", "vault/", ".gbrain/"):
                self.assertIn(expected, crypt_ls.stdout)
            uncommitted_ls = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    "-v", f"{self.volume_names['obsidian-rclone-config']}:/config/rclone:ro",
                    "-v", f"{self.underlying}:/underlying:ro",
                    RCLONE_IMAGE, "lsf",
                    f"vault-recovery-crypt:Josemar/vault-recovery/uncommitted/{gen}",
                ], capture_output=True, text=True, timeout=120, check=False,
            )
            # The commit moves remove every FILE and the READY sentinel from
            # the uncommitted generation. rclone keeps the emptied source
            # dirs when --create-empty-src-dirs is used, so at most empty
            # directories remain there — nothing listable or recoverable
            # (recover only reads the committed namespace).
            self.assertIn(uncommitted_ls.returncode, (0, 3), uncommitted_ls.stderr)
            if uncommitted_ls.returncode == 0:
                self.assertTrue(
                    all(line.endswith("/") for line in uncommitted_ls.stdout.splitlines()),
                    "uncommitted namespace must hold ONLY empty directories "
                    f"(no files, no sentinels): {uncommitted_ls.stdout!r}",
                )
                self.assertNotIn("READY", uncommitted_ls.stdout)
                self.assertNotIn("manifest.json", uncommitted_ls.stdout)

            # --- Recover: profile-gated rclone step downloads + validates.
            recover = self.compose(
                "--profile", "recovery", "run", "--rm", "--no-deps",
                "vault-recovery-recover", "download", gen, timeout=300,
            )
            self.assertEqual(recover.returncode, 0, recover.stdout + recover.stderr)

            # --- Verify: short-lived hermes run mounts the recovery volume
            # and runs the disposable doctor (never the live state).
            # The long-running hermes service runs as root (s6 init); the
            # restore wrapper enforces the issue #110 runtime identity, so
            # short-lived runs must explicitly run as the hermes uid.
            verify = self.compose(
                "run", "--rm", "--no-deps", "--user", "10000:10000",
                "-v", f"{self.volume_names['vault-recovery-recovery']}:/recovery",
                "--entrypoint", RESTORE_WRAPPER,
                "hermes", "verify-recovery", "/recovery", timeout=300,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            verified = json.loads(verify.stdout)
            self.assertEqual(verified["generation_id"], gen)
            self.assertEqual(verified["trees"][".gbrain"]["exact_match"], True)
            self.assertEqual(verified["trees"]["vault"]["exact_match"], True)

            # --- Install into the REAL mount layout. /opt/data/obsidian is
            # the root of the obsidian-vault volume: rename(2) on the mount
            # root fails EBUSY, so the vault swap must be the journaled
            # per-entry path while .gbrain gets the atomic rename swap.
            install = self.compose(
                "run", "--rm", "--no-deps", "--user", "10000:10000",
                "-v", f"{self.volume_names['vault-recovery-recovery']}:/recovery",
                "--entrypoint", RESTORE_WRAPPER,
                "hermes", "install-recovery", "/recovery",
                "--live-vault", "/opt/data/obsidian",
                "--live-gbrain", "/opt/data/.gbrain",
                "--generation", gen,
                "--i-confirm-this-overwrites-production", timeout=300,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            result = json.loads(install.stdout)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["swap_modes"], {".gbrain": "atomic", "vault": "per-entry"})
            journal = json.loads(
                self._hermes(
                    f"cat /opt/data/vault-recovery/install-journal/{gen}/journal.json",
                    timeout=60,
                ).stdout
            )
            self.assertEqual(journal["status"], "complete")

            # --- Post-install proofs: the restored live .gbrain opens on the
            # doctor, the restored page/note contents are live, the
            # pre-install-only file was moved aside into the backup root.
            self._doctor_ok()
            page = self._hermes(f"{GBRAIN_ENV} {NATIVE} get note-a", timeout=120).stdout
            self.assertIn(PLAINTEXT_MARKER, page)
            note = self._hermes("cat /opt/data/obsidian/note.md", timeout=60).stdout
            self.assertIn(PLAINTEXT_MARKER, note)
            pre_install_gone = self._hermes(
                "test ! -e /opt/data/obsidian/pre-install.txt", timeout=60, check=False,
            )
            self.assertEqual(pre_install_gone.returncode, 0, "pre-install.txt must be moved aside by the install")
            backup = self._hermes(
                "cat /opt/data/obsidian/.vault-recovery-install/"
                f"{gen}/vault-backup/pre-install.txt", timeout=60, check=False,
            )
            self.assertEqual(backup.returncode, 0, backup.stdout + backup.stderr)
            self.assertIn("PRE_INSTALL_ONLY_CONTENT", backup.stdout)

            # --- Operator rollback restores the ORIGINAL live content.
            rollback = self.compose(
                "run", "--rm", "--no-deps", "--user", "10000:10000",
                "--entrypoint", RESTORE_WRAPPER,
                "hermes", "rollback", gen, timeout=300,
            )
            self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
            self.assertEqual(json.loads(rollback.stdout)["status"], "rolled-back")
            restored_pre = self._hermes(
                "cat /opt/data/obsidian/pre-install.txt", timeout=60,
            )
            self.assertIn("PRE_INSTALL_ONLY_CONTENT", restored_pre.stdout)

        finally:
            self.compose("down", "-v", "--remove-orphans", timeout=240)
            shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
