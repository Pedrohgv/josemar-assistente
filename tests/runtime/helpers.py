from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
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
    # TS_EXTRA_ARGS (which could smuggle an `--auth-key=...` into
    # tailscaled) is blanked separately via FORCED_EMPTY_CONTROL_SECRET_KEYS
    # to keep this tuple's contract with the Mnemosyne eval runner intact.
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
    "VAULT_RECOVERY_RCLONE_REMOTE",
    "VAULT_RECOVERY_RCLONE_PATH",
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


# Control-plane secrets that can smuggle credentials via CLI arguments even
# when the main secret is blanked: TS_EXTRA_ARGS could carry
# `--auth-key=...` into tailscaled. Kept OUT of FORCED_EMPTY_ENV_KEYS
# because the Mnemosyne eval runner (scripts/mnemosyne_retrieval_eval/
# runner.py) maintains its own parallel tuple that the pilot tests assert
# against that tuple's contract; these keys are blanked by
# sanitized_test_env() AND hard-blanked at the compose layer by the
# tailscale isolation overlay, which covers every test stack that starts
# Tailscale.
FORCED_EMPTY_CONTROL_SECRET_KEYS = (
    "TS_EXTRA_ARGS",
)

# Compose selector variables: never inherited from the caller environment
# (a leaked COMPOSE_FILE/COMPOSE_PROFILES could select production overlays,
# and a leaked COMPOSE_PROJECT_NAME could collide with the production
# project). sanitized_test_env() never copies them in the first place (the
# env is built from an explicit allowlist), so they cannot appear at all;
# tests set their own explicit values where needed.
COMPOSE_SELECTOR_ENV_KEYS = (
    "COMPOSE_FILE",
    "COMPOSE_PATH_SEPARATOR",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_PROFILES",
)

