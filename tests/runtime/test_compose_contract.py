from __future__ import annotations

from pathlib import Path
import unittest

from .helpers import ComposeRuntime


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
OVERLAY = REPO_ROOT / "docker-compose.browser-control.yml"


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


def top_level_block(text: str, header: str) -> str:
    """Return the last top-level `<header>:` block (column 0)."""
    return text.rsplit(f"\n{header}:", 1)[1]


class ComposeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = COMPOSE.read_text(encoding="utf-8")
        self.overlay = OVERLAY.read_text(encoding="utf-8")

    def test_container_names_are_parameterized_for_runtime_test_isolation(self) -> None:
        # browser-tunnel lives in the overlay; the rest in base.
        for service in ["aux-ml", "hermes", "syncthing", "tailscale", "obsidian-backup"]:
            with self.subTest(service=service):
                block = service_block(self.text, service)
                self.assertIn("container_name: ${JOSEMAR_CONTAINER_PREFIX:-josemar}-", block)
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("container_name: ${JOSEMAR_CONTAINER_PREFIX:-josemar}-", block)

    def test_hermes_volume_contract(self) -> None:
        block = service_block(self.text, "hermes")
        self.assertIn("- hermes-data:/opt/data", block)
        self.assertIn("- aux-ml-shared:/shared", block)
        self.assertIn("- obsidian-vault:/opt/data/obsidian", block)
        self.assertNotIn("VAULT_GATEWAY", block)

    def test_aux_ml_shared_volume_is_read_only(self) -> None:
        block = service_block(self.text, "aux-ml")
        self.assertIn("- aux-ml-shared:/shared:ro", block)
        self.assertIn("- AUX_ML_ALLOWED_INPUT_DIRS=${AUX_ML_ALLOWED_INPUT_DIRS:-/shared}", block)

    def test_syncthing_uses_hermes_uid_gid_for_vault_access(self) -> None:
        block = service_block(self.text, "syncthing")
        self.assertIn('user: "${HERMES_UID:-10000}:${HERMES_GID:-10000}"', block)
        self.assertIn("- obsidian-vault:/var/syncthing/data/obsidian", block)

    def test_backup_vault_mount_is_read_only(self) -> None:
        block = service_block(self.text, "obsidian-backup")
        self.assertIn("- obsidian-vault:/data/obsidian:ro", block)
        self.assertIn("- obsidian-backup-state:/state", block)

    def test_public_ports_are_localhost_bound_by_default(self) -> None:
        block = service_block(self.text, "hermes")
        self.assertIn("${HERMES_API_SERVER_BIND_IP:-127.0.0.1}", block)
        self.assertIn("${HERMES_DASHBOARD_BIND_IP:-127.0.0.1}", block)

    def test_runtime_helper_scopes_container_prefix_and_tailscale_hostname(self) -> None:
        runtime = ComposeRuntime()
        self.assertTrue(runtime.project.startswith("josemar-test-"))
        self.assertEqual(runtime.env["JOSEMAR_CONTAINER_PREFIX"], runtime.project)
        self.assertEqual(runtime.env["TAILSCALE_HOSTNAME"], f"{runtime.project}-server")

    def test_gbrain_env_defaults_are_present(self) -> None:
        block = service_block(self.text, "hermes")
        self.assertIn("- GBRAIN_HOME=${GBRAIN_HOME:-/opt/data}", block)
        self.assertIn("- GBRAIN_BRAIN_REPO=${GBRAIN_BRAIN_REPO:-/opt/data/obsidian}", block)
        self.assertIn("- GBRAIN_SCHEMA_PACK=${GBRAIN_SCHEMA_PACK:-gbrain-base-v2}", block)
        self.assertIn("- GBRAIN_SCHEMA_SOURCE_ROOT=${GBRAIN_SCHEMA_SOURCE_ROOT:-/opt/data/.gbrain/schema-packs}", block)
        self.assertIn("- GBRAIN_REFRESH_INTERVAL=${GBRAIN_REFRESH_INTERVAL:-5}", block)

    def test_gbrain_removed_env_vars_absent(self) -> None:
        """Removed gating/bounding env vars must not appear in the hermes service."""
        block = service_block(self.text, "hermes")
        self.assertNotIn("GBRAIN_ENABLED", block)
        self.assertNotIn("GBRAIN_QUERY_TIMEOUT_SECONDS", block)
        self.assertNotIn("GBRAIN_QUERY_MAX_INPUT_CHARS", block)
        self.assertNotIn("GBRAIN_QUERY_MAX_OUTPUT_CHARS", block)
        self.assertNotIn("GBRAIN_QUERY_MAX_LIMIT", block)
        self.assertNotIn("GBRAIN_CONTENT_MAX_CHARS", block)

    def test_gbrain_does_not_add_sidecar_or_volume(self) -> None:
        # No new volume and no new service should be introduced for gbrain.
        self.assertNotIn("gbrain-data:", self.text)
        self.assertNotIn("gbrain:", self.text.split("services:")[1].split("networks:")[0])
        # HERMES_WRITABLE_VOLUMES lives in docker-hermes-init.sh, not compose;
        # ensure .gbrain is not added to any compose writable-volume list.
        self.assertNotIn("HERMES_WRITABLE_VOLUMES", self.text)

    # --- Browser control: true optionality (overlay) ---

    def test_base_has_no_browser_control_network_or_service(self) -> None:
        # The base file must NOT define the browser-control network or the
        # browser-tunnel service. True optionality means base-only deploys are
        # unchanged.
        self.assertNotIn("browser-control:", self.text)
        self.assertNotIn("browser-tunnel:", self.text)
        self.assertNotIn("hermes-browser-tunnel", self.text)
        self.assertNotIn("browser-tunnel-state", self.text)
        self.assertNotIn("browser-tunnel-authorized-keys", self.text)

    def test_base_has_tailscale_serve_config_volume_and_env(self) -> None:
        # The always-present tailscale-serve-config volume and TS_SERVE_CONFIG
        # env live in base so a disabled redeploy writes {} and clears stale
        # tcp:2222.
        block = service_block(self.text, "tailscale")
        self.assertIn("TS_SERVE_CONFIG=/config/tailscale-serve/serve.json", block)
        self.assertIn("tailscale-serve-config:/config/tailscale-serve:ro", block)
        volumes_block = top_level_block(self.text, "volumes")
        self.assertIn("tailscale-serve-config:", volumes_block)

    def test_overlay_defines_browser_control_network_internal(self) -> None:
        networks_block = top_level_block(self.overlay, "networks")
        self.assertIn("browser-control:", networks_block)
        self.assertIn("internal: true", networks_block)
        self.assertIn("subnet: ${BROWSER_CONTROL_SUBNET:-172.31.250.0/29}", networks_block)

    def test_overlay_browser_tunnel_is_profiled(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("profiles:", block)
        self.assertIn("- browser-control", block)

    def test_overlay_browser_tunnel_uses_hermes_network_mode(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("network_mode: service:hermes", block)
        # Must NOT have its own networks: key (incompatible with network_mode).
        self.assertNotIn("\n    networks:", block)

    def test_overlay_browser_tunnel_publishes_no_host_ports(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertNotIn("ports:", block)
        self.assertNotIn("expose:", block)

    def test_overlay_browser_tunnel_hardening(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("read_only: true", block)
        self.assertIn("no-new-privileges:true", block)
        self.assertIn("cap_drop:", block)
        self.assertIn("- ALL", block)
        # Minimal caps: CHOWN, SETUID, SETGID, SYS_CHROOT. No NET_BIND_SERVICE.
        self.assertIn("- CHOWN", block)
        self.assertIn("- SETUID", block)
        self.assertIn("- SETGID", block)
        self.assertIn("- SYS_CHROOT", block)
        self.assertNotIn("NET_BIND_SERVICE", block)
        # Persistent host key volume.
        self.assertIn("browser-tunnel-state:/var/lib/browser-tunnel", block)
        # Authorized keys from a named volume (not a checkout bind mount).
        self.assertIn("browser-tunnel-authorized-keys:/authorized-keys:ro", block)
        # No bind mount from credentials/ checkout.
        self.assertNotIn("./credentials/", block)

    def test_overlay_browser_tunnel_fixed_constants(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        # Only BROWSER_CONTROL_HERMES_IP is configurable; SSH user/port and
        # CDP port are fixed constants in the image.
        self.assertIn("BROWSER_CONTROL_HERMES_IP=${BROWSER_CONTROL_HERMES_IP:-172.31.250.2}", block)
        self.assertNotIn("BROWSER_TUNNEL_SSH_PORT", block)
        self.assertNotIn("BROWSER_TUNNEL_CDP_PORT", block)
        self.assertNotIn("BROWSER_TUNNEL_USER", block)

    def test_overlay_hermes_has_browser_control_alias_and_static_ip(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("hermes-browser-tunnel", block)
        self.assertIn("ipv4_address: ${BROWSER_CONTROL_HERMES_IP:-172.31.250.2}", block)

    def test_overlay_tailscale_has_browser_control_static_ip(self) -> None:
        block = service_block(self.overlay, "tailscale")
        self.assertIn("ipv4_address: ${BROWSER_CONTROL_TAILSCALE_IP:-172.31.250.3}", block)

    def test_overlay_volumes_declared(self) -> None:
        volumes_block = top_level_block(self.overlay, "volumes")
        self.assertIn("browser-tunnel-state:", volumes_block)
        self.assertIn("browser-tunnel-authorized-keys:", volumes_block)

    def test_syncthing_namespace_and_volume_unchanged(self) -> None:
        block = service_block(self.text, "syncthing")
        self.assertIn("network_mode: service:tailscale", block)
        self.assertIn("- syncthing-config:/var/syncthing/config", block)
        self.assertIn("- obsidian-vault:/var/syncthing/data/obsidian", block)


if __name__ == "__main__":
    unittest.main()
