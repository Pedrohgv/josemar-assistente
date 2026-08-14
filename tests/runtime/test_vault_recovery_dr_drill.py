"""Docker-gated vault-recovery Phase-3 full disaster-recovery drill.

Combines the Phase-1 real-vector proof with the Phase-2 encrypted lane and an
explicit DESTROY step, end to end on the pinned images (see
docs/vault-recovery-operations.md -> "Disaster-recovery drill"):

  1. disposable isolated Hermes runtime (project volumes only);
  2. real gbrain PGLite state: pages + a DB-only manual link, config keys,
     schema-pack files + the `active-schema-pack` marker, and a vault note;
  3. REAL vector-bearing DB state through the pinned gbrain embedding
     workflow (issue #65): a stub OpenAI-compatible embeddings endpoint, then
     `migrate embeddings --no-embed` + `embed --stale
     --include-null-signature`, and the completion marker with the real model
     tuple; the LIVE semantic query returns the expected page;
  4. production export wrapper -> staged generation (READY + manifest +
     per-tree entries index); production uploader ONE-SHOT -> uncommitted ->
     remote decrypted verification -> committed + local ack ledger;
   5. MAINTENANCE WINDOW (ordered, mirroring the runbook): pause/disable ALL
      THREE owned jobs (gbrain-refresh, gbrain-embedding-refresh,
      vault-recovery-export) and ASSERT they are absent from jobs.json; then
      stop Hermes AND the REAL server Syncthing container (started by this
      drill, asserted running, then stopped and asserted not running — the
      runtime cannot stop the PAIRED device's Syncthing, which the runbook
      models as an explicit operator quiescence gate). Only then:
  6. DESTROY both live trees (writers stopped): the complete
     /opt/data/.gbrain tree and every vault entry are deleted (the mount
     roots stay so the journaled install can swap into them) — the live
     state is gone;
  7. production recover step (profile-gated rclone service) -> validated
     RECOVERY_READY handoff; short-lived hermes verify-recovery (disposable
     doctor on a copy) -> VERIFIED_READY;
  8. short-lived hermes install-recovery into the destroyed mount layout:
     .gbrain gets the atomic rename swap, the vault (root of the
     obsidian-vault volume) gets the journaled per-entry swap;
  9. CONTROLLED RESTART: start Hermes again (the documented post-install
     step), wait for the init completion marker, restart the embeddings
     stub, then run the SURVIVAL proofs: the restored live .gbrain opens on
     the doctor (connection/jsonb_integrity/schema_version/pgvector ok),
     the DB-only manual link is readable, page + vault contents are live
     and byte-identical to the staged generation, config keys survive
     (search.mcp_keyword_only false), schema-pack files + the embedding
     completion marker are byte-identical, and the RESTORED vectors answer
     the semantic query with ZERO stale rows — no reindex/rebuild/sync;
 10. operator `rollback` (Hermes stopped again, per the documented
     install/rollback sequence) restores the (destroyed) pre-install state
     with journal status rolled-back.

Local runs skip unless RUN_DOCKER_TESTS=1 and the docker CLI is available.
Release/deploy runs (CI) set VAULT_RECOVERY_DR_DRILL_REQUIRED=1: the opt-in
env var is then IGNORED and a missing docker CLI FAILS the test — the full
drill is a mandatory release/deploy gate and cannot be bypassed.
Never uses production volumes, credentials, or remotes.
"""

from __future__ import annotations

import base64
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
from unittest import mock

from .helpers import docker_available

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
VAULT_RECOVERY_OVERLAY = REPO_ROOT / "docker-compose.vault-recovery.yml"
RCLONE_IMAGE = "rclone/rclone@sha256:b06aed988cf5967de7c25be5925240983981c757f4ed1ac9d2fa659d51d60548"

# The three owned Hermes cron jobs that must be paused/disabled for the
# drill's maintenance window (mirrors the runbook: pause all gbrain jobs and
# the export cron before a destructive restore/install).
OWNED_JOB_NAMES = (
    "gbrain-refresh",
    "gbrain-embedding-refresh",
    "vault-recovery-export",
)

