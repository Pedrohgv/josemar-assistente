from __future__ import annotations

from pathlib import Path
import sys
import unittest

from .helpers import FakeVault, GATEWAY_ROOT


sys.path.insert(0, str(GATEWAY_ROOT))

from lib import vault_ops  # noqa: E402


class VaultOpsUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeVault()

    def tearDown(self) -> None:
        self.fake.cleanup()

    def test_resolve_relative_path_rejects_absolute_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "Absolute paths"):
            vault_ops._resolve_relative_path(self.fake.vault_dir, "/etc/passwd")

    def test_resolve_relative_path_rejects_parent_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "escapes vault root"):
            vault_ops._resolve_relative_path(self.fake.vault_dir, "../outside.md")

    def test_resolve_relative_path_rejects_backslash_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "escapes vault root"):
            vault_ops._resolve_relative_path(self.fake.vault_dir, "folder\\..\\..\\outside.md")

    def test_resolve_relative_path_allows_nested_vault_paths(self) -> None:
        resolved = vault_ops._resolve_relative_path(self.fake.vault_dir, "01-Projects/Test.md")
        self.assertEqual(resolved, self.fake.vault_dir / "01-Projects" / "Test.md")

    def test_filename_stem_preserves_diacritics_and_removes_obsidian_unsafe_chars(self) -> None:
        stem = vault_ops._filename_stem_from_title("Casa e Decoração / ESX: [Draft]? #^")
        self.assertEqual(stem, "Casa e Decoração - ESX Draft")

    def test_capture_note_generates_unique_filename_on_collision(self) -> None:
        first = vault_ops.capture_note(self.fake.vault_dir, "first", title="Repeated")
        second = vault_ops.capture_note(self.fake.vault_dir, "second", title="Repeated")

        self.assertEqual(first["path"], "00-Inbox/Repeated.md")
        self.assertEqual(second["path"], "00-Inbox/Repeated-2.md")
        self.assertTrue((self.fake.vault_dir / "00-Inbox" / "Repeated-2.md").exists())

    def test_update_note_appends_to_section(self) -> None:
        self.fake.write_note("01-Projects/Plan.md", "# Plan\n\n## Next\nold\n\n## Later\nend\n")

        vault_ops.update_note(
            self.fake.vault_dir,
            path="01-Projects/Plan.md",
            text="new",
            mode="section_append",
            section_heading="Next",
        )

        content = (self.fake.vault_dir / "01-Projects" / "Plan.md").read_text(encoding="utf-8")
        self.assertIn("## Next\nold\nnew\n", content)
        self.assertIn("## Later\nend", content)

    def test_update_note_prepends_to_section(self) -> None:
        self.fake.write_note("01-Projects/Plan.md", "# Plan\n\n## Next\nold\n")

        vault_ops.update_note(
            self.fake.vault_dir,
            path="01-Projects/Plan.md",
            text="new",
            mode="section_prepend",
            section_heading="Next",
        )

        content = (self.fake.vault_dir / "01-Projects" / "Plan.md").read_text(encoding="utf-8")
        self.assertIn("## Next\nnew\n\nold\n", content)

    def test_update_note_rejects_duplicate_section_names(self) -> None:
        self.fake.write_note("01-Projects/Plan.md", "# Plan\n\n## Next\none\n\n## Next\ntwo\n")

        with self.assertRaisesRegex(ValueError, "Multiple sections"):
            vault_ops.update_note(
                self.fake.vault_dir,
                path="01-Projects/Plan.md",
                text="new",
                mode="section_append",
                section_heading="Next",
            )

    def test_frontmatter_update_preserves_body(self) -> None:
        self.fake.write_note("01-Projects/Plan.md", "# Plan\n\nbody\n")

        vault_ops.update_note(
            self.fake.vault_dir,
            path="01-Projects/Plan.md",
            mode="frontmatter",
            frontmatter_fields={"status": "active", "priority": 2},
        )

        content = (self.fake.vault_dir / "01-Projects" / "Plan.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\nstatus: active\npriority: 2\n---\n"))
        self.assertIn("# Plan\n\nbody", content)

    def test_replace_managed_block_preserves_manual_content(self) -> None:
        content = "# Index\n\nManual text.\n\n<!-- VG:BEGIN managed-summary -->\nold\n<!-- VG:END managed-summary -->\n"

        updated, changed = vault_ops._replace_managed_block(
            content,
            vault_ops.INDEX_MANAGED_BEGIN,
            vault_ops.INDEX_MANAGED_END,
            "new summary",
        )

        self.assertTrue(changed)
        self.assertIn("Manual text.", updated)
        self.assertIn("new summary", updated)
        self.assertNotIn("\nold\n", updated)

    def test_read_note_decodes_latin1_fallback(self) -> None:
        note = self.fake.vault_dir / "00-Inbox" / "latin.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_bytes("# Café\n\nconteúdo".encode("latin-1"))

        result = vault_ops.read_note(self.fake.vault_dir, path="00-Inbox/latin.md")

        self.assertIn("Café", result["body"])
        self.assertIn("conteúdo", result["body"])


if __name__ == "__main__":
    unittest.main()
