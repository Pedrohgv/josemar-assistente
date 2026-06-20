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
        self.assertIn("- VAULT_GATEWAY_ALLOWED_ROOTS=/opt/data/obsidian:/shared", block)

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


if __name__ == "__main__":
    unittest.main()
