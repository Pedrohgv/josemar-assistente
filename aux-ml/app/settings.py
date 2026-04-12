from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_allowed_input_dirs(raw: str | None) -> tuple[Path, ...]:
    if not raw:
        return (Path("/root/.openclaw/workspace"),)

    entries: list[Path] = []
    for part in raw.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        entries.append(Path(stripped).expanduser())

    if not entries:
        return (Path("/root/.openclaw/workspace"),)
    return tuple(entries)


@dataclass(frozen=True)
class Settings:
    bind_host: str
    port: int
    llama_base_url: str
    model_registry_path: Path
    max_queue: int
    job_timeout_seconds: int
    poll_interval_seconds: float
    allowed_input_dirs: tuple[Path, ...]
    enforce_memory_limit: bool
    memory_limit_mb: int | None
    ocr_max_pages: int


def load_settings() -> Settings:
    memory_limit_raw = os.getenv("AUX_ML_MEMORY_LIMIT_MB", "").strip()
    memory_limit_mb: int | None
    if memory_limit_raw:
        try:
            memory_limit_mb = int(memory_limit_raw)
        except ValueError:
            memory_limit_mb = None
    else:
        memory_limit_mb = None

    return Settings(
        bind_host=os.getenv("AUX_ML_BIND_HOST", "0.0.0.0"),
        port=_env_int("AUX_ML_PORT", 8091),
        llama_base_url=os.getenv("AUX_ML_LLAMACPP_URL", "http://127.0.0.1:8080").rstrip("/"),
        model_registry_path=Path(os.getenv("AUX_ML_MODEL_REGISTRY", "/app/config/models.yaml")),
        max_queue=max(_env_int("AUX_ML_MAX_QUEUE", 50), 1),
        job_timeout_seconds=max(_env_int("AUX_ML_JOB_TIMEOUT_SECONDS", 1800), 1),
        poll_interval_seconds=max(_env_float("AUX_ML_POLL_INTERVAL_SECONDS", 1.0), 0.1),
        allowed_input_dirs=_parse_allowed_input_dirs(os.getenv("AUX_ML_ALLOWED_INPUT_DIRS")),
        enforce_memory_limit=_env_bool("AUX_ML_ENFORCE_MEMORY_LIMIT", True),
        memory_limit_mb=memory_limit_mb,
        ocr_max_pages=max(_env_int("AUX_ML_OCR_MAX_PAGES", 50), 1),
    )
