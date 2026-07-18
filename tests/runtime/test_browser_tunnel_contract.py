from __future__ import annotations

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSER_TUNNEL_DIR = REPO_ROOT / "browser-tunnel"
ENTRYPOINT = BROWSER_TUNNEL_DIR / "entrypoint.sh"
SSHD_TEMPLATE = BROWSER_TUNNEL_DIR / "sshd_config.template"
DOCKERFILE = BROWSER_TUNNEL_DIR / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
OVERLAY = REPO_ROOT / "docker-compose.browser-control.yml"
HERMES_CONFIG = REPO_ROOT / "config" / "hermes-config.yaml"


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


class BrowserTunnelImageContractTests(unittest.TestCase):
    def test_image_files_exist(self) -> None:
        for path in (DOCKERFILE, ENTRYPOINT, SSHD_TEMPLATE):
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing {path}")

    def test_dockerfile_pins_alpine_320(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("FROM alpine:3.20", text)
        self.assertIn("openssh-server", text)

    def test_dockerfile_creates_unlocked_passwordless_non_root_user(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("adduser", text)
        self.assertIn("-S", text)
        self.assertIn("/bin/false", text)
        # Unlocked passwordless: passwd -d (not passwd -l which locks).
        self.assertIn("passwd -d tunnel", text)
        self.assertNotIn("passwd -l", text)
        # Home is NOT the host-key volume path (so the volume stays root-owned).
        self.assertNotIn("-h /var/lib/browser-tunnel", text)

    def test_entrypoint_validates_required_inputs(self) -> None:
        text = ENTRYPOINT.read_text(encoding="utf-8")
        # Only BROWSER_CONTROL_HERMES_IP is configurable.
        self.assertIn("BROWSER_CONTROL_HERMES_IP", text)
        # SSH user/port and CDP port are fixed constants.
        self.assertIn("SSH_PORT=2222", text)
        self.assertIn("CDP_PORT=9222", text)
        self.assertIn("TUNNEL_USER=tunnel", text)
        # Fail-fast helper is present.
        self.assertIn("die()", text)
        # Authorized keys file is required and validated at the named-volume path.
        self.assertIn("/authorized-keys/authorized_keys", text)
        self.assertIn("no valid SSH public key line found", text)

    def test_entrypoint_does_not_read_removed_env_vars(self) -> None:
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertNotIn("BROWSER_TUNNEL_SSH_PORT", text)
        self.assertNotIn("BROWSER_TUNNEL_CDP_PORT", text)
        self.assertNotIn("BROWSER_TUNNEL_USER", text)

    def test_entrypoint_generates_persistent_ed25519_host_key(self) -> None:
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("HOST_KEY_DIR=/var/lib/browser-tunnel", text)
        self.assertIn("ssh_host_ed25519_key", text)
        self.assertIn("ssh-keygen", text)
        self.assertIn("-t ed25519", text)

    def test_entrypoint_prefixes_authorized_keys_with_restrictive_options(self) -> None:
        text = ENTRYPOINT.read_text(encoding="utf-8")
        # Per-key options that prevent shell/TTY/agent/local-forwarding.
        self.assertIn("no-pty", text)
        self.assertIn("no-X11-forwarding", text)
        self.assertIn("no-agent-forwarding", text)
        self.assertIn("no-user-rc", text)
        self.assertIn('permitlisten="127.0.0.1:9222"', text)
        # The KEY_OPTS line must not set no-port-forwarding (would block remote).
        key_opts_line = next(line for line in text.splitlines() if line.startswith("KEY_OPTS="))
        self.assertNotIn("no-port-forwarding", key_opts_line)
        # authorized_keys kept root-owned (no chown to tunnel that would need
        # extra caps and then fail to chmod).
        self.assertNotIn("chown tunnel", text)

    def test_sshd_template_binds_only_to_browser_control_ip(self) -> None:
        text = SSHD_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("ListenAddress __BROWSER_CONTROL_HERMES_IP__", text)
        self.assertIn("Port 2222", text)
        # Public-key only, enforced via AuthenticationMethods.
        self.assertIn("PasswordAuthentication no", text)
        self.assertIn("KbdInteractiveAuthentication no", text)
        self.assertIn("PermitRootLogin no", text)
        self.assertIn("AuthenticationMethods publickey", text)
        # MaxSessions 0 denies shell/command/subsystem channels.
        self.assertIn("MaxSessions 0", text)
        # Remote forwarding only, restricted listener.
        self.assertIn("AllowTcpForwarding remote", text)
        self.assertIn("GatewayPorts no", text)
        self.assertIn("PermitListen 127.0.0.1:9222", text)
        # No shell/TTY/X11/agent/stream/tunnel.
        self.assertIn("PermitTTY no", text)
        self.assertIn("X11Forwarding no", text)
        self.assertIn("AllowStreamLocalForwarding no", text)
        self.assertIn("PermitTunnel no", text)

    def test_sshd_template_does_not_bind_wildcard(self) -> None:
        text = SSHD_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("0.0.0.0", text)
        self.assertNotIn("ListenAddress ::", text)

    def test_sshd_template_fixed_constants(self) -> None:
        text = SSHD_TEMPLATE.read_text(encoding="utf-8")
        # Port and user are constants, not templated.
        self.assertIn("Port 2222", text)
        self.assertIn("AllowUsers tunnel", text)
        self.assertNotIn("__BROWSER_TUNNEL_SSH_PORT__", text)
        self.assertNotIn("__BROWSER_TUNNEL_USER__", text)


class ComposeBrowserControlContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = COMPOSE.read_text(encoding="utf-8")
        self.overlay = OVERLAY.read_text(encoding="utf-8")

    def test_hermes_config_has_fixed_cdp_url(self) -> None:
        text = HERMES_CONFIG.read_text(encoding="utf-8")
        self.assertIn("browser:", text)
        self.assertIn('cdp_url: "http://127.0.0.1:9222"', text)

    def test_overlay_browser_tunnel_no_host_ports(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertNotIn("ports:", block)
        self.assertNotIn("expose:", block)

    def test_overlay_browser_tunnel_depends_on_hermes(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("depends_on:", block)
        self.assertIn("- hermes", block)

    def test_overlay_browser_tunnel_authorized_keys_named_volume(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        # Named volume, read-only, at /authorized-keys. No checkout bind mount.
        self.assertIn("browser-tunnel-authorized-keys:/authorized-keys:ro", block)
        self.assertNotIn("./credentials/", block)
        self.assertNotIn("authorized_keys:/authorized_keys", block)

    def test_overlay_browser_tunnel_tmpfs_mounts(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("tmpfs:", block)
        self.assertIn("/run", block)
        self.assertIn("/tmp", block)
        self.assertIn("/etc/ssh/runtime", block)

    def test_overlay_browser_tunnel_minimal_caps_no_net_bind_service(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("cap_drop:", block)
        self.assertIn("- ALL", block)
        self.assertIn("- CHOWN", block)
        self.assertIn("- SETUID", block)
        self.assertIn("- SETGID", block)
        self.assertIn("- SYS_CHROOT", block)
        self.assertNotIn("NET_BIND_SERVICE", block)

    def test_base_tailscale_serve_config_always_present(self) -> None:
        block = service_block(self.base, "tailscale")
        # TS_SERVE_CONFIG is a fixed path in base, not an empty-default env.
        self.assertIn("TS_SERVE_CONFIG=/config/tailscale-serve/serve.json", block)
        # Named volume, read-only. No checkout bind mount.
        self.assertIn("tailscale-serve-config:/config/tailscale-serve:ro", block)
        self.assertNotIn("./config/tailscale-serve", block)

    def test_base_has_no_generated_bind_mounts(self) -> None:
        # No bind mounts from credentials/ or config/tailscale-serve/ in base.
        self.assertNotIn("./credentials/browser-tunnel", self.base)
        self.assertNotIn("./config/tailscale-serve", self.base)

    def test_overlay_has_no_generated_bind_mounts(self) -> None:
        self.assertNotIn("./credentials/", self.overlay)
        self.assertNotIn("./config/tailscale-serve", self.overlay)


class LaptopFixtureContractTests(unittest.TestCase):
    """Contract tests for the runtime-test laptop fixture image."""

    FIXTURE_DIR = REPO_ROOT / "tests" / "runtime" / "fixtures" / "browser-tunnel-laptop"
    FIXTURE_DOCKERFILE = FIXTURE_DIR / "Dockerfile"
    FIXTURE_SERVER = FIXTURE_DIR / "mock_cdp_server.py"

    def test_fixture_files_exist(self) -> None:
        for path in (self.FIXTURE_DOCKERFILE, self.FIXTURE_SERVER):
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing {path}")

    def test_fixture_dockerfile_pins_alpine_320_and_ssh_client(self) -> None:
        text = self.FIXTURE_DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("FROM alpine:3.20.3", text)
        # openssh-client installed at BUILD time (RUN instruction).
        self.assertIn("RUN apk add", text)
        self.assertIn("openssh-client", text)

    def test_fixture_dockerfile_no_runtime_apk_add(self) -> None:
        text = self.FIXTURE_DOCKERFILE.read_text(encoding="utf-8")
        # The CMD line must not run apk add (no runtime package install).
        cmd_idx = text.find("CMD")
        if cmd_idx == -1:
            cmd_idx = len(text)
        self.assertNotIn("apk add", text[cmd_idx:])

    def test_fixture_mock_server_binds_localhost_9222(self) -> None:
        text = self.FIXTURE_SERVER.read_text(encoding="utf-8")
        self.assertIn("127.0.0.1", text)
        self.assertIn("9222", text)
        self.assertIn("CDP-MOCK-OK", text)


if __name__ == "__main__":
    unittest.main()