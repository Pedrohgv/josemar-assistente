from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills-factory" / "aux-ml" / "aux-ml"


def load_skill_module():
    loader = importlib.machinery.SourceFileLoader("aux_ml_skill_under_test", str(SKILL_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Could not load aux-ml skill module")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AuxMLSkillStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.skill = load_skill_module()
        self.skill.AUX_ML_SHARED_DIR = str(self.shared)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_file_inside_shared_is_returned_without_copying(self) -> None:
        source = self.shared / "input.txt"
        source.write_text("inside", encoding="utf-8")

        staged = self.skill._stage_file_for_aux_ml(str(source))

        self.assertEqual(staged, str(source.resolve()))
        self.assertFalse((self.shared / "staged" / "input.txt").exists())

    def test_symlink_inside_shared_pointing_outside_is_rejected(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.shared / "escape.txt"
        link.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "escapes shared root"):
            self.skill._stage_file_for_aux_ml(str(link))

        self.assertFalse((self.shared / "staged" / "escape.txt").exists())

    def test_symlink_inside_shared_pointing_inside_is_accepted_as_resolved_target(self) -> None:
        target = self.shared / "target.txt"
        target.write_text("inside", encoding="utf-8")
        link = self.shared / "link.txt"
        link.symlink_to(target)

        staged = self.skill._stage_file_for_aux_ml(str(link))

        self.assertEqual(staged, str(target.resolve()))

    def test_file_outside_shared_is_copied_to_staging_dir(self) -> None:
        source = self.root / "outside.txt"
        source.write_text("outside", encoding="utf-8")

        staged = Path(self.skill._stage_file_for_aux_ml(str(source)))

        self.assertEqual(staged, self.shared / "staged" / "outside.txt")
        self.assertEqual(staged.read_text(encoding="utf-8"), "outside")

    def test_staging_avoids_name_collision(self) -> None:
        source = self.root / "outside.txt"
        source.write_text("new", encoding="utf-8")
        staged_dir = self.shared / "staged"
        staged_dir.mkdir()
        (staged_dir / "outside.txt").write_text("existing", encoding="utf-8")

        staged = Path(self.skill._stage_file_for_aux_ml(str(source)))

        self.assertEqual(staged.parent, staged_dir)
        self.assertNotEqual(staged.name, "outside.txt")
        self.assertEqual(staged.read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()
