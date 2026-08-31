#!/usr/bin/env python3
"""Lightweight, dependency-free documentation integrity checks for Josemar."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote

MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
SKILL_VIEW_RE = re.compile(r"skill_view\(\s*[\"']([^\"']+)[\"']\s*,\s*file_path\s*=\s*[\"']([^\"']+)[\"']\s*\)")
REFERENCE_CODE_RE = re.compile(r"`(references/[A-Za-z0-9._/\-]+\.md)`")
REPO_DOC_CODE_RE = re.compile(r"`((?:docs|tests|credentials|skills-factory|templates|\.github)/[A-Za-z0-9._*/\-]+\.md)`")
SKILL_WARN_LINES = 220
SKILL_ERROR_LINES = 350
AGENTS_WARN_LINES = 300
EXCLUDED_MARKDOWN_DIRS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__",
    "agent-state", "dev-tools-venv", "dump_folder", "graphify-out",
    "node_modules", "venv",
}


@dataclass(frozen=True)
class Finding:
    path: Path
    message: str

    def render(self, root: Path, level: str) -> str:
        try:
            relative = self.path.relative_to(root)
        except ValueError:
            relative = self.path
        return f"{level}: {relative}: {self.message}"


def documentation_files(root: Path) -> list[Path]:
    """Return public repo-owned Markdown, pruning private/generated/tool trees."""
    root = root.resolve()
    candidates: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_MARKDOWN_DIRS)
        current_path = Path(current)
        for filename in sorted(filenames):
            if filename.endswith(".md"):
                candidates.append(current_path / filename)
    return sorted(candidates)


def _strip_markdown_link_title(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing != -1:
            return target[1:closing].strip()
    for quote in (' "', " '", " ("):
        marker = target.find(quote)
        if marker != -1:
            return target[:marker].strip()
    return target


def _local_link_target(source: Path, raw_target: str) -> Path | None:
    target = _strip_markdown_link_title(raw_target)
    if not target or target.startswith("#"):
        return None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("//"):
        return None
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not target or target.startswith("/"):
        return None
    return (source.parent / target).resolve()


def check_markdown_links(root: Path, paths: Iterable[Path]) -> list[Finding]:
    errors: list[Finding] = []
    root_resolved = root.resolve()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK_RE.findall(text):
            target = _local_link_target(path, raw_target)
            if target is None:
                continue
            try:
                target.relative_to(root_resolved)
            except ValueError:
                errors.append(Finding(path, f"local Markdown link escapes repository: {raw_target}"))
                continue
            if not target.exists():
                errors.append(Finding(path, f"broken local Markdown link: {raw_target}"))
    return errors


def check_repo_doc_path_references(root: Path, paths: Iterable[Path]) -> list[Finding]:
    """Validate explicit repo-root documentation paths used for harness routing."""
    errors: list[Finding] = []
    for source in paths:
        text = source.read_text(encoding="utf-8")
        for relative_path in REPO_DOC_CODE_RE.findall(text):
            if "*" in relative_path:
                continue
            if not (root / relative_path).is_file():
                errors.append(Finding(source, f"referenced repository document does not exist: {relative_path}"))
    return errors


def check_skill_references(root: Path) -> list[Finding]:
    errors: list[Finding] = []
    skills_root = root / "skills-factory"
    if not skills_root.is_dir():
        return errors
    for skill_file in sorted(skills_root.glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8")
        for referenced_skill, relative_path in SKILL_VIEW_RE.findall(text):
            if not (skills_root / referenced_skill / relative_path).is_file():
                errors.append(Finding(skill_file, f"skill_view target does not exist: {referenced_skill}/{relative_path}"))
        for relative_path in REFERENCE_CODE_RE.findall(text):
            if not (skill_file.parent / relative_path).is_file():
                errors.append(Finding(skill_file, f"referenced skill document does not exist: {relative_path}"))
        references_dir = skill_file.parent / "references"
        if references_dir.exists() and not references_dir.is_dir():
            errors.append(Finding(skill_file, "references exists but is not a directory"))
    return errors


def check_context_budgets(root: Path) -> tuple[list[Finding], list[Finding]]:
    """Return (errors, warnings) for obviously oversized always-loaded docs."""
    errors: list[Finding] = []
    warnings: list[Finding] = []
    for skill_file in sorted((root / "skills-factory").glob("*/SKILL.md")):
        lines = len(skill_file.read_text(encoding="utf-8").splitlines())
        if lines > SKILL_ERROR_LINES:
            errors.append(Finding(skill_file, f"{lines} lines exceeds the {SKILL_ERROR_LINES}-line guardrail; keep routine use self-contained but move non-routine depth to references/"))
        elif lines > SKILL_WARN_LINES:
            warnings.append(Finding(skill_file, f"{lines} lines exceeds the {SKILL_WARN_LINES}-line review heuristic; confirm the extra context is routine-use material"))
    for agents_file in (path for path in documentation_files(root) if path.name == "AGENTS.md"):
        lines = len(agents_file.read_text(encoding="utf-8").splitlines())
        if lines > AGENTS_WARN_LINES:
            warnings.append(Finding(agents_file, f"{lines} lines exceeds the {AGENTS_WARN_LINES}-line review heuristic; consider narrower routing/on-demand docs"))
    return errors, warnings


def run_checks(root: Path) -> tuple[list[Finding], list[Finding]]:
    root = root.resolve()
    errors: list[Finding] = []
    warnings: list[Finding] = []
    for required in (root / "docs" / "README.md", root / "docs" / "documentation-policy.md"):
        if not required.is_file():
            errors.append(Finding(required, "required documentation architecture file is missing"))
    paths = documentation_files(root)
    errors.extend(check_markdown_links(root, paths))
    errors.extend(check_repo_doc_path_references(root, paths))
    errors.extend(check_skill_references(root))
    budget_errors, budget_warnings = check_context_budgets(root)
    errors.extend(budget_errors)
    warnings.extend(budget_warnings)
    return errors, warnings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors, warnings = run_checks(args.root)
    root = args.root.resolve()
    for warning in warnings:
        print(warning.render(root, "WARNING"), file=sys.stderr)
    for error in errors:
        print(error.render(root, "ERROR"), file=sys.stderr)
    if errors:
        print(f"documentation check failed: {len(errors)} error(s), {len(warnings)} warning(s)", file=sys.stderr)
        return 1
    print(f"documentation check passed: {len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
