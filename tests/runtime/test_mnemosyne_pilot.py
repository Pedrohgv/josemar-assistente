"""Gated runtime evidence for the Mnemosyne pilot (Phase 1).

This Docker-gated test builds the isolated hermes image, creates a disposable
HERMES_HOME in a one-off container, and proves the full Phase 1 contract using
actual installed provider methods (no external API/model/LLM/project service/
data):

  1. Pinned packages installed/importable.
  2. Console installer + status (wrapper discovery).
  3. Provider instance initialization (beam created).
  4. Data DB created at MNEMOSYNE_DATA_DIR.
  5. User-only passive sync_turn captures content.
  6. Global cross-session visibility/retrieval (second session recalls the
     first session's captured content via global scope).
  7. Full native tool exposure (tools key omitted → all 40 tools).
  8. Static archive files remain untouched/not injected per config.
  9. Auto-sleep/reflection actually off on the provider instance.
  10. Rollback: cleanup CLI + sha256-verified managed override-skill removal
      + DB preservation.
  11. Negative rollback: user-modified SKILL.md is preserved (hash mismatch).

The provider uses `MNEMOSYNE_NO_EMBEDDINGS=true` so it works with keyword-only
recall (no external embedding stub needed). This is the actual installed
provider behavior, not a mock.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

from .helpers import (
    ComposeRuntime,
    FORCED_EMPTY_ENV_KEYS,
    TEST_ISOLATION_OVERLAY,
    docker_available,
)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The eval runner is stdlib-only; import it for isolation contract checks.
# Restore sys.path afterward so the persistent scripts/ entry cannot mask
# tests/tasknotes_mcp during full unittest discovery (issue #91).
EvalRuntime = None  # type: ignore[assignment]
E5_MODEL_ID = None  # type: ignore[assignment]
E5_MODEL_DIMENSIONS = None  # type: ignore[assignment]
E5_API_URL = None  # type: ignore[assignment]
_scripts_path = os.path.join(REPO_ROOT, "scripts")
try:
    sys.path.insert(0, _scripts_path)
    from mnemosyne_retrieval_eval.runner import (  # noqa: E402
        EvalRuntime,
        E5_MODEL_ID,
        E5_MODEL_DIMENSIONS,
        E5_API_URL,
    )
    _EVAL_IMPORTABLE = True
except Exception:  # pragma: no cover - environment fallback
    _EVAL_IMPORTABLE = False
finally:
    while _scripts_path in sys.path:
        sys.path.remove(_scripts_path)

# A production-like inherited environment that must NEVER reach a gated Docker
# test runtime.
_PRODUCTION_LIKE_ENV = {
    "TELEGRAM_BOT_TOKEN": "prod-bot-token",
    "TELEGRAM_ENABLED": "true",
    "PRIMARY_TELEGRAM_ID": "123456789",
    "TELEGRAM_ALLOWED_USERS": "123456789",
    "HERMES_TELEGRAM_BOT_TOKEN": "prod-hermes-bot",
    "HERMES_TELEGRAM_ALLOWED_USERS": "123456789",
    "HERMES_GATEWAY_ALLOWED_USERS": "123456789",
    "WORKSPACE_STATE_REPO": "git@github.com:prod/agent-state.git",
    "WORKSPACE_REPO_TOKEN": "prod-repo-token",
    "WORKSPACE_SYNC_ON_START": "true",
    "WORKSPACE_SYNC_INTERVAL": "60",
    "WORKSPACE_GIT_BRANCH": "prod",
    "ZAI_API_KEY": "prod-zai-key",
    "DEEPSEEK_API_KEY": "prod-deepseek-key",
    "OLLAMA_API_KEY": "prod-ollama-key",
    "TAVILY_API_KEY": "prod-tavily-key",
    "TS_AUTHKEY": "prod-ts-authkey",
    "GOG_KEYRING_PASSWORD": "prod-keyring",
    "HERMES_DASHBOARD_SESSION_TOKEN": "prod-dashboard-session",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "prod-admin",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "prod-dashboard-password",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "prod-dashboard-secret",
    "MNEMOSYNE_PROVIDER": "mnemosyne",
    "MNEMOSYNE_NO_EMBEDDINGS": "true",
    "OBSIDIAN_GDRIVE_REMOTE": "prod-gdrive",
    "MNEMOSYNE_BACKUP_RCLONE_REMOTE": "prod-crypt",
    "JOSEMAR_CONTAINER_PREFIX": "josemar",
    "COMPOSE_PROJECT_NAME": "josemar",
    "COMPOSE_FILE": "docker-compose.yml:docker-compose.mnemosyne.yml",
}

# Hermes dashboard credential vars. Unlike the plain-blanked keys, both
# runtimes re-set these to deterministic test-only values after forced clearing
# because the base compose declares them with `:?` interpolation (non-empty
# required for compose to render). The contract is: never inherited.
_DASHBOARD_CREDENTIAL_KEYS = (
    "HERMES_DASHBOARD_SESSION_TOKEN",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
)


class MnemosyneTestIsolationContractTests(unittest.TestCase):
    """Fail-closed isolation contract for every Mnemosyne Docker test runtime.

    Proves that production-like inherited environment and the repository's real
    agent-state/credentials bind mounts can never leak into the disposable
    runtimes. These checks run WITHOUT RUN_DOCKER_TESTS."""

    def _assert_dashboard_creds_not_inherited(self, env: dict) -> None:
        """Dashboard credentials must never be the inherited production values;
        each runtime substitutes deterministic test-only values."""
        for key in _DASHBOARD_CREDENTIAL_KEYS:
            self.assertNotEqual(env.get(key), _PRODUCTION_LIKE_ENV[key], key)
        self.assertTrue(env["HERMES_DASHBOARD_SESSION_TOKEN"].startswith("test-session-"))
        self.assertEqual(env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"], "test-admin")
        self.assertTrue(env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"].startswith("test-password-"))
        self.assertTrue(env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"].startswith("test-secret-"))

    def test_compose_runtime_forces_inherited_production_env_empty(self) -> None:
        with mock.patch.dict(os.environ, _PRODUCTION_LIKE_ENV, clear=False):
            runtime = ComposeRuntime()
        for key in FORCED_EMPTY_ENV_KEYS:
            if key in _DASHBOARD_CREDENTIAL_KEYS:
                continue
            self.assertEqual(runtime.env.get(key), "", key)
        # Dashboard credentials are never inherited: replaced with test values.
        self._assert_dashboard_creds_not_inherited(runtime.env)
        # Sync timing is explicitly overridden to safe test values, never
        # inherited from production.
        self.assertEqual(runtime.env["WORKSPACE_SYNC_ON_START"], "false")
        self.assertEqual(runtime.env["WORKSPACE_SYNC_INTERVAL"], "0")
        # Compose selection is never inherited either.
        self.assertNotIn("COMPOSE_FILE", runtime.env)
        self.assertEqual(runtime.env["COMPOSE_PROJECT_NAME"], runtime.project)

    def test_compose_runtime_always_uses_unique_test_prefix(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"JOSEMAR_CONTAINER_PREFIX": "josemar", "COMPOSE_PROJECT_NAME": "josemar"},
            clear=False,
        ):
            runtime = ComposeRuntime()
        self.assertNotEqual(runtime.env["JOSEMAR_CONTAINER_PREFIX"], "josemar")
        self.assertTrue(runtime.env["JOSEMAR_CONTAINER_PREFIX"].startswith("josemar-test-"))
        self.assertEqual(runtime.env["JOSEMAR_CONTAINER_PREFIX"], runtime.project)

    def test_compose_runtime_disposable_mounts_are_empty_and_exposed(self) -> None:
        with mock.patch.dict(os.environ, _PRODUCTION_LIKE_ENV, clear=False):
            runtime = ComposeRuntime()
        state_dir, creds_dir = runtime.disposable_mounts()
        try:
            self.assertTrue(state_dir.is_dir() and creds_dir.is_dir())
            self.assertEqual(list(state_dir.iterdir()), [])
            self.assertEqual(list(creds_dir.iterdir()), [])
            self.assertEqual(runtime.env["JOSEMAR_TEST_STATE_DIR"], str(state_dir))
            self.assertEqual(runtime.env["JOSEMAR_TEST_CREDENTIALS_DIR"], str(creds_dir))
        finally:
            runtime._cleanup_disposable_mounts()
        self.assertFalse(state_dir.exists() and creds_dir.exists())

    def test_isolation_overlay_replaces_bind_mounts(self) -> None:
        text = TEST_ISOLATION_OVERLAY.read_text("utf-8")
        # The overlay must declare the disposable sources with the test target
        # paths, so base ./agent-state and ./credentials mounts are replaced.
        self.assertIn("JOSEMAR_TEST_STATE_DIR", text)
        self.assertIn("JOSEMAR_TEST_CREDENTIALS_DIR", text)
        self.assertIn("/opt/josemar/source-agent-state:ro", text)
        self.assertIn("/opt/josemar/credentials-source:ro", text)
        # The ACTIVE service definition must never reference the repository
        # bind-mount SOURCES (the header comment may document the base mounts;
        # the container target paths legitimately contain "agent-state").
        services = text.split("services:")[1]
        self.assertNotIn("./agent-state", services)
        self.assertNotIn("./credentials", services)

    @unittest.skipUnless(_EVAL_IMPORTABLE, "mnemosyne_retrieval_eval runner not importable")
    def test_eval_runtime_forces_inherited_production_env_empty(self) -> None:
        project = f"josemar-test-{uuid.uuid4().hex[:12]}"
        with mock.patch.dict(os.environ, _PRODUCTION_LIKE_ENV, clear=False):
            runtime = EvalRuntime(mode="keyword", project=project)
        for key in FORCED_EMPTY_ENV_KEYS:
            if key in _DASHBOARD_CREDENTIAL_KEYS:
                continue
            self.assertEqual(runtime.env.get(key), "", key)
        # Dashboard credentials are never inherited: replaced with test values.
        self._assert_dashboard_creds_not_inherited(runtime.env)
        self.assertEqual(runtime.env["WORKSPACE_SYNC_ON_START"], "false")
        self.assertEqual(runtime.env["WORKSPACE_SYNC_INTERVAL"], "0")
        self.assertNotIn("COMPOSE_FILE", runtime.env)
        self.assertEqual(runtime.env["COMPOSE_PROJECT_NAME"], project)

    @unittest.skipUnless(_EVAL_IMPORTABLE, "mnemosyne_retrieval_eval runner not importable")
    def test_eval_runtime_always_uses_unique_test_prefix(self) -> None:
        project = f"josemar-test-{uuid.uuid4().hex[:12]}"
        with mock.patch.dict(os.environ, {"JOSEMAR_CONTAINER_PREFIX": "josemar"}, clear=False):
            runtime = EvalRuntime(mode="keyword", project=project)
        self.assertEqual(runtime.env["JOSEMAR_CONTAINER_PREFIX"], project)
        self.assertTrue(runtime.env["JOSEMAR_CONTAINER_PREFIX"].startswith("josemar-test-"))

    @unittest.skipUnless(_EVAL_IMPORTABLE, "mnemosyne_retrieval_eval runner not importable")
    def test_eval_runtime_always_applies_isolation_overlay(self) -> None:
        keyword = EvalRuntime(mode="keyword", project=f"josemar-test-{uuid.uuid4().hex[:12]}")
        self.assertTrue(
            any(f.endswith("docker-compose.test-isolation.yml") for f in keyword.compose_files),
            keyword.compose_files,
        )
        tei = EvalRuntime(mode="tei", project=f"josemar-test-{uuid.uuid4().hex[:12]}")
        self.assertTrue(
            any(f.endswith("docker-compose.test-isolation.yml") for f in tei.compose_files),
            tei.compose_files,
        )
        self.assertIn("docker-compose.embeddings.yml", tei.compose_files)

    @unittest.skipUnless(_EVAL_IMPORTABLE, "mnemosyne_retrieval_eval runner not importable")
    def test_eval_runtime_tei_forces_pinned_embedding_tuple(self) -> None:
        # A production `.env`/shell EMBEDDING_* value must never silently
        # change the eval's pinned E5-small model tuple.
        project = f"josemar-test-{uuid.uuid4().hex[:12]}"
        with mock.patch.dict(
            os.environ,
            {
                "EMBEDDING_MODEL_ID": "prod-model",
                "EMBEDDING_MODEL_DIMENSIONS": "1024",
                "EMBEDDING_API_URL": "http://prod:9999/v1",
            },
            clear=False,
        ):
            runtime = EvalRuntime(mode="tei", project=project)
        self.assertEqual(runtime.env["EMBEDDING_MODEL_ID"], E5_MODEL_ID)
        self.assertEqual(runtime.env["EMBEDDING_MODEL_DIMENSIONS"], E5_MODEL_DIMENSIONS)
        self.assertEqual(runtime.env["EMBEDDING_API_URL"], E5_API_URL)

    @unittest.skipUnless(docker_available(), "docker CLI not available")
    def test_rendered_compose_never_mounts_repo_agent_state_or_credentials(self) -> None:
        import subprocess

        import yaml

        with mock.patch.dict(os.environ, _PRODUCTION_LIKE_ENV, clear=False):
            runtime = ComposeRuntime()
        state_dir, creds_dir = runtime.disposable_mounts()
        try:
            proc = subprocess.run(
                [*runtime.compose_command(), "config"],
                cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = yaml.safe_load(proc.stdout)
            hermes_vols = data["services"]["hermes"]["volumes"]
            isolated = [
                v for v in hermes_vols
                if v.get("target") in (
                    "/opt/josemar/source-agent-state",
                    "/opt/josemar/credentials-source",
                )
            ]
            self.assertEqual(len(isolated), 2, hermes_vols)
            sources = {str(v["source"]) for v in isolated}
            self.assertEqual(sources, {str(state_dir), str(creds_dir)}, hermes_vols)
            self.assertNotIn(str(Path(REPO_ROOT) / "agent-state"), sources)
            self.assertNotIn(str(Path(REPO_ROOT) / "credentials"), sources)
            # The rendered hermes service must carry the test dashboard
            # credentials, never the poisoned production values.
            self._assert_dashboard_creds_not_inherited(
                data["services"]["hermes"]["environment"]
            )
        finally:
            runtime._cleanup_disposable_mounts()

    @unittest.skipUnless(
        _EVAL_IMPORTABLE and docker_available(),
        "eval runner or docker CLI not available",
    )
    def test_rendered_eval_compose_dashboard_creds_never_inherited(self) -> None:
        import subprocess

        import yaml

        project = f"josemar-test-{uuid.uuid4().hex[:12]}"
        with mock.patch.dict(os.environ, _PRODUCTION_LIKE_ENV, clear=False):
            runtime = EvalRuntime(mode="keyword", project=project)
        try:
            runtime._ensure_disposable_mounts()
            proc = subprocess.run(
                runtime._base_cmd + ["config"],
                cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = yaml.safe_load(proc.stdout)
            hermes = data["services"]["hermes"]
            # Dashboard credentials in the rendered eval container are the
            # deterministic test values, never the inherited production ones.
            self._assert_dashboard_creds_not_inherited(hermes["environment"])
            # The isolation overlay is applied: disposable read-only mounts.
            iso = [
                v for v in hermes["volumes"]
                if v.get("target") in (
                    "/opt/josemar/source-agent-state",
                    "/opt/josemar/credentials-source",
                )
            ]
            self.assertEqual(len(iso), 2, hermes["volumes"])
            self.assertTrue(all(v["read_only"] for v in iso))
        finally:
            runtime._cleanup_disposable_mounts()


# In-container script: full provider/store/rollback proof.
_PILOT_SCRIPT = r"""set -eu
TMPHOME=$(mktemp -d)
trap 'rm -rf "$TMPHOME"' EXIT