# The aux-ml image build (aux-ml/Dockerfile) copies the repo's local model
# files (aux-ml/models/, the build context) into /models and then verifies
# them against SHA256 build args. The compose defaults describe the
# DOWNLOAD sources (URL + expected hash), which may not match the local
# files actually present in the repo. ComposeRuntime pins each build arg to
# the sha256 of the LOCAL file when it exists (authoritative build context),
# so the checksum verification passes against the real files; when a file
# is absent the arg is left unset and the build downloads it with the
# default URL/hash. Values are hashes, never secrets — this does not weaken
# secret isolation.
AUX_ML_MODEL_SHA256_BUILD_ARGS = {
    "glm-ocr.gguf": "AUX_ML_GLM_OCR_SHA256",
    "mmproj-glm-ocr.gguf": "AUX_ML_GLM_OCR_MMPROJ_SHA256",
    "granite-speech-4.1-2b-Q8_0.gguf": "AUX_ML_GRANITE_SPEECH_SHA256",
    "mmproj-granite-speech-4.1-2b-f16.gguf": "AUX_ML_GRANITE_SPEECH_MMPROJ_SHA256",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def aux_ml_model_sha256_env() -> dict[str, str]:
    """Build-arg SHA256 env for the aux-ml image build, pinned to the repo's
    local model files (see AUX_ML_MODEL_SHA256_BUILD_ARGS). Only files that
    exist are pinned; missing files fall back to the compose defaults."""
    out: dict[str, str] = {}
    for filename, key in AUX_ML_MODEL_SHA256_BUILD_ARGS.items():
        path = REPO_ROOT / "aux-ml" / "models" / filename
        if path.is_file():
            out[key] = _sha256_file(path)
    return out

# The ONLY caller-environment keys passed through into the sanitized test
# env (and thus into the disposable env-file). Everything else in the
# runner environment is deliberately NOT copied: unknown runner secrets
# (shell/editor/agent tokens, ...) can neither reach the compose process
# environment nor be serialized into the disposable env-file, and can never
# become interpolation sources for future compose keys. These are the
# minimum the docker CLI / subprocess plumbing needs; none of them is
# interpolated by any compose file, so they cannot leak into rendered
# service environments.
SAFE_PASSTHROUGH_ENV_KEYS = (
    "PATH",      # docker CLI and tool lookup
    "HOME",      # docker CLI config lookup (also ssh/git behavior)
    "TMPDIR",    # temp-file placement for the CLI/subprocesses
    # Daemon selectors: gated tests must reach the SAME docker daemon the
    # runner uses (e.g. docker-in-docker CI). Not interpolated by the
    # compose files, so safe to pass through.
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
)


def sanitized_test_env() -> dict[str, str]:
    """A fail-closed environment for gated Docker tests, built from an
    EXPLICIT SAFE ALLOWLIST — never a copy of the caller environment.

    Contains ONLY:
      1. the SAFE_PASSTHROUGH_ENV_KEYS values present in the caller
         environment (docker CLI plumbing; not interpolated by compose),
      2. every FORCED_EMPTY_ENV_KEYS key blanked (compose gives the shell
         env precedence over the repo `.env` file, so empty values here
         defeat a production `.env` too),
      3. whatever deterministic test-only values callers add on top
         (project name, dashboard credentials, ...).

    Unknown runner secrets are absent by construction, so they can neither
    reach the compose process environment nor the disposable env-file, and
    cannot be picked up by future compose interpolation.
    """
    env: dict[str, str] = {}
    for key in SAFE_PASSTHROUGH_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    for key in FORCED_EMPTY_ENV_KEYS:
        env[key] = ""
    for key in FORCED_EMPTY_CONTROL_SECRET_KEYS:
        env[key] = ""
    return env


def write_disposable_env_file(env: dict[str, str], path: Path) -> Path:
    """Write ``env`` as a docker compose env-file (KEY=VALUE lines).

    Passed via ``docker compose --env-file <file>`` so the repository's
    real ``.env`` file is NEVER read by test compose invocations. Combined
    with the sanitized process env (shell env wins over the env-file),
    production-like values are defeated by two independent layers: even if
    a future key is forgotten in one of them, the other still blanks it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in env.items()),
        encoding="utf-8",
    )
    return path


# Paths the runtime tests depend on being writable by the hermes runtime
# user before any exec probe. Mirrors the writable surface the permission
# contract asserts: HERMES_HOME, the aux-ml handoff volume, the obsidian
# vault (a fresh named volume's ownership comes from the base image copy —
# the init deliberately does NOT chown this cross-service volume), and the
# vault-recovery staging dir (chowned by the init allowlist).
HERMES_WRITABLE_PROBE_PATHS = (
    "/opt/data",
    "/shared",
    "/opt/data/obsidian",
    "/opt/data/vault-recovery/staging",
)


def hermes_writable_probe_command() -> str:
    """`sh -lc` body asserting every HERMES_WRITABLE_PROBE_PATHS entry is
    writable by the hermes runtime user (issue #110 conventions: never as
    root), by touching and removing a probe file in each path."""
    probes = " ".join(
        f"{path}/.runtime-perm-probe" for path in HERMES_WRITABLE_PROBE_PATHS
    )
    return "su -s /bin/sh hermes -c " f"'touch {probes} && rm -f {probes}'"


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
        self._env_file: Path | None = None
        # Fail-closed: the centralized sanitizer blanks every inherited
        # production-influencing value BEFORE the test values below are
        # applied. Compose gives the shell env precedence over the repo
        # `.env` file, so empty values here defeat a production `.env` too.
        env = sanitized_test_env()
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
            # Pin the aux-ml image build-arg SHA256 values to the repo's
            # local model files (build context), so the Dockerfile's
            # checksum verification matches the files actually present.
            env.update(aux_ml_model_sha256_env())
        self.env = env

    def _ensure_disposable_mounts(self) -> None:
        """Create (once) disposable EMPTY dirs that replace the repository's
        real agent-state/credentials bind mounts, and expose them to compose.
        Also (re)writes the disposable env-file so it always reflects the
        CURRENT env (callers mutate ``self.env`` after construction)."""
        if self._state_dir is None:
            self._state_dir = Path(tempfile.mkdtemp(prefix=f"{self.project}-state-"))
            self._credentials_dir = Path(tempfile.mkdtemp(prefix=f"{self.project}-creds-"))
            self._env_file = (
                Path(tempfile.mkdtemp(prefix=f"{self.project}-env-")) / "compose.env"
            )
        self.env["JOSEMAR_TEST_STATE_DIR"] = str(self._state_dir)
        self.env["JOSEMAR_TEST_CREDENTIALS_DIR"] = str(self._credentials_dir)
        assert self._env_file is not None
        write_disposable_env_file(self.env, self._env_file)

    def disposable_mounts(self) -> tuple[Path, Path]:
        """Return the (state, credentials) disposable mount dirs, creating them
        on first use. Both are empty and never point at repository state."""
        self._ensure_disposable_mounts()
        assert self._state_dir is not None and self._credentials_dir is not None
        return self._state_dir, self._credentials_dir

    def compose_command(self) -> list[str]:
        """Base `docker compose` invocation, always carrying the dedicated
        test-isolation overlay so the real agent-state/credentials bind mounts
        from docker-compose.yml are replaced with disposable empty dirs, and
        always pinning the disposable env-file so the repo `.env` is never
        read (defense in depth against production-like values)."""
        self._ensure_disposable_mounts()
        assert self._env_file is not None
        return [
            "docker",
            "compose",
            "--env-file",
            str(self._env_file),
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

    def wait_until_hermes_writable(self, timeout: int = 90) -> None:
        """Wait until every HERMES_WRITABLE_PROBE_PATHS entry is writable
        by the hermes runtime user.

        `docker compose up -d` returns as soon as the container STARTS,
        while docker-hermes-init.sh chowns the root-owned named volumes
        asynchronously during s6 cont-init — an immediate `exec` can race
        it and hit "Permission denied" on /shared (and /opt/data/obsidian
        is a fresh named volume whose ownership comes from the image copy,
        never from the init allowlist). Probes the exact writable state
        the runtime tests depend on (as the hermes runtime user, issue
        #110 conventions) and fails with the last probe output on
        timeout."""
        deadline = time.monotonic() + timeout
        last: subprocess.CompletedProcess[str] | None = None
        while time.monotonic() < deadline:
            proc = self.exec(
                "hermes", "sh", "-lc", hermes_writable_probe_command(),
                check=False, timeout=30,
            )
            if proc.returncode == 0:
                return
            last = proc
            time.sleep(2)
        detail = ""
        if last is not None:
            detail = f": {(last.stderr or last.stdout).strip()[-800:]}"
        raise AssertionError(
            f"hermes writable paths not ready within {timeout}s{detail}"
        )

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
        if self._env_file is not None:
            shutil.rmtree(self._env_file.parent, ignore_errors=True)
            self._env_file = None
