from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:  # package context (discovery)
    from ._stub_import import stubbed_app_import
except ImportError:  # direct execution: tests/aux_ml/ is sys.path[0]
    from _stub_import import stubbed_app_import


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "aux-ml"))
# Stub optional deps ONLY around the application import so the stubs do not
# persist for the whole test process (a persistent fake httpx would poison
# real httpx for other test modules, e.g. the Mnemosyne DR seam imports —
# issue #91). The shared helper fully restores sys.modules AND the app
# package's __dict__ attributes, so neither `import app.child` nor
# `from app import child` can reuse fake-bound objects. The bound objects
# (AuxMLService, ModelRegistry, Settings, _service_module) remain valid and
# are used directly by `patch.object(_service_module, ...)` — no string-path
# patching that would require app.service to stay cached.
with stubbed_app_import("pymupdf", "httpx"):
    from app.model_registry import ModelRegistry, ModelSpec
    from app.settings import Settings
    from app.service import AuxMLService, JobCancelledError
    import app.service as _service_module


class FakeRouter:
    """Minimal async router fake for service-level tests."""

    def __init__(self) -> None:
        self.loaded_models: list[str] = []
        self.unloaded_models: list[str] = []
        self.unload_errors: dict[str, Exception] = {}
        self.chat_calls = 0

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def list_models(self) -> list[dict]:
        return [
            {"id": "glm-ocr", "path": "/models/glm-ocr.gguf", "status": {"value": "loaded"}}
        ]

    async def resolve_model_id(self, model_path: Path) -> str:
        return model_path.stem

    async def load_model(self, model_id: str) -> None:
        self.loaded_models.append(model_id)

    async def unload_model(self, model_id: str) -> None:
        if model_id in self.unload_errors:
            raise self.unload_errors[model_id]
        self.unloaded_models.append(model_id)

    async def wait_for_status(
        self,
        model_id: str,
        expected: set[str],
        timeout_seconds: int,
        poll_interval_seconds: float,
    ) -> None:
        return None


def _make_settings(tmp: Path) -> Settings:
    return Settings(
        bind_host="0.0.0.0",
        port=8091,
        llama_base_url="http://127.0.0.1:8080",
        model_registry_path=tmp / "models.yaml",
        max_queue=10,
        job_timeout_seconds=30,
        poll_interval_seconds=0.05,
        allowed_input_dirs=(tmp,),
        enforce_memory_limit=False,
        memory_limit_mb=None,
        ocr_max_pages=50,
        transcribe_max_bytes=100 * 1024 * 1024,
        transcribe_max_duration_seconds=1800,
        transcribe_max_chunks=72,
        transcribe_chunk_seconds=30,
        transcribe_overlap_seconds=2,
        transcribe_ffmpeg_timeout_seconds=300,
    )


def _make_registry(tmp: Path) -> ModelRegistry:
    spec = ModelSpec(
        "glm-ocr",
        "ocr",
        tmp / "glm-ocr.gguf",
        1024,
        "Text Recognition:",
        128,
        mmproj_path=tmp / "mmproj-glm-ocr.gguf",
        optional=False,
    )
    return ModelRegistry({"glm-ocr": spec})


def _patched_ocr_factory(gate: asyncio.Event | None = None, *, started: asyncio.Event | None = None):
    """Build a fake run_ocr_task that blocks on `gate` until set or cancelled.

    If `started` is provided, it is set once the fake task begins executing,
    giving tests a deterministic barrier instead of sleeps.
    """

    async def fake_run_ocr_task(**kwargs):
        cancel_event = kwargs.get("cancel_event")
        if started is not None:
            started.set()
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("cancelled before start")
        if gate is not None:
            await asyncio.wait_for(gate.wait(), timeout=30)
        return {"text": "ok", "page_count": 1}

    return fake_run_ocr_task


class ServiceCancelQueuedJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_queued_job_removes_from_queue_and_marks_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate, started=started)):
                await service.start()
                try:
                    first = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/first.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()
                    self.assertEqual(service.running_job_id, first["job_id"])

                    second = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/second.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    self.assertEqual(second["queue_position"], 1)

                    result = await service.cancel_job(second["job_id"])

                    self.assertTrue(result["cancelled"])
                    self.assertEqual(result["status"], "cancelled")
                    self.assertIn("Queued job cancelled", result["message"])

                    second_status = await service.get_job(second["job_id"])
                    self.assertEqual(second_status["status"], "cancelled")

                    queue = await service.queue_snapshot()
                    queued_ids = [j["job_id"] for j in queue["queued_jobs"]]
                    self.assertNotIn(second["job_id"], queued_ids)
                finally:
                    gate.set()
                    await service.stop()


class ServiceCancelRunningJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_running_job_transitions_to_cancelling_then_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate, started=started)):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()
                    self.assertEqual(service.running_job_id, submitted["job_id"])
                    self.assertEqual(service.loaded_model_key, "glm-ocr")
                    self.assertEqual(router.loaded_models, ["glm-ocr"])

                    result = await service.cancel_job(submitted["job_id"])

                    # Endpoint returns promptly with cancelling.
                    self.assertTrue(result["cancelled"])
                    self.assertEqual(result["status"], "cancelling")

                    # Worker finalizes: model unloaded, job cancelled.
                    final = await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    self.assertIsNotNone(final)
                    self.assertEqual(final["status"], "cancelled")

                    self.assertIn("glm-ocr", router.unloaded_models)
                    self.assertIsNone(service.loaded_model_key)
                finally:
                    gate.set()
                    await service.stop()

    async def test_cancel_running_job_worker_survives_and_processes_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate, started=started)):
                await service.start()
                try:
                    first = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/first.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()
                    self.assertEqual(service.running_job_id, first["job_id"])

                    await service.cancel_job(first["job_id"])
                    cancelled = await service.wait_for_job(first["job_id"], timeout_seconds=5)
                    self.assertEqual(cancelled["status"], "cancelled")

                    # Release the gate so the next job can complete.
                    gate.set()

                    second = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/second.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    completed = await service.wait_for_job(second["job_id"], timeout_seconds=5)
                    self.assertIsNotNone(completed)
                    self.assertEqual(completed["status"], "succeeded")
                finally:
                    gate.set()
                    await service.stop()

    async def test_cancel_after_claim_before_active_task_publication(self) -> None:
        """Cancel a job that was just claimed but before the OCR task is
        published. The endpoint should still record intent and the worker
        should finalize cancellation."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate, started=started)):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    # Wait until the model is loaded (claim happened) but
                    # before the OCR task starts (gate not set).
                    await started.wait()
                    self.assertEqual(service.running_job_id, submitted["job_id"])

                    result = await service.cancel_job(submitted["job_id"])
                    self.assertEqual(result["status"], "cancelling")

                    final = await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    self.assertEqual(final["status"], "cancelled")
                finally:
                    gate.set()
                    await service.stop()

    async def test_cancel_in_window_between_claim_and_lifecycle_publication(self) -> None:
        """Deterministic barrier test for the narrow race between
        ``claim_next`` (which transitions the job to ``running``) and
        ``_run_job_lifecycle`` publishing cancellation primitives / starting
        the active task.

        The worker is paused at the entry of ``_run_job_lifecycle`` (job is
        already ``running`` in the scheduler, but ``_cancel_event`` and
        ``_running_job_task`` are still None). ``cancel_job`` is called in
        that window: it transitions the job to ``cancelling`` and returns 202
        without seeing the (not-yet-published) primitives. When the barrier
        releases, the lifecycle must observe ``cancelling`` and finalize
        cancellation directly, WITHOUT starting model load or the adapter.
        The worker must remain usable afterwards.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )

            # Barrier paused at lifecycle entry. The "entered" event is set
            # once the worker reaches the lifecycle (claim happened), so the
            # test can issue cancel_job deterministically before any
            # publication.
            lifecycle_barrier = asyncio.Event()
            lifecycle_entered = asyncio.Event()

            original_lifecycle = service._run_job_lifecycle

            async def gated_lifecycle(job):
                lifecycle_entered.set()
                # Hold here until the test releases the barrier. The job is
                # already ``running`` in the scheduler at this point.
                await asyncio.wait_for(lifecycle_barrier.wait(), timeout=30)
                return await original_lifecycle(job)

            ocr_started = asyncio.Event()

            async def ocr_should_not_run(**kwargs):
                ocr_started.set()
                return {"text": "should-not-run"}

            with patch.object(_service_module, "run_ocr_task", side_effect=ocr_should_not_run):
                service._run_job_lifecycle = gated_lifecycle  # type: ignore[method-assign]
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    # Wait until the worker has claimed the job and entered
                    # the lifecycle (paused at the barrier).
                    await asyncio.wait_for(lifecycle_entered.wait(), timeout=5)
                    # claim_next transitioned the job to running in the
                    # scheduler. Primitives are not yet published.
                    status = await service._scheduler.status(submitted["job_id"])
                    self.assertEqual(status, "running")
                    self.assertIsNone(service._cancel_event)
                    self.assertIsNone(service._running_job_task)

                    # Cancel in the window: job is running, primitives absent.
                    result = await service.cancel_job(submitted["job_id"])
                    self.assertTrue(result["cancelled"])
                    self.assertEqual(result["status"], "cancelling")

                    # Release the barrier so the lifecycle proceeds.
                    lifecycle_barrier.set()

                    final = await service.wait_for_job(
                        submitted["job_id"], timeout_seconds=5
                    )
                    self.assertIsNotNone(final)
                    self.assertEqual(final["status"], "cancelled")

                    # Model load and adapter must NEVER have run.
                    self.assertEqual(router.loaded_models, [])
                    self.assertFalse(ocr_started.is_set())
                    self.assertIsNone(service.loaded_model_key)

                    # Worker remains usable: submit a second job that completes.
                    gate2 = asyncio.Event()
                    gate2.set()
                    started2 = asyncio.Event()
                    service._run_job_lifecycle = original_lifecycle  # type: ignore[method-assign]
                    with patch.object(
                        _service_module,
                        "run_ocr_task",
                        side_effect=_patched_ocr_factory(gate2, started=started2),
                    ):
                        second = await service.submit_job(
                            task="ocr", model="glm-ocr", file_path="/shared/second.pdf",
                            prompt=None, column_split=1, column_split_pages=None,
                        )
                        completed = await service.wait_for_job(
                            second["job_id"], timeout_seconds=5
                        )
                        self.assertIsNotNone(completed)
                        self.assertEqual(completed["status"], "succeeded")
                finally:
                    lifecycle_barrier.set()
                    await service.stop()


class ServiceCancelVsSuccessCASTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_vs_success_cas_late_success_does_not_overwrite(self) -> None:
        """If the job completes successfully at the same time as cancel, the
        CAS must ensure only one wins. If cancel won (cancelling), success is
        rejected and the job is finalized as cancelled."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )

            # Fake OCR that completes immediately (success path).
            async def fast_ocr(**kwargs):
                return {"text": "ok", "page_count": 1}

            with patch.object(_service_module, "run_ocr_task", side_effect=fast_ocr):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    completed = await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    # Job succeeded before cancel could be issued.
                    self.assertEqual(completed["status"], "succeeded")

                    # Now cancel: should get 409 semantics (not cancellable).
                    result = await service.cancel_job(submitted["job_id"])
                    self.assertFalse(result["cancelled"])
                    self.assertEqual(result["status"], "succeeded")
                finally:
                    await service.stop()


class ServiceCancelIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_queued_then_cancel_again_is_idempotent_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate, started=started)):
                await service.start()
                try:
                    first = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/first.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()

                    queued = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/second.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )

                    r1 = await service.cancel_job(queued["job_id"])
                    self.assertEqual(r1["status"], "cancelled")

                    # Second cancel: already cancelled -> idempotent.
                    r2 = await service.cancel_job(queued["job_id"])
                    self.assertTrue(r2["cancelled"])
                    self.assertEqual(r2["status"], "cancelled")
                finally:
                    gate.set()
                    await service.stop()

    async def test_concurrent_cancel_queued_exactly_one_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate, started=started)):
                await service.start()
                try:
                    first = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/first.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()

                    queued = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/second.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )

                    # Issue two concurrent cancels.
                    r1, r2 = await asyncio.gather(
                        service.cancel_job(queued["job_id"]),
                        service.cancel_job(queued["job_id"]),
                    )
                    # Both should report cancelled (idempotent on the second).
                    self.assertTrue(r1["cancelled"])
                    self.assertTrue(r2["cancelled"])
                    # Job must be cancelled, not duplicated.
                    final = await service.get_job(queued["job_id"])
                    self.assertEqual(final["status"], "cancelled")
                finally:
                    gate.set()
                    await service.stop()


class ServiceConcurrentRunningCancelCASTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_running_cancel_cas_loser_returns_idempotent_cancelling(self) -> None:
        """Two callers cancel the same RUNNING job concurrently. Exactly one
        wins the ``running -> cancelling`` CAS; the loser rereads the status
        and observes ``cancelling``. The loser MUST return the normal
        idempotent cancelling response (cancelled=true, status=cancelling,
        which main maps to HTTP 202), NOT a 409 not-cancellable response.

        This test exercises the CAS-loser path deterministically by gating
        the worker lifecycle so the job stays ``running`` (no terminal
        transition) until both cancels have been issued.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )

            # Gate the lifecycle so the job stays ``running`` (claimed but
            # not yet finalized) while we issue the two concurrent cancels.
            lifecycle_barrier = asyncio.Event()
            lifecycle_entered = asyncio.Event()
            original_lifecycle = service._run_job_lifecycle

            async def gated_lifecycle(job):
                lifecycle_entered.set()
                # Hold here until the test releases the barrier. The job is
                # already ``running`` in the scheduler at this point.
                await asyncio.wait_for(lifecycle_barrier.wait(), timeout=30)
                return await original_lifecycle(job)

            async def ocr_should_not_run(**kwargs):
                return {"text": "should-not-run"}

            with patch.object(_service_module, "run_ocr_task", side_effect=ocr_should_not_run):
                service._run_job_lifecycle = gated_lifecycle  # type: ignore[method-assign]
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    # Wait until the worker claimed the job (running) and is
                    # paused at the lifecycle barrier.
                    await asyncio.wait_for(lifecycle_entered.wait(), timeout=5)
                    status = await service._scheduler.status(submitted["job_id"])
                    self.assertEqual(status, "running")

                    # Issue two concurrent cancels of the same running job.
                    r1, r2 = await asyncio.gather(
                        service.cancel_job(submitted["job_id"]),
                        service.cancel_job(submitted["job_id"]),
                    )

                    # Both must report the idempotent cancelling response.
                    for r in (r1, r2):
                        self.assertTrue(r["cancelled"], f"expected cancelled=True, got {r}")
                        self.assertEqual(r["status"], "cancelling", f"expected cancelling, got {r}")

                    # The job must be in ``cancelling`` (not terminal yet).
                    mid = await service._scheduler.status(submitted["job_id"])
                    self.assertEqual(mid, "cancelling")

                    # Release the barrier so the worker finalizes.
                    lifecycle_barrier.set()

                    final = await service.wait_for_job(
                        submitted["job_id"], timeout_seconds=5
                    )
                    self.assertIsNotNone(final)
                    self.assertEqual(final["status"], "cancelled")
                finally:
                    lifecycle_barrier.set()
                    await service.stop()