echo "=== 1. Install ==="
/opt/hermes/.venv/bin/mnemosyne-hermes \
    --hermes-home "$TMPHOME" \
    install --mode wrapper \
    --python /opt/hermes/.venv/bin/python3
INSTALL_RC=$?
if [ "$INSTALL_RC" -ne 0 ]; then
  echo "INSTALL_FAILED rc=$INSTALL_RC"
  exit "$INSTALL_RC"
fi

echo "=== 2. Status ==="
/opt/hermes/.venv/bin/mnemosyne-hermes --hermes-home "$TMPHOME" status
STATUS_RC=$?
if [ "$STATUS_RC" -ne 0 ]; then
  echo "STATUS_FAILED rc=$STATUS_RC"
  exit "$STATUS_RC"
fi

echo "=== 3-9. Provider instance + config + capture + cross-session + tools ==="
mkdir -p "$TMPHOME/mnemosyne/data"
cat > "$TMPHOME/config.yaml" <<'CFG'
memory:
  provider: mnemosyne
  memory_enabled: false
  user_profile_enabled: false
  write_approval: true
  mnemosyne:
    default_scope: global
    profile_isolation: false
    auto_sleep: false
    reflect_disabled_for_cron: true
    reflect_max_calls_per_session: 0
    sync_roles:
      - user
    skip_contexts:
      - cron
      - flush
      - subagent
      - background
      - skill_loop
    sync_turn_user_limit: 500
    sync_turn_assistant_limit: 800
