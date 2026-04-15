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

    def _write_template(self, relative_path: str, content: str) -> Path:
        template_path = self.vault_dir / relative_path
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text(content, encoding="utf-8")
        return template_path

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

    def test_template_list_discovers_templates(self) -> None:
        self._write_template(
            "Templates/Client.md",
            """---
vg_template: true
vg_template_id: client-v1
vg_title: Client
vg_description: Client profile template
vg_default_target_folder: 05-People
vg_aliases: [client, customer]
vg_fields:
  - name: client_name
    type: string
    required: true
  - name: contact_email
    type: string
    required: true
---

# {{client_name}}
Contact: {{contact_email}}
""",
        )

        code, output = run_gateway(
            {
                "route": "template.list",
                "payload": {"query": "client"},
            },
            self.env,
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        templates = output.get("result", {}).get("templates", [])
        self.assertTrue(any(item.get("template_id") == "client-v1" for item in templates))

    def test_template_inspect_returns_field_schema(self) -> None:
        self._write_template(
            "Templates/Client.md",
            """---
vg_template: true
vg_template_id: client-v1
vg_fields:
  - name: client_name
    type: string
    required: true
    prompt: Nome do cliente?
  - name: contact_email
    type: string
    required: true
---

# {{client_name}}
Email: {{contact_email}}
""",
        )

        code, output = run_gateway(
            {
                "route": "template.inspect",
                "payload": {
                    "template_path": "Templates/Client.md",
                    "include_placeholders": True,
                },
            },
            self.env,
        )

        self.assertEqual(code, 0)
        result = output.get("result", {})
        fields = result.get("fields", [])
        self.assertTrue(any(field.get("name") == "client_name" for field in fields))
        self.assertIn("client_name", result.get("placeholders", []))

    def test_note_capture_template_missing_required_fields_requests_input(self) -> None:
        self._write_template(
            "Templates/Client.md",
            """---
vg_template: true
vg_template_id: client-v1
vg_fields:
  - name: client_name
    type: string
    required: true
  - name: contact_email
    type: string
    required: true
    prompt: Qual o email de contato?
---

# {{client_name}}
Email: {{contact_email}}
""",
        )

        code, output = run_gateway(
            {
                "route": "note.capture",
                "payload": {
                    "template_id": "client-v1",
                    "field_values": {
                        "client_name": "Acme Ltd",
                    },
                    "template_mode": "strict",
                    "missing_fields_policy": "ask",
                },
            },
            self.env,
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        self.assertTrue(output.get("needs_user_input"))
        self.assertEqual(output.get("phase"), "awaiting_template_fields")
        missing = output.get("result", {}).get("missing_fields", [])
        self.assertTrue(any(item.get("name") == "contact_email" for item in missing))
        inbox = self.vault_dir / "00-Inbox"
        inbox_notes = [path for path in inbox.rglob("*.md")] if inbox.exists() else []
        self.assertEqual(inbox_notes, [])

    def test_note_capture_template_renders_fields(self) -> None:
        self._write_template(
            "Templates/Client.md",
            """---
vg_template: true
vg_template_id: client-v1
vg_default_target_folder: 05-People
vg_fields:
  - name: client_name
    type: string
    required: true
  - name: contact_email
    type: string
    required: true
---

# {{client_name}}
Email: {{contact_email}}

## Notes
{{captured_context}}
""",
        )

        code, output = run_gateway(
            {
                "route": "note.capture",
                "payload": {
                    "template_id": "client-v1",
                    "field_values": {
                        "client_name": "Acme Ltd",
                        "contact_email": "ops@acme.example",
                    },
                    "text": "Primeira reuniao comercial",
                    "template_mode": "strict",
                },
            },
            self.env,
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        self.assertFalse(output.get("needs_user_input"))

        relative_path = output.get("result", {}).get("path", "")
        self.assertTrue(relative_path.startswith("05-People/"))
        created = self.vault_dir / relative_path
        self.assertTrue(created.exists())
        created_text = created.read_text(encoding="utf-8")
        self.assertIn("Acme Ltd", created_text)
        self.assertIn("ops@acme.example", created_text)
        self.assertIn("Primeira reuniao comercial", created_text)
        self.assertNotIn("{{client_name}}", created_text)

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

    def test_note_read_returns_content_and_frontmatter(self) -> None:
        note_path = self.vault_dir / "00-Inbox" / "test-note.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\ntype: meeting\nstatus: active\n---\n\n# Test Note\n\nSome content here.\n",
            encoding="utf-8",
        )

        code, output = run_gateway(
            {
                "route": "note.read",
                "payload": {"path": "00-Inbox/test-note.md"},
            },
            self.env,
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        result = output.get("result", {})
        self.assertEqual(result.get("frontmatter", {}).get("type"), "meeting")
        self.assertIn("Some content here.", result.get("body", ""))

    def test_note_read_selective_body_only(self) -> None:
        note_path = self.vault_dir / "00-Inbox" / "body-only.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\ntags: [test]\n---\n\nBody content.\n",
            encoding="utf-8",
        )

        code, output = run_gateway(
            {
                "route": "note.read",
                "payload": {
                    "path": "00-Inbox/body-only.md",
                    "include_frontmatter": False,
                },
            },
            self.env,
        )

        self.assertEqual(code, 0)
        result = output.get("result", {})
        self.assertNotIn("frontmatter", result)
        self.assertIn("Body content.", result.get("body", ""))

    def test_note_read_selective_frontmatter_only(self) -> None:
        note_path = self.vault_dir / "00-Inbox" / "fm-only.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\nstatus: inbox\n---\n\nBig body text.\n",
            encoding="utf-8",
        )

        code, output = run_gateway(
            {
                "route": "note.read",
                "payload": {
                    "path": "00-Inbox/fm-only.md",
                    "include_body": False,
                },
            },
            self.env,
        )

        self.assertEqual(code, 0)
        result = output.get("result", {})
        self.assertIn("frontmatter", result)
        self.assertNotIn("body", result)

    def test_note_read_requires_path(self) -> None:
        code, output = run_gateway(
            {
                "route": "note.read",
                "payload": {},
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")

    def test_note_update_replace_preserves_frontmatter(self) -> None:
        note_path = self.vault_dir / "00-Inbox" / "fm-safe.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\ntype: meeting\nstatus: active\n---\n\n# Meeting\n\nOld content.\n",
            encoding="utf-8",
        )

        code, output = run_gateway(
            {
                "route": "note.update",
                "payload": {
                    "path": "00-Inbox/fm-safe.md",
                    "text": "# Updated Title\n\nNew content only.",
                    "mode": "replace",
                },
            },
            self.env,
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        result = output.get("result", {})
        self.assertIn("warnings", result)

        updated_text = note_path.read_text(encoding="utf-8")
        self.assertTrue(updated_text.startswith("---"))
        self.assertIn("type: meeting", updated_text)
        self.assertIn("New content only.", updated_text)

    def test_note_update_replace_with_yaml_keeps_provided(self) -> None:
        note_path = self.vault_dir / "00-Inbox" / "fm-explicit.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\ntype: old\n---\n\n# Old\n",
            encoding="utf-8",
        )

        code, output = run_gateway(
            {
                "route": "note.update",
                "payload": {
                    "path": "00-Inbox/fm-explicit.md",
                    "text": "---\ntype: new\npriority: high\n---\n\n# New Body\n",
                    "mode": "replace",
                },
            },
            self.env,
        )

        self.assertEqual(code, 0)
        result = output.get("result", {})
        self.assertNotIn("warnings", result)

        updated_text = note_path.read_text(encoding="utf-8")
        self.assertIn("type: new", updated_text)
        self.assertIn("priority: high", updated_text)
        self.assertNotIn("type: old", updated_text)

    def test_note_update_frontmatter_mode_surgical(self) -> None:
        note_path = self.vault_dir / "00-Inbox" / "fm-surgical.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\ntype: task\nstatus: inbox\n---\n\n# My Task\n\nBody stays intact.\n",
            encoding="utf-8",
        )

        code, output = run_gateway(
            {
                "route": "note.update",
                "payload": {
                    "path": "00-Inbox/fm-surgical.md",
                    "mode": "frontmatter",
                    "frontmatter_fields": {
                        "status": "active",
                        "updated": "2026-04-15",
                    },
                },
            },
            self.env,
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))

        updated_text = note_path.read_text(encoding="utf-8")
        self.assertIn("status: active", updated_text)
        self.assertIn("updated: 2026-04-15", updated_text)
        self.assertIn("type: task", updated_text)
        self.assertIn("Body stays intact.", updated_text)

    def test_note_update_frontmatter_mode_requires_fields(self) -> None:
        note_path = self.vault_dir / "00-Inbox" / "fm-nofields.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("---\ntype: note\n---\n\nSome body.\n", encoding="utf-8")

        code, output = run_gateway(
            {
                "route": "note.update",
                "payload": {
                    "path": "00-Inbox/fm-nofields.md",
                    "mode": "frontmatter",
                },
            },
            self.env,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")

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
