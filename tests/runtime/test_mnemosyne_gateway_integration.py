"""Final, disposable gateway proof for the four Mnemosyne overlays.

The test is deliberately opt-in: the normal unittest suite never downloads an
image or a model.  The Docker case uses a generated Compose override so that
the repository's agent-state, credentials, and persistent volumes cannot be
selected accidentally.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
import uuid

from .helpers import docker_available


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.embeddings.yml",
    ROOT / "docker-compose.mnemosyne.yml",
    ROOT / "docker-compose.mnemosyne-backup.yml",
)


def _docker_enabled() -> bool:
    return os.getenv("RUN_DOCKER_TESTS") == "1" and docker_available()


class MnemosyneGatewayOverlayPreflightTests(unittest.TestCase):
    def test_exact_four_overlay_order_and_recovery_is_profile_gated(self) -> None:
        self.assertEqual(
            [p.name for p in COMPOSE_FILES],
            [
                "docker-compose.yml",
                "docker-compose.embeddings.yml",
                "docker-compose.mnemosyne.yml",
                "docker-compose.mnemosyne-backup.yml",
            ],
        )
        backup = COMPOSE_FILES[-1].read_text(encoding="utf-8")
        self.assertIn('profiles: ["recovery"]', backup)
        self.assertNotIn("COMPOSE_PROFILES: recovery", backup)


@unittest.skipUnless(_docker_enabled(), "set RUN_DOCKER_TESTS=1 for Docker lifecycle evidence")
class MnemosyneGatewayIntegrationTests(unittest.TestCase):
    """Run the actual gateway, TEI, provider, cron, and encrypted uploader."""

    def setUp(self) -> None:
        token = uuid.uuid4().hex[:12]
        self.project = f"josemar-gateway-test-{token}"
        self.tmp = Path(tempfile.mkdtemp(prefix="josemar-gateway-test-"))
        self.state = self.tmp / "agent-state"
        self.credentials = self.tmp / "credentials"
        self.state.mkdir()
        self.credentials.mkdir()
        self.volume_names = {
            "hermes-data": f"{self.project}-hermes-data",
            "aux-ml-shared": f"{self.project}-aux-shared",
            "obsidian-vault": f"{self.project}-obsidian",
            "obsidian-rclone-config": f"{self.project}-rclone-config",
            "mnemosyne-backup-staging": f"{self.project}-backup-staging",
            "mnemosyne-backup-state": f"{self.project}-backup-state",
            "mnemosyne-backup-recovery": f"{self.project}-backup-recovery",
            "embedding-model-cache": f"{self.project}-embedding-cache",
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
                      - {self.state}:/opt/josemar/source-agent-state:ro
                      - {self.credentials}:/opt/josemar/credentials-source:ro
                  tailscale:
                    ports: !reset []
                  mnemosyne-backup-uploader:
                    # Keep the overlay's read-only boundary; this test only
                    # initializes the config with a separate docker run.
                    volumes:
                      - obsidian-rclone-config:/config/rclone:ro
                      - mnemosyne-backup-staging:/staging:ro
                      - mnemosyne-backup-state:/state
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
                "ZAI_API_KEY": "",
                "GLM_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "OLLAMA_API_KEY": "",
                "TAVILY_API_KEY": "",
                "WORKSPACE_GIT_BRANCH": "test",
                "MNEMOSYNE_PROVIDER": "mnemosyne",
                "MNEMOSYNE_BACKUP_EXPORT_INTERVAL": "1",
                "MNEMOSYNE_BACKUP_RUN_ON_START": "false",
                "MNEMOSYNE_BACKUP_POLL_INTERVAL": "1",
                "MNEMOSYNE_BACKUP_RCLONE_REMOTE": "mnemosyne-crypt",
                "MNEMOSYNE_BACKUP_RCLONE_PATH": "test-backups",
                "COMPOSE_PROFILES": "",
            }
        )

    def compose(self, *args: str, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose"]
        for path in (*COMPOSE_FILES, self.override):
            command.extend(("-f", str(path)))
        command.extend(("-p", self.project, *args))
        return subprocess.run(
            command, cwd=ROOT, env=self.env, capture_output=True, text=True,
            check=check, timeout=timeout,
        )

    def _init_local_crypt(self) -> None:
        """Put a local-only crypt remote in the disposable config volume."""
        volume = self.volume_names["obsidian-rclone-config"]
        state = self.volume_names["mnemosyne-backup-state"]
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none",
                "-v", f"{volume}:/config/rclone",
                "-v", f"{state}:/state",
                "rclone/rclone:latest", "config", "create", "local", "local",
            ], capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none",
                "-v", f"{volume}:/config/rclone",
                "-v", f"{state}:/state",
                "rclone/rclone:latest", "config", "create", "mnemosyne-crypt",
                "crypt", "remote", "local:/state/rclone-underlying",
                "password", "test-password", "password2", "test-password2",
            ], capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inspect = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none",
                "-v", f"{volume}:/config/rclone:ro",
                "rclone/rclone:latest", "config", "show", "mnemosyne-crypt",
            ], capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        self.assertIn("type = crypt", inspect.stdout)

    def _exec(self, service: str, *command: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return self.compose("exec", "-T", service, *command, timeout=timeout)

    def _assert_no_published_ports(self) -> None:
        rendered = self.compose("config", timeout=120, check=True).stdout
        self.assertIn("embeddings-net:", rendered)
        # The generated override removes the base's two host bindings and the
        # overlays do not publish ports for TEI or the uploader.
        self.assertNotIn("127.0.0.1:8642:", rendered)
        self.assertNotIn("127.0.0.1:9119:", rendered)
        self.assertNotIn("127.0.0.1:8384:", rendered)

    def _provider_boundary_probe(self) -> str:
        """Keep package/config checks separate from the gateway proof."""
        script = r'''
import json, os, sqlite3
from pathlib import Path
os.environ["HERMES_HOME"] = "/opt/data"
os.environ["MNEMOSYNE_DATA_DIR"] = "/opt/data/mnemosyne/data"
from mnemosyne_hermes import MnemosyneMemoryProvider
p1 = MnemosyneMemoryProvider()
p1.initialize("gateway-session-1", hermes_home="/opt/data", agent_context="primary")
assert getattr(p1, "_beam", None) is not None
assert getattr(p1, "_auto_sleep_enabled", True) is False
assert getattr(p1, "_reflect_max_calls_per_session", 1) == 0
assert os.environ.get("MNEMOSYNE_EMBEDDINGS_VIA_API") == "true"
assert os.environ.get("MNEMOSYNE_EMBEDDING_API_URL") == "http://embeddings:80/v1"
try:
    from mnemosyne_hermes import ALL_TOOL_SCHEMAS
    assert {x["name"] for x in p1._configured_tool_schemas()} == {x["name"] for x in ALL_TOOL_SCHEMAS}
    print("FULL_NATIVE_TOOLS_OK")
except ImportError:
    names = {x["name"] for x in p1._configured_tool_schemas()}
    assert {"mnemosyne_remember", "mnemosyne_recall", "mnemosyne_export"} <= names
    print("NATIVE_TOOLS_OK")
p1.sync_turn("DISPOSABLE_LOWER_LEVEL_VECTOR_MARKER", "provider reply", session_id="provider-session-1")
p2 = MnemosyneMemoryProvider()
p2.initialize("provider-session-2", hermes_home="/opt/data", agent_context="primary")
result = p2.prefetch("DISPOSABLE_LOWER_LEVEL_VECTOR_MARKER", session_id="provider-session-2")
assert "DISPOSABLE_LOWER_LEVEL_VECTOR_MARKER" in str(result)
db = Path("/opt/data/mnemosyne/data/mnemosyne.db")
assert db.exists()
conn = sqlite3.connect(db)
vec_tables = [r[0] for r in conn.execute("select name from sqlite_master where name like '%vec%'")]
vector_rows = 0
for table in vec_tables:
    try:
        vector_rows += conn.execute('select count(*) from "' + table.replace('"', '""') + '"').fetchone()[0]
    except sqlite3.DatabaseError:
        pass
conn.close()
assert vector_rows > 0, (vec_tables, vector_rows)
print("LOWER_LEVEL_PROVIDER_BOUNDARY_OK")
print("LOWER_LEVEL_CROSS_SESSION_RECALL_OK")
print("VECTOR_ROWS=%d" % vector_rows)
'''
        proc = self._exec(
            "hermes", "/opt/hermes/.venv/bin/python3", "-c", script, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("LOWER_LEVEL_PROVIDER_BOUNDARY_OK", proc.stdout)
        self.assertIn("LOWER_LEVEL_CROSS_SESSION_RECALL_OK", proc.stdout)
        self.assertTrue("FULL_NATIVE_TOOLS_OK" in proc.stdout or "NATIVE_TOOLS_OK" in proc.stdout)
        self.assertRegex(proc.stdout, r"VECTOR_ROWS=[1-9][0-9]*")
        return proc.stdout

    def _gateway_probe(self) -> str:
        """Exercise the real GatewayRunner path with a disposable local model."""
        script = r'''
import asyncio, json, os, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["HERMES_HOME"] = "/opt/data"
os.environ["MNEMOSYNE_DATA_DIR"] = "/opt/data/mnemosyne/data"
capture = Path("/opt/data/gateway-memory-stub.jsonl")
capture.unlink(missing_ok=True)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        with capture.open("a", encoding="utf-8") as f:
            f.write(json.dumps(body, sort_keys=True) + "\n")
        chunks = [
            {"id": "disposable", "object": "chat.completion.chunk", "created": 0,
             "model": body.get("model", "stub-model"),
             "choices": [{"index": 0, "delta": {"role": "assistant"},
                          "finish_reason": None}]},
            {"id": "disposable", "object": "chat.completion.chunk", "created": 0,
             "model": body.get("model", "stub-model"),
             "choices": [{"index": 0, "delta": {"content": "DETERMINISTIC_GATEWAY_RESPONSE"},
                          "finish_reason": None}]},
            {"id": "disposable", "object": "chat.completion.chunk", "created": 0,
             "model": body.get("model", "stub-model"),
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ]
        encoded = b"".join((b"data: " + json.dumps(chunk).encode() + b"\n\n") for chunk in chunks) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def log_message(self, *args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", 18080), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

import yaml
config_path = Path("/opt/data/config.yaml")
config = yaml.safe_load(config_path.read_text()) or {}
config["model"] = {"default": "disposable-stub-model", "provider": "custom",
                   "base_url": "http://127.0.0.1:18080/v1", "api_key": "disposable",
                   "api_mode": "chat_completions", "max_tokens": 64}
config.setdefault("auxiliary", {})["title_generation"] = {"enabled": False}
config_path.write_text(yaml.safe_dump(config, sort_keys=False))

from gateway.run import GatewayRunner, MessageEvent, Platform, SessionSource

def event(text, chat_id, message_id):
    source = SessionSource(platform=Platform.LOCAL, chat_id=chat_id,
                           chat_type="dm", user_id="disposable-user",
                           user_name="Disposable User", role_authorized=True)
    return MessageEvent(text=text, source=source, message_id=message_id)

async def main():
    runner = GatewayRunner()
    first = "DISPOSABLE_GATEWAY_AUTOMATIC_MEMORY_MARKER"
    await runner._handle_message(event("Please remember this exact marker: " + first,
                                       "gateway-proof-one", "gateway-message-one"))
    db = Path("/opt/data/mnemosyne/data/mnemosyne.db")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db)
        found = False
        for (table,) in conn.execute("select name from sqlite_master where type='table'"):
            try:
                found = found or any(first in str(row) for row in conn.execute(
                    'select * from "' + table.replace('"', '""') + '"'))
            except sqlite3.DatabaseError:
                pass
        conn.close()
        if found:
            break
        await asyncio.sleep(0.25)
    assert found, "automatic gateway sync did not persist the first marker"
    await runner._handle_message(event("What exact marker did I ask you to remember?",
                                       "gateway-proof-two", "gateway-message-two"))

asyncio.run(main())
server.shutdown()
requests = [json.loads(line) for line in capture.read_text().splitlines() if line]
chat_requests = [request for request in requests if "messages" in request]
assert len(chat_requests) == 2, [(len(chat_requests)), requests]
requests = chat_requests
second_context = json.dumps(requests[1], sort_keys=True)
assert "DISPOSABLE_GATEWAY_AUTOMATIC_MEMORY_MARKER" in second_context, second_context

conn = sqlite3.connect("/opt/data/mnemosyne/data/mnemosyne.db")
vec_tables = [r[0] for r in conn.execute("select name from sqlite_master where name like '%vec%'")]
vector_rows = 0
for table in vec_tables:
    try:
        vector_rows += conn.execute('select count(*) from "' + table.replace('"', '""') + '"').fetchone()[0]
    except sqlite3.DatabaseError:
        pass
conn.close()
assert vector_rows > 0, (vec_tables, vector_rows)
print("GATEWAY_EVENTS_AUTOMATIC_MEMORY_OK")
print("GATEWAY_MODEL_CALLS=%d" % len(requests))
print("SECOND_REQUEST_PREFETCH_CONTEXT_OK")
print("VECTOR_ROWS=%d" % vector_rows)
'''
        proc = self._exec(
            "hermes", "/opt/hermes/.venv/bin/python3", "-c", script, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("GATEWAY_EVENTS_AUTOMATIC_MEMORY_OK", proc.stdout)
        self.assertIn("GATEWAY_MODEL_CALLS=2", proc.stdout)
        self.assertIn("SECOND_REQUEST_PREFETCH_CONTEXT_OK", proc.stdout)
        self.assertRegex(proc.stdout, r"VECTOR_ROWS=[1-9][0-9]*")
        return proc.stdout

    def _wait_for_activation(self, timeout: int = 180) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probe = self._exec(
                "hermes", "sh", "-lc",
                "grep -q '^  provider: mnemosyne$' /opt/data/config.yaml",
                timeout=30,
            )
            if probe.returncode == 0:
                return
            time.sleep(2)
        logs = self.compose("logs", "hermes", timeout=60)
        self.fail("gateway init did not activate Mnemosyne:\n" + logs.stdout + logs.stderr)

    def _wait_for_init_complete(self, timeout: int = 180) -> None:
        """Wait for the recreated container's actual init completion marker."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            logs = self.compose("logs", "--no-color", "hermes", timeout=60)
            if "Josemar Hermes setup complete" in (logs.stdout + logs.stderr):
                return
            time.sleep(1)
        logs = self.compose("logs", "--no-color", "hermes", timeout=60)
        self.fail("Hermes init did not reach its completion marker:\n" + logs.stdout + logs.stderr)

    def _jobs(self) -> list[dict]:
        proc = self._exec(
            "hermes", "/opt/hermes/.venv/bin/python3", "-c",
            "import json; print(json.dumps(json.load(open('/opt/data/cron/jobs.json'))['jobs']))",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_four_overlay_gateway_backup_lifecycle(self) -> None:
        try:
            self._init_local_crypt()
            up = self.compose(
                "up", "-d", "--build", "--wait", "--wait-timeout", "600",
                "embeddings", "hermes", "mnemosyne-backup-uploader", timeout=1200,
            )
            self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
            self._assert_no_published_ports()
            # Compose's healthcheck is intentionally cheap (hermes version),
            # so wait for the init hook's actual config activation boundary.
            self._wait_for_activation()

            config = self._exec(
                "hermes", "/opt/hermes/.venv/bin/python3", "-c",
                "import yaml; c=yaml.safe_load(open('/opt/data/config.yaml')); "
                "m=c['memory']; n=m['mnemosyne']; "
                "assert m['provider']=='mnemosyne'; assert m['memory_enabled'] is False; "
                "assert m['user_profile_enabled'] is False; assert n['default_scope']=='global'; "
                "assert n['profile_isolation'] is False; assert n['auto_sleep'] is False; "
                "assert n['reflect_max_calls_per_session']==0; "
                "print('ACTIVE_CONFIG_OK')",
            )
            self.assertEqual(config.returncode, 0, config.stdout + config.stderr)
            self.assertIn("ACTIVE_CONFIG_OK", config.stdout)

            status = self._exec("hermes", "/opt/hermes/.venv/bin/mnemosyne-hermes", "status")
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertIn("mnemosyne", (status.stdout + status.stderr).lower())
            self.assertIn("MNEMOSYNE_PROVIDER=mnemosyne", self.compose("logs", "hermes").stdout + self.compose("logs", "hermes").stderr)
            self._provider_boundary_probe()
            self._gateway_probe()

            jobs = self._jobs()
            owned = [j for j in jobs if j.get("name") == "mnemosyne-backup-export"]
            self.assertEqual(len(owned), 1, jobs)
            self.assertEqual(owned[0]["schedule"]["kind"], "interval")
            self.assertEqual(owned[0]["schedule"]["minutes"], 1)
            self.assertEqual(owned[0]["script"], "mnemosyne-backup-export.sh")
            self.assertIs(owned[0]["no_agent"], True)
            self.assertEqual(owned[0]["workdir"], "/opt/data")

            export = self._exec(
                "hermes", "sh", "-lc",
                "/opt/data/scripts/mnemosyne-backup-export.sh",
                timeout=180,
            )
            self.assertEqual(export.returncode, 0, export.stdout + export.stderr)
            upload = self.compose(
                # The production uploader is a daemon; this disposable
                # integration invocation must use its explicit one-shot mode
                # so the compose run exits after the upload attempt.
                "run", "--rm", "--no-deps", "-e", "MNEMOSYNE_BACKUP_ONCE=true",
                "mnemosyne-backup-uploader",
                timeout=180,
            )
            self.assertEqual(upload.returncode, 0, upload.stdout + upload.stderr)
            state = self._exec(
                "hermes", "sh", "-lc",
                "test -s /opt/data/mnemosyne-backup/staging/latest && "
                "test -s /opt/data/mnemosyne-backup/uploader-state/uploaded-generations.jsonl && "
                "grep -q 'mnemosyne-backup-export' /opt/data/cron/jobs.json",
            )
            self.assertEqual(state.returncode, 0, state.stdout + state.stderr)

            remote = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    "-v", f"{self.volume_names['obsidian-rclone-config']}:/config/rclone:ro",
                    "-v", f"{self.volume_names['mnemosyne-backup-state']}:/state:ro",
                    "rclone/rclone:latest", "lsf",
                    # Uploader slots are full-snapshot directories named
                    # slot-1, slot-2, ... (not bare numeric directories).
                    "mnemosyne-crypt:test-backups/slot-1",
                ], capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(remote.returncode, 0, remote.stdout + remote.stderr)
            self.assertTrue(remote.stdout.strip(), "encrypted remote has no uploaded generation")
            self.assertNotIn("DISPOSABLE_GATEWAY_AUTOMATIC_MEMORY_MARKER", remote.stdout)

            # Same-positive-interval lifecycle: a schema-valid owned job must
            # be idempotent across a real Hermes init/restart. Wait for the
            # recreated container's init completion marker before inspecting
            # jobs.json; do not infer completion from the cheap healthcheck.
            first_id = owned[0]["id"]
            first_created_at = owned[0]["created_at"]
            same_interval_restart = self.compose(
                "up", "-d", "--force-recreate", "hermes", timeout=300,
            )
            self.assertEqual(
                same_interval_restart.returncode, 0,
                same_interval_restart.stdout + same_interval_restart.stderr,
            )
            self._wait_for_init_complete()
            jobs_same_interval = self._jobs()
            owned_same_interval = [
                j for j in jobs_same_interval
                if j.get("name") == "mnemosyne-backup-export"
            ]
            self.assertEqual(len(owned_same_interval), 1, jobs_same_interval)
            self.assertEqual(owned_same_interval[0]["id"], first_id)
            self.assertEqual(owned_same_interval[0]["created_at"], first_created_at)

            # Explicit interval=0 reinitialization is the negative lifecycle:
            # init must remove only its owned job while preserving activation.
            self.env["MNEMOSYNE_BACKUP_EXPORT_INTERVAL"] = "0"
            restart = self.compose("up", "-d", "--force-recreate", "hermes", timeout=300)
            self.assertEqual(restart.returncode, 0, restart.stdout + restart.stderr)
            self._wait_for_init_complete()
            jobs_after = self._jobs()
            self.assertFalse(
                any(j.get("name") == "mnemosyne-backup-export" for j in jobs_after),
                jobs_after,
            )
        finally:
            self.compose("down", "-v", "--remove-orphans", timeout=240)
            shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