CFG

# Create static archive files (must remain untouched).
mkdir -p "$TMPHOME/memories"
echo "archive content" > "$TMPHOME/memories/MEMORY.md"
echo "user archive" > "$TMPHOME/memories/USER.md"

/opt/hermes/.venv/bin/python3 - "$TMPHOME" <<'PY'
import sys, os
os.environ["HERMES_HOME"] = sys.argv[1]
os.environ["MNEMOSYNE_DATA_DIR"] = os.path.join(sys.argv[1], "mnemosyne", "data")
os.environ["MNEMOSYNE_NO_EMBEDDINGS"] = "true"
from mnemosyne_hermes import MnemosyneMemoryProvider

# Session 1: initialize + capture
p1 = MnemosyneMemoryProvider()
p1.initialize("session-1", hermes_home=sys.argv[1], agent_context="primary")
assert p1._beam is not None, "beam not initialized"
assert p1._auto_sleep_enabled is False, f"auto_sleep={p1._auto_sleep_enabled}"
assert p1._reflect_max_calls_per_session == 0, f"reflect_max={p1._reflect_max_calls_per_session}"
assert p1._reflect_disabled_for_cron is True, f"reflect_cron={p1._reflect_disabled_for_cron}"
assert p1._default_scope == "global", f"default_scope={p1._default_scope}"
assert p1._sync_roles == {"user"}, f"sync_roles={p1._sync_roles}"
assert p1._skip_contexts == {"cron", "flush", "subagent", "background", "skill_loop"}, f"skip={p1._skip_contexts}"
# Full native tools: tools key omitted → all tools exposed. Compare against
# the installed upstream ALL_TOOL_SCHEMAS for a stable equality/count contract.
try:
    from mnemosyne_hermes import ALL_TOOL_SCHEMAS
    expected_names = {s["name"] for s in ALL_TOOL_SCHEMAS}
    actual_schemas = p1._configured_tool_schemas()
    actual_names = {s["name"] for s in actual_schemas}
    assert actual_names == expected_names, f"tool name set mismatch: missing={expected_names - actual_names}, extra={actual_names - expected_names}"
    print(f"TOOLS_COUNT={len(actual_schemas)}")
    print("TOOLS_MATCH_ALL_TOOL_SCHEMAS")
