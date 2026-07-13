from __future__ import annotations

from pathlib import Path
import unittest

from .helpers import ComposeRuntime


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"


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


class ComposeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = COMPOSE.read_text(encoding="utf-8")

    def test_container_names_are_parameterized_for_runtime_test_isolation(self) -> None:
        for service in ["aux-ml", "hermes", "syncthing", "tailscale", "obsidian-backup"]:
            with self.subTest(service=service):
                block = service_block(self.text, service)
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
        self.assertIn("- GBRAIN_SCHEMA_SOURCE_ROOT=${GBRAIN_SCHEMA_SOURCE_ROOT:-/opt/data/gbrain/schema-packs}", block)
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


if __name__ == "__main__":
    unittest.main()
