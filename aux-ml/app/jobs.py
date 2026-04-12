from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import uuid


TERMINAL_STATUSES = {"succeeded", "failed"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueFullError(RuntimeError):
    pass


class QueueManager:
    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._queue: deque[str] = deque()
        self._condition = asyncio.Condition()

    async def enqueue(self, job_id: str) -> int:
        async with self._condition:
            if len(self._queue) >= self._max_size:
                raise QueueFullError(f"Queue is full ({self._max_size})")
            self._queue.append(job_id)
            position = len(self._queue)
            self._condition.notify()
            return position

    async def dequeue(self) -> str:
        async with self._condition:
            while not self._queue:
                await self._condition.wait()
            return self._queue.popleft()

    async def peek(self) -> str | None:
        async with self._condition:
            if not self._queue:
                return None
            return self._queue[0]

    async def snapshot_ids(self) -> list[str]:
        async with self._condition:
            return list(self._queue)

    async def size(self) -> int:
        async with self._condition:
            return len(self._queue)


@dataclass
class JobRecord:
    id: str
    task: str
    model: str
    file_path: str
    prompt: str | None
    column_split: int
    column_split_pages: tuple[int, ...] | None
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: dict | None = None
    error: str | None = None

    def to_response(self) -> dict:
        return {
            "job_id": self.id,
            "task": self.task,
            "model": self.model,
            "file_path": self.file_path,
            "column_split": self.column_split,
            "column_split_pages": list(self.column_split_pages) if self.column_split_pages else None,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class JobStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[str, JobRecord] = {}
        self._completion_events: dict[str, asyncio.Event] = {}

    async def create(
        self,
        task: str,
        model: str,
        file_path: str,
        prompt: str | None,
        column_split: int,
        column_split_pages: tuple[int, ...] | None,
    ) -> JobRecord:
        job_id = str(uuid.uuid4())
        record = JobRecord(
            id=job_id,
            task=task,
            model=model,
            file_path=file_path,
            prompt=prompt,
            column_split=column_split,
            column_split_pages=column_split_pages,
            status="queued",
            created_at=_utc_now_iso(),
        )

        async with self._lock:
            self._jobs[job_id] = record
            self._completion_events[job_id] = asyncio.Event()

        return record

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def delete(self, job_id: str) -> None:
        async with self._lock:
            self._jobs.pop(job_id, None)
            self._completion_events.pop(job_id, None)

    async def mark_running(self, job_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "running"
            job.started_at = _utc_now_iso()

    async def mark_succeeded(self, job_id: str, result: dict) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "succeeded"
            job.result = result
            job.error = None
            job.finished_at = _utc_now_iso()
            event = self._completion_events.get(job_id)
            if event is not None:
                event.set()

    async def mark_failed(self, job_id: str, error: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "failed"
            job.error = error
            job.finished_at = _utc_now_iso()
            event = self._completion_events.get(job_id)
            if event is not None:
                event.set()

    async def wait_for_terminal(self, job_id: str, timeout_seconds: int) -> JobRecord | None:
        async with self._lock:
            event = self._completion_events.get(job_id)
        if event is None:
            return None

        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        return await self.get(job_id)