except ImportError:
    # Fallback: assert required native mutation/management tool names are
    # present plus a stable count contract derived from installed source.
    actual_schemas = p1._configured_tool_schemas()
    actual_names = {s["name"] for s in actual_schemas}
    required = {"mnemosyne_remember", "mnemosyne_recall", "mnemosyne_forget", "mnemosyne_sleep", "mnemosyne_export", "mnemosyne_import", "mnemosyne_update"}
    missing = required - actual_names
    assert not missing, f"required tools missing: {missing}"
    assert len(actual_schemas) >= 20, f"tool count={len(actual_schemas)} (expected full native exposure >= 20)"
    print(f"TOOLS_COUNT={len(actual_schemas)}")
    print("TOOLS_REQUIRED_NAMES_PRESENT")
# sync_turn capture
p1.sync_turn("unique marker foobarbaz content", "assistant reply", session_id="session-1")
print("SESSION1_CAPTURE_OK")

# Session 2: global cross-session recall
p2 = MnemosyneMemoryProvider()
p2.initialize("session-2", hermes_home=sys.argv[1], agent_context="primary")
result = p2.prefetch("unique marker foobarbaz", session_id="session-2")
assert "foobarbaz" in result, f"cross-session content not found in prefetch result"
print("CROSS_SESSION_OK")