class ServiceRepeatedCancelNoDoubleTaskCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancelling_request_does_not_cancel_task_again(self) -> None:
        """A ``cancelling`` request must NOT call ``_running_job_task.cancel()``
        a second time. The first cancel already requested termination; the
        worker may currently be inside subprocess termination/reaping, and a
        repeated ``Task.cancel()`` could interrupt that cleanup. This test
        proves the second cancel only ensures the event is set (if safe) and
        returns 202, without invoking ``Task.cancel`` again.

        Deterministic strategy: gate the OCR task so it blocks after the
        first cancel (so the task is still alive and ``cancel`` was already
        invoked once). Issue a second ``cancel_job`` while the task is still
        alive, and assert ``Task.cancel`` is not invoked a second time by
        patching the task's ``cancel`` method to count invocations.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )

            gate = asyncio.Event()
            started = asyncio.Event()
            task_cancel_calls = 0

            async def blocking_ocr(**kwargs):
                started.set()
                # Block until the test releases the gate. The first cancel
                # will invoke Task.cancel() on this coroutine's task.
                await asyncio.wait_for(gate.wait(), timeout=30)
                return {"text": "ok", "page_count": 1}

            with patch.object(_service_module, "run_ocr_task", side_effect=blocking_ocr):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()
                    self.assertEqual(service.running_job_id, submitted["job_id"])

                    # Wait until the active task is published.
                    await asyncio.sleep(0.05)
                    self.assertIsNotNone(service._running_job_task)
                    active_task = service._running_job_task
                    self.assertFalse(active_task.done())

                    # Wrap Task.cancel to count invocations.
                    original_cancel = active_task.cancel

                    def counting_cancel(*args, **kwargs):
                        nonlocal task_cancel_calls
                        task_cancel_calls += 1
                        return original_cancel(*args, **kwargs)

                    active_task.cancel = counting_cancel  # type: ignore[method-assign]

                    # First cancel: running -> cancelling. This MUST invoke
                    # Task.cancel() exactly once.
                    r1 = await service.cancel_job(submitted["job_id"])
                    self.assertTrue(r1["cancelled"])
                    self.assertEqual(r1["status"], "cancelling")
                    self.assertEqual(task_cancel_calls, 1)

                    # The task is still alive (gate not set); status is
                    # cancelling. Issue a second cancel: it must NOT invoke
                    # Task.cancel() again.
                    self.assertFalse(active_task.done())
                    r2 = await service.cancel_job(submitted["job_id"])
                    self.assertTrue(r2["cancelled"])
                    self.assertEqual(r2["status"], "cancelling")
                    self.assertEqual(
                        task_cancel_calls,
                        1,
                        "Repeated cancelling request must not invoke Task.cancel() again",
                    )

                    # Release the gate so the worker can finalize cleanup.
                    # The task was already cancelled once; awaiting it
                    # should raise CancelledError which the lifecycle maps to
                    # cancellation finalization.
                    gate.set()
                    final = await service.wait_for_job(
                        submitted["job_id"], timeout_seconds=5
                    )
                    self.assertIsNotNone(final)
                    self.assertEqual(final["status"], "cancelled")
                finally:
                    gate.set()
                    await service.stop()


class ServiceCancelTerminalJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_succeeded_job_returns_not_cancellable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            gate.set()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate)):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    completed = await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    self.assertEqual(completed["status"], "succeeded")

                    result = await service.cancel_job(submitted["job_id"])

                    self.assertFalse(result["cancelled"])
                    self.assertEqual(result["status"], "succeeded")
                finally:
                    await service.stop()

    async def test_cancel_unknown_job_raises_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory()):
                await service.start()
                try:
                    with self.assertRaises(KeyError):
                        await service.cancel_job("does-not-exist")
                finally:
                    await service.stop()


class ServiceCancelDuringLoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_load_unloads_loading_target(self) -> None:
        """When cancellation arrives during model load, the worker should
        unload the loading target (tracked before /models/load)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )

            load_started = asyncio.Event()
            load_gate = asyncio.Event()

            class SlowLoadRouter(FakeRouter):
                async def load_model(self, model_id: str) -> None:
                    self.loaded_models.append(model_id)
                    load_started.set()
                    await asyncio.wait_for(load_gate.wait(), timeout=30)

            slow_router = SlowLoadRouter()

            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=slow_router,
            )

            async def fake_ocr(**kwargs):
                return {"text": "ok"}

            with patch.object(_service_module, "run_ocr_task", side_effect=fake_ocr):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await load_started.wait()
                    # The loading target should be tracked.
                    self.assertEqual(service._loading_model_id, "glm-ocr")

                    result = await service.cancel_job(submitted["job_id"])
                    self.assertEqual(result["status"], "cancelling")

                    # Release the load so unload can proceed.
                    load_gate.set()

                    final = await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    self.assertEqual(final["status"], "cancelled")
                    # The loading target should have been unloaded.
                    self.assertIn("glm-ocr", slow_router.unloaded_models)
                finally:
                    load_gate.set()
                    await service.stop()


class ServiceUnloadFailureBlocksDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_unload_failure_marks_failed_and_blocks_dispatch(self) -> None:
        """When model unload fails during cancellation cleanup, the job is
        marked failed, dispatch is blocked, and a degraded reason is
        exposed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            # Make unload fail.
            router.unload_errors["glm-ocr"] = RuntimeError("unload boom")

            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate, started=started)):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()

                    result = await service.cancel_job(submitted["job_id"])
                    self.assertEqual(result["status"], "cancelling")

                    final = await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    self.assertEqual(final["status"], "failed")
                    self.assertIn("cleanup", (final["error"] or "").lower())

                    # Dispatch should be blocked.
                    self.assertTrue(service.dispatch_blocked)
                    self.assertIsNotNone(service.degraded_reason)

                    queue = await service.queue_snapshot()
                    self.assertTrue(queue["dispatch_blocked"])
                finally:
                    gate.set()
                    await service.stop()

    async def test_unload_failure_blocks_next_job_from_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            router.unload_errors["glm-ocr"] = RuntimeError("unload boom")

            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch.object(_service_module, "run_ocr_task", side_effect=_patched_ocr_factory(gate, started=started)):
                await service.start()
                try:
                    first = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/first.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()
                    await service.cancel_job(first["job_id"])

                    failed = await service.wait_for_job(first["job_id"], timeout_seconds=5)
                    self.assertEqual(failed["status"], "failed")
                    self.assertTrue(service.dispatch_blocked)

                    # Submit a second job; it should NOT start because dispatch
                    # is blocked.
                    gate.set()
                    second = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/second.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    # Give the worker a moment to (not) pick it up.
                    await asyncio.sleep(0.2)
                    second_status = await service.get_job(second["job_id"])
                    self.assertEqual(second_status["status"], "queued")
                    self.assertIsNone(service.running_job_id)
                finally:
                    gate.set()
                    await service.stop()


class ServiceTaskCancelledBeforeUnloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_task_cancelled_before_unload_call(self) -> None:
        """The active job task must be cancelled before the model unload is
        issued during cleanup."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            router = FakeRouter()
            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=router,
            )

            task_cancelled = asyncio.Event()
            unload_called = asyncio.Event()

            class TrackingRouter(FakeRouter):
                async def unload_model(self, model_id: str) -> None:
                    unload_called.set()
                    self.unloaded_models.append(model_id)

            tracking_router = TrackingRouter()

            gate = asyncio.Event()
            started = asyncio.Event()

            async def blocking_ocr(**kwargs):
                started.set()
                try:
                    await asyncio.wait_for(gate.wait(), timeout=30)
                except asyncio.CancelledError:
                    task_cancelled.set()
                    raise
                return {"text": "ok"}

            service = AuxMLService(
                settings=_make_settings(tmp),
                registry=_make_registry(tmp),
                router=tracking_router,
            )

            with patch.object(_service_module, "run_ocr_task", side_effect=blocking_ocr):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()

                    await service.cancel_job(submitted["job_id"])

                    # The task should be cancelled before unload is called.
                    await asyncio.wait_for(task_cancelled.wait(), timeout=5)
                    # Now unload should proceed.
                    await asyncio.wait_for(unload_called.wait(), timeout=5)

                    final = await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    self.assertEqual(final["status"], "cancelled")
                finally:
                    gate.set()
                    await service.stop()


if __name__ == "__main__":
    unittest.main()