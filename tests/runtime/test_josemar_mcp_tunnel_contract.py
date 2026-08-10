"""Contract tests for the josemar-mcp in-container sshd config and init script.

The josemar-mcp feature runs a hardened sshd INSIDE the Hermes container
(not a separate sidecar). These tests verify the sshd config template, the
cont-init init script, and the Compose overlay structure. Pure-source: reads
repo files without executing Docker.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = REPO_ROOT / "josemar-mcp"
SSHD_TEMPLATE = MCP_DIR / "sshd_config.template"
INIT_SCRIPT = MCP_DIR / "init-sshd.sh"
SSHD_RUN = MCP_DIR / "sshd-run"
SSHD_DOWN = MCP_DIR / "down"
OVERLAY = REPO_ROOT / "docker-compose.josemar-mcp.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile.hermes"


def service_block(text: str, service: str) -> str:
    lines = text.splitlines(keepends=True)
    marker = f"  {service}:\n"
    start = next(index for index, line in enumerate(lines) if line == marker)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            end = index
            break
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            end = index
            break
    return "".join(lines[start:end])


class JosemarMcpSshdConfigContractTests(unittest.TestCase):
    def test_sshd_config_files_exist(self) -> None:
        for path in (SSHD_TEMPLATE, INIT_SCRIPT, SSHD_RUN, SSHD_DOWN):
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing {path}")

    def test_sshd_template_binds_only_to_josemar_mcp_ip(self) -> None:
        text = SSHD_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("ListenAddress __JOSEMAR_MCP_HERMES_IP__", text)
        self.assertIn("Port 2223", text)
        self.assertIn("PasswordAuthentication no", text)
        self.assertIn("KbdInteractiveAuthentication no", text)
        self.assertIn("PermitRootLogin no", text)
        self.assertIn("AuthenticationMethods publickey", text)
        self.assertIn("MaxSessions 1", text)
        self.assertIn("AllowTcpForwarding no", text)
        self.assertIn("GatewayPorts no", text)
        self.assertIn("PermitTTY no", text)
        self.assertIn("X11Forwarding no", text)
        self.assertIn("AllowStreamLocalForwarding no", text)
        self.assertIn("PermitTunnel no", text)
        # SFTP/subsystems disabled (no Subsystem directive declared).
        self.assertNotIn("Subsystem sftp", text)
        # Forced-command user is hermes (not a separate mcp user).
        self.assertIn("AllowUsers hermes", text)

    def test_sshd_template_does_not_bind_wildcard(self) -> None:
        text = SSHD_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("0.0.0.0", text)
        self.assertNotIn("ListenAddress ::", text)

    def test_sshd_template_no_funnel(self) -> None:
        text = SSHD_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("Funnel", text)
        self.assertNotIn("funnel", text)

    def test_sshd_template_host_key_in_hermes_data(self) -> None:
        text = SSHD_TEMPLATE.read_text(encoding="utf-8")
        # Host key persisted in hermes-data volume, not a separate volume.
        self.assertIn("/var/lib/josemar-mcp-hostkeys/ssh_host_ed25519_key", text)


class JosemarMcpInitScriptContractTests(unittest.TestCase):
    def test_init_script_checks_enabled_flag(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("JOSEMAR_MCP_ENABLED", text)
        self.assertIn('!= "true"', text)
        self.assertIn("skipping sshd startup", text)

    def test_init_script_checks_root(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("id -u", text)
        self.assertIn('"0"', text)

    def test_init_script_uses_hermes_ip(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("JOSEMAR_MCP_HERMES_IP", text)

    def test_init_script_validates_forced_command_and_mcp_script(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("josemar-knowledge-mcp-forced", text)
        self.assertIn("josemar_knowledge_mcp.py", text)

    def test_init_script_checks_hermes_user_exists(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("id hermes", text)

    def test_init_script_uses_unusable_random_password_hash_policy(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("openssl rand -hex 32", text)
        self.assertIn("openssl passwd -6 -stdin", text)
        self.assertIn("usermod -p", text)
        self.assertIn("sshd_config still disables password", text)

    def test_init_script_creates_run_sshd(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/run/sshd", text)
        self.assertIn("chmod 0755", text)

    def test_init_script_generates_persistent_host_key(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/var/lib/josemar-mcp-hostkeys", text)
        self.assertIn("ssh_host_ed25519_key", text)
        self.assertIn("ssh-keygen", text)
        self.assertIn("-t ed25519", text)

    def test_init_script_prefixes_authorized_keys_with_forced_command(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("no-pty", text)
        self.assertIn("no-X11-forwarding", text)
        self.assertIn("no-agent-forwarding", text)
        self.assertIn("no-user-rc", text)
        self.assertIn("no-port-forwarding", text)
        self.assertIn("command=", text)
        self.assertIn("josemar-knowledge-mcp-forced", text)

    def test_init_script_only_enables_supervised_foreground_sshd(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        # The sshd is started without -D (not in foreground) so it daemonizes
        # and the cont-init script can return, allowing the gateway to start.
        self.assertIn("services.d/josemar-mcp-sshd", text)
        self.assertIn('rm -f "${SERVICE_DIR}/down"', text)
        self.assertNotIn("sshd -f", text)

    def test_longrun_is_foreground_and_gated(self) -> None:
        text = SSHD_RUN.read_text(encoding="utf-8")
        self.assertIn("JOSEMAR_MCP_ENABLED", text)
        self.assertIn("sshd -D", text)

    def test_wrapper_recovers_only_api_key_from_s6_environment(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("ENV_DIR=\"/var/run/s6/container_environment\"", text)
        self.assertIn('API_SERVER_KEY=\"$(cat \"${ENV_DIR}/API_SERVER_KEY\")\"', text)
        self.assertIn("AcceptEnv none", SSHD_TEMPLATE.read_text(encoding="utf-8"))
        self.assertIn("PermitUserEnvironment no", SSHD_TEMPLATE.read_text(encoding="utf-8"))

    def test_init_script_non_fatal_on_error(self) -> None:
        text = INIT_SCRIPT.read_text(encoding="utf-8")
        # The init script must not abort the whole container if the optional
        # feature is misconfigured; the gateway must still start.
        self.assertIn("return 0", text)


class JosemarMcpDockerfileContractTests(unittest.TestCase):
    def test_dockerfile_installs_openssh(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("openssh-server", text)
        self.assertIn("/run/sshd", text)

    def test_dockerfile_copies_sshd_config_and_init(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("josemar-mcp/sshd_config.template", text)
        self.assertIn("josemar-mcp/init-sshd.sh", text)
        self.assertIn("josemar-mcp/sshd-run", text)
        self.assertIn("josemar-mcp/sshd-run", text)
        self.assertIn("02-josemar-mcp-sshd", text)

    def test_dockerfile_copies_mcp_skill(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("skills-factory/josemar-mcp", text)

    def test_dockerfile_creates_forced_command_wrapper(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("josemar-knowledge-mcp-forced", text)
        self.assertIn("/opt/hermes/.venv/bin/python3", text)


class JosemarMcpComposeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.overlay = OVERLAY.read_text(encoding="utf-8")

    def test_overlay_no_separate_sidecar_service(self) -> None:
        # The overlay must NOT define a josemar-mcp-tunnel service.
        lines = self.overlay.splitlines()
        service_markers = [
            line for line in lines
            if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":")
        ]
        service_names = [m.strip().rstrip(":") for m in service_markers]
        self.assertNotIn("josemar-mcp-tunnel", service_names)
        # Only hermes and tailscale should be present.
        self.assertIn("hermes", service_names)
        self.assertIn("tailscale", service_names)

    def test_overlay_hermes_on_josemar_mcp_network(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("josemar-mcp:", block)
        self.assertIn("JOSEMAR_MCP_HERMES_IP", block)

    def test_overlay_hermes_mounts_authorized_keys(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("josemar-mcp-authorized-keys:/josemar-mcp-authorized-keys:ro", block)

    def test_overlay_hermes_env_sets_enabled_and_ip(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("JOSEMAR_MCP_ENABLED=true", block)
        self.assertIn("JOSEMAR_MCP_HERMES_IP", block)

    def test_overlay_no_host_ports(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertNotIn("ports:", block)

    def test_overlay_no_funnel(self) -> None:
        code_lines = [
            line for line in self.overlay.splitlines() if not line.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        self.assertNotIn("Funnel", code)
        self.assertNotIn("funnel", code)

    def test_overlay_network_is_internal(self) -> None:
        self.assertIn("internal: true", self.overlay)

    def test_overlay_distinct_subnet(self) -> None:
        self.assertIn("172.31.251.0/29", self.overlay)

    def test_overlay_no_profile_gating(self) -> None:
        # No profiles needed — the overlay only modifies existing services.
        code_lines = [
            line for line in self.overlay.splitlines() if not line.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        self.assertNotIn("profiles:", code)

    def test_overlay_no_generated_bind_mounts(self) -> None:
        self.assertNotIn("./credentials/", self.overlay)
        self.assertNotIn("./config/tailscale-serve", self.overlay)

    def test_overlay_no_sidecar_volumes(self) -> None:
        self.assertIn("josemar-mcp-hostkeys:/var/lib/josemar-mcp-hostkeys", self.overlay)


if __name__ == "__main__":
    unittest.main()