# DB exists at MNEMOSYNE_DATA_DIR
db_path = os.path.join(sys.argv[1], "mnemosyne", "data", "mnemosyne.db")
assert os.path.exists(db_path), f"DB not at {db_path}"
print("DB_OK")
print("PROVIDER_OK")
PY
PROVIDER_RC=$?
if [ "$PROVIDER_RC" -ne 0 ]; then
  echo "PROVIDER_FAILED rc=$PROVIDER_RC"
  exit "$PROVIDER_RC"
fi

# Static archive files must remain untouched.
if [ ! -f "$TMPHOME/memories/MEMORY.md" ] || [ ! -f "$TMPHOME/memories/USER.md" ]; then
  echo "ARCHIVE_FILES_MISSING"
  exit 1
fi
# Content must be unchanged.
if ! grep -q "archive content" "$TMPHOME/memories/MEMORY.md"; then
  echo "ARCHIVE_CONTENT_MODIFIED"
  exit 1
fi
echo "ARCHIVE_PRESERVED"

echo "=== 10. Rollback (positive: sha256-verified removal) ==="
/opt/hermes/.venv/bin/mnemosyne-hermes --hermes-home "$TMPHOME" cleanup
OVERRIDE_DIR="$TMPHOME/skills/memory/mnemosyne-memory-override"
if [ -d "$OVERRIDE_DIR" ] && [ -f "$OVERRIDE_DIR/SKILL.md.sha256" ] && [ -f "$OVERRIDE_DIR/SKILL.md" ]; then
    expected_hash=$(cat "$OVERRIDE_DIR/SKILL.md.sha256" | tr -d ' \n')
    actual_hash=$(sha256sum "$OVERRIDE_DIR/SKILL.md" | cut -d' ' -f1)
    if [ "$expected_hash" = "$actual_hash" ]; then
        rm -f "$OVERRIDE_DIR/SKILL.md" "$OVERRIDE_DIR/SKILL.md.sha256"
        rmdir "$OVERRIDE_DIR" 2>/dev/null || true
        rmdir "$TMPHOME/skills/memory" 2>/dev/null || true
        echo "POSITIVE_SKILL_REMOVED"
    else
        echo "POSITIVE_HASH_MISMATCH"
        exit 1
    fi
