"""Focused tests for the cancel endpoint HTTP code mapping.

These tests verify the exact HTTP status codes the cancel endpoint must
emit: queued 200 cancelled; running 202 cancelling; cancelling 202
idempotent; cancelled 200 idempotent; succeeded/failed 409; unknown 404.

They do not require fastapi to be installed: they drive the service-level
``cancel_job`` method and apply the same status-code mapping that
``main.cancel_job`` uses. The mapping logic is duplicated here from
``main.py`` so the test is self-contained and does not import fastapi.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "aux-ml"))
sys.modules.setdefault("pymupdf", types.ModuleType("pymupdf"))
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

from app.model_registry import ModelRegistry, ModelSpec
from app.service import AuxMLService
from app.settings import Settings


def _map_status_code(status: str) -> int:
    """Mirror of main.cancel_job status-code selection logic."""
    if status == "cancelled":
        return 200
    if status == "cancelling":
        return 202
    # succeeded / failed -> 409
    return 409


class FakeRouter:
    def __init__(self) -> None:
        self.loaded_models: list[str] = []
        self.unloaded_models: list[str] = []

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def list_models(self) -> list[dict]:
        return [{"id": "glm-ocr", "path": "/models/glm-ocr.gguf", "status": {"value": "loaded"}}]

    async def resolve_model_id(self, model_path: Path) -> str:
        return model_path.stem

    async def load_model(self, model_id: str) -> None:
        self.loaded_models.append(model_id)

    async def unload_model(self, model_id: str) -> None:
        self.unloaded_models.append(model_id)

    async def wait_for_status(self, model_id, expected, timeout_seconds, poll_interval_seconds) -> None:
        return None


def _make_settings(tmp: Path) -> Settings:
    return Settings(
        bind_host="0.0.0.0", port=8091, llama_base_url="http://127.0.0.1:8080",
        model_registry_path=tmp / "models.yaml", max_queue=10, job_timeout_seconds=30,
        poll_interval_seconds=0.05, allowed_input_dirs=(tmp,), enforce_memory_limit=False,
        memory_limit_mb=None, ocr_max_pages=50, transcribe_max_bytes=100 * 1024 * 1024,
        transcribe_max_duration_seconds=1800, transcribe_max_chunks=72,
        transcribe_chunk_seconds=30, transcribe_overlap_seconds=2,
        transcribe_ffmpeg_timeout_seconds=300,
    )


def _make_registry(tmp: Path) -> ModelRegistry:
    spec = ModelSpec("glm-ocr", "ocr", tmp / "glm-ocr.gguf", 1024, "Text Recognition:", 128,
                     mmproj_path=tmp / "mmproj-glm-ocr.gguf", optional=False)
    return ModelRegistry({"glm-ocr": spec})


def _patched_ocr(gate: asyncio.Event, started: asyncio.Event | None = None):
    async def fake(**kwargs):
        if started is not None:
            started.set()
        await asyncio.wait_for(gate.wait(), timeout=30)
        return {"text": "ok"}
    return fake


class CancelHttpCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_cancel_returns_200_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            service = AuxMLService(settings=_make_settings(tmp), registry=_make_registry(tmp), router=FakeRouter())
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch("app.service.run_ocr_task", side_effect=_patched_ocr(gate, started)):
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
                    result = await service.cancel_job(queued["job_id"])
                    self.assertEqual(_map_status_code(result["status"]), 200)
                    self.assertEqual(result["status"], "cancelled")
                finally:
                    gate.set()
                    await service.stop()

    async def test_running_cancel_returns_202_cancelling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            service = AuxMLService(settings=_make_settings(tmp), registry=_make_registry(tmp), router=FakeRouter())
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch("app.service.run_ocr_task", side_effect=_patched_ocr(gate, started)):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()
                    result = await service.cancel_job(submitted["job_id"])
                    self.assertEqual(_map_status_code(result["status"]), 202)
                    self.assertEqual(result["status"], "cancelling")
                finally:
                    gate.set()
                    await service.stop()

    async def test_cancelling_idempotent_returns_202(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            service = AuxMLService(settings=_make_settings(tmp), registry=_make_registry(tmp), router=FakeRouter())
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch("app.service.run_ocr_task", side_effect=_patched_ocr(gate, started)):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await started.wait()
                    # First cancel -> cancelling.
                    r1 = await service.cancel_job(submitted["job_id"])
                    self.assertEqual(r1["status"], "cancelling")
                    # Second cancel while still cancelling -> idempotent 202.
                    r2 = await service.cancel_job(submitted["job_id"])
                    self.assertEqual(_map_status_code(r2["status"]), 202)
                    self.assertEqual(r2["status"], "cancelling")
                finally:
                    gate.set()
                    await service.stop()

    async def test_cancelled_idempotent_returns_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            service = AuxMLService(settings=_make_settings(tmp), registry=_make_registry(tmp), router=FakeRouter())
            gate = asyncio.Event()
            started = asyncio.Event()
            with patch("app.service.run_ocr_task", side_effect=_patched_ocr(gate, started)):
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
                    await service.cancel_job(queued["job_id"])
                    # Now cancelled; second cancel -> idempotent 200.
                    r2 = await service.cancel_job(queued["job_id"])
                    self.assertEqual(_map_status_code(r2["status"]), 200)
                    self.assertEqual(r2["status"], "cancelled")
                finally:
                    gate.set()
                    await service.stop()

    async def test_succeeded_returns_409(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            service = AuxMLService(settings=_make_settings(tmp), registry=_make_registry(tmp), router=FakeRouter())
            gate = asyncio.Event()
            gate.set()
            with patch("app.service.run_ocr_task", side_effect=_patched_ocr(gate)):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    result = await service.cancel_job(submitted["job_id"])
                    self.assertEqual(_map_status_code(result["status"]), 409)
                    self.assertFalse(result["cancelled"])
                finally:
                    await service.stop()

    async def test_failed_returns_409(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            service = AuxMLService(settings=_make_settings(tmp), registry=_make_registry(tmp), router=FakeRouter())

            async def failing_ocr(**kwargs):
                raise RuntimeError("boom")

            with patch("app.service.run_ocr_task", side_effect=failing_ocr):
                await service.start()
                try:
                    submitted = await service.submit_job(
                        task="ocr", model="glm-ocr", file_path="/shared/doc.pdf",
                        prompt=None, column_split=1, column_split_pages=None,
                    )
                    completed = await service.wait_for_job(submitted["job_id"], timeout_seconds=5)
                    self.assertEqual(completed["status"], "failed")
                    result = await service.cancel_job(submitted["job_id"])
                    self.assertEqual(_map_status_code(result["status"]), 409)
                    self.assertFalse(result["cancelled"])
                finally:
                    await service.stop()

    async def test_unknown_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            service = AuxMLService(settings=_make_settings(tmp), registry=_make_registry(tmp), router=FakeRouter())
            with patch("app.service.run_ocr_task", side_effect=_patched_ocr(asyncio.Event())):
                await service.start()
                try:
                    with self.assertRaises(KeyError):
                        await service.cancel_job("does-not-exist")
                finally:
                    await service.stop()


if __name__ == "__main__":
    unittest.main()