from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import uuid


# Job lifecycle states:
#
#   queued -----> running -----> succeeded
#     |             |  |
#     |             |  +------> failed
#     |             v
#     +--------> cancelling -----> cancelled
#                    |          |
#                    |          +-> failed (cleanup error)
#                    v
#                  cancelled
#
# `cancelling` is an observable intermediate state used while the worker
# unloads the model and finalizes a running-job cancellation. A late
# `succeeded`/`failed` result must NOT overwrite `cancelling`/`cancelled`.
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
CANCELLABLE_STATUSES = {"queued", "running"}

# Allowed state transitions for compare-and-set operations. Anything not
# listed here is rejected by the CAS helpers.
_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelling"},
    "cancelling": {"cancelled", "failed"},
    # Terminal states have no outgoing transitions.
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueFullError(RuntimeError):
    pass


class JobStateError(RuntimeError):
    """Raised when a CAS transition is rejected because the current state
    does not match the expected one."""


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


class JobScheduler:
    """Unified FIFO queue + job state store with atomic CAS transitions.

    A single ``asyncio.Condition`` guards both the deque and the job map so
    that claim (``queued -> running``) and queued cancellation
    (``queued -> cancelled`` + FIFO removal) cannot race each other. All
    state transitions are compare-and-set: a transition is only applied when
    the current status matches the expected one, which prevents a late
    success/failure from overwriting a ``cancelling``/``cancelled`` job.
    """

    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._queue: deque[str] = deque()
        self._jobs: dict[str, JobRecord] = {}
        self._completion_events: dict[str, asyncio.Event] = {}
        self._condition = asyncio.Condition()

    # ------------------------------------------------------------------ #
    # Internal CAS helper. Must be called while holding the condition.
    # ------------------------------------------------------------------ #
    def _cas_locked(
        self,
        job_id: str,
        expected: str,
        new_status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status != expected:
            return False
        if new_status not in _TRANSITIONS.get(expected, set()):
            raise JobStateError(
                f"Invalid transition {expected!r} -> {new_status!r} for job {job_id}"
            )
        job.status = new_status
        if new_status in TERMINAL_STATUSES:
            job.finished_at = _utc_now_iso()
        if new_status == "running":
            job.started_at = _utc_now_iso()
        if result is not None:
            job.result = result
            job.error = None
        if error is not None:
            job.error = error
        if new_status in TERMINAL_STATUSES:
            event = self._completion_events.get(job_id)
            if event is not None:
                event.set()
        return True

    # ------------------------------------------------------------------ #
    # Job creation / lookup
    # ------------------------------------------------------------------ #
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
        async with self._condition:
            self._jobs[job_id] = record
            self._completion_events[job_id] = asyncio.Event()
        return record

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._condition:
            return self._jobs.get(job_id)

    async def delete(self, job_id: str) -> None:
        async with self._condition:
            self._jobs.pop(job_id, None)
            self._completion_events.pop(job_id, None)
            try:
                self._queue.remove(job_id)
            except ValueError:
                pass

    async def wait_for_terminal(self, job_id: str, timeout_seconds: int) -> JobRecord | None:
        async with self._condition:
            event = self._completion_events.get(job_id)
        if event is None:
            return None
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        return await self.get(job_id)

    # ------------------------------------------------------------------ #
    # Queue operations (atomic with state)
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # Atomic claim / cancel-queued.
    #
    # ``claim_next`` atomically dequeues the next job AND transitions it to
    # ``running``. If the job was cancelled while queued (status no longer
    # ``queued``), it is skipped and the next one is tried. This closes the
    # race where a separate dequeue + mark_running could execute a job that
    # was cancelled between the two operations.
    # ------------------------------------------------------------------ #
    async def claim_next(self) -> JobRecord | None:
        async with self._condition:
            while True:
                while self._queue:
                    job_id = self._queue.popleft()
                    job = self._jobs.get(job_id)
                    if job is None:
                        continue
                    if job.status != "queued":
                        # Already cancelled (or otherwise transitioned) while
                        # queued: skip it.
                        continue
                    self._cas_locked(job_id, expected="queued", new_status="running")
                    return job
                # Queue empty: wait for a new enqueue, then retry.
                await self._condition.wait()

    async def cancel_queued(self, job_id: str) -> bool:
        """Atomically remove a job from the FIFO and transition it to
        ``cancelled``. Returns True when the job was queued and is now
        cancelled, False otherwise (unknown, already running, or terminal)."""
        async with self._condition:
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued":
                return False
            try:
                self._queue.remove(job_id)
            except ValueError:
                # Not in the deque but still queued: mark cancelled anyway.
                pass
            return self._cas_locked(job_id, expected="queued", new_status="cancelled")

    # ------------------------------------------------------------------ #
    # CAS transitions for running jobs.
    # ------------------------------------------------------------------ #
    async def transition(
        self,
        job_id: str,
        expected: str,
        new_status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> bool:
        async with self._condition:
            return self._cas_locked(
                job_id,
                expected=expected,
                new_status=new_status,
                result=result,
                error=error,
            )

    async def mark_running(self, job_id: str) -> bool:
        return await self.transition(job_id, expected="queued", new_status="running")

    async def mark_succeeded(self, job_id: str, result: dict) -> bool:
        # Late success must not overwrite cancelling/cancelled.
        return await self.transition(job_id, expected="running", new_status="succeeded", result=result)

    async def mark_failed(self, job_id: str, error: str, *, expected: str = "running") -> bool:
        # Late failure must not overwrite cancelling/cancelled unless the
        # caller explicitly expects ``cancelling`` (cleanup failure path).
        return await self.transition(job_id, expected=expected, new_status="failed", error=error)

    async def mark_cancelling(self, job_id: str) -> bool:
        return await self.transition(job_id, expected="running", new_status="cancelling")

    async def mark_cancelled(self, job_id: str) -> bool:
        return await self.transition(job_id, expected="cancelling", new_status="cancelled")

    async def mark_failed_cleanup(self, job_id: str, error: str) -> bool:
        """Mark a job failed while it was in ``cancelling`` (model unload
        failed). This is the only allowed ``cancelling -> failed`` path."""
        return await self.transition(job_id, expected="cancelling", new_status="failed", error=error)

    async def status(self, job_id: str) -> str | None:
        async with self._condition:
            job = self._jobs.get(job_id)
            return job.status if job is not None else None

    async def is_cancellable(self, job_id: str) -> bool:
        async with self._condition:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            return job.status in CANCELLABLE_STATUSES


# Backwards-compatible aliases kept so external callers and tests that
# import the old names keep working during the transition.
JobStore = JobScheduler


__all__ = [
    "CANCELLABLE_STATUSES",
    "JobRecord",
    "JobScheduler",
    "JobStateError",
    "JobStore",
    "QueueFullError",
    "TERMINAL_STATUSES",
]