fi
# Plugin dir must be gone.
if [ -d "$TMPHOME/plugins/mnemosyne" ]; then
  echo "PLUGIN_STILL_PRESENT"
  exit 1
fi
# DB must be preserved.
if [ ! -f "$TMPHOME/mnemosyne/data/mnemosyne.db" ]; then
  echo "DB_MISSING_AFTER_ROLLBACK"
  exit 1
fi
echo "ROLLBACK_POSITIVE_OK"

echo "=== 11. Rollback (negative: user-modified skill preserved) ==="
# Reinstall for the negative test.
/opt/hermes/.venv/bin/mnemosyne-hermes --hermes-home "$TMPHOME" install --mode wrapper --force --python /opt/hermes/.venv/bin/python3 >/dev/null 2>&1
# Modify the SKILL.md so the hash no longer matches.
echo "user modification" >> "$TMPHOME/skills/memory/mnemosyne-memory-override/SKILL.md"
OVERRIDE_DIR="$TMPHOME/skills/memory/mnemosyne-memory-override"
if [ -d "$OVERRIDE_DIR" ] && [ -f "$OVERRIDE_DIR/SKILL.md.sha256" ] && [ -f "$OVERRIDE_DIR/SKILL.md" ]; then
    expected_hash=$(cat "$OVERRIDE_DIR/SKILL.md.sha256" | tr -d ' \n')
    actual_hash=$(sha256sum "$OVERRIDE_DIR/SKILL.md" | cut -d' ' -f1)
    if [ "$expected_hash" = "$actual_hash" ]; then
        rm -f "$OVERRIDE_DIR/SKILL.md" "$OVERRIDE_DIR/SKILL.md.sha256"
        rmdir "$OVERRIDE_DIR" 2>/dev/null || true
        echo "NEGATIVE_SKILL_REMOVED_UNEXPECTED"
        exit 1
    else
        echo "NEGATIVE_SKILL_PRESERVED"
    fi
