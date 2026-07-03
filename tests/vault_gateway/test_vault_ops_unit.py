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

    def test_capture_note_with_user_frontmatter_creates_single_block(self) -> None:
        text = "---\ndate: 2026-07-03\nvg_fields:\n  - summary\n---\nCaptured body text."
        result = vault_ops.capture_note(
            self.fake.vault_dir, text, title="With Frontmatter"
        )

        note_path = self.fake.vault_dir / result["path"]
        content = note_path.read_text(encoding="utf-8")

        # Only one frontmatter block should be present (two `---` fences).
        self.assertEqual(content.count("\n---\n"), 1)
        self.assertTrue(content.startswith("---\n"))

        # The user's second block must not leak into the body.
        body = content.split("---\n", 2)[2]
        self.assertNotIn("---", body)
        self.assertIn("Captured body text.", body)

    def test_capture_note_preserves_user_metadata_in_frontmatter(self) -> None:
        from datetime import date as date_type

        text = "---\ndate: 2026-07-03\ncustom_fields:\n  - summary\npriority: high\n---\nBody."
        result = vault_ops.capture_note(self.fake.vault_dir, text, title="Meta Preserved")

        note_path = self.fake.vault_dir / result["path"]
        fm, body = vault_ops._extract_frontmatter(note_path.read_text(encoding="utf-8"))

        # YAML parses `date: 2026-07-03` as a date object; round-trips through
        # serialization back to the same value.
        self.assertEqual(fm.get("date"), date_type(2026, 7, 3))
        self.assertEqual(fm.get("priority"), "high")
        self.assertEqual(fm.get("custom_fields"), ["summary"])
        # Gateway-controlled fields still present.
        self.assertEqual(fm.get("type"), "note")
        self.assertIn("created", fm)

    def test_capture_note_strips_template_control_fields_from_rendered_daily_text(self) -> None:
        text = """---
type: daily
date: ""
tags: [daily]
vg_fields:
  - name: Date
    type: date
    required: true
    title: true
---
# 2026-07-04

## Morning Intention
"""

        result = vault_ops.capture_note(self.fake.vault_dir, text, title="2026-07-04")

        note_path = self.fake.vault_dir / result["path"]
        content = note_path.read_text(encoding="utf-8")
        fm, body = vault_ops._extract_frontmatter(content)

        self.assertEqual(content.count("\n---\n"), 1)
        self.assertEqual(fm.get("type"), "daily")
        self.assertEqual(fm.get("date"), "")
        self.assertEqual(fm.get("tags"), ["daily"])
        self.assertNotIn("vg_fields", fm)
        self.assertNotIn("---", body)
        self.assertIn("# 2026-07-04", body)

    def test_capture_note_strips_template_control_fields_from_template_output(self) -> None:
        self.fake.write_note(
            "dump_folder/Daily Note.md",
            """---
type: daily
date: ""
tags: [daily]
vg_fields:
  - name: Date
    type: date
    required: true
    title: true
---

# {{Date}}
""",
        )

        result = vault_ops.capture_note(
            self.fake.vault_dir,
            "",
            template_path="dump_folder/Daily Note.md",
            template_mode="strict",
            field_values={"Date": "2026-07-04"},
            target_folder="dump_folder",
        )

        note_path = self.fake.vault_dir / result["path"]
        content = note_path.read_text(encoding="utf-8")
        fm, body = vault_ops._extract_frontmatter(content)

        self.assertEqual(fm.get("type"), "daily")
        self.assertEqual(fm.get("date"), "")
        self.assertEqual(fm.get("tags"), ["daily"])
        self.assertNotIn("vg_fields", fm)
        self.assertIn("# 2026-07-04", body)

    def test_capture_note_preserves_user_type(self) -> None:
        text = "---\ntype: meeting\n---\nBody."
        result = vault_ops.capture_note(self.fake.vault_dir, text, title="Type Preserved")

        note_path = self.fake.vault_dir / result["path"]
        fm, _ = vault_ops._extract_frontmatter(note_path.read_text(encoding="utf-8"))

        self.assertEqual(fm.get("type"), "meeting")

    def test_capture_note_explicit_tags_override_user_tags(self) -> None:
        text = "---\ntags: [user-tag-1, user-tag-2]\n---\nBody."
        result = vault_ops.capture_note(
            self.fake.vault_dir, text, title="Tags Override", tags=["gateway-tag"]
        )

        note_path = self.fake.vault_dir / result["path"]
        fm, _ = vault_ops._extract_frontmatter(note_path.read_text(encoding="utf-8"))

        self.assertEqual(fm.get("tags"), ["gateway-tag"])

    def test_capture_note_absent_tags_preserves_user_tags(self) -> None:
        text = "---\ntags: [user-tag-1, user-tag-2]\n---\nBody."
        result = vault_ops.capture_note(self.fake.vault_dir, text, title="Tags Preserved")

        note_path = self.fake.vault_dir / result["path"]
        fm, _ = vault_ops._extract_frontmatter(note_path.read_text(encoding="utf-8"))

        self.assertEqual(fm.get("tags"), ["user-tag-1", "user-tag-2"])

    def test_capture_note_without_frontmatter_unchanged(self) -> None:
        # Regression guard: plain text (no leading frontmatter) must behave as
        # before, with a single generated frontmatter block.
        result = vault_ops.capture_note(
            self.fake.vault_dir, "plain body", title="No Frontmatter"
        )

        note_path = self.fake.vault_dir / result["path"]
        content = note_path.read_text(encoding="utf-8")

        self.assertEqual(content.count("\n---\n"), 1)
        self.assertTrue(content.startswith("---\n"))
        fm, body = vault_ops._extract_frontmatter(content)
        self.assertEqual(fm.get("type"), "note")
        self.assertIn("plain body", body)


if __name__ == "__main__":
    unittest.main()
