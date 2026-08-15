"""Static runtime-wiring contracts for the Josemar Knowledge MCP integration.

Pure-source tests: they read repo files without executing Docker or GitHub
Actions. They cover the image install, compose overlay, forced-command
wrapper, and skill/doc presence.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class IntegrationContractTests(unittest.TestCase):
    def test_image_copies_mcp_server_and_compiles(self) -> None:
        text = (REPO_ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")
        self.assertIn(
            "COPY scripts/josemar_knowledge_mcp.py /opt/josemar/scripts/josemar_knowledge_mcp.py",
            text,
        )
        self.assertIn(
            "/opt/josemar/scripts/josemar_knowledge_mcp.py",
            text,
        )

    def test_image_creates_forced_command_wrapper(self) -> None:
        text = (REPO_ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")
        self.assertIn("/usr/local/bin/josemar-knowledge-mcp-forced", text)
        self.assertIn("josemar_knowledge_mcp.py", text)
        # Wrapper execs the Hermes venv python.
        self.assertIn("/opt/hermes/.venv/bin/python3", text)
        self.assertIn("exec", text)

    def test_overlay_file_exists(self) -> None:
        path = REPO_ROOT / "docker-compose.josemar-mcp.yml"
        self.assertTrue(path.is_file())

    def test_overlay_uses_distinct_subnet_from_browser_control(self) -> None:
        overlay = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(
            encoding="utf-8"
        )
        browser = (REPO_ROOT / "docker-compose.browser-control.yml").read_text(
            encoding="utf-8"
        )
        # The actual subnet config line must use a distinct subnet.
        self.assertIn("subnet: ${JOSEMAR_MCP_SUBNET:-172.31.251.0/29}", overlay)
        self.assertIn("subnet: ${BROWSER_CONTROL_SUBNET:-172.31.250.0/29}", browser)

    def test_overlay_sshd_runs_inside_hermes(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        self.assertIn("josemar-mcp", text)
        # No separate sidecar service — sshd runs inside hermes.
        self.assertNotIn("josemar-mcp-tunnel", text)
        self.assertNotIn("network_mode: service:hermes", text)

    def test_overlay_hermes_on_josemar_mcp_network(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        self.assertIn("josemar-mcp:", text)
        self.assertIn("JOSEMAR_MCP_HERMES_IP", text)

    def test_overlay_sidecar_no_host_ports(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        # The hermes block must not publish host ports for josemar-mcp.
        block = text.split("  hermes:")[1].split("  tailscale:")[0]
        self.assertNotIn("ports:", block)

    def test_overlay_authorized_keys_mounted_into_hermes(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        self.assertIn("josemar-mcp-authorized-keys:/josemar-mcp-authorized-keys:ro", text)

    def test_overlay_no_sidecar_volumes(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        # No josemar-mcp-tunnel-state volume — host key lives in hermes-data.
        self.assertNotIn("josemar-mcp-tunnel-state", text)

    def test_overlay_network_is_internal(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        self.assertIn("internal: true", text)

    def test_overlay_authorized_keys_named_volume(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        self.assertIn("josemar-mcp-authorized-keys:/josemar-mcp-authorized-keys:ro", text)
        self.assertNotIn("./credentials/", text)

    def test_overlay_no_sidecar_state_volume(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        # No josemar-mcp-tunnel-state volume — host key lives in hermes-data.
        self.assertNotIn("josemar-mcp-tunnel-state", text)

    def test_overlay_has_no_mcp_profile(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        self.assertNotIn("profiles:", text)

    def test_overlay_no_funnel_in_config(self) -> None:
        text = (REPO_ROOT / "docker-compose.josemar-mcp.yml").read_text(encoding="utf-8")
        # The actual YAML config (non-comment lines) must not enable Funnel.
        # Strip comment lines before checking.
        code_lines = [
            line for line in text.splitlines() if not line.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        self.assertNotIn("Funnel", code)
        self.assertNotIn("funnel", code)

    def test_skill_files_exist(self) -> None:
        for rel in (
            "skills-factory/josemar-mcp/SKILL.md",
            "skills-factory/josemar-mcp/SETUP.md",
            "skills-factory/josemar-mcp/references/tools.md",
        ):
            with self.subTest(rel=rel):
                self.assertTrue(
                    (REPO_ROOT / rel).is_file(), f"missing {rel}"
                )

    def test_docs_file_exists(self) -> None:
        self.assertTrue((REPO_ROOT / "docs" / "josemar-mcp.md").is_file())

    def test_env_example_documents_josemar_mcp(self) -> None:
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("JOSEMAR_MCP_ENABLED=false", text)
        self.assertIn("JOSEMAR_MCP_AUTHORIZED_KEY", text)
        self.assertIn("172.31.251.0/29", text)

    def test_opencode_json_not_modified_with_user_ssh_path(self) -> None:
        text = (REPO_ROOT / "opencode.json").read_text(encoding="utf-8")
        self.assertNotIn("josemar_mcp", text)
        self.assertNotIn("ssh", text.lower())


if __name__ == "__main__":
    unittest.main()
