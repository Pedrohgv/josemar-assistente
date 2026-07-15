from __future__ import annotations

from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "aux-ml"))

from app.jobs import (
    CANCELLABLE_STATUSES,
    JobScheduler,
    TERMINAL_STATUSES,
)


class JobStatusConstantsTests(unittest.TestCase):
    def test_cancelled_is_terminal(self) -> None:
        self.assertIn("cancelled", TERMINAL_STATUSES)
        self.assertIn("succeeded", TERMINAL_STATUSES)
        self.assertIn("failed", TERMINAL_STATUSES)

    def test_queued_and_running_are_cancellable(self) -> None:
        self.assertIn("queued", CANCELLABLE_STATUSES)
        self.assertIn("running", CANCELLABLE_STATUSES)
        self.assertNotIn("succeeded", CANCELLABLE_STATUSES)
        self.assertNotIn("failed", CANCELLABLE_STATUSES)
        self.assertNotIn("cancelled", CANCELLABLE_STATUSES)


class SchedulerCancelQueuedTests(unittest.IsolatedAsyncioTestCase):
    async def _create(self, scheduler: JobScheduler):
        return await scheduler.create(
            task="ocr",
            model="glm-ocr",
            file_path="/shared/x.pdf",
            prompt=None,
            column_split=1,
            column_split_pages=None,
        )

    async def test_cancel_queued_removes_from_fifo_and_marks_cancelled(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.enqueue(job.id)

        ok = await scheduler.cancel_queued(job.id)

        self.assertTrue(ok)
        record = await scheduler.get(job.id)
        self.assertEqual(record.status, "cancelled")
        self.assertEqual(await scheduler.snapshot_ids(), [])

    async def test_cancel_queued_returns_false_for_unknown(self) -> None:
        scheduler = JobScheduler(max_size=10)
        self.assertFalse(await scheduler.cancel_queued("missing"))

    async def test_cancel_queued_returns_false_for_running(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.enqueue(job.id)
        claimed = await scheduler.claim_next()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, "running")

        ok = await scheduler.cancel_queued(job.id)
        self.assertFalse(ok)

    async def test_cancel_queued_returns_false_for_terminal(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.enqueue(job.id)
        await scheduler.claim_next()
        await scheduler.mark_succeeded(job.id, result={"text": "ok"})

        ok = await scheduler.cancel_queued(job.id)
        self.assertFalse(ok)


class SchedulerCASTests(unittest.IsolatedAsyncioTestCase):
    async def _create(self, scheduler: JobScheduler):
        return await scheduler.create(
            task="ocr",
            model="glm-ocr",
            file_path="/shared/x.pdf",
            prompt=None,
            column_split=1,
            column_split_pages=None,
        )

    async def test_mark_running_transitions_queued_to_running(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        self.assertTrue(await scheduler.mark_running(job.id))
        record = await scheduler.get(job.id)
        self.assertEqual(record.status, "running")
        self.assertIsNotNone(record.started_at)

    async def test_mark_succeeded_rejected_when_cancelling(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.mark_running(job.id)
        await scheduler.mark_cancelling(job.id)

        ok = await scheduler.mark_succeeded(job.id, result={"text": "late"})
        self.assertFalse(ok)
        record = await scheduler.get(job.id)
        self.assertEqual(record.status, "cancelling")

    async def test_mark_failed_rejected_when_cancelling(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.mark_running(job.id)
        await scheduler.mark_cancelling(job.id)

        ok = await scheduler.mark_failed(job.id, "late failure")
        self.assertFalse(ok)
        record = await scheduler.get(job.id)
        self.assertEqual(record.status, "cancelling")

    async def test_mark_cancelled_transitions_cancelling_to_cancelled(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.mark_running(job.id)
        await scheduler.mark_cancelling(job.id)

        ok = await scheduler.mark_cancelled(job.id)
        self.assertTrue(ok)
        record = await scheduler.get(job.id)
        self.assertEqual(record.status, "cancelled")
        self.assertIsNotNone(record.finished_at)

    async def test_mark_failed_cleanup_transitions_cancelling_to_failed(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.mark_running(job.id)
        await scheduler.mark_cancelling(job.id)

        ok = await scheduler.mark_failed_cleanup(job.id, "unload failed")
        self.assertTrue(ok)
        record = await scheduler.get(job.id)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error, "unload failed")

    async def test_mark_cancelled_rejected_when_running(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.mark_running(job.id)

        # mark_cancelled expects cancelling, not running.
        ok = await scheduler.mark_cancelled(job.id)
        self.assertFalse(ok)

    async def test_is_cancellable_reflects_current_status(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)

        self.assertTrue(await scheduler.is_cancellable(job.id))
        await scheduler.mark_running(job.id)
        self.assertTrue(await scheduler.is_cancellable(job.id))
        await scheduler.mark_succeeded(job.id, result={"text": "ok"})
        self.assertFalse(await scheduler.is_cancellable(job.id))

    async def test_is_cancellable_returns_false_for_unknown_job(self) -> None:
        scheduler = JobScheduler(max_size=10)
        self.assertFalse(await scheduler.is_cancellable("missing"))

    async def test_wait_for_terminal_wakes_on_cancelled(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.mark_running(job.id)
        await scheduler.mark_cancelling(job.id)
        await scheduler.mark_cancelled(job.id)

        record = await scheduler.wait_for_terminal(job.id, timeout_seconds=1)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "cancelled")


class SchedulerClaimNextTests(unittest.IsolatedAsyncioTestCase):
    async def _create(self, scheduler: JobScheduler, **overrides):
        defaults = dict(
            task="ocr",
            model="glm-ocr",
            file_path="/shared/x.pdf",
            prompt=None,
            column_split=1,
            column_split_pages=None,
        )
        defaults.update(overrides)
        return await scheduler.create(**defaults)

    async def test_claim_next_transitions_queued_to_running(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job = await self._create(scheduler)
        await scheduler.enqueue(job.id)

        claimed = await scheduler.claim_next()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.status, "running")

    async def test_claim_next_skips_cancelled_queued_job(self) -> None:
        scheduler = JobScheduler(max_size=10)
        job1 = await self._create(scheduler)
        job2 = await self._create(scheduler)
        await scheduler.enqueue(job1.id)
        await scheduler.enqueue(job2.id)

        # Cancel job1 while queued.
        await scheduler.cancel_queued(job1.id)

        # claim_next should skip job1 and claim job2.
        claimed = await scheduler.claim_next()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job2.id)
        self.assertEqual(claimed.status, "running")

        record1 = await scheduler.get(job1.id)
        self.assertEqual(record1.status, "cancelled")

    async def test_claim_next_returns_none_when_empty_then_waits(self) -> None:
        scheduler = JobScheduler(max_size=10)
        # Empty queue: claim_next waits. Use a short timeout via cancel.
        import asyncio

        async def enqueue_soon():
            await asyncio.sleep(0.05)
            job = await self._create(scheduler)
            await scheduler.enqueue(job.id)

        asyncio.create_task(enqueue_soon())
        claimed = await scheduler.claim_next()
        self.assertIsNotNone(claimed)


class SchedulerQueueCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_raises_when_full(self) -> None:
        scheduler = JobScheduler(max_size=1)
        job1 = await scheduler.create(
            task="ocr", model="m", file_path="/x", prompt=None,
            column_split=1, column_split_pages=None,
        )
        job2 = await scheduler.create(
            task="ocr", model="m", file_path="/x", prompt=None,
            column_split=1, column_split_pages=None,
        )
        await scheduler.enqueue(job1.id)
        with self.assertRaises(Exception):
            await scheduler.enqueue(job2.id)

    async def test_fifo_order_preserved(self) -> None:
        scheduler = JobScheduler(max_size=10)
        ids = []
        for i in range(3):
            job = await scheduler.create(
                task="ocr", model="m", file_path=f"/{i}", prompt=None,
                column_split=1, column_split_pages=None,
            )
            await scheduler.enqueue(job.id)
            ids.append(job.id)

        claimed_order = []
        for _ in range(3):
            claimed = await scheduler.claim_next()
            if claimed is not None:
                claimed_order.append(claimed.id)

        self.assertEqual(claimed_order, ids)


if __name__ == "__main__":
    unittest.main()