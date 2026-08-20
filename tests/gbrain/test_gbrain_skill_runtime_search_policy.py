"""Source contract for runtime-aware gbrain search guidance (issue #122)."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills-factory" / "gbrain" / "SKILL.md"


class GbrainSkillRuntimeSearchPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = SKILL_PATH.read_text(encoding="utf-8")

    def test_skill_does_not_present_keyword_only_as_live_default(self) -> None:
        self.assertNotIn("Keyword-only search by default", self.skill)
        self.assertNotIn(
            "In the base (keyword-only) deploy this command is not available",
            self.skill,
        )

    def test_skill_treats_live_status_as_search_mode_authority(self) -> None:
        self.assertIn("Search mode follows the live runtime", self.skill)
        self.assertIn("Use `gbrain status`", self.skill)
        self.assertIn("live runtime", self.skill)

    def test_skill_documents_both_runtime_modes(self) -> None:
        self.assertIn("`gbrain query --no-expand`", self.skill)
        self.assertIn("hybrid/semantic provider path", self.skill)
        self.assertIn("keyword-only", self.skill)
        self.assertIn("embeddings are disabled or not configured", self.skill)

    def test_skill_preserves_keyword_only_base_activation_contract(self) -> None:
        self.assertIn("base activation starts keyword-only", self.skill)
        self.assertIn("`search.mcp_keyword_only=true`", self.skill)
        self.assertIn("`embedding_disabled` sentinel", self.skill)
        self.assertIn("`josemar-gbrain enable-embeddings`", self.skill)
        self.assertIn("`josemar-gbrain embed-backfill`", self.skill)


if __name__ == "__main__":
    unittest.main()