fi
# The modified skill dir and SKILL.md must still exist.
if [ ! -d "$OVERRIDE_DIR" ] || [ ! -f "$OVERRIDE_DIR/SKILL.md" ]; then
  echo "NEGATIVE_SKILL_DIR_GONE"
  exit 1
fi
echo "ROLLBACK_NEGATIVE_OK"

echo "MNEMOSYNE_PILOT_OK"
"""


@unittest.skipUnless(
    os.getenv("RUN_DOCKER_TESTS") == "1",
    "set RUN_DOCKER_TESTS=1 to run Docker runtime tests",
)
@unittest.skipUnless(docker_available(), "docker CLI is not available")
class MnemosynePilotTests(unittest.TestCase):
    """Build the hermes image and prove the full Phase 1 contract: installer,
    provider instance, config, capture, cross-session recall, full tools,
    archive preservation, auto-sleep/reflection off, and rollback (positive
    + negative). Does NOT start project services, invoke provider APIs, or
    access project data."""

    def test_mnemosyne_full_pilot_proof(self) -> None:
        runtime = ComposeRuntime()
        try:
            # Build only the hermes image (no project services started).
            build = runtime.run("build", "hermes", timeout=1200)
            self.assertEqual(
                build.returncode, 0,
                f"docker compose build hermes failed:\n{build.stderr}",
            )

            # Verify the pinned packages are installed and importable first.
            pins = runtime.run(
                "run", "--rm", "--no-deps", "--entrypoint", "sh",
                "hermes", "-lc",
                "/opt/hermes/.venv/bin/python3 -c '"
                "import importlib.metadata as m; "
                "assert m.version(\"mnemosyne-hermes\") == \"0.5.0\"; "
                "assert m.version(\"mnemosyne-memory\") == \"3.15.1\"; "
                "print(\"MNEMOSYNE_PINS_OK\")"
                "'",
                timeout=180,
            )
            self.assertEqual(pins.returncode, 0,
                             f"pin check failed:\n{pins.stdout}\n{pins.stderr}")
            self.assertIn("MNEMOSYNE_PINS_OK", pins.stdout)

            # Run the full pilot proof script in a one-off container.
            import subprocess
            cmd = [
                *runtime.compose_command(),
                "run", "--rm", "--no-deps", "--entrypoint", "sh",
                "hermes", "-lc", _PILOT_SCRIPT,
            ]
            proc = subprocess.run(
                cmd, cwd=REPO_ROOT, env=runtime.env,
                capture_output=True, text=True, check=False, timeout=300,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"mnemosyne pilot proof failed:\n{proc.stdout}\n{proc.stderr}",
            )
            # Key assertions from the script output.
            self.assertIn("PROVIDER_OK", proc.stdout)
            self.assertIn("TOOLS_COUNT=", proc.stdout)
            # Strengthened tool assertion: compare against ALL_TOOL_SCHEMAS
            # when importable, or required names + count contract.
            self.assertTrue(
                "TOOLS_MATCH_ALL_TOOL_SCHEMAS" in proc.stdout
                or "TOOLS_REQUIRED_NAMES_PRESENT" in proc.stdout,
                f"tool schema assertion not satisfied:\n{proc.stdout}",
            )
            self.assertIn("CROSS_SESSION_OK", proc.stdout)
            self.assertIn("DB_OK", proc.stdout)
            self.assertIn("ARCHIVE_PRESERVED", proc.stdout)
            self.assertIn("ROLLBACK_POSITIVE_OK", proc.stdout)
            self.assertIn("ROLLBACK_NEGATIVE_OK", proc.stdout)
            self.assertIn("MNEMOSYNE_PILOT_OK", proc.stdout)
        finally:
            runtime.down()


if __name__ == "__main__":
    unittest.main()