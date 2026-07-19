"""Static runtime-wiring contracts for the TaskNotes MCP adapter."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class IntegrationContractTests(unittest.TestCase):
    def test_hermes_registers_serial_stdio_server_with_bounded_timeouts(self) -> None:
        text = (REPO_ROOT / "config" / "hermes-config.yaml").read_text(encoding="utf-8")
        self.assertIn("mcp_servers:", text)
        self.assertIn('command: "/opt/hermes/.venv/bin/python3"', text)
        self.assertIn('"/opt/josemar/scripts/tasknotes_mcp.py"', text)
        self.assertIn("connect_timeout: 30", text)
        self.assertIn("timeout: 180", text)
        self.assertIn("supports_parallel_tool_calls: false", text)

    def test_image_installs_server_core_lock_runner_and_skill(self) -> None:
        text = (REPO_ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")
        self.assertIn("COPY skills-factory/tasknotes /opt/josemar/skills/tasknotes", text)
        for name in (
            "tasknotes_mcp_core.py",
            "tasknotes_mcp.py",
            "tasknotes_lock_run.py",
        ):
            self.assertIn(name, text)
        self.assertIn("/opt/hermes/.venv/bin/python3 -m compileall -q", text)

    def test_refresh_cron_uses_shared_nonblocking_lock(self) -> None:
        text = (REPO_ROOT / "scripts" / "hermes-gbrain-refresh-cron.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("tasknotes.lock", text)
        self.assertIn("--nonblocking", text)
        self.assertIn("--timeout", text)
        self.assertIn('if [ "$status" -eq 75 ]', text)
        self.assertIn("refresh skipped", text)

    def test_skill_names_only_the_six_supported_tools(self) -> None:
        text = (REPO_ROOT / "skills-factory" / "tasknotes" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "task_create",
            "task_get",
            "task_list",
            "task_update",
            "task_complete",
            "task_archive",
        ):
            self.assertIn(f"`{name}`", text)
        self.assertIn("Do not use native `gbrain", text)
        self.assertIn("cron/reminder", text)
        self.assertIn("One author at a time", text)

    def test_runbook_documents_external_prerequisites_and_recovery(self) -> None:
        text = (REPO_ROOT / "docs" / "tasknotes-mcp.md").read_text(encoding="utf-8")
        self.assertIn("https://tasknotes.dev/", text)
        self.assertIn("Verify the existing gbrain Git repository", text)
        self.assertIn("reinitialize an existing vault", text)
        self.assertIn("Exclude `.git/` from Syncthing", text)
        self.assertIn("TaskNotes `4.11.1`", text)
        self.assertIn("tasknotes-recovery.marker", text)
        self.assertIn("git -C \"$GBRAIN_BRAIN_REPO\" gc", text)
        self.assertIn("gbrain sources harden", text)
        self.assertIn("local-only gbrain", text)
        self.assertIn("pulls or pushes", text)

    def test_compose_passes_refresh_timeout(self) -> None:
        text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("GBRAIN_REFRESH_TIMEOUT=${GBRAIN_REFRESH_TIMEOUT:-240}", text)


if __name__ == "__main__":
    unittest.main()
