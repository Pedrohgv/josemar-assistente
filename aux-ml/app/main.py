from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .jobs import QueueFullError
from .llama_router import LlamaRouterClient
from .model_registry import ModelRegistry
from .service import AuxMLService
from .settings import Settings, load_settings


def _detect_cgroup_memory_limit_mb() -> int | None:
    paths = [
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ]
    for path in paths:
        if not path.exists():
            continue

        raw = path.read_text(encoding="utf-8").strip()
        if not raw or raw == "max":
            return None

        try:
            value = int(raw)
        except ValueError:
            continue

        if value <= 0 or value >= 2**60:
            return None
        return value // (1024 * 1024)

    return None


def _validate_memory_policy(settings: Settings, registry: ModelRegistry) -> dict:
    required_mb = registry.max_required_memory_mb()
    configured_mb = settings.memory_limit_mb
    detected_cgroup_mb = _detect_cgroup_memory_limit_mb()

    effective_mb = configured_mb if configured_mb is not None else detected_cgroup_mb
    if settings.enforce_memory_limit and effective_mb is None:
        raise RuntimeError(
            "Cannot validate memory budget. Set AUX_ML_MEMORY_LIMIT_MB explicitly."
        )

    if settings.enforce_memory_limit and effective_mb is not None and effective_mb < required_mb:
        raise RuntimeError(
            "Configured memory budget is smaller than required model memory: "
            f"configured={effective_mb}MB required={required_mb}MB"
        )

    return {
        "required_memory_mb": required_mb,
        "configured_memory_mb": configured_mb,
        "detected_cgroup_memory_mb": detected_cgroup_mb,
        "effective_memory_mb": effective_mb,
        "enforce_memory_limit": settings.enforce_memory_limit,
    }


def _validate_model_files(registry: ModelRegistry) -> None:
    missing: list[str] = []
    for spec in registry.specs():
        if not spec.model_path.exists():
            missing.append(str(spec.model_path))
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing model files in container image: {joined}")


class SubmitJobRequest(BaseModel):
    task: str = Field(default="ocr")
    model: str | None = None
    file_path: str
    prompt: str | None = None
    column_split: Annotated[int, Field(ge=1, le=4, strict=True)] = 1
    column_split_pages: list[Annotated[int, Field(ge=1, strict=True)]] | None = None


class RunJobRequest(SubmitJobRequest):
    wait_timeout_seconds: int | None = Field(default=None, ge=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    registry = ModelRegistry.from_file(settings.model_registry_path)
    _validate_model_files(registry)
    memory_policy = _validate_memory_policy(settings, registry)

    router = LlamaRouterClient(
        base_url=settings.llama_base_url,
        default_timeout_seconds=settings.job_timeout_seconds,
    )
    service = AuxMLService(settings=settings, registry=registry, router=router)
    await service.start()

    app.state.settings = settings
    app.state.registry = registry
    app.state.service = service
    app.state.memory_policy = memory_policy

    try:
        yield
    finally:
        await service.stop()


app = FastAPI(
    title="Auxiliary ML Orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)


def _service() -> AuxMLService:
    return app.state.service


@app.get("/health")
async def health() -> dict:
    service = _service()
    router_ok = await service.router.ping()
    queue_state = await service.queue_snapshot()

    status = "ok" if router_ok else "degraded"
    return {
        "status": status,
        "router_reachable": router_ok,
        "memory_policy": app.state.memory_policy,
        "queue": queue_state,
        "registered_models": app.state.registry.list_models(),
    }


@app.get("/queue")
async def queue_state() -> dict:
    service = _service()
    return await service.queue_snapshot()


@app.post("/jobs")
async def submit_job(payload: SubmitJobRequest) -> dict:
    service = _service()
    try:
        return await service.submit_job(
            task=payload.task,
            model=payload.model,
            file_path=payload.file_path,
            prompt=payload.prompt,
            column_split=payload.column_split,
            column_split_pages=payload.column_split_pages,
        )
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    service = _service()
    job = await service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job id: {job_id}")
    return job


@app.post("/run")
async def run_and_wait(payload: RunJobRequest) -> dict:
    service = _service()
    settings: Settings = app.state.settings

    try:
        submitted = await service.submit_job(
            task=payload.task,
            model=payload.model,
            file_path=payload.file_path,
            prompt=payload.prompt,
            column_split=payload.column_split,
            column_split_pages=payload.column_split_pages,
        )
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    wait_timeout = payload.wait_timeout_seconds or settings.job_timeout_seconds
    try:
        job = await service.wait_for_job(submitted["job_id"], timeout_seconds=wait_timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Timed out waiting for job '{submitted['job_id']}'",
        ) from exc

    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job id: {submitted['job_id']}")
    return job
