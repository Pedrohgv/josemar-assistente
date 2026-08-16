"""Contract tests for the repo-owned backup-operations skill.

The skill is instruction-only (SKILL.md + references/, no executable) and
provides agent-facing backup guidance. These tests enforce the agreed design:

- skill layout: SKILL.md + non-empty references/ directory, all markdown;
- size: SKILL.md under 150 lines (AGENTS.md SKILL.md policy);
- valid frontmatter (name/description/categories) and English-only prompt
  source (project prompt-language policy);
- `josemar-backup-status` is the ONLY backup command the skill may reference;
- status output must be labeled a LOCAL STAGING OBSERVATION, with remote
  status explicitly unknown / operator-only;
- recovery guidance must be a confirmation-gated human checklist requiring an
  explicitly user-selected lane AND generation, and must never silently
  select the latest generation;
- absolute safety constraint: the skill and all its references must not
  contain or render Docker, rclone, download, verify, install, rollback
  (or recover/restore) commands and must provide no execution capability;
- bounded-claim accuracy: no export scheduling/enablement claims, truncated
  means partial, empty results are ambiguous, marker observations are local
  integrity signals only, and the defense-in-depth threat-model disclaimer
  (issue #110 wording) is present;
- Dockerfile image-baking: the skill is COPYed into the image and the bare
  `josemar-backup-status` name is provided on PATH via symlink, so the
  skill's sanctioned command is actually reachable.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - environment-dependent
    yaml = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills-factory" / "backup-operations"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES_DIR = SKILL_DIR / "references"
DOCKERFILE = REPO_ROOT / "Dockerfile.hermes"

MAX_SKILL_LINES = 150

# Operational command / execution-capability tokens that must never appear in
# the skill or any reference (word-boundary match, case-insensitive).
# NOTE: "shell" is deliberately NOT in this list: the issue #110 defense-in-
# depth disclaimer ("...against a compromised same-UID container/shell") is a
# required threat-model statement. test_shell_word_only_in_threat_model
# restricts "shell" to exactly that disclaimer.
FORBIDDEN_WORDS = [
    "docker", "rclone", "compose", "download", "verify", "install",
    "rollback", "restore", "recover", "bash", "sh", "exec", "ssh",
    "curl", "wget", "git",
]

# Command / service / script / path-shaped substrings that must never appear.
# (Runbook document filenames such as docs/vault-recovery-operations.md are
# permitted references; the service/script names themselves are not.)
FORBIDDEN_SUBSTRINGS = [
    "docker compose", "docker-compose", "docker run", "docker exec",
    "docker kill", "docker ps", "docker volume", "docker logs",
    "su -", "su -s", "sh -c", "/bin/sh", "/bin/bash",
    "/opt/josemar", "/opt/data", "/opt/hermes",
    "vault-recovery-recover", "vault-recovery-restore",
    "vault-recovery-uploader", "vault-recovery-export", "vault_recovery",
    "mnemosyne-backup-recover", "mnemosyne-backup-restore",
    "mnemosyne-backup-uploader", "mnemosyne-backup-export",
    "obsidian-backup", "```", "$(",
    "`docker", "`rclone", "`su ", "`sh ", "`bash",
]

# Required concepts in SKILL.md, grouped by concern: (label, needle).
REQUIRED_SKILL_STRINGS = [
    ("only sanctioned status command", "josemar-backup-status"),
    ("local staging observation label", "local staging observation"),
    ("remote status unknown", "remote status is unknown"),
    ("remote status operator-only", "operator-only"),
    ("no execution capability", "no execution"),
    ("confirmation-gated recovery", "confirmation-gated"),
    ("human checklist", "human checklist"),
    ("explicit lane selection", "lane"),
    ("explicit generation selection", "generation"),
    ("no silent latest selection", "never silently select the latest"),
    ("truncated means partial", "truncated:true"),
    ("empty result ambiguous", "ambiguous"),
    ("defense in depth disclaimer", "defense in depth, not a complete security boundary"),
    ("do not overstate protection", "do not overstate"),
    ("threat model same-UID", "same-uid container/shell"),
    ("operator runbook link (default lane)", "docs/vault-recovery-operations.md"),
    ("operator runbook link (mnemosyne lane)", "docs/mnemosyne-operations.md"),
    ("operator handoff", "operator"),
]

# Portuguese tokens that must not appear: LLM-facing prompt sources are
# English per project policy (smoke check only).
FORBIDDEN_PORTUGUESE_TOKENS = [
    "não", "você", "faça", "restaurar", "baixar", "operador",
    "confirmação", "geração", "verificar", "instalar",
]


def skill_files() -> list[Path]:
    """All markdown files that make up the skill (SKILL.md + references)."""
    return sorted([SKILL_MD, *REFERENCES_DIR.glob("*.md")])


def parse_frontmatter(text: str) -> dict:
    assert yaml is not None, "PyYAML required"
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    parts = text.split("---\n", 2)
    assert len(parts) >= 3, "SKILL.md must have closing frontmatter delimiter"
    data = yaml.safe_load(parts[1])
    assert isinstance(data, dict), "frontmatter must parse to a mapping"
    return data


class BackupOperationsSkillLayoutTests(unittest.TestCase):
    """Skill layout: SKILL.md + non-empty references/, all markdown."""

    def test_skill_directory_layout(self) -> None:
        self.assertTrue(SKILL_MD.is_file(), f"missing skill file: {SKILL_MD}")
        self.assertTrue(REFERENCES_DIR.is_dir(), f"missing references dir: {REFERENCES_DIR}")
        refs = list(REFERENCES_DIR.glob("*.md"))
        self.assertTrue(refs, "references/ must contain at least one markdown reference")
        for path in SKILL_DIR.rglob("*"):
            if path.is_dir():
                continue
            self.assertTrue(path.suffix == ".md",
                            f"skill tree must be markdown-only, found: {path}")
            self.assertNotEqual(path.name, "SETUP.md",
                                "backup-operations has no operator-specific SETUP.md")
    def test_skill_md_under_150_lines(self) -> None:
        lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
        self.assertLess(
            len(lines), MAX_SKILL_LINES,
            f"SKILL.md must stay under {MAX_SKILL_LINES} lines "
            f"(AGENTS.md skill policy); got {len(lines)}",
        )

    def test_every_reference_is_linked_from_skill_md(self) -> None:
        text = SKILL_MD.read_text(encoding="utf-8")
        for ref in REFERENCES_DIR.glob("*.md"):
            with self.subTest(reference=ref.name):
                self.assertIn(ref.name, text,
                              f"reference {ref.name} must be linked from SKILL.md")


class BackupOperationsSkillFrontmatterTests(unittest.TestCase):
    """Frontmatter contract: name, description, categories."""

    def setUp(self) -> None:
        self.assertTrue(SKILL_MD.is_file(), f"missing skill file: {SKILL_MD}")
        self.text = SKILL_MD.read_text(encoding="utf-8")

    def test_frontmatter_fields(self) -> None:
        if yaml is None:
            self.skipTest("PyYAML not available")
        data = parse_frontmatter(self.text)
        self.assertEqual(data.get("name"), "backup-operations")
        desc = data.get("description")
        self.assertIsInstance(desc, str)
        assert isinstance(desc, str)
        self.assertTrue(desc.strip(), "description must be non-empty")
        categories = data.get("categories")
        self.assertIsInstance(categories, list)
        assert isinstance(categories, list)
        self.assertTrue(categories, "categories must be a non-empty list")


class BackupOperationsSkillContentTests(unittest.TestCase):
    """Required caveats and confirmation-gated recovery guidance."""

    def setUp(self) -> None:
        self.assertTrue(SKILL_MD.is_file(), f"missing skill file: {SKILL_MD}")
        self.text = SKILL_MD.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_skill_teaches_required_concepts(self) -> None:
        for label, needle in REQUIRED_SKILL_STRINGS:
            with self.subTest(concept=label, needle=needle):
                self.assertIn(
                    needle.lower(), self.lower,
                    f"missing required concept {label!r}: {needle!r}",
                )

    def test_only_sanctioned_status_command_referenced(self) -> None:
        matches = set(re.findall(r"josemar-[a-z0-9-]+", self.lower))
        self.assertEqual(
            matches, {"josemar-backup-status"},
            f"the ONLY josemar command the skill may reference is "
            f"josemar-backup-status; found: {sorted(matches)}",
        )

    def test_no_silent_latest_selection(self) -> None:
        for path in skill_files():
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8").lower()
                remaining = text.replace("never silently select the latest", "")
                self.assertNotIn(
                    "latest", remaining,
                    f"{path.name}: 'latest' may appear only inside the "
                    f"'never silently select the latest' prohibition",
                )

    def test_recovery_reference_is_confirmation_gated_checklist(self) -> None:
        checklist = (REFERENCES_DIR / "recovery-checklist.md").read_text(encoding="utf-8").lower()
        for needle in ("checklist", "explicit", "lane", "generation",
                       "confirm", "operator", "wait", "hand"):
            with self.subTest(needle=needle):
                self.assertIn(needle, checklist,
                              f"recovery checklist must teach {needle!r}")

    def test_status_reference_labels_local_observation(self) -> None:
        status_ref = (REFERENCES_DIR / "status-observation.md").read_text(encoding="utf-8").lower()
        for needle in ("local staging observation", "unknown", "operator",
                       "never"):
            with self.subTest(needle=needle):
                self.assertIn(needle, status_ref,
                              f"status reference must teach {needle!r}")


class BackupOperationsSafetyConstraintTests(unittest.TestCase):
    """Absolute safety constraint: no operational commands, no execution
    capability, in SKILL.md and every reference."""

    def setUp(self) -> None:
        self.files = {p.name: p.read_text(encoding="utf-8") for p in skill_files()}

    def test_no_forbidden_words_anywhere(self) -> None:
        for name, text in self.files.items():
            for word in FORBIDDEN_WORDS:
                with self.subTest(file=name, word=word):
                    self.assertIsNone(
                        re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE),
                        f"{name}: forbidden operational token {word!r}",
                    )

    def test_no_forbidden_substrings_anywhere(self) -> None:
        for name, text in self.files.items():
            lower = text.lower()
            for needle in FORBIDDEN_SUBSTRINGS:
                with self.subTest(file=name, needle=needle):
                    self.assertNotIn(needle, lower,
                                     f"{name}: forbidden operational text {needle!r}")

    def test_shell_word_only_in_threat_model_disclaimer(self) -> None:
        # "shell" is allowed ONLY inside the issue #110 threat-model
        # disclaimer ("...a compromised same-UID container/shell"); anywhere
        # else it would be an execution-capability reference.
        for name, text in self.files.items():
            with self.subTest(file=name):
                remaining = text.lower().replace("same-uid container/shell", "")
                self.assertNotIn(
                    "shell", remaining,
                    f"{name}: 'shell' may appear only inside the "
                    f"'same-UID container/shell' threat-model disclaimer",
                )

    def test_no_command_execution_shapes(self) -> None:
        for name, text in self.files.items():
            with self.subTest(file=name):
                self.assertNotIn("```", text, f"{name}: fenced code block present")
                # CLI-flag shaped text (e.g. --generation) renders a command
                # surface; YAML frontmatter delimiters (---) are not flags.
                self.assertIsNone(
                    re.search(r"--[a-zA-Z]", text),
                    f"{name}: CLI-flag-shaped text present",
                )
                for line in text.splitlines():
                    stripped = line.strip()
                    self.assertFalse(
                        stripped.startswith(("$ ", "> ", "#!")),
                        f"{name}: execution-shaped line: {line!r}",
                    )

    def test_english_prompt_source(self) -> None:
        for name, text in self.files.items():
            lower = text.lower()
            for token in FORBIDDEN_PORTUGUESE_TOKENS:
                with self.subTest(file=name, token=token):
                    self.assertNotIn(token, lower,
                                     f"{name}: non-English prompt source token {token!r}")


class BackupOperationsDockerfileContractTests(unittest.TestCase):
    """Image wiring the skill depends on: baked-in COPY and the bare status
    command on PATH (the skill sanctions the bare name `josemar-backup-status`
    and must be reachable that way at runtime)."""

    def setUp(self) -> None:
        self.assertTrue(DOCKERFILE.is_file(), f"missing Dockerfile: {DOCKERFILE}")
        self.text = DOCKERFILE.read_text(encoding="utf-8")

    def test_skill_baked_into_image(self) -> None:
        self.assertIn(
            "COPY skills-factory/backup-operations /opt/josemar/skills/backup-operations",
            self.text,
            "backup-operations must be baked into the image like every "
            "other repo-owned skill",
        )
        for skill in ("aux-ml", "workspace-sync", "gbrain", "tasknotes",
                      "browser-control"):
            with self.subTest(skill=skill):
                self.assertIn(
                    f"COPY skills-factory/{skill} /opt/josemar/skills/{skill}",
                    self.text,
                )

    def test_bare_status_command_on_path(self) -> None:
        self.assertIn(
            "ln -s /opt/josemar/scripts/josemar-backup-status.py "
            "/usr/local/bin/josemar-backup-status",
            self.text,
            "the bare josemar-backup-status name must be on PATH via "
            "symlink (repo convention for agent-facing commands)",
        )


if __name__ == "__main__":
    unittest.main()
