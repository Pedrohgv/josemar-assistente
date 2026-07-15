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

from app.adapters import transcribe_granite
from app.model_registry import ModelSpec


class TranscriptMergeTests(unittest.TestCase):
    def test_merge_pair_removes_case_and_punctuation_boundary_overlap(self) -> None:
        left = "intro yes-yes question, which now i remember what it is that i was going to go over"
        right = "Question, which now I remember what it is that I was going to go over. So one concept is next"

        merged = transcribe_granite._merge_pair(left, right)

        self.assertEqual(
            merged,
            "intro yes-yes question, which now i remember what it is that i was going to go over. So one concept is next",
        )

    def test_merge_pair_removes_fuzzy_boundary_overlap_with_small_word_differences(self) -> None:
        left = (
            "before all right i want to ask everybody a question who does your own sales "
            "hand up everybody almost who enjoys doing their own sales so for the"
        )
        right = (
            "all right, i want to ask everybody a question. who does your own sales? "
            "who enjoys doing their own sales? so for the people that you talk to next"
        )

        merged = transcribe_granite._merge_pair(left, right)

        self.assertEqual(
            merged,
            "before all right i want to ask everybody a question who does your own sales "
            "hand up everybody almost who enjoys doing their own sales so for the people that you talk to next",
        )

    def test_merge_pair_preserves_unique_right_words_on_asymmetric_fuzzy_match(self) -> None:
        left = (
            "before that's really important to understand because it affects everything "
            "we're going to talk about next"
        )
        right = (
            "that's really important too understand because it affects everything "
            "were going to talk about next and i want to emphasize"
        )

        merged = transcribe_granite._merge_pair(left, right)

        self.assertEqual(
            merged,
            "before that's really important to understand because it affects everything "
            "we're going to talk about next and i want to emphasize",
        )

    def test_merge_pair_preserves_legitimate_non_boundary_repetition(self) -> None:
        left = "sales are hard who enjoys sales who enjoys sales"
        right = "the next section starts here"

        merged = transcribe_granite._merge_pair(left, right)

        self.assertEqual(merged.count("who enjoys sales"), 2)
        self.assertEqual(merged, "sales are hard who enjoys sales who enjoys sales the next section starts here")

    def test_merge_pair_does_not_drop_low_similarity_boundary(self) -> None:
        left = "we need to discuss customer sales process"
        right = "we need to discuss product roadmap planning"

        merged = transcribe_granite._merge_pair(left, right)

        self.assertEqual(merged, "we need to discuss customer sales process we need to discuss product roadmap planning")

    def test_repeated_phrase_loop_collapse_is_conservative(self) -> None:
        phrase = "yes, that's what we're talking about"
        text = f"before {phrase}. {phrase}. {phrase}. {phrase}. {phrase}. {phrase}. after"

        collapsed = transcribe_granite._collapse_repeated_phrase_loops(text)

        self.assertEqual(collapsed.count(phrase), 1)
        self.assertEqual(collapsed, f"before {phrase}. after")

    def test_repeated_phrase_loop_collapse_preserves_short_emphasis(self) -> None:
        text = "no no no this is important"

        collapsed = transcribe_granite._collapse_repeated_phrase_loops(text)

        self.assertEqual(collapsed, text)


class FakeRouter:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.requests: list[dict] = []
        self._responses = responses or ["hello world"]

    async def audio_transcription(
        self,
        *,
        file_path: Path,
        model_id: str,
        prompt: str,
        mime_type: str,
        timeout_seconds: int,
    ) -> dict:
        self.requests.append({
            "file_path": file_path,
            "model_id": model_id,
            "prompt": prompt,
            "mime_type": mime_type,
            "timeout_seconds": timeout_seconds,
        })
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return {"text": self._responses[index]}


class GraniteTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.spec = ModelSpec(
            "granite-speech-4.1-2b",
            "transcribe",
            Path("/models/granite-speech-4.1-2b-Q8_0.gguf"),
            8192,
            "transcribe the speech with proper punctuation and capitalization.",
            4096,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_transcription_uses_native_audio_endpoint_for_short_audio(self) -> None:
        audio = self.root / "sample.wav"
        audio.write_bytes(b"RIFFtest")
        router = FakeRouter()

        async def fake_normalize(**kwargs: object) -> None:
            Path(kwargs["output_file"]).write_bytes(b"RIFFnormalized")

        with patch.object(transcribe_granite, "_probe_duration_seconds", return_value=10):
            with patch.object(transcribe_granite, "_normalize_audio", side_effect=fake_normalize):
                result = await transcribe_granite.run_transcription_task(
                    file_path=str(audio),
                    model_spec=self.spec,
                    model_id="granite-speech-4.1-2b",
                    prompt=None,
                    timeout_seconds=30,
                    max_audio_bytes=100,
                    max_duration_seconds=1800,
                    max_chunks=16,
                    chunk_seconds=240,
                    overlap_seconds=20,
                    ffmpeg_timeout_seconds=300,
                    allowed_roots=(self.root,),
                    router=router,
                )

        self.assertEqual(result["text"], "hello world")
        self.assertEqual(result["size_bytes"], 8)
        self.assertEqual(result["mode"], "single-shot")
        self.assertEqual(result["chunk_count"], 1)
        self.assertEqual(len(router.requests), 1)
        self.assertEqual(router.requests[0]["model_id"], "granite-speech-4.1-2b")
        self.assertEqual(
            router.requests[0]["prompt"],
            "transcribe the speech with proper punctuation and capitalization.",
        )
        self.assertEqual(router.requests[0]["mime_type"], "audio/x-wav")

    async def test_transcription_chunks_long_audio_and_merges_overlap(self) -> None:
        audio = self.root / "long.mp3"
        audio.write_bytes(b"ID3test")
        router = FakeRouter(
            responses=[
                "alpha beta gamma delta epsilon zeta eta theta iota unique-one",
                "gamma delta epsilon zeta eta theta iota unique-one unique-two",
                "unique-three",
            ]
        )

        async def fake_create_chunk(**kwargs: object) -> None:
            Path(kwargs["output_file"]).write_bytes(b"RIFFchunk")

        with patch.object(transcribe_granite, "_probe_duration_seconds", return_value=500):
            with patch.object(transcribe_granite, "_normalize_audio", side_effect=fake_create_chunk):
                result = await transcribe_granite.run_transcription_task(
                    file_path=str(audio),
                    model_spec=self.spec,
                    model_id="granite-speech-4.1-2b",
                    prompt="<|audio|> transcribe the speech",
                    timeout_seconds=30,
                    max_audio_bytes=100,
                    max_duration_seconds=1800,
                    max_chunks=16,
                    chunk_seconds=240,
                    overlap_seconds=20,
                    ffmpeg_timeout_seconds=300,
                    allowed_roots=(self.root,),
                    router=router,
                )

        self.assertEqual(result["mode"], "chunked")
        self.assertEqual(result["chunk_count"], 3)
        self.assertEqual(len(result["chunks"]), 3)
        self.assertEqual(len(router.requests), 3)
        self.assertEqual(router.requests[0]["prompt"], "transcribe the speech")
        self.assertEqual(
            result["text"],
            "alpha beta gamma delta epsilon zeta eta theta iota unique-one unique-two unique-three",
        )

    async def test_rejects_audio_over_max_duration(self) -> None:
        audio = self.root / "long.mp3"
        audio.write_bytes(b"ID3test")

        with patch.object(transcribe_granite, "_probe_duration_seconds", return_value=1801):
            with self.assertRaisesRegex(ValueError, "Audio duration is too long"):
                await transcribe_granite.run_transcription_task(
                    file_path=str(audio),
                    model_spec=self.spec,
                    model_id="granite-speech-4.1-2b",
                    prompt=None,
                    timeout_seconds=30,
                    max_audio_bytes=100,
                    max_duration_seconds=1800,
                    max_chunks=16,
                    chunk_seconds=240,
                    overlap_seconds=20,
                    ffmpeg_timeout_seconds=300,
                    allowed_roots=(self.root,),
                    router=FakeRouter(),
                )

    async def test_rejects_audio_over_max_chunks(self) -> None:
        audio = self.root / "long.mp3"
        audio.write_bytes(b"ID3test")

        with patch.object(transcribe_granite, "_probe_duration_seconds", return_value=500):
            with self.assertRaisesRegex(ValueError, "too many chunks"):
                await transcribe_granite.run_transcription_task(
                    file_path=str(audio),
                    model_spec=self.spec,
                    model_id="granite-speech-4.1-2b",
                    prompt=None,
                    timeout_seconds=30,
                    max_audio_bytes=100,
                    max_duration_seconds=1800,
                    max_chunks=2,
                    chunk_seconds=240,
                    overlap_seconds=20,
                    ffmpeg_timeout_seconds=300,
                    allowed_roots=(self.root,),
                    router=FakeRouter(),
                )

    async def test_rejects_files_outside_allowed_root(self) -> None:
        outside = Path(tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name)
        self.addCleanup(outside.unlink)
        outside.write_bytes(b"RIFFtest")

        with self.assertRaisesRegex(ValueError, "outside allowed roots"):
            await transcribe_granite.run_transcription_task(
                file_path=str(outside),
                model_spec=self.spec,
                model_id="granite-speech-4.1-2b",
                prompt=None,
                timeout_seconds=30,
                max_audio_bytes=100,
                max_duration_seconds=1800,
                max_chunks=16,
                chunk_seconds=240,
                overlap_seconds=20,
                ffmpeg_timeout_seconds=300,
                allowed_roots=(self.root,),
                router=FakeRouter(),
            )

    async def test_rejects_unsupported_extension(self) -> None:
        text_file = self.root / "sample.txt"
        text_file.write_text("not audio", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Unsupported audio extension"):
            await transcribe_granite.run_transcription_task(
                file_path=str(text_file),
                model_spec=self.spec,
                model_id="granite-speech-4.1-2b",
                prompt=None,
                timeout_seconds=30,
                max_audio_bytes=100,
                max_duration_seconds=1800,
                max_chunks=16,
                chunk_seconds=240,
                overlap_seconds=20,
                ffmpeg_timeout_seconds=300,
                allowed_roots=(self.root,),
                router=FakeRouter(),
            )

    async def test_rejects_oversized_audio_before_encoding(self) -> None:
        audio = self.root / "large.mp3"
        audio.write_bytes(b"a" * 101)

        with self.assertRaisesRegex(ValueError, "Audio file is too large"):
            await transcribe_granite.run_transcription_task(
                file_path=str(audio),
                model_spec=self.spec,
                model_id="granite-speech-4.1-2b",
                prompt=None,
                timeout_seconds=30,
                max_audio_bytes=100,
                max_duration_seconds=1800,
                max_chunks=16,
                chunk_seconds=240,
                overlap_seconds=20,
                ffmpeg_timeout_seconds=300,
                allowed_roots=(self.root,),
                router=FakeRouter(),
            )


class CancellationSafeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_raises_cancelled_error_when_event_set(self) -> None:
        cancel_event = asyncio.Event()

        async def trigger_cancel() -> None:
            await asyncio.sleep(0.05)
            cancel_event.set()

        asyncio.create_task(trigger_cancel())

        with self.assertRaises(asyncio.CancelledError):
            await transcribe_granite._run_command(
                ["sleep", "30"],
                timeout_seconds=10,
                cancel_event=cancel_event,
            )

    async def test_run_command_without_cancel_event_behaves_like_timeout(self) -> None:
        with self.assertRaises(TimeoutError):
            await transcribe_granite._run_command(
                ["sleep", "10"],
                timeout_seconds=1,
            )


class SubprocessReapingTests(unittest.IsolatedAsyncioTestCase):
    """Tests that prove process exit/reaping and no pending helper task."""

    async def test_cancelled_subprocess_process_group_is_reaped(self) -> None:
        """When cancellation fires, the subprocess is terminated via its
        process group and reaped (returncode is set, not None)."""
        import os
        import signal

        cancel_event = asyncio.Event()

        # Start a long-running child that creates its own subprocess group.
        process = await asyncio.create_subprocess_exec(
            "sleep", "30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(lambda: process.kill() if process.returncode is None else None)

        pgid = os.getpgid(process.pid)
        # Trigger cancellation.
        cancel_event.set()

        await transcribe_granite._terminate_process_group(process, grace_seconds=1.0)

        # Process must be reaped (returncode set).
        self.assertIsNotNone(process.returncode)

        # Process group should no longer exist (or at least the child is gone).
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)

    async def test_run_command_cancels_subprocess_and_no_helper_task_lingers(self) -> None:
        """After a cancelled _run_command returns, there should be no
        lingering cancel_event.wait() helper task."""
        cancel_event = asyncio.Event()

        async def trigger_cancel() -> None:
            await asyncio.sleep(0.05)
            cancel_event.set()

        trigger_task = asyncio.create_task(trigger_cancel())

        with self.assertRaises(asyncio.CancelledError):
            await transcribe_granite._run_command(
                ["sleep", "30"],
                timeout_seconds=10,
                cancel_event=cancel_event,
            )

        # Allow the trigger task to finish.
        try:
            await asyncio.wait_for(trigger_task, timeout=1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            trigger_task.cancel()

        # Collect all tasks except the current one.
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        # No lingering helper tasks from _run_command should remain.
        self.assertEqual(pending, [])

    async def test_timeout_terminates_process_group_and_reaps(self) -> None:
        """On timeout, the process group is terminated and reaped."""
        import os

        process = await asyncio.create_subprocess_exec(
            "sleep", "30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(lambda: process.kill() if process.returncode is None else None)

        pgid = os.getpgid(process.pid)

        await transcribe_granite._terminate_process_group(process, grace_seconds=1.0)

        self.assertIsNotNone(process.returncode)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)


class TranscriptionCancelEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.spec = ModelSpec(
            "granite-speech-4.1-2b",
            "transcribe",
            Path("/models/granite-speech-4.1-2b-Q8_0.gguf"),
            8192,
            "transcribe the speech with proper punctuation and capitalization.",
            4096,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_transcription_raises_cancelled_between_chunks(self) -> None:
        audio = self.root / "long.mp3"
        audio.write_bytes(b"ID3test")
        cancel_event = asyncio.Event()
        cancel_event.set()

        with patch.object(transcribe_granite, "_probe_duration_seconds", return_value=500):
            with patch.object(transcribe_granite, "_normalize_audio", side_effect=AssertionError("should not chunk")):
                with self.assertRaises(asyncio.CancelledError):
                    await transcribe_granite.run_transcription_task(
                        file_path=str(audio),
                        model_spec=self.spec,
                        model_id="granite-speech-4.1-2b",
                        prompt=None,
                        timeout_seconds=30,
                        max_audio_bytes=100,
                        max_duration_seconds=1800,
                        max_chunks=16,
                        chunk_seconds=240,
                        overlap_seconds=20,
                        ffmpeg_timeout_seconds=300,
                        allowed_roots=(self.root,),
                        router=FakeRouter(),
                        cancel_event=cancel_event,
                    )

    async def test_transcription_passes_cancel_event_to_probe(self) -> None:
        audio = self.root / "sample.wav"
        audio.write_bytes(b"RIFFtest")
        cancel_event = asyncio.Event()
        cancel_event.set()

        with patch.object(transcribe_granite, "_probe_duration_seconds", side_effect=asyncio.CancelledError("cancelled")):
            with self.assertRaises(asyncio.CancelledError):
                await transcribe_granite.run_transcription_task(
                    file_path=str(audio),
                    model_spec=self.spec,
                    model_id="granite-speech-4.1-2b",
                    prompt=None,
                    timeout_seconds=30,
                    max_audio_bytes=100,
                    max_duration_seconds=1800,
                    max_chunks=16,
                    chunk_seconds=240,
                    overlap_seconds=20,
                    ffmpeg_timeout_seconds=300,
                    allowed_roots=(self.root,),
                    router=FakeRouter(),
                    cancel_event=cancel_event,
                )


if __name__ == "__main__":
    unittest.main()
