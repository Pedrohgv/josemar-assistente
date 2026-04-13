import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_EXECUTABLE = REPO_ROOT / "skills-factory" / "vault-gateway" / "vault-gateway"


def run_gateway(payload: dict, env: dict) -> tuple[int, dict]:
    try:
        process = subprocess.run(
            [str(GATEWAY_EXECUTABLE)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"Gateway timed out for payload route={payload.get('route')!r}"
        ) from exc

    try:
        data = json.loads(process.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Gateway output is not valid JSON. stdout={process.stdout!r}, stderr={process.stderr!r}"
        ) from exc

    return process.returncode, data


class VaultGatewayContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp(prefix="vault-gateway-tests-")
        self.workspace_dir = Path(self._tmp_dir) / "workspace"
        self.vault_dir = Path(self._tmp_dir) / "vault"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)

        self.env = os.environ.copy()
        self.env.update(
            {
                "WORKSPACE_DIR": str(self.workspace_dir),
                "OBSIDIAN_VAULT_DIR": str(self.vault_dir),
                "VAULT_GATEWAY_ALLOWED_ROOTS": str(self.vault_dir),
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_rejects_top_level_keys_outside_contract(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.capture",
                "text": "invalid",
                "payload": {"text": "valid text"},
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_contract")

    def test_requires_route_field(self) -> None:
        code, output = run_gateway(
            {
                "payload": {"text": "abc"},
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "route_required")

    def test_rejects_legacy_action_selector(self) -> None:
        code, output = run_gateway(
            {
                "action": "note.capture",
                "payload": {"text": "abc"},
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_contract")

    def test_rejects_unknown_route(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.unknown",
                "payload": {},
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_route")

    def test_requires_payload_object_when_payload_present(self) -> None:
        code, output = run_gateway(
            {
                "route": "inbox.triage",
                "payload": "oops",
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_contract")

    def test_note_capture_happy_path(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.capture",
                "payload": {"text": "conversation with client Claudio"},
            },
            self.env,
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        self.assertEqual(output.get("route"), "note.capture")

        result = output.get("result", {})
        relative_path = result.get("path", "")
        self.assertIsInstance(relative_path, str)
        self.assertFalse(relative_path.startswith("/"))
        self.assertNotIn("..", relative_path)
        note_path = self.vault_dir / relative_path
        self.assertTrue(note_path.exists())
        self.assertIn("conversation with client Claudio", note_path.read_text(encoding="utf-8"))

    def test_rejects_unknown_payload_keys(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.capture",
                "payload": {
                    "text": "valid",
                    "unexpected": "value",
                },
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")
        details = output.get("details", [])
        self.assertTrue(any("Unknown payload keys" in item for item in details))

    def test_note_update_requires_path(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.update",
                "payload": {"text": "missing path"},
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")

    def test_note_update_rejects_path_traversal(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.update",
                "payload": {
                    "path": "../secret.md",
                    "text": "x",
                },
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")

    def test_note_file_rejects_absolute_target_folder(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.file",
                "payload": {
                    "source_path": "00-Inbox/example.md",
                    "target_folder": "/tmp",
                },
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")

    def test_note_search_rejects_invalid_limit_type(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.search",
                "payload": {
                    "query": "abc",
                    "limit": "ten",
                },
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")

    def test_note_link_rejects_invalid_bidirectional_type(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.link",
                "payload": {
                    "source_path": "00-Inbox/a.md",
                    "target_path": "00-Inbox/b.md",
                    "bidirectional": "yes",
                },
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")

    def test_onboarding_requires_state_key(self) -> None:
        code, output = run_gateway(
            {
                "route": "onboarding",
                "payload": {"mode": "new"},
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")

    def test_onboarding_port_destructive_warning_flow(self) -> None:
        code1, output1 = run_gateway(
            {
                "route": "onboarding",
                "payload": {"state_key": "sess-1", "mode": "port"},
            },
            self.env,
        )
        self.assertEqual(code1, 0)
        self.assertEqual(output1.get("phase"), "ask_destructive")

        code2, output2 = run_gateway(
            {
                "route": "onboarding",
                "payload": {"state_key": "sess-1", "input": "sim"},
            },
            self.env,
        )
        self.assertEqual(code2, 0)
        self.assertEqual(output2.get("phase"), "warn_backup")
        self.assertIn("RECOMENDACAO FORTE", output2.get("message", ""))

    def test_transcribe_is_dormant(self) -> None:
        code, output = run_gateway(
            {
                "route": "transcribe",
                "payload": {"input": "audio"},
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "route_dormant")
        self.assertFalse(output.get("executed", True))


if __name__ == "__main__":
    unittest.main()
