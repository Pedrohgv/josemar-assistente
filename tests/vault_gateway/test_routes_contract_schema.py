from __future__ import annotations

import json
import unittest

from .helpers import FakeVault, GATEWAY_ROOT


class RouteContractSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract_path = GATEWAY_ROOT / "contracts" / "routes.json"
        self.contract = json.loads(self.contract_path.read_text(encoding="utf-8"))

    def test_routes_contract_has_expected_shape(self) -> None:
        self.assertEqual(self.contract.get("version"), 2)
        self.assertEqual(self.contract.get("public_entrypoint"), "vault-gateway")
        self.assertIsInstance(self.contract.get("routes"), dict)
        self.assertTrue(self.contract["routes"])

    def test_every_route_declares_status_summary_and_payload(self) -> None:
        for route, metadata in self.contract["routes"].items():
            with self.subTest(route=route):
                self.assertIn(metadata.get("status"), {"active", "dormant"})
                self.assertIsInstance(metadata.get("summary"), str)
                self.assertTrue(metadata.get("summary", "").strip())
                self.assertIsInstance(metadata.get("payload"), dict)

    def test_active_routes_are_accepted_by_gateway_contract(self) -> None:
        vault = FakeVault()
        try:
            for route, metadata in self.contract["routes"].items():
                if metadata.get("status") != "active":
                    continue
                with self.subTest(route=route):
                    code, output = vault.run_gateway({"route": route, "payload": {}})
                    self.assertNotEqual(output.get("error"), "invalid_route")
                    self.assertNotEqual(output.get("error"), "route_dormant")
                    if code != 0:
                        self.assertIn(output.get("error"), {"invalid_payload", "validation_error"})
        finally:
            vault.cleanup()

    def test_dormant_transcribe_remains_dormant(self) -> None:
        vault = FakeVault()
        try:
            code, output = vault.run_gateway({"route": "transcribe", "payload": {}})
            self.assertEqual(code, 1)
            self.assertEqual(output.get("error"), "route_dormant")
            self.assertEqual(output.get("status"), "dormant")
        finally:
            vault.cleanup()

    def test_note_create_alias_resolves_to_note_capture(self) -> None:
        vault = FakeVault()
        try:
            code, output = vault.run_gateway(
                {
                    "route": "note.create",
                    "payload": {
                        "title": "Alias Note",
                        "text": "created through alias",
                    },
                }
            )
            self.assertEqual(code, 0)
            self.assertTrue(output.get("success"))
            self.assertEqual(output.get("resolved_route"), "note.capture")
            self.assertTrue((vault.vault_dir / "00-Inbox" / "Alias Note.md").exists())
        finally:
            vault.cleanup()


if __name__ == "__main__":
    unittest.main()
