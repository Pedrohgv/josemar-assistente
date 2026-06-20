from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import uuid


REPO_ROOT = Path(__file__).resolve().parents[2]


def docker_available() -> bool:
    return shutil.which("docker") is not None


class ComposeRuntime:
    def __init__(self, *, include_aux_ml: bool = False) -> None:
        token = uuid.uuid4().hex[:12]
        if len(token) != 12:
            raise AssertionError("Generated test token must be 12 hex chars")
        self.project = f"josemar-test-{token}"
        self.container_prefix = self.project
        if not self.container_prefix.strip() or self.container_prefix == "josemar":
            raise AssertionError("Runtime tests must not use the production container prefix")
        self.include_aux_ml = include_aux_ml
        self.env = os.environ.copy()
        self.env.update(
            {
                "COMPOSE_PROJECT_NAME": self.project,
                "JOSEMAR_CONTAINER_PREFIX": self.container_prefix,
                "TAILSCALE_HOSTNAME": f"{self.project}-server",
                "HERMES_DASHBOARD_SESSION_TOKEN": f"test-session-{token}",
                "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "test-admin",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": f"test-password-{token}",
                "HERMES_DASHBOARD_BASIC_AUTH_SECRET": f"test-secret-{token}",
                "WORKSPACE_SYNC_ON_START": "false",
                "WORKSPACE_SYNC_INTERVAL": "0",
                "WORKSPACE_STATE_REPO": "",
                "WORKSPACE_REPO_TOKEN": "",
                "AUX_ML_ENABLED": "true" if include_aux_ml else "false",
            }
        )
        if include_aux_ml:
            self.env["COMPOSE_PROFILES"] = "aux-ml"
        else:
            self.env.pop("COMPOSE_PROFILES", None)

    def run(self, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", "-f", "docker-compose.yml", "-p", self.project, *args]
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def up(self, *services: str, timeout: int = 600) -> None:
        args = ["up", "-d", "--build", *services]
        self.run(*args, timeout=timeout)

    def exec(self, service: str, *command: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return self.run("exec", "-T", service, *command, check=check, timeout=timeout)

    def logs(self, service: str) -> str:
        process = self.run("logs", service, check=False, timeout=120)
        return process.stdout + process.stderr

    def down(self) -> None:
        self.run("down", "-v", "--remove-orphans", check=False, timeout=180)
