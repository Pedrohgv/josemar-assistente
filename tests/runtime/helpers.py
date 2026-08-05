from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


REPO_ROOT = Path(__file__).resolve().parents[2]

# Dedicated test-only Compose overlay that replaces the repository's real
# agent-state/credentials bind mounts with disposable empty dirs. Applied by
# ComposeRuntime so gated Docker tests can never mount production state.
TEST_ISOLATION_OVERLAY = (
    REPO_ROOT / "tests" / "runtime" / "docker-compose.test-isolation.yml"
)


def docker_available() -> bool:
    return shutil.which("docker") is not None


# Env vars that must NEVER inherit into gated Docker tests. Any production-like
# value present in the caller environment (or the repo `.env` file, which
# compose reads when the key is absent from the shell env) is forcibly blanked
# so the disposable runtime stays fail-closed. Two groups are then re-set with
# deterministic test-only values by each runtime because the base compose
# declares them with `:?` interpolation (they must be non-empty for compose to
# render): the Hermes dashboard credentials and the workspace sync timing
# (WORKSPACE_SYNC_ON_START/INTERVAL).
FORCED_EMPTY_ENV_KEYS = (
    # Telegram / gateway identity: a leaked value would let the disposable
    # runtime claim the production bot or accept production users.
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ENABLED",
    "PRIMARY_TELEGRAM_ID",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HOME_CHANNEL",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_AUTH_PASSWORD",
    "GATEWAY_AUTH_TOKEN",
    "HERMES_TELEGRAM_BOT_TOKEN",
    "HERMES_TELEGRAM_ALLOWED_USERS",
    "HERMES_TELEGRAM_HOME_CHANNEL",
    "HERMES_GATEWAY_ALLOWED_USERS",
    # Workspace state sync: a leaked value would sync production agent-state
    # or push commits from the disposable runtime. WORKSPACE_SYNC_ON_START and
    # WORKSPACE_SYNC_INTERVAL are NOT blanked here because ComposeRuntime and
    # EvalRuntime always override them with the explicit safe values
    # ("false" / "0") after this forced-clearing step.
    "WORKSPACE_STATE_REPO",
    "WORKSPACE_REPO_TOKEN",
    "WORKSPACE_GIT_BRANCH",
    "WORKSPACE_GIT_USER_EMAIL",
    "WORKSPACE_GIT_USER_NAME",
    "WORKSPACE_MEMORY_DAYS",
    # Hosted provider credentials.
    "ZAI_API_KEY",
    "GLM_API_KEY",
    "DEEPSEEK_API_KEY",
    "OLLAMA_API_KEY",
    "TAVILY_API_KEY",
    "APOLLO_IO_API_KEY",
    "HERMES_MODEL",
    # Tailscale / keyring secrets and control-plane credentials.
    "TS_AUTHKEY",
    "GOG_KEYRING_PASSWORD",
    "HERMES_API_SERVER_KEY",
    "CONTROL_UI_ALLOWED_ORIGIN_1",
    "CONTROL_UI_ALLOWED_ORIGIN_2",
    "FORCE_OVERWRITE_SKILLS",
    # Hermes dashboard credentials (session token + basic auth). Never
    # inherited from host/.env. ComposeRuntime replaces them with deterministic
    # test-only values below (the base compose declares them with `:?`
    # interpolation, so they must be non-empty for compose to render).
    "HERMES_DASHBOARD_SESSION_TOKEN",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    # Remote backup targets.
    "OBSIDIAN_GDRIVE_REMOTE",
    "OBSIDIAN_GDRIVE_PATH",
    "MNEMOSYNE_BACKUP_RCLONE_REMOTE",
    "MNEMOSYNE_BACKUP_RCLONE_PATH",
    # Mnemosyne activation/runtime switches: never inherit the production
    # provider or embedding mode into disposable stores.
    "MNEMOSYNE_PROVIDER",
    "MNEMOSYNE_DATA_DIR",
    "MNEMOSYNE_HOME",
    "MNEMOSYNE_NO_EMBEDDINGS",
    "MNEMOSYNE_EMBEDDINGS_VIA_API",
    "MNEMOSYNE_EMBEDDING_MODEL",
    "MNEMOSYNE_EMBEDDING_DIM",
    "MNEMOSYNE_EMBEDDING_API_URL",
)


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
        self._state_dir: Path | None = None
        self._credentials_dir: Path | None = None
        env = os.environ.copy()
        # Fail-closed: blank every inherited production-influencing value BEFORE
        # the test values below are applied. Compose gives the shell env
        # precedence over the repo `.env` file, so empty values here defeat a
        # production `.env` too.
        for key in FORCED_EMPTY_ENV_KEYS:
            env[key] = ""
        # Compose selection itself must never be inherited either.
        env.pop("COMPOSE_FILE", None)
        env.pop("COMPOSE_PATH_SEPARATOR", None)
        env.pop("COMPOSE_PROJECT_NAME", None)
        env.pop("COMPOSE_PROFILES", None)
        env.update(
            {
                "COMPOSE_PROJECT_NAME": self.project,
                "JOSEMAR_CONTAINER_PREFIX": self.container_prefix,
                "TAILSCALE_HOSTNAME": f"{self.project}-server",
                "HERMES_UID": "10000",
                "HERMES_GID": "10000",
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
                "AUX_ML_ENABLED": "true" if include_aux_ml else "false",
            }
        )
        if include_aux_ml:
            env["COMPOSE_PROFILES"] = "aux-ml"
        self.env = env

    def _ensure_disposable_mounts(self) -> None:
        """Create (once) disposable EMPTY dirs that replace the repository's
        real agent-state/credentials bind mounts, and expose them to compose."""
        if self._state_dir is None:
            self._state_dir = Path(tempfile.mkdtemp(prefix=f"{self.project}-state-"))
            self._credentials_dir = Path(tempfile.mkdtemp(prefix=f"{self.project}-creds-"))
        self.env["JOSEMAR_TEST_STATE_DIR"] = str(self._state_dir)
        self.env["JOSEMAR_TEST_CREDENTIALS_DIR"] = str(self._credentials_dir)

    def disposable_mounts(self) -> tuple[Path, Path]:
        """Return the (state, credentials) disposable mount dirs, creating them
        on first use. Both are empty and never point at repository state."""
        self._ensure_disposable_mounts()
        assert self._state_dir is not None and self._credentials_dir is not None
        return self._state_dir, self._credentials_dir

    def compose_command(self) -> list[str]:
        """Base `docker compose` invocation, always carrying the dedicated
        test-isolation overlay so the real agent-state/credentials bind mounts
        from docker-compose.yml are replaced with disposable empty dirs."""
        self._ensure_disposable_mounts()
        return [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            str(TEST_ISOLATION_OVERLAY),
            "-p",
            self.project,
        ]

    def run(self, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        command = [*self.compose_command(), *args]
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
        self._cleanup_disposable_mounts()

    def _cleanup_disposable_mounts(self) -> None:
        for path in (self._state_dir, self._credentials_dir):
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)
        self._state_dir = None
        self._credentials_dir = None
