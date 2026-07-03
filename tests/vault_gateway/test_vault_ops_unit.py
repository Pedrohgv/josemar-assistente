from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

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

    def test_read_note_succeeds_after_transient_missing(self) -> None:
        note = self.fake.write_note("00-Inbox/Transient.md", "# Transient\n\nbody")
        original_exists = Path.exists
        original_is_file = Path.is_file
        calls = {"count": 0}

        def fake_exists(self, *args, **kwargs):
            if self == note:
                calls["count"] += 1
                # First two checks report missing (simulating Syncthing race),
                # then the file reappears.
                if calls["count"] <= 2:
                    return False
            return original_exists(self, *args, **kwargs)

        def fake_is_file(self, *args, **kwargs):
            if self == note and calls["count"] <= 2:
                return False
            return original_is_file(self, *args, **kwargs)

        with mock.patch.object(Path, "exists", fake_exists), \
             mock.patch.object(Path, "is_file", fake_is_file):
            result = vault_ops.read_note(self.fake.vault_dir, path="00-Inbox/Transient.md")

        self.assertEqual(result["path"], "00-Inbox/Transient.md")
        self.assertIn("Transient", result["body"])
        self.assertGreater(calls["count"], 2)

    def test_read_note_raises_when_persistently_missing(self) -> None:
        # Note is never created; resolution must keep raising the same ValueError.
        with self.assertRaisesRegex(ValueError, "Note not found at path: 00-Inbox/Gone.md"):
            vault_ops.read_note(self.fake.vault_dir, path="00-Inbox/Gone.md")

    def test_resolve_note_path_retries_then_raises_on_persistent_absence(self) -> None:
        note = self.fake.vault_dir / "00-Inbox" / "Flaky.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        # File never appears despite retries.
        original_exists = Path.exists
        original_is_file = Path.is_file

        def fake_exists(self, *args, **kwargs):
            if self == note:
                return False
            return original_exists(self, *args, **kwargs)

        def fake_is_file(self, *args, **kwargs):
            if self == note:
                return False
            return original_is_file(self, *args, **kwargs)

        with mock.patch.object(Path, "exists", fake_exists), \
             mock.patch.object(Path, "is_file", fake_is_file):
            with self.assertRaisesRegex(ValueError, "Note not found at path: 00-Inbox/Flaky.md"):
                vault_ops._resolve_note_path(self.fake.vault_dir, path="00-Inbox/Flaky.md")

    def test_resolve_capture_template_retries_transient_missing(self) -> None:
        template = self.fake.write_note("Templates/Tpl.md", "# Tpl\n\nbody")
        original_exists = Path.exists
        original_is_file = Path.is_file
        calls = {"count": 0}

        def fake_exists(self, *args, **kwargs):
            if self == template:
                calls["count"] += 1
                if calls["count"] <= 1:
                    return False
            return original_exists(self, *args, **kwargs)

        def fake_is_file(self, *args, **kwargs):
            if self == template and calls["count"] <= 1:
                return False
            return original_is_file(self, *args, **kwargs)

        with mock.patch.object(Path, "exists", fake_exists), \
             mock.patch.object(Path, "is_file", fake_is_file):
            resolved, record = vault_ops._resolve_capture_template(
                self.fake.vault_dir, template_path="Templates/Tpl.md"
            )

        self.assertEqual(resolved, template)
        self.assertIsNotNone(record)
        self.assertGreater(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()
