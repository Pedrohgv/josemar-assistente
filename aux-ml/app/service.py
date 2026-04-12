from __future__ import annotations

import asyncio

from .adapters import run_ocr_task
from .jobs import JobStore, QueueFullError, QueueManager
from .llama_router import LlamaRouterClient
from .model_registry import ModelRegistry, ModelSpec
from .settings import Settings


class AuxMLService:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: ModelRegistry,
        router: LlamaRouterClient,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._router = router
        self._queue = QueueManager(max_size=settings.max_queue)
        self._jobs = JobStore()

        self._worker_task: asyncio.Task | None = None
        self._running_job_id: str | None = None
        self._loaded_model_key: str | None = None
        self._loaded_model_id: str | None = None

    @property
    def router(self) -> LlamaRouterClient:
        return self._router

    @property
    def running_job_id(self) -> str | None:
        return self._running_job_id

    @property
    def loaded_model_key(self) -> str | None:
        return self._loaded_model_key

    async def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker_loop(), name="aux-ml-worker")

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        await self._unload_current_model()
        await self._router.close()

    async def submit_job(
        self,
        *,
        task: str,
        model: str | None,
        file_path: str,
        prompt: str | None,
    ) -> dict:
        normalized_task = task.strip().lower()
        if not normalized_task:
            raise ValueError("Task is required")

        if model:
            spec = self._registry.get(model)
        else:
            spec = self._registry.default_for_task(normalized_task)

        if spec.task != normalized_task:
            raise ValueError(
                f"Model '{spec.key}' is configured for task '{spec.task}', not '{normalized_task}'"
            )

        job = await self._jobs.create(
            task=normalized_task,
            model=spec.key,
            file_path=file_path,
            prompt=prompt,
        )
        try:
            queue_position = await self._queue.enqueue(job.id)
        except QueueFullError:
            await self._jobs.delete(job.id)
            raise
        return {
            "job_id": job.id,
            "status": job.status,
            "queue_position": queue_position,
            "model": spec.key,
            "task": normalized_task,
        }

    async def get_job(self, job_id: str) -> dict | None:
        job = await self._jobs.get(job_id)
        if job is None:
            return None
        return job.to_response()

    async def wait_for_job(self, job_id: str, timeout_seconds: int) -> dict | None:
        record = await self._jobs.wait_for_terminal(job_id, timeout_seconds=timeout_seconds)
        if record is None:
            return None
        return record.to_response()

    async def queue_snapshot(self) -> dict:
        queued_ids = await self._queue.snapshot_ids()
        queued_jobs: list[dict] = []
        for position, job_id in enumerate(queued_ids, start=1):
            job = await self._jobs.get(job_id)
            if job is None:
                continue
            queued_jobs.append(
                {
                    "position": position,
                    "job_id": job.id,
                    "task": job.task,
                    "model": job.model,
                    "created_at": job.created_at,
                }
            )

        return {
            "queue_size": len(queued_jobs),
            "running_job_id": self._running_job_id,
            "loaded_model": self._loaded_model_key,
            "queued_jobs": queued_jobs,
        }

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.dequeue()
            job = await self._jobs.get(job_id)
            if job is None:
                continue

            self._running_job_id = job_id
            await self._jobs.mark_running(job_id)

            try:
                model_spec = self._registry.get(job.model)
                request_model_id = await self._ensure_model_loaded(model_spec)

                if job.task == "ocr":
                    result = await run_ocr_task(
                        file_path=job.file_path,
                        model_spec=model_spec,
                        model_id=request_model_id,
                        prompt=job.prompt,
                        timeout_seconds=self._settings.job_timeout_seconds,
                        max_pages=self._settings.ocr_max_pages,
                        allowed_roots=self._settings.allowed_input_dirs,
                        router=self._router,
                    )
                else:
                    raise ValueError(f"Unsupported task: {job.task}")

                await self._jobs.mark_succeeded(job_id, result=result)
            except Exception as exc:
                await self._jobs.mark_failed(job_id, error=str(exc))
            finally:
                next_model = await self._peek_next_model_key()
                if self._loaded_model_key is not None and next_model != self._loaded_model_key:
                    await self._unload_current_model()
                self._running_job_id = None

    async def _peek_next_model_key(self) -> str | None:
        next_job_id = await self._queue.peek()
        if next_job_id is None:
            return None
        next_job = await self._jobs.get(next_job_id)
        if next_job is None:
            return None
        return next_job.model

    async def _ensure_model_loaded(self, model_spec: ModelSpec) -> str:
        if self._loaded_model_key == model_spec.key and self._loaded_model_id is not None:
            return self._loaded_model_id

        if self._loaded_model_id is not None:
            await self._unload_current_model()

        model_id = await self._router.resolve_model_id(model_spec.model_path)
        await self._router.load_model(model_id)
        await self._router.wait_for_status(
            model_id=model_id,
            expected={"loaded"},
            timeout_seconds=self._settings.job_timeout_seconds,
            poll_interval_seconds=self._settings.poll_interval_seconds,
        )

        self._loaded_model_key = model_spec.key
        self._loaded_model_id = model_id
        return model_id

    async def _unload_current_model(self) -> None:
        if self._loaded_model_id is None:
            self._loaded_model_key = None
            return

        try:
            await self._router.unload_model(self._loaded_model_id)
            await self._router.wait_for_status(
                model_id=self._loaded_model_id,
                expected={"unloaded", "sleeping"},
                timeout_seconds=self._settings.job_timeout_seconds,
                poll_interval_seconds=self._settings.poll_interval_seconds,
            )
        except Exception:
            pass
        finally:
            self._loaded_model_key = None
            self._loaded_model_id = None


__all__ = ["AuxMLService", "QueueFullError"]
