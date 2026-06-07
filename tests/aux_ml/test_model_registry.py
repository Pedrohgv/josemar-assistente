from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "aux-ml"))

from app.model_registry import ModelRegistry


class ModelRegistryTests(unittest.TestCase):
    def test_optional_models_missing_files_are_filtered_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "glm.gguf"
            mmproj_path = root / "mmproj-glm.gguf"
            model_path.write_bytes(b"model")
            mmproj_path.write_bytes(b"mmproj")
            registry_path = root / "models.yaml"
            registry_path.write_text(
                """
models:
  glm-ocr:
    task: ocr
    model_path: {model_path}
    mmproj_path: {mmproj_path}
    required_memory_mb: 1024
    default_prompt: "Text Recognition:"
    max_tokens: 128
  granite-speech-4.1-2b:
    task: transcribe
    model_path: {missing_model_path}
    mmproj_path: {missing_mmproj_path}
    required_memory_mb: 1024
    default_prompt: transcribe
    max_tokens: 128
    optional: true
""".format(
                    model_path=model_path,
                    mmproj_path=mmproj_path,
                    missing_model_path=root / "missing-granite.gguf",
                    missing_mmproj_path=root / "missing-mmproj.gguf",
                ),
                encoding="utf-8",
            )

            registry = ModelRegistry.from_file(registry_path).with_available_optional_models()

            self.assertEqual(registry.list_models(), ["glm-ocr"])
            self.assertEqual(registry.get("glm-ocr").required_paths(), (model_path, mmproj_path))

    def test_required_models_missing_files_are_kept_for_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry_path = root / "models.yaml"
            registry_path.write_text(
                """
models:
  glm-ocr:
    task: ocr
    model_path: {missing_model_path}
    mmproj_path: {missing_mmproj_path}
    required_memory_mb: 1024
    default_prompt: "Text Recognition:"
    max_tokens: 128
""".format(
                    missing_model_path=root / "missing-glm.gguf",
                    missing_mmproj_path=root / "missing-mmproj.gguf",
                ),
                encoding="utf-8",
            )

            registry = ModelRegistry.from_file(registry_path).with_available_optional_models()

            self.assertEqual(registry.list_models(), ["glm-ocr"])
            self.assertEqual(
                registry.get("glm-ocr").required_paths(),
                (root / "missing-glm.gguf", root / "missing-mmproj.gguf"),
            )


if __name__ == "__main__":
    unittest.main()
