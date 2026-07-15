from __future__ import annotations

import asyncio

from .adapters import run_ocr_task, run_transcription_task
from .jobs import JobScheduler, QueueFullError
from .llama_router import LlamaRouterClient
from .model_registry import ModelRegistry, ModelSpec
from .settings import Settings


class JobCancelledError(asyncio.CancelledError):
    """Raised when a running job is cancelled by an explicit cancel request."""


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
        self._scheduler = JobScheduler(max_size=settings.max_queue)

        self._worker_task: asyncio.Task | None = None
        self._running_job_id: str | None = None
        self._running_job_task: asyncio.Task | None = None
        self._cancel_event: asyncio.Event | None = None
        self._loaded_model_key: str | None = None
        self._loaded_model_id: str | None = None
        # Model id we are currently attempting to load. Tracked *before*
        # /models/load so cancellation during loading can unload the right
        # target even if _loaded_model_id has not been committed yet.
        self._loading_model_id: str | None = None
        self._stopping = False
        # Dispatch health. When a cleanup failure leaves the runtime in an
        # unknown state, dispatch is paused so replacement work cannot start
        # unsafely. Recovery requires a restart.
        self._dispatch_blocked = False
        self._degraded_reason: str | None = None

    # ------------------------------------------------------------------ #
    # Public accessors
    # ------------------------------------------------------------------ #
    @property
    def router(self) -> LlamaRouterClient:
        return self._router

    @property
    def running_job_id(self) -> str | None:
        return self._running_job_id

    @property
    def loaded_model_key(self) -> str | None:
        return self._loaded_model_key

    @property
    def dispatch_blocked(self) -> bool:
        return self._dispatch_blocked

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    async def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker_loop(), name="aux-ml-worker")

    async def stop(self) -> None:
        """Explicit shutdown.

        Sets the stopping flag, cancels the worker task, cancels any running
        job task, unloads the current model, and closes the router client.
        The worker loop treats ``_stopping`` as a signal to propagate
        ``CancelledError`` rather than marking the job cancelled, so shutdown
        does not race with the cancel path.
        """
        self._stopping = True
        if self._cancel_event is not None:
            self._cancel_event.set()

        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        if self._running_job_task is not None and not self._running_job_task.done():
            self._running_job_task.cancel()
            try:
                await self._running_job_task
            except (asyncio.CancelledError, JobCancelledError, Exception):
                pass
            self._running_job_task = None

        await self._unload_current_model(strict=False)
        await self._router.close()

    # ------------------------------------------------------------------ #
    # Job submission / lookup
    # ------------------------------------------------------------------ #
    async def submit_job(
        self,
        *,
        task: str,
        model: str | None,
        file_path: str,
        prompt: str | None,
        column_split: int,
        column_split_pages: list[int] | None,
    ) -> dict:
        normalized_task = task.strip().lower()
        if not normalized_task:
            raise ValueError("Task is required")

        if column_split < 1:
            raise ValueError("column_split must be >= 1")

        normalized_split_pages: tuple[int, ...] | None = None
        if column_split_pages:
            normalized_split_pages = tuple(sorted(set(column_split_pages)))

        if model:
            spec = self._registry.get(model)
        else:
            spec = self._registry.default_for_task(normalized_task)

        if spec.task != normalized_task:
            raise ValueError(
                f"Model '{spec.key}' is configured for task '{spec.task}', not '{normalized_task}'"
            )

        job = await self._scheduler.create(
            task=normalized_task,
            model=spec.key,
            file_path=file_path,
            prompt=prompt,
            column_split=column_split,
            column_split_pages=normalized_split_pages,
        )
        try:
            queue_position = await self._scheduler.enqueue(job.id)
        except QueueFullError:
            await self._scheduler.delete(job.id)
            raise
        return {
            "job_id": job.id,
            "status": job.status,
            "queue_position": queue_position,
            "model": spec.key,
            "task": normalized_task,
            "column_split": column_split,
            "column_split_pages": list(normalized_split_pages) if normalized_split_pages else None,
        }

    async def get_job(self, job_id: str) -> dict | None:
        job = await self._scheduler.get(job_id)
        if job is None:
            return None
        return job.to_response()

    async def wait_for_job(self, job_id: str, timeout_seconds: int) -> dict | None:
        record = await self._scheduler.wait_for_terminal(job_id, timeout_seconds=timeout_seconds)
        if record is None:
            return None
        return record.to_response()

    # ------------------------------------------------------------------ #
    # Cancellation endpoint.
    #
    # The endpoint ONLY records intent: it sets the cancellation event,
    # transitions the job to ``cancelling`` (for running jobs), and cancels
    # the separately owned active task. It returns promptly. The WORKER
    # alone owns model cleanup and the final ``cancelled`` status.
    # ------------------------------------------------------------------ #
    async def cancel_job(self, job_id: str) -> dict:
        status = await self._scheduler.status(job_id)
        if status is None:
            raise KeyError(f"Unknown job id: {job_id}")

        if status == "queued":
            # Atomic: remove from FIFO + mark cancelled under one lock.
            ok = await self._scheduler.cancel_queued(job_id)
            if ok:
                return {
                    "job_id": job_id,
                    "status": "cancelled",
                    "cancelled": True,
                    "message": "Queued job cancelled.",
                }
            # Lost the race (job was claimed between status check and
            # cancel_queued). Fall through to re-read current status.
            status = await self._scheduler.status(job_id)
            if status is None:
                raise KeyError(f"Unknown job id: {job_id}")

        if status == "running":
            # Record intent: transition to cancelling, set the cancel event,
            # and cancel the active task. Do NOT unload the model here; the
            # worker owns cleanup and the final status.
            ok = await self._scheduler.mark_cancelling(job_id)
            if not ok:
                # Lost the CAS race to another caller (or to a terminal
                # transition). Re-read the current status and respond
                # idempotently: a concurrent running-cancel that lost the
                # CAS but now observes ``cancelling`` must return the normal
                # idempotent cancelling response (202 via main), NOT a 409
                # not-cancellable response.
                status = await self._scheduler.status(job_id)
                if status is None:
                    raise KeyError(f"Unknown job id: {job_id}")
                if status == "cancelling":
                    return self._cancelling_response(job_id)
                if status == "cancelled":
                    return self._cancelled_response(job_id)
                # succeeded / failed / running-again: not cancellable from
                # this caller's perspective.
                return self._not_cancellable_response(job_id, status)

            if self._cancel_event is not None:
                self._cancel_event.set()
            if self._running_job_task is not None and not self._running_job_task.done():
                self._running_job_task.cancel()
            return self._cancelling_response(job_id)

        if status == "cancelling":
            # Idempotent: cancellation already in progress. Only ensure the
            # event is set if it is safe to do so. Do NOT cancel the active
            # task again: the first cancel already requested termination,
            # and the worker may currently be inside subprocess
            # termination/reaping. A repeated Task.cancel() could interrupt
            # that cleanup and leave the runtime in an inconsistent state.
            if self._cancel_event is not None and not self._cancel_event.is_set():
                self._cancel_event.set()
            return self._cancelling_response(job_id)

        if status == "cancelled":
            # Idempotent: already cancelled.
            return self._cancelled_response(job_id)

        # succeeded / failed -> 409 conflict.
        return self._not_cancellable_response(job_id, status)

    def _cancelling_response(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "status": "cancelling",
            "cancelled": True,
            "message": "Cancellation already in progress.",
        }

    def _cancelled_response(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "status": "cancelled",
            "cancelled": True,
            "message": "Job already cancelled.",
        }

    def _not_cancellable_response(self, job_id: str, status: str) -> dict:
        return {
            "job_id": job_id,
            "status": status,
            "cancelled": False,
            "message": f"Job is not cancellable in status '{status}'.",
        }

    # ------------------------------------------------------------------ #
    # Queue / health snapshot
    # ------------------------------------------------------------------ #
    async def queue_snapshot(self) -> dict:
        queued_ids = await self._scheduler.snapshot_ids()
        queued_jobs: list[dict] = []
        for position, job_id in enumerate(queued_ids, start=1):
            job = await self._scheduler.get(job_id)
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
            "dispatch_blocked": self._dispatch_blocked,
            "degraded_reason": self._degraded_reason,
        }

    # ------------------------------------------------------------------ #
    # Worker loop
    # ------------------------------------------------------------------ #
    async def _worker_loop(self) -> None:
        try:
            while True:
                if self._stopping:
                    return
                if self._dispatch_blocked:
                    # Dispatch is paused due to a cleanup failure. Do not
                    # start new work; wait for shutdown/restart.
                    await asyncio.sleep(0.1)
                    continue

                job = await self._scheduler.claim_next()
                if job is None:
                    continue
                if job.status != "running":
                    # claim_next already transitioned; this is a guard.
                    continue

                await self._run_job_lifecycle(job)
        except asyncio.CancelledError:
            # Shutdown: propagate.
            raise

    async def _run_job_lifecycle(self, job) -> None:
        job_id = job.id
        # Publish cancellation primitives BEFORE inspecting scheduler status
        # so a cancel_job that raced between claim_next() and this point is
        # observed safely. claim_next() already transitioned the job to
        # ``running``; a racing cancel_job may have since transitioned it to
        # ``cancelling`` without seeing these primitives (they were None).
        self._running_job_id = job_id
        self._cancel_event = asyncio.Event()
        try:
            # If cancel already won the race, do not start model load/adapter;
            # run worker-owned cancellation finalization directly.
            current = await self._scheduler.status(job_id)
            if current == "cancelling":
                await self._finalize_cancellation(job_id)
                return

            # Status is still ``running``. Create and start the active task.
            # A cancel_job that races between this status check and task
            # creation will set ``_cancel_event`` (already published above);
            # the task observes it via its cancellation checks and raises
            # JobCancelledError before expensive work.
            self._running_job_task = asyncio.create_task(
                self._run_job(job), name=f"aux-ml-job-{job_id}"
            )
            try:
                await self._running_job_task
            except JobCancelledError:
                # Cancellation was observed inside the job task. Run the
                # cleanup sequence: unload model -> confirm -> mark cancelled.
                await self._finalize_cancellation(job_id)
            except asyncio.CancelledError:
                if self._stopping:
                    # Shutdown in progress: propagate without finalizing.
                    raise
                # Hard cancel from the endpoint path. The endpoint already
                # transitioned to cancelling; run cleanup.
                await self._finalize_cancellation(job_id)
            except Exception as exc:
                # Late failure: only apply if still running (CAS guards
                # against overwriting cancelling/cancelled).
                ok = await self._scheduler.mark_failed(job_id, str(exc))
                if not ok:
                    # Job was already cancelling/cancelled; the failure
                    # happened during unwind. Treat as cancellation
                    # finalization.
                    await self._finalize_cancellation(job_id)
            else:
                # Success: CAS running -> succeeded. If the job was already
                # cancelling (cancel arrived during the final adapter call),
                # mark_succeeded returns False and we finalize cancellation.
                result = (
                    self._running_job_task.result()
                    if self._running_job_task is not None
                    else None
                )
                if result is None:
                    result = {}
                ok = await self._scheduler.mark_succeeded(job_id, result)
                if not ok:
                    await self._finalize_cancellation(job_id)
        finally:
            self._running_job_task = None
            self._cancel_event = None
            next_model = await self._peek_next_model_key()
            if self._loaded_model_key is not None and next_model != self._loaded_model_key:
                await self._unload_current_model(strict=False)
            self._running_job_id = None

    async def _finalize_cancellation(self, job_id: str) -> None:
        """Worker-owned cleanup sequence after a running job is cancelled.

        Order: the job is already in ``cancelling`` (set by the endpoint or
        by the job task raising JobCancelledError). We:
          1. Cancel the active task if still alive (defensive).
          2. Await its unwind.
          3. Unload the model/loading target.
          4. Confirm unload.
          5. Mark cancelled (CAS cancelling -> cancelled).

        If unload fails, mark the job failed with a cleanup error, expose a
        degraded reason, and block dispatch so replacement work cannot
        start unsafely.
        """
        # 1-2. Cancel and await the active task if still alive.
        if self._running_job_task is not None and not self._running_job_task.done():
            self._running_job_task.cancel()
            try:
                await self._running_job_task
            except (asyncio.CancelledError, JobCancelledError, Exception):
                pass

        # 3-4. Unload the model (or the loading target if cancellation
        # happened during load). Never swallow unload errors.
        try:
            await self._unload_for_cancellation(strict=True)
        except Exception as exc:
            # Cleanup failure: mark failed (cancelling -> failed), expose
            # degraded reason, and block dispatch.
            cleanup_error = f"Cancellation cleanup failed: {exc}"
            await self._scheduler.mark_failed_cleanup(job_id, cleanup_error)
            self._dispatch_blocked = True
            self._degraded_reason = cleanup_error
            return

        # 5. Confirm cancelled (CAS cancelling -> cancelled). If the job is
        # still running (defensive: cancel_event set before mark_cancelling
        # completed in a rare race), transition running -> cancelling first.
        current = await self._scheduler.status(job_id)
        if current == "running":
            await self._scheduler.mark_cancelling(job_id)
        ok = await self._scheduler.mark_cancelled(job_id)
        if not ok:
            # Already terminal (e.g. concurrent cleanup-failure path). Nothing
            # more to do.
            pass

    async def _run_job(self, job) -> dict:
        try:
            model_spec = self._registry.get(job.model)
            request_model_id = await self._ensure_model_loaded(model_spec)

            # Check for cancellation after model load.
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise JobCancelledError()

            if job.task == "ocr":
                result = await run_ocr_task(
                    file_path=job.file_path,
                    model_spec=model_spec,
                    model_id=request_model_id,
                    prompt=job.prompt,
                    timeout_seconds=self._settings.job_timeout_seconds,
                    max_pages=self._settings.ocr_max_pages,
                    column_split=job.column_split,
                    column_split_pages=job.column_split_pages,
                    allowed_roots=self._settings.allowed_input_dirs,
                    router=self._router,
                    cancel_event=self._cancel_event,
                )
            elif job.task == "transcribe":
                result = await run_transcription_task(
                    file_path=job.file_path,
                    model_spec=model_spec,
                    model_id=request_model_id,
                    prompt=job.prompt,
                    timeout_seconds=self._settings.job_timeout_seconds,
                    max_audio_bytes=self._settings.transcribe_max_bytes,
                    max_duration_seconds=self._settings.transcribe_max_duration_seconds,
                    max_chunks=self._settings.transcribe_max_chunks,
                    chunk_seconds=self._settings.transcribe_chunk_seconds,
                    overlap_seconds=self._settings.transcribe_overlap_seconds,
                    ffmpeg_timeout_seconds=self._settings.transcribe_ffmpeg_timeout_seconds,
                    allowed_roots=self._settings.allowed_input_dirs,
                    router=self._router,
                    cancel_event=self._cancel_event,
                )
            else:
                raise ValueError(f"Unsupported task: {job.task}")

            return result
        except (JobCancelledError, asyncio.CancelledError):
            raise
        except Exception as exc:
            # If a cancellation was requested during the failed operation,
            # treat it as cancellation rather than failure.
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise JobCancelledError() from exc
            raise

    async def _peek_next_model_key(self) -> str | None:
        next_job_id = await self._scheduler.peek()
        if next_job_id is None:
            return None
        next_job = await self._scheduler.get(next_job_id)
        if next_job is None:
            return None
        return next_job.model

    # ------------------------------------------------------------------ #
    # Model load / unload
    # ------------------------------------------------------------------ #
    async def _ensure_model_loaded(self, model_spec: ModelSpec) -> str:
        if self._loaded_model_key == model_spec.key and self._loaded_model_id is not None:
            return self._loaded_model_id

        if self._loaded_model_id is not None:
            await self._unload_current_model(strict=False)

        # Check cancellation before starting the load.
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise JobCancelledError()

        model_id = await self._router.resolve_model_id(model_spec.model_path)

        # Track the loading target BEFORE issuing /models/load so a cancel
        # during loading can unload the right model.
        self._loading_model_id = model_id
        try:
            await self._router.load_model(model_id)
            await self._router.wait_for_status(
                model_id=model_id,
                expected={"loaded"},
                timeout_seconds=self._settings.job_timeout_seconds,
                poll_interval_seconds=self._settings.poll_interval_seconds,
            )

            # Check cancellation after the load/wait completes.
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise JobCancelledError()

            self._loaded_model_key = model_spec.key
            self._loaded_model_id = model_id
            return model_id
        finally:
            # Once the load is committed (or failed/cancelled), clear the
            # loading target only if the load succeeded. On failure/cancel,
            # keep _loading_model_id set so the cancellation cleanup path can
            # unload the right model.
            if self._loaded_model_id == model_id:
                self._loading_model_id = None

    async def _unload_for_cancellation(self, *, strict: bool) -> None:
        """Unload the model or the in-flight loading target.

        Used by the cancellation cleanup path. When ``strict`` is True,
        unload errors are propagated (never swallowed) and the model
        identity is only cleared after a confirmed unload.
        """
        target = self._loaded_model_id
        if target is None:
            target = self._loading_model_id
        if target is None:
            self._loaded_model_key = None
            return

        try:
            await self._router.unload_model(target)
            await self._router.wait_for_status(
                model_id=target,
                expected={"unloaded", "sleeping"},
                timeout_seconds=self._settings.job_timeout_seconds,
                poll_interval_seconds=self._settings.poll_interval_seconds,
            )
        except Exception:
            if strict:
                # Do NOT clear model identity on unconfirmed unload.
                raise
            # Non-strict (best-effort, e.g. shutdown): clear anyway.
            self._loaded_model_key = None
            self._loaded_model_id = None
            self._loading_model_id = None
            return

        # Confirmed unloaded: safe to clear identity.
        self._loaded_model_key = None
        self._loaded_model_id = None
        self._loading_model_id = None

    async def _unload_current_model(self, *, strict: bool = False) -> None:
        """Best-effort unload used by the normal worker cycle and shutdown."""
        if self._loaded_model_id is None and self._loading_model_id is None:
            self._loaded_model_key = None
            return
        await self._unload_for_cancellation(strict=strict)


__all__ = ["AuxMLService", "JobCancelledError", "QueueFullError"]
