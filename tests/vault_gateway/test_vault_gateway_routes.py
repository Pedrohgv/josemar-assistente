from __future__ import annotations

import unittest

from .helpers import FakeVault


class VaultGatewayRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeVault()

    def tearDown(self) -> None:
        self.fake.cleanup()

    def test_note_read_returns_frontmatter_and_body(self) -> None:
        self.fake.write_note("01-Projects/Alpha.md", "---\ntype: project\n---\n\n# Alpha\n\nbody text\n")

        code, output = self.fake.run_gateway(
            {"route": "note.read", "payload": {"path": "01-Projects/Alpha.md"}}
        )

        self.assertEqual(code, 0)
        result = output["result"]
        self.assertEqual(result["frontmatter"].get("type"), "project")
        self.assertIn("body text", result["body"])

    def test_note_read_rejects_path_traversal(self) -> None:
        code, output = self.fake.run_gateway(
            {"route": "note.read", "payload": {"path": "../outside.md"}}
        )

        self.assertEqual(code, 1)
        self.assertIn(output.get("error"), {"invalid_payload", "validation_error"})

    def test_note_update_frontmatter_mode(self) -> None:
        self.fake.write_note("01-Projects/Alpha.md", "# Alpha\n\nbody\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.update",
                "payload": {
                    "path": "01-Projects/Alpha.md",
                    "mode": "frontmatter",
                    "frontmatter_fields": {"status": "active"},
                },
            }
        )

        self.assertEqual(code, 0)
        self.assertEqual(output["result"]["mode"], "frontmatter")
        content = (self.fake.vault_dir / "01-Projects" / "Alpha.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\nstatus: active\n---\n"))
        self.assertIn("# Alpha", content)

    def test_note_update_rejects_missing_section(self) -> None:
        self.fake.write_note("01-Projects/Alpha.md", "# Alpha\n\nbody\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.update",
                "payload": {
                    "path": "01-Projects/Alpha.md",
                    "mode": "section_append",
                    "section_heading": "Missing",
                    "text": "new",
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")
        self.assertIn("was not found", output.get("details", ""))

    def test_note_search_finds_content_and_honors_prefix(self) -> None:
        self.fake.write_note("01-Projects/Alpha.md", "# Alpha\n\nneedle project\n")
        self.fake.write_note("03-Resources/Beta.md", "# Beta\n\nneedle resource\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.search",
                "payload": {"query": "needle", "path_prefix": "01-Projects"},
            }
        )

        self.assertEqual(code, 0)
        result_paths = [item["path"] for item in output["result"]["results"]]
        self.assertEqual(result_paths, ["01-Projects/Alpha.md"])

    def test_note_search_rejects_escaping_prefix(self) -> None:
        code, output = self.fake.run_gateway(
            {"route": "note.search", "payload": {"query": "x", "path_prefix": ".."}}
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")

    def test_note_link_creates_bidirectional_wikilinks(self) -> None:
        self.fake.write_note("01-Projects/Alpha.md", "# Alpha\n")
        self.fake.write_note("01-Projects/Beta.md", "# Beta\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.link",
                "payload": {
                    "source_path": "01-Projects/Alpha.md",
                    "target_path": "01-Projects/Beta.md",
                    "bidirectional": True,
                },
            }
        )

        self.assertEqual(code, 0)
        self.assertTrue(output["result"]["inserted_forward"])
        self.assertTrue(output["result"]["inserted_back"])
        self.assertIn("[[Beta]]", (self.fake.vault_dir / "01-Projects" / "Alpha.md").read_text(encoding="utf-8"))
        self.assertIn("[[Alpha]]", (self.fake.vault_dir / "01-Projects" / "Beta.md").read_text(encoding="utf-8"))

    def test_note_file_moves_note_and_uses_unique_destination(self) -> None:
        self.fake.write_note("00-Inbox/Alpha.md", "# Incoming Alpha\n")
        self.fake.write_note("01-Projects/Alpha.md", "# Existing Alpha\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.file",
                "payload": {
                    "source_path": "00-Inbox/Alpha.md",
                    "target_folder": "01-Projects",
                },
            }
        )

        self.assertEqual(code, 0)
        self.assertEqual(output["result"]["to"], "01-Projects/Alpha-2.md")
        self.assertFalse((self.fake.vault_dir / "00-Inbox" / "Alpha.md").exists())
        self.assertTrue((self.fake.vault_dir / "01-Projects" / "Alpha-2.md").exists())

    def test_note_rename_rewrites_wikilinks(self) -> None:
        self.fake.write_note("01-Projects/Alpha.md", "# Alpha\n")
        self.fake.write_note("01-Projects/Beta.md", "# Beta\n\n[[Alpha]] and [[Alpha|alias]]\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.rename",
                "payload": {
                    "path": "01-Projects/Alpha.md",
                    "new_title": "Alpha Renamed",
                },
            }
        )

        self.assertEqual(code, 0)
        self.assertEqual(output["result"]["to"], "01-Projects/Alpha Renamed.md")
        beta = (self.fake.vault_dir / "01-Projects" / "Beta.md").read_text(encoding="utf-8")
        self.assertIn("[[Alpha Renamed]]", beta)
        self.assertIn("[[Alpha Renamed|alias]]", beta)

    def test_template_list_and_inspect_structured_template(self) -> None:
        self.fake.write_note(
            "Templates/Project.md",
            "---\n"
            "vg_template_id: project\n"
            "vg_template_name: Project\n"
            "vg_template_type: capture\n"
            "vg_fields:\n"
            "  - name: project_name\n"
            "    type: string\n"
            "    required: true\n"
            "---\n\n# {{project_name}}\n",
        )

        code, list_output = self.fake.run_gateway({"route": "template.list", "payload": {}})
        self.assertEqual(code, 0)
        templates = list_output["result"]["templates"]
        self.assertTrue(any(item.get("template_id") == "project" for item in templates))

        code, inspect_output = self.fake.run_gateway(
            {"route": "template.inspect", "payload": {"template_path": "Templates/Project.md"}}
        )
        self.assertEqual(code, 0)
        self.assertEqual(inspect_output["result"]["template_id"], "project")
        self.assertEqual(inspect_output["result"]["fields"][0]["name"], "project_name")

    def test_summary_routes_return_safe_fake_vault_summaries(self) -> None:
        self.fake.write_note("00-Inbox/Inbox.md", "# Inbox\n#tag\n")
        self.fake.write_note("Loose.md", "# Loose\n")
        self.fake.write_note("03-Resources/Dupe.md", "# Dupe\n")
        self.fake.write_note("01-Projects/Dupe.md", "# Dupe\n")
        (self.fake.vault_dir / "02-Areas" / "Empty").mkdir(parents=True, exist_ok=True)

        for route in ["inbox.triage", "vault.audit", "vault.defrag", "vault.deep-clean", "tags.garden"]:
            with self.subTest(route=route):
                code, output = self.fake.run_gateway({"route": route, "payload": {}})
                self.assertEqual(code, 0)
                self.assertTrue(output.get("success"))
                self.assertIn("summary", output)

    def _write_daily_template(self) -> None:
        self.fake.write_note(
            "Templates/Daily Note.md",
            "---\n"
            "type: daily\n"
            "date: \"\"\n"
            "tags: [daily]\n"
            "vg_template: true\n"
            "vg_template_id: daily-v1\n"
            "vg_title: Daily Note\n"
            "vg_default_target_folder: 07-Daily\n"
            "vg_fields:\n"
            "  - name: Date\n"
            "    type: string\n"
            "    required: true\n"
            "    title: true\n"
            "---\n\n"
            "# {{Date}}\n\n"
            "## Morning Intention\n",
        )

    def test_note_instantiate_daily_template_creates_dated_note(self) -> None:
        self._write_daily_template()

        code, output = self.fake.run_gateway(
            {
                "route": "note.instantiate",
                "payload": {
                    "template_id": "daily-v1",
                    "field_values": {"Date": "2026-07-03"},
                },
            }
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        result = output["result"]
        self.assertEqual(result["action"], "created")
        self.assertTrue(result["created"])
        self.assertEqual(result["path"], "07-Daily/2026-07-03.md")

        content = (self.fake.vault_dir / "07-Daily" / "2026-07-03.md").read_text(encoding="utf-8")
        self.assertIn("# 2026-07-03", content)
        self.assertNotIn("vg_fields", content)
        self.assertNotIn("vg_template", content)
        self.assertIn("type: daily", content)
        self.assertIn("tags: [daily]", content)

    def test_note_instantiate_explicit_path_skip_returns_not_created(self) -> None:
        self._write_daily_template()
        self.fake.write_note(
            "07-Daily/2026-07-03.md",
            "---\ntype: daily\ndate: 2026-07-03\n---\n\n# Existing\n",
        )

        code, output = self.fake.run_gateway(
            {
                "route": "note.instantiate",
                "payload": {
                    "template_id": "daily-v1",
                    "field_values": {"Date": "2026-07-03"},
                    "path": "07-Daily/2026-07-03.md",
                    "if_exists": "skip",
                },
            }
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        result = output["result"]
        self.assertEqual(result["action"], "already_exists")
        self.assertFalse(result["created"])
        self.assertEqual(
            (self.fake.vault_dir / "07-Daily" / "2026-07-03.md").read_text(encoding="utf-8"),
            "---\ntype: daily\ndate: 2026-07-03\n---\n\n# Existing\n",
        )

    def test_note_instantiate_explicit_path_default_fail(self) -> None:
        self._write_daily_template()
        self.fake.write_note("07-Daily/2026-07-03.md", "# Existing\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.instantiate",
                "payload": {
                    "template_id": "daily-v1",
                    "field_values": {"Date": "2026-07-03"},
                    "path": "07-Daily/2026-07-03.md",
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")
        self.assertIn("Note already exists at path", output.get("details", ""))

    def test_note_instantiate_missing_required_field_default_fail(self) -> None:
        self._write_daily_template()

        code, output = self.fake.run_gateway(
            {
                "route": "note.instantiate",
                "payload": {
                    "template_id": "daily-v1",
                    "field_values": {},
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")
        self.assertIn("Missing required template fields", output.get("details", ""))

    def test_note_instantiate_rejects_unsafe_path(self) -> None:
        self._write_daily_template()

        code, output = self.fake.run_gateway(
            {
                "route": "note.instantiate",
                "payload": {
                    "template_id": "daily-v1",
                    "field_values": {"Date": "2026-07-03"},
                    "path": "../outside.md",
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertIn(output.get("error"), {"invalid_payload", "validation_error"})

    def test_note_write_writes_exact_markdown_without_mutation(self) -> None:
        content = "# Raw Note\n\nThis is **verbatim** markdown.\n"

        code, output = self.fake.run_gateway(
            {
                "route": "note.write",
                "payload": {
                    "path": "00-Inbox/Raw.md",
                    "content": content,
                },
            }
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        result = output["result"]
        self.assertEqual(result["action"], "created")
        self.assertTrue(result["created"])

        written = (self.fake.vault_dir / "00-Inbox" / "Raw.md").read_text(encoding="utf-8")
        self.assertEqual(written, content)
        self.assertFalse(written.startswith("---\n"))

    def test_note_write_existing_default_fail(self) -> None:
        self.fake.write_note("00-Inbox/Existing.md", "# Existing\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.write",
                "payload": {
                    "path": "00-Inbox/Existing.md",
                    "content": "# Should Not Write\n",
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")
        self.assertIn("Note already exists at path", output.get("details", ""))
        self.assertEqual(
            (self.fake.vault_dir / "00-Inbox" / "Existing.md").read_text(encoding="utf-8"),
            "# Existing\n",
        )

    def test_note_write_existing_skip_does_not_overwrite(self) -> None:
        self.fake.write_note("00-Inbox/Existing.md", "# Existing\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.write",
                "payload": {
                    "path": "00-Inbox/Existing.md",
                    "content": "# Should Not Write\n",
                    "if_exists": "skip",
                },
            }
        )

        self.assertEqual(code, 0)
        self.assertTrue(output.get("success"))
        result = output["result"]
        self.assertEqual(result["action"], "already_exists")
        self.assertFalse(result["created"])
        self.assertEqual(
            (self.fake.vault_dir / "00-Inbox" / "Existing.md").read_text(encoding="utf-8"),
            "# Existing\n",
        )

    def test_note_write_rejects_unsafe_path(self) -> None:
        code, output = self.fake.run_gateway(
            {
                "route": "note.write",
                "payload": {
                    "path": "../outside.md",
                    "content": "# Outside\n",
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertIn(output.get("error"), {"invalid_payload", "validation_error"})

    def test_note_export_pdf_rejects_non_markdown_source(self) -> None:
        self.fake.write_note("00-Inbox/Doc.txt", "text\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.export_pdf",
                "payload": {"path": "00-Inbox/Doc.txt"},
            }
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")

    def test_note_export_pdf_rejects_missing_source(self) -> None:
        code, output = self.fake.run_gateway(
            {
                "route": "note.export_pdf",
                "payload": {"path": "00-Inbox/Missing.md"},
            }
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")
        self.assertIn("not found", output.get("details", "").lower())

    def test_note_export_pdf_rejects_traversal_source(self) -> None:
        code, output = self.fake.run_gateway(
            {
                "route": "note.export_pdf",
                "payload": {"path": "../outside.md"},
            }
        )

        self.assertEqual(code, 1)
        self.assertIn(output.get("error"), {"invalid_payload", "validation_error"})

    def test_note_export_pdf_rejects_non_pdf_output(self) -> None:
        self.fake.write_note("00-Inbox/Note.md", "# Note\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.export_pdf",
                "payload": {
                    "path": "00-Inbox/Note.md",
                    "output_path": "00-Inbox/Note.txt",
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "validation_error")

    def test_note_export_pdf_rejects_traversal_output(self) -> None:
        self.fake.write_note("00-Inbox/Note.md", "# Note\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.export_pdf",
                "payload": {
                    "path": "00-Inbox/Note.md",
                    "output_path": "../outside.pdf",
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertIn(output.get("error"), {"invalid_payload", "validation_error"})

    def test_note_export_pdf_rejects_unknown_payload_keys(self) -> None:
        self.fake.write_note("00-Inbox/Note.md", "# Note\n")

        code, output = self.fake.run_gateway(
            {
                "route": "note.export_pdf",
                "payload": {
                    "path": "00-Inbox/Note.md",
                    "unexpected": "value",
                },
            }
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")

    def test_note_export_pdf_requires_path(self) -> None:
        code, output = self.fake.run_gateway(
            {"route": "note.export_pdf", "payload": {}}
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.get("error"), "invalid_payload")


if __name__ == "__main__":
    unittest.main()
