from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ModelSpec:
    key: str
    task: str
    model_path: Path
    required_memory_mb: int
    default_prompt: str
    max_tokens: int


class ModelRegistry:
    def __init__(self, models: dict[str, ModelSpec]) -> None:
        if not models:
            raise ValueError("Model registry is empty")
        self._models = models

    @classmethod
    def from_file(cls, file_path: Path) -> "ModelRegistry":
        if not file_path.exists():
            raise FileNotFoundError(f"Model registry not found: {file_path}")

        payload = yaml.safe_load(file_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid model registry: expected top-level object")

        raw_models = payload.get("models")
        if not isinstance(raw_models, dict):
            raise ValueError("Invalid model registry: missing 'models' mapping")

        parsed: dict[str, ModelSpec] = {}
        for model_key, entry in raw_models.items():
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid model entry for '{model_key}': expected object")

            task = str(entry.get("task", "")).strip().lower()
            model_path = str(entry.get("model_path", "")).strip()
            required_memory_mb = int(entry.get("required_memory_mb", 0))
            default_prompt = str(entry.get("default_prompt", "")).strip()
            max_tokens = int(entry.get("max_tokens", 2048))

            if not task:
                raise ValueError(f"Model '{model_key}' is missing task")
            if not model_path:
                raise ValueError(f"Model '{model_key}' is missing model_path")
            if required_memory_mb <= 0:
                raise ValueError(f"Model '{model_key}' has invalid required_memory_mb")

            parsed[model_key] = ModelSpec(
                key=model_key,
                task=task,
                model_path=Path(model_path),
                required_memory_mb=required_memory_mb,
                default_prompt=default_prompt,
                max_tokens=max_tokens,
            )

        return cls(parsed)

    def get(self, key: str) -> ModelSpec:
        spec = self._models.get(key)
        if spec is None:
            valid = ", ".join(sorted(self._models.keys()))
            raise KeyError(f"Unknown model '{key}'. Available models: {valid}")
        return spec

    def default_for_task(self, task: str) -> ModelSpec:
        for spec in self._models.values():
            if spec.task == task:
                return spec
        raise KeyError(f"No model configured for task '{task}'")

    def max_required_memory_mb(self) -> int:
        return max(spec.required_memory_mb for spec in self._models.values())

    def list_models(self) -> list[str]:
        return sorted(self._models.keys())

    def specs(self) -> list[ModelSpec]:
        return [self._models[key] for key in sorted(self._models.keys())]
