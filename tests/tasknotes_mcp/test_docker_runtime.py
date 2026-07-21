"""Opt-in built-image runtime test for the real gbrain TaskNotes lifecycle."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_SCRIPT = REPO_ROOT / "tests" / "tasknotes_mcp" / "real_gbrain_e2e.py"
IMAGE = os.environ.get("TASKNOTES_TEST_IMAGE", "josemar-assistente-hermes:latest")


@unittest.skipUnless(
    os.environ.get("RUN_DOCKER_TESTS") == "1",
    "set RUN_DOCKER_TESTS=1 to run Docker runtime tests",
)
class TaskNotesDockerRuntimeTests(unittest.TestCase):
    def test_real_gbrain_mcp_lifecycle_in_built_image(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/opt/hermes/.venv/bin/python3",
                "-e",
                "TELEGRAM_BOT_TOKEN=",
                "-e",
                "PRIMARY_TELEGRAM_ID=",
                "-e",
                "TELEGRAM_ALLOWED_USERS=",
                "-e",
                "TELEGRAM_HOME_CHANNEL=",
                "-e",
                "GATEWAY_ALLOWED_USERS=",
                "-e",
                "ZAI_API_KEY=",
                "-e",
                "GLM_API_KEY=",
                "-e",
                "DEEPSEEK_API_KEY=",
                "-e",
                "OLLAMA_API_KEY=",
                "-e",
                "TAVILY_API_KEY=",
                "-v",
                f"{E2E_SCRIPT}:/tmp/real_gbrain_e2e.py:ro",
                IMAGE,
                "/tmp/real_gbrain_e2e.py",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("real-gbrain MCP lifecycle: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
