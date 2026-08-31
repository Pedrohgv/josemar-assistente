from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "docs_check.py"
SPEC = importlib.util.spec_from_file_location("docs_check", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
docs_check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = docs_check
SPEC.loader.exec_module(docs_check)


class DocsCheckTests(unittest.TestCase):
    def make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        tempdir = tempfile.TemporaryDirectory()
        root = Path(tempdir.name)
        (root / "docs").mkdir()
        (root / "skills-factory").mkdir()
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / "docs" / "README.md").write_text("# Docs\n", encoding="utf-8")
        (root / "docs" / "documentation-policy.md").write_text("# Policy\n", encoding="utf-8")
        return tempdir, root

    def test_markdown_links_resolve_relative_to_source(self) -> None:
        tempdir, root = self.make_root()
        self.addCleanup(tempdir.cleanup)
        (root / "docs" / "target.md").write_text("# Target\n", encoding="utf-8")
        source = root / "docs" / "README.md"
        source.write_text('[target](target.md#section "title")\n', encoding="utf-8")

        errors = docs_check.check_markdown_links(root, [source])

        self.assertEqual([], errors)

    def test_broken_markdown_link_is_error(self) -> None:
        tempdir, root = self.make_root()
        self.addCleanup(tempdir.cleanup)
        source = root / "docs" / "README.md"
        source.write_text("[missing](missing.md)\n", encoding="utf-8")

        errors = docs_check.check_markdown_links(root, [source])

        self.assertEqual(1, len(errors))
        self.assertIn("broken local Markdown link", errors[0].message)

    def test_documentation_files_include_templates_and_exclude_private_generated_trees(self) -> None:
        tempdir, root = self.make_root()
        self.addCleanup(tempdir.cleanup)
        template_doc = root / "templates" / "agent-state-template" / "README.md"
        template_doc.parent.mkdir(parents=True)
        template_doc.write_text("# Template\n", encoding="utf-8")
        private_doc = root / "agent-state" / "README.md"
        private_doc.parent.mkdir()
        private_doc.write_text("# Private\n", encoding="utf-8")
        generated_doc = root / "graphify-out" / "GRAPH_REPORT.md"
        generated_doc.parent.mkdir()
        generated_doc.write_text("# Generated\n", encoding="utf-8")

        paths = docs_check.documentation_files(root)

        self.assertIn(template_doc, paths)
        self.assertNotIn(private_doc, paths)
        self.assertNotIn(generated_doc, paths)

    def test_repo_root_document_route_must_exist(self) -> None:
        tempdir, root = self.make_root()
        self.addCleanup(tempdir.cleanup)
        source = root / "AGENTS.md"
        source.write_text("Read `docs/missing.md` before changing it.\n", encoding="utf-8")

        errors = docs_check.check_repo_doc_path_references(root, [source])
        self.assertEqual(1, len(errors))

        (root / "docs" / "missing.md").write_text("# Exists\n", encoding="utf-8")
        self.assertEqual([], docs_check.check_repo_doc_path_references(root, [source]))

    def test_skill_view_reference_must_exist(self) -> None:
        tempdir, root = self.make_root()
        self.addCleanup(tempdir.cleanup)
        skill_dir = root / "skills-factory" / "example"
        skill_dir.mkdir()
        skill = skill_dir / "SKILL.md"
        skill.write_text(
            'load `skill_view("example", file_path="references/deep.md")` when needed\n',
            encoding="utf-8",
        )

        errors = docs_check.check_skill_references(root)
        self.assertEqual(1, len(errors))

        references = skill_dir / "references"
        references.mkdir()
        (references / "deep.md").write_text("# Deep\n", encoding="utf-8")
        self.assertEqual([], docs_check.check_skill_references(root))

    def test_skill_budget_warns_then_errors_at_guardrail(self) -> None:
        tempdir, root = self.make_root()
        self.addCleanup(tempdir.cleanup)
        skill_dir = root / "skills-factory" / "example"
        skill_dir.mkdir()
        skill = skill_dir / "SKILL.md"

        skill.write_text("x\n" * (docs_check.SKILL_WARN_LINES + 1), encoding="utf-8")
        errors, warnings = docs_check.check_context_budgets(root)
        self.assertEqual([], errors)
        self.assertEqual(1, len(warnings))

        skill.write_text("x\n" * (docs_check.SKILL_ERROR_LINES + 1), encoding="utf-8")
        errors, warnings = docs_check.check_context_budgets(root)
        self.assertEqual(1, len(errors))
        self.assertEqual([], warnings)

    def test_run_checks_requires_policy_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "skills-factory").mkdir()
            errors, _warnings = docs_check.run_checks(root)

        messages = [finding.message for finding in errors]
        self.assertEqual(2, messages.count("required documentation architecture file is missing"))

    def test_repository_documentation_contract(self) -> None:
        errors, _warnings = docs_check.run_checks(REPO_ROOT)
        self.assertEqual([], [finding.render(REPO_ROOT, "ERROR") for finding in errors])


if __name__ == "__main__":
    unittest.main()