GBRAIN_ENV = (
    "GBRAIN_HOME=/opt/data GBRAIN_BRAIN_REPO=/opt/data/obsidian "
    "GBRAIN_SCHEMA_PACK=josemar GBRAIN_SKIP_STARTUP_HOOKS=1 "
    "HOME=/opt/data XDG_CONFIG_HOME=/opt/data/.config"
)
NATIVE = "/opt/josemar/libexec/gbrain-native"
STAGING = "/opt/data/vault-recovery/staging"
RESTORE_WRAPPER = "/opt/josemar/scripts/vault-recovery-restore.sh"
PLAINTEXT_MARKER = "DR_DRILL_PLAINTEXT_MARKER_9c1e7a"

# Pinned E5 model tuple (issue #65 / .env.example defaults) — identical to
# the phase-1 portability proof so the drill creates REAL vector rows. The
# gbrain provider id carries the `llama-server:` prefix exactly like the
# production compose overlay wires it (the migration signature includes the
# revision, so these must match what the stub serves).
EMBEDDING_MODEL = "llama-server:intfloat/multilingual-e5-small"
EMBEDDING_DIMENSIONS = 384
EMBEDDING_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
EMBED_STUB_PORT = 8799
EMBED_STUB_URL = f"http://127.0.0.1:{EMBED_STUB_PORT}/v1"
EMBED_ENV = (
    f"GBRAIN_EMBEDDING_MODEL={EMBEDDING_MODEL} "
    f"GBRAIN_EMBEDDING_DIMENSIONS={EMBEDDING_DIMENSIONS} "
    f"GBRAIN_EMBEDDING_MODEL_REVISION={EMBEDDING_REVISION} "
    f"LLAMA_SERVER_BASE_URL={EMBED_STUB_URL} "
    "LLAMA_SERVER_API_KEY=test-key "
    "GBRAIN_EMBED_CONCURRENCY=1"
)

# Deterministic OpenAI-shaped embeddings stub (same as the portability proof).
EMBED_STUB_SOURCE = r'''
import hashlib, json, math
from http.server import BaseHTTPRequestHandler, HTTPServer

DIM = 384
MODEL = %(model)r
REVISION = %(revision)r

def vec_for(text):
    v = [0.0] * DIM
    for tok in text.lower().split():
        idx = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], 'big') %% DIM
        v[idx] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / norm, 8) for x in v]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self._send(200, {"status": "healthy"})
        elif self.path == "/info":
            self._send(200, {
                "model_id": MODEL,
                "model_revision": REVISION,
                "sha": REVISION,
                "max_batch_tokens": 16384,
                "max_batch_requests": 64,
                "max_client_batch_size": 64,
            })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.endswith("/embeddings"):
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(length))
        inputs = req.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        data = [
            {"embedding": vec_for(text), "index": i, "object": "embedding"}
            for i, text in enumerate(inputs)
        ]
        tokens = sum(len(text.split()) for text in inputs)
        self._send(200, {
            "data": data,
            "model": req.get("model", MODEL),
            "object": "list",
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        })

HTTPServer(("127.0.0.1", %(port)d), Handler).serve_forever()
''' % {"model": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION,
       "port": EMBED_STUB_PORT}


@unittest.skipUnless(os.getenv("RUN_DOCKER_TESTS") == "1" or os.getenv("VAULT_RECOVERY_DR_DRILL_REQUIRED") == "1",
                     "set RUN_DOCKER_TESTS=1 with a docker CLI for the drill")
class VaultRecoveryDrDrillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.required = os.getenv("VAULT_RECOVERY_DR_DRILL_REQUIRED") == "1"
        token = uuid.uuid4().hex[:12]
        self.project = f"josemar-vr-dr-{token}"
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-dr-drill-"))
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
            "syncthing-config": f"{self.project}-syncthing-config",
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
                # Pause/disable ALL THREE owned jobs for the drill's
                # maintenance window (mirrors the runbook's "stop Hermes,
                # server Syncthing and all gbrain jobs first" sequence):
                #   - gbrain-refresh: GBRAIN_REFRESH_INTERVAL=0 removes the
                #     owned job (its `josemar-gbrain refresh` would re-create
                #     live state (PGLite dir, page files) inside the destroy
                #     -> install window, breaking the drill's "live state is
                #     gone" premise and polluting the journaled pre-install
                #     backup);
                #   - gbrain-embedding-refresh: GBRAIN_EMBED_REFRESH_SCHEDULE=0
                #     removes the owned daily embedding refresh job;
                #   - vault-recovery-export: VAULT_RECOVERY_EXPORT_ENABLED=false
                #     removes the owned daily export cron (its lock-held export
                #     would also repopulate state inside the window).
                # The test asserts all three are absent from jobs.json BEFORE
                # the stop/destroy phase (ordering check).
                "GBRAIN_REFRESH_INTERVAL": "0",
                "GBRAIN_EMBED_REFRESH_SCHEDULE": "0",
                "VAULT_RECOVERY_EXPORT_ENABLED": "false",
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

    def _hermes(self, script: str, *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a shell fragment as the hermes runtime user (issue #110)."""
        return self._exec(
            "hermes", "su", "-s", "/bin/sh", "hermes", "-c", script,
            timeout=timeout, check=check,
        )

    def _root(self, script: str, *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a shell fragment as root in a SHORT-LIVED disposable hermes
        container (only for the DESTROY step on the disposable state). Uses
        `compose run` because the long-running hermes container is STOPPED
        during the drill's destructive window (documented sequence)."""
        return self.compose(
            "run", "--rm", "--no-deps", "--user", "0:0",
            "--entrypoint", "/bin/sh", "hermes", "-lc", script,
            timeout=timeout, check=check,
        )

    def _short(self, script: str, *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a shell fragment as the hermes runtime user in a SHORT-LIVED
        disposable hermes container (hermes may be stopped)."""
        return self.compose(
            "run", "--rm", "--no-deps", "--user", "10000:10000",
            "--entrypoint", "/bin/sh", "hermes", "-lc", script,
            timeout=timeout, check=check,
        )

    def _assert_service_state(self, service: str, running: bool) -> None:
        """Assert the named compose service container is running/stopped.

        Uses `docker compose ps -a` so stopped containers are visible. The
        State column is the container's current state ("running"/"exited").
        A container that was never created counts as "not running" (the
        disposable drill composition starts hermes only; syncthing is never
        created, which is still the required stopped state for the window).
        """
        proc = self.compose(
            "ps", "-a", "--format", "{{.Name}} {{.State}}", timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        expected = "running" if running else "exited"
        found = None
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == f"{self.project}-{service}":
                found = parts[1]
                break
        if found is None:
            self.assertFalse(
                running,
                f"{service} container not found but expected running:\n{proc.stdout}",
            )
            return
        self.assertEqual(
            found, expected,
            f"{service} state is {found!r}, expected {expected!r}\n{proc.stdout}",
        )

    def _assert_jobs_paused(self) -> None:
        """Assert ALL three owned jobs are paused/disabled: jobs.json (the
        real Hermes cron store) contains none of them. Runs while Hermes is
        up (before the maintenance-window stop)."""
        proc = self._hermes(
            "python3 - <<'PY'\n"
            "import json, sys\n"
            "names = %r\n"
            "with open('/opt/data/cron/jobs.json') as fh:\n"
            "    data = json.load(fh)\n"
            "present = [j.get('name') for j in data.get('jobs', [])\n"
            "           if isinstance(j, dict) and j.get('name') in names]\n"
            "print('PAUSED' if not present else 'PRESENT: ' + ','.join(present))\n"
            "PY" % (list(OWNED_JOB_NAMES),),
            timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            "PAUSED", proc.stdout,
            f"owned jobs not all paused before the destructive window:\n{proc.stdout}",
        )

    def _init_local_crypt(self) -> None:
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

    def _init_syncthing_config(self) -> None:
        """Pre-create the disposable syncthing-config volume with runtime-uid
        ownership. Fresh named volumes are root-owned (0755), so the
        syncthing service (which runs as HERMES_UID, like production) would
        exit on cert generation without this — the drill must run a REAL
        Syncthing to stop it in the maintenance window."""
        vol = self.volume_names["syncthing-config"]
        create = subprocess.run(
            ["docker", "volume", "create", vol],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(create.returncode, 0, create.stderr)
        chown = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{vol}:/var/syncthing/config",
                "alpine:3.20", "sh", "-c",
                "chown -R 10000:10000 /var/syncthing/config",
            ], capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(chown.returncode, 0, chown.stderr)

    def _wait_for_init_complete(self, timeout: int = 240, since: str | None = None) -> None:
        # `since` ("10s", "1m", ...) restricts the log scan to recent lines so
        # a CONTROLLED RESTART can wait for the NEW init completion marker
        # instead of matching the first boot's marker in the retained log.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            args = ["logs", "--no-color"]
            if since is not None:
                args.extend(("--since", since))
            args.append("hermes")
            logs = self.compose(*args, timeout=60)
            if "Josemar Hermes setup complete" in (logs.stdout + logs.stderr):
                return
            time.sleep(2)
        logs = self.compose("logs", "--no-color", "hermes", timeout=60)
        self.fail("Hermes init did not reach its completion marker:\n" + logs.stdout + logs.stderr)

    def _doctor_ok(self) -> None:
        proc = self._hermes(f"{GBRAIN_ENV} {NATIVE} doctor --json", timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        for check in ("connection", "jsonb_integrity", "schema_version", "pgvector"):
            found = [c for c in report["checks"] if c["name"] == check]
            self.assertEqual(len(found), 1, report)
            self.assertEqual(found[0]["status"], "ok", report)

    def _start_embed_stub(self) -> None:
        b64 = base64.b64encode(EMBED_STUB_SOURCE.encode()).decode()
        self._hermes(f"echo {b64} | base64 -d > /tmp/vr-dr-embed-stub.py")
        self._hermes(
            f"nohup python3 /tmp/vr-dr-embed-stub.py "
            f"> /tmp/vr-dr-embed-stub.log 2>&1 & echo $! > /tmp/vr-dr-embed-stub.pid"
        )
        for _ in range(30):
            proc = self._hermes(
                "python3 - <<'PY'\n"
                "import json, urllib.request\n"
                "try:\n"
                "    with urllib.request.urlopen("
                f"'http://127.0.0.1:{EMBED_STUB_PORT}/health', timeout=2) as r:\n"
                "        print(r.status)\n"
                "except Exception as exc:\n"
                "    print('down', exc)\n"
                "PY",
                check=False,
            )
            if proc.stdout.strip().startswith("200"):
                return
            time.sleep(2)
        self.fail(
            "embedding stub did not become healthy:\n"
            f"{self._hermes('cat /tmp/vr-dr-embed-stub.log', check=False).stdout[-2000:]}"
        )

    def _semantic_query(self, query: str) -> str:
        return self._hermes(f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} query {query!r} --no-expand").stdout

    def _assert_no_stale(self, proc: subprocess.CompletedProcess[str]) -> None:
        match = re.search(r"(\d+)\s+stale\s+found", proc.stdout.lower())
        self.assertIsNotNone(
            match,
            f"embed dry-run output lacks a stale count:\n{proc.stdout[-3000:]}",
        )
        self.assertEqual(
            match.group(1), "0",
            f"stale embeddings remain after restore:\n{proc.stdout[-3000:]}",
        )

    def test_disaster_drill_destroy_both_restore_and_rollback(self) -> None:
        # Local opt-in; the release/deploy workflow forces the drill with
        # VAULT_RECOVERY_DR_DRILL_REQUIRED=1, which makes a missing docker
        # CLI a FAILURE (no opt-in bypass).
        if not self.required:
            if os.getenv("RUN_DOCKER_TESTS") != "1":
                self.skipTest("set RUN_DOCKER_TESTS=1 to run the disaster-recovery drill")
        if not docker_available():
            if self.required:
                self.fail(
                    "VAULT_RECOVERY_DR_DRILL_REQUIRED=1 but the docker CLI is "
                    "unavailable: the full disaster-recovery drill cannot run "
                    "and the release is blocked."
                )
            self.skipTest("docker CLI is not available")
        try:
            self._init_local_crypt()
            self._init_syncthing_config()
            # Start Hermes AND the REAL Syncthing service for the drill:
            # the maintenance window must stop a genuinely running
            # server-side Syncthing (asserted running first), mirroring the
            # runbook. syncthing depends on tailscale (its network mode),
            # so the disposable tailscale container is created too — it
            # runs unauthenticated and never touches the production node.
            up = self.compose(
                "up", "-d", "--build", "--wait", "--wait-timeout", "600",
                "hermes", "syncthing", timeout=1800,
            )
            self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
            self._wait_for_init_complete()
            self._assert_service_state("syncthing", running=True)

            chown = self._exec(
                "hermes", "sh", "-lc",
                "chown -R 10000:10000 /opt/data/obsidian",
                timeout=120, check=False,
            )
            self.assertEqual(chown.returncode, 0, chown.stdout + chown.stderr)
            self._hermes("mkdir -p /opt/data/.locks", timeout=60)

            # --- Live state: gbrain PGLite + DB-only link + vault + schema.
            init = self._hermes(
                f"{GBRAIN_ENV} {NATIVE} init --pglite --no-embedding",
                timeout=180, check=False,
            )
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            self._hermes(f"{GBRAIN_ENV} {NATIVE} config set sync.repo_path /opt/data/obsidian")
            self._hermes(f"{GBRAIN_ENV} {NATIVE} config set search.mcp_keyword_only true")
            self._hermes(f"{GBRAIN_ENV} {NATIVE} put pa --content '# Page A\n\n{PLAINTEXT_MARKER} A'")
            self._hermes(f"{GBRAIN_ENV} {NATIVE} put pb --content '# Page B\n\n{PLAINTEXT_MARKER} B'")
            self._hermes(
                f"{GBRAIN_ENV} {NATIVE} link pa pb --link-type mentions "
                '--context "drill proof" --link-source manual'
            )
            self._hermes(
                "mkdir -p /opt/data/.gbrain/schema-packs/josemar && "
                "printf 'schema: josemar-drill\\n' > /opt/data/.gbrain/schema-packs/josemar/pack.yaml && "
                "printf 'josemar\\n' > /opt/data/.gbrain/active-schema-pack"
            )
            self._hermes(
                f"printf '# Vault note\\n{PLAINTEXT_MARKER}\\n' > /opt/data/obsidian/note.md && "
                "mkdir -p /opt/data/obsidian/attachments && "
                "printf 'attachment-bytes\\n' > /opt/data/obsidian/attachments/a.bin"
            )

            # --- REAL vectors (issue #65 native sequence).
            self._start_embed_stub()
            migrate = self._hermes(
                f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} migrate embeddings "
                f"--to {EMBEDDING_MODEL} --dim {EMBEDDING_DIMENSIONS} --yes "
                "--no-embed --ignore-env-override",
                timeout=180, check=False,
            )
            self.assertEqual(migrate.returncode, 0, migrate.stdout + migrate.stderr)
            self._hermes(f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} config set search.mcp_keyword_only false")
            backfill = self._hermes(
                f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} embed --stale --include-null-signature",
                timeout=300, check=False,
            )
            self.assertEqual(backfill.returncode, 0, backfill.stdout + backfill.stderr)
            verify = self._hermes(
                f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} embed --stale --include-null-signature --dry-run",
                timeout=300, check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self._assert_no_stale(verify)
            self._hermes(
                "python3 - <<'PY'\n"
                "import json\n"
                "payload = {'model': %r, 'dimensions': int(%r), 'revision': %r}\n"
                "with open('/opt/data/.gbrain/embedding-backfill-complete.json', 'w') as f:\n"
                "    json.dump(payload, f, sort_keys=True); f.write('\\n')\n"
                "PY" % (EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, EMBEDDING_REVISION)
            )
            live_query = self._semantic_query(PLAINTEXT_MARKER)
            self.assertIn("pa", live_query, f"live semantic query did not return pa:\n{live_query[-3000:]}")
            self._doctor_ok()

            # --- Export (production wrapper, hermes user).
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
            manifest = json.loads(
                self._hermes(f"cat {STAGING}/{gen}/manifest.json", timeout=60).stdout
            )
            self.assertEqual(manifest["generation_id"], gen)
            self.assertEqual(manifest["phase"], 1)
            self.assertFalse(manifest["remote"]["uploaded"])
            self._hermes(f"test -f {STAGING}/{gen}/READY", timeout=60)

            # --- Upload (one-shot): uncommitted -> verified -> committed -> ack.
            upload = self.compose(
                "run", "--rm", "--no-deps", "-e", "VAULT_RECOVERY_ONCE=true",
                "vault-recovery-uploader", timeout=300,
            )
            self.assertEqual(upload.returncode, 0, upload.stdout + upload.stderr)

            # --- MAINTENANCE WINDOW (ordered, mirrors the runbook).
            # 1. Pause/disable ALL THREE owned jobs and assert the pause
            #    BEFORE any stop/destroy (ordering check).
            self._assert_jobs_paused()
            # 2. Stop Hermes AND the REAL server Syncthing and assert both
            #    are not running BEFORE the destructive restore/install.
            #    (The PAIRED device's Syncthing/Obsidian cannot be stopped
            #    from the runtime — the runbook models that as an explicit
            #    operator quiescence gate, see the drill docs.)
            self._assert_service_state("hermes", running=True)
            stop = self.compose("stop", "hermes", "syncthing", timeout=120)
            self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
            self._assert_service_state("hermes", running=False)
            self._assert_service_state("syncthing", running=False)

            # --- DESTROY both live trees (writers stopped). The mount roots
            # stay (the install swaps INTO them); every file/dir inside
            # .gbrain and the vault is deleted. The live state is gone.
            destroy = self._root(
                "find /opt/data/.gbrain -mindepth 1 -delete && "
                "find /opt/data/obsidian -mindepth 1 -delete && "
                "test -z \"$(ls -A /opt/data/.gbrain)\" && "
                "test -z \"$(ls -A /opt/data/obsidian)\"",
                timeout=120,
            )
            self.assertEqual(destroy.returncode, 0, destroy.stdout + destroy.stderr)

            # --- Recover: profile-gated rclone step downloads + validates.
            recover = self.compose(
                "--profile", "recovery", "run", "--rm", "--no-deps",
                "vault-recovery-recover", "download", gen, timeout=300,
            )
            self.assertEqual(recover.returncode, 0, recover.stdout + recover.stderr)

            # --- Verify: short-lived hermes run, disposable doctor.
            verify_step = self.compose(
                "run", "--rm", "--no-deps", "--user", "10000:10000",
                "-v", f"{self.volume_names['vault-recovery-recovery']}:/recovery",
                "--entrypoint", RESTORE_WRAPPER,
                "hermes", "verify-recovery", "/recovery", timeout=300,
            )
            self.assertEqual(verify_step.returncode, 0, verify_step.stdout + verify_step.stderr)
            verified = json.loads(verify_step.stdout)
            self.assertEqual(verified["generation_id"], gen)
            self.assertEqual(verified["trees"][".gbrain"]["exact_match"], True)
            self.assertEqual(verified["trees"]["vault"]["exact_match"], True)

            # --- Verifier isolation proof (fix 3): the exported config
            # carries the LIVE absolute database_path/sync.repo_path (the
            # native init wrote them), yet the disposable doctor must never
            # open — or RE-CREATE — the live trees. Immediately after the
            # verify step, the destroyed live roots must still be EMPTY.
            untouched = self._root(
                "test -z \"$(ls -A /opt/data/.gbrain)\" && "
                "test -z \"$(ls -A /opt/data/obsidian)\"",
                timeout=120,
            )
            self.assertEqual(
                untouched.returncode, 0, untouched.stdout + untouched.stderr
            )

            # --- Install into the DESTROYED mount layout.
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
                self._short(
                    f"cat /opt/data/vault-recovery/install-journal/{gen}/journal.json",
                    timeout=60,
                ).stdout
            )
            self.assertEqual(journal["status"], "complete")

            # --- CONTROLLED RESTART (documented post-install step): start
            # Hermes again and wait for the NEW init completion marker (the
            # jobs stay paused: the drill env keeps the three owned jobs
            # disabled). The embeddings stub died with the stopped gateway,
            # so it is restarted before the vector survival proofs.
            restart = self.compose("start", "hermes", timeout=120)
            self.assertEqual(restart.returncode, 0, restart.stdout + restart.stderr)
            self._wait_for_init_complete(since="10s")
            self._start_embed_stub()

            # --- SURVIVAL proofs (vault manifest/link/doctor/vector/config/
            # schema all survive the destroy -> remote -> restore cycle).
            # 1. The restored live .gbrain opens on the real doctor.
            self._doctor_ok()
            # 2. DB-only manual link survived.
            backlinks = self._hermes(f"{GBRAIN_ENV} {NATIVE} backlinks pb", timeout=120).stdout
            self.assertIn("pa", backlinks)
            # 3. Page content survived.
            page = self._hermes(f"{GBRAIN_ENV} {NATIVE} get pa", timeout=120).stdout
            self.assertIn(PLAINTEXT_MARKER, page)
            # 4. Config survived: embeddings active (not a no-embedding sentinel).
            keyword_only = self._hermes(
                f"{GBRAIN_ENV} {NATIVE} config get search.mcp_keyword_only", timeout=60
            ).stdout.strip()
            self.assertEqual(keyword_only, "false")
            # 5. Schema-pack files + completion marker survived byte-identical
            # to the staged generation.
            for rel in (
                "active-schema-pack",
                "embedding-backfill-complete.json",
                "schema-packs/josemar/pack.yaml",
            ):
                self._hermes(f"cmp {STAGING}/{gen}/.gbrain/{rel} /opt/data/.gbrain/{rel}", timeout=60)
            # 6. Vault manifest/tree survived byte-identical to the staged
            # generation (vault note + attachment).
            for rel in ("note.md", "attachments/a.bin"):
                self._hermes(f"cmp {STAGING}/{gen}/vault/{rel} /opt/data/obsidian/{rel}", timeout=60)
            # 7. REAL restored vectors: the semantic query still returns the
            # page and the backfill verification finds ZERO stale rows — no
            # reindex/rebuild/sync.
            restored_query = self._semantic_query(PLAINTEXT_MARKER)
            self.assertIn(
                "pa", restored_query,
                f"restored semantic query did not return pa (vectors lost?):\n{restored_query[-3000:]}",
            )
            restored_verify = self._hermes(
                f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} embed --stale --include-null-signature --dry-run",
                timeout=300, check=False,
            )
            self.assertEqual(restored_verify.returncode, 0, restored_verify.stdout + restored_verify.stderr)
            self._assert_no_stale(restored_verify)

            # --- Operator rollback restores the (destroyed) pre-install state.
            # The runbook mandates stopping Hermes before install/rollback;
            # the survival proofs above need the gateway up, so the stop
            # happens here, right before the rollback — proving the rollback
            # restores the destroyed-empty pre-install state when writers are
            # stopped (the documented sequence).
            stop = self.compose("stop", "hermes", timeout=120)
            self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
            self._assert_service_state("hermes", running=False)
            rollback = self.compose(
                "run", "--rm", "--no-deps", "--user", "10000:10000",
                "--entrypoint", RESTORE_WRAPPER,
                "hermes", "rollback", gen, timeout=300,
            )
            self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
            self.assertEqual(json.loads(rollback.stdout)["status"], "rolled-back")
            # The pre-install state was destroyed, so the live trees are empty
            # again after the rollback — EXCEPT the documented install
            # leftover: a completed install/rollback leaves
            # `<live-vault>/.vault-recovery-install/<gen>/` (staged tree +
            # backup dirs) for the rollback window. The operator removes it
            # after the window (docs/vault-recovery-operations.md →
            # "Post-install cleanup"); the drill performs exactly that
            # operator cleanup and THEN asserts full emptiness. The checks
            # run in short-lived containers (hermes is stopped, per the
            # documented install/rollback sequence).
            leftover = self._short(
                "test -z \"$(ls -A /opt/data/.gbrain)\" && "
                "[ \"$(ls -A /opt/data/obsidian)\" = '.vault-recovery-install' ]",
                check=False,
            )
            if leftover.returncode != 0:
                dbg = self._short(
                    "echo GBRAIN:; ls -la /opt/data/.gbrain 2>&1; "
                    "echo VAULT:; ls -la /opt/data/obsidian 2>&1; "
                    "echo VAULT_INSTALL:; find /opt/data/obsidian/.vault-recovery-install -maxdepth 2 2>&1; "
                    "echo GLOBAL_INSTALL:; find /opt/data/.vault-recovery-install -maxdepth 2 2>&1; "
                    "echo JOURNAL:; python3 -c \"import json,glob;"
                    "j=json.load(open(glob.glob('/opt/data/vault-recovery/install-journal/*/journal.json')[0]));"
                    "print(json.dumps({'steps':j['steps'],'status':j['status']},indent=1))\"; true",
                    timeout=60, check=False,
                )
                print("=== LEFTOVER FAILURE DIAG ===\n" + dbg.stdout + dbg.stderr, flush=True)
            self.assertEqual(leftover.returncode, 0, leftover.stdout + leftover.stderr)
            self._short(
                "rm -rf /opt/data/obsidian/.vault-recovery-install && "
                "rm -rf /opt/data/.vault-recovery-install",
            )
            empty = self._short(
                "test -z \"$(ls -A /opt/data/.gbrain)\" && "
                "test -z \"$(ls -A /opt/data/obsidian)\"",
            )
            self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)

        finally:
            self.compose("down", "-v", "--remove-orphans", timeout=240)
            shutil.rmtree(self.tmp, ignore_errors=True)


class VaultRecoveryDrDrillGateTests(unittest.TestCase):
    """The REQUIRED docker gate of the disaster-recovery drill is
    fail-closed (council fix): with VAULT_RECOVERY_DR_DRILL_REQUIRED=1 a
    missing docker CLI FAILS the drill; the skip applies only when the
    drill is NOT required. Pure-logic tests — no docker, no containers:
    the gated drill method is invoked directly with a patched
    `docker_available`, so the gate ordering itself is what runs."""

    def _make_case(self, required: bool) -> "VaultRecoveryDrDrillTests":
        case = VaultRecoveryDrDrillTests(
            methodName="test_disaster_drill_destroy_both_restore_and_rollback"
        )
        case.required = required
        return case

    def test_required_without_docker_fails_closed(self) -> None:
        """The release/deploy environment (REQUIRED=1, RUN_DOCKER_TESTS=1)
        must FAIL — not skip — when the docker CLI is unavailable."""
        case = self._make_case(required=True)
        with mock.patch.dict(
            os.environ, {"VAULT_RECOVERY_DR_DRILL_REQUIRED": "1", "RUN_DOCKER_TESTS": "1"}
        ):
            with mock.patch(
                f"{__name__}.docker_available",
                return_value=False,
            ):
                with self.assertRaises(AssertionError):
                    case.test_disaster_drill_destroy_both_restore_and_rollback()

    def test_optional_without_docker_skips(self) -> None:
        """Without REQUIRED, a missing docker CLI is a skip (local
        opt-in), never a failure."""
        case = self._make_case(required=False)
        with mock.patch.dict(os.environ, {"RUN_DOCKER_TESTS": "1"}):
            with mock.patch(
                f"{__name__}.docker_available",
                return_value=False,
            ):
                with self.assertRaises(unittest.SkipTest):
                    case.test_disaster_drill_destroy_both_restore_and_rollback()

    def test_optional_without_run_docker_env_skips_before_docker_check(self) -> None:
        """The local default (no RUN_DOCKER_TESTS, not required) skips
        BEFORE the docker availability check — the drill only runs on
        explicit opt-in or the mandatory release path."""
        case = self._make_case(required=False)
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch(
                f"{__name__}.docker_available",
                return_value=True,
            ):
                with self.assertRaises(unittest.SkipTest):
                    case.test_disaster_drill_destroy_both_restore_and_rollback()


if __name__ == "__main__":
    unittest.main()
