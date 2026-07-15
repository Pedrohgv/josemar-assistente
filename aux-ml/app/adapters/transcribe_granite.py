from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import mimetypes
from pathlib import Path
import re
import tempfile
import unicodedata

from ..llama_router import LlamaRouterClient
from ..model_registry import ModelSpec


SUPPORTED_AUDIO_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
}

# One-pass ffmpeg loudnorm filter applied to every transcription request
# (short/single-shot and each long-audio chunk) before llama.cpp. Produces
# 16 kHz mono PCM WAV, which is what Granite Speech expects.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"
NORMALIZED_SAMPLE_RATE = 16000

BOUNDARY_SCAN_WORDS = 120
MAX_OVERLAP_WORDS = 100
MIN_OVERLAP_WORDS = 8
MAX_OVERLAP_SIZE_DELTA = 12
MIN_OVERLAP_SIMILARITY = 0.74
MIN_LOOP_PHRASE_WORDS = 4
MAX_LOOP_PHRASE_WORDS = 12
MIN_LOOP_REPETITIONS = 5


def _normalize_word(word: str) -> str:
    normalized = word.lower().replace("’", "'").replace("`", "'")
    normalized = unicodedata.normalize("NFKD", normalized)
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _normalized_words(words: list[str]) -> list[str]:
    return [_normalize_word(word) for word in words]

def _resolve_safe_input_path(file_path: str, allowed_roots: tuple[Path, ...]) -> Path:
    candidate = Path(file_path).expanduser().resolve()
    if not candidate.exists():
        raise ValueError(f"Input file does not exist: {candidate}")
    if not candidate.is_file():
        raise ValueError(f"Input path is not a file: {candidate}")

    for root in allowed_roots:
        resolved_root = root.expanduser().resolve()
        if candidate == resolved_root or resolved_root in candidate.parents:
            return candidate

    joined_roots = ", ".join(str(root) for root in allowed_roots)
    raise ValueError(
        f"Input file '{candidate}' is outside allowed roots: {joined_roots}"
    )


def _guess_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type:
        return mime_type
    return "application/octet-stream"


def _extract_text_from_completion(response: dict) -> str:
    content = response.get("text")
    if isinstance(content, str):
        return content.strip()

    content = response.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


async def _run_command(
    args: list[str],
    timeout_seconds: int,
    *,
    cancel_event: asyncio.Event | None = None,
) -> str:
    """Run a subprocess with rigorous cancellation.

    The child is started in its own session (``start_new_session=True``) so
    we can terminate the whole process group. On cancellation or timeout we:
      1. Terminate the process group (SIGTERM) with a bounded grace period.
      2. If still alive, SIGKILL the group.
      3. Reap the process (await communicate).
      4. Always cancel and await the ``cancel_event.wait()`` helper task so
         no pending helper task lingers.
    """
    import os
    import signal

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    communicate_task = asyncio.ensure_future(process.communicate())
    cancel_task: asyncio.Task | None = None
    if cancel_event is not None:
        cancel_task = asyncio.ensure_future(cancel_event.wait())

    try:
        wait_set = {communicate_task}
        if cancel_task is not None:
            wait_set.add(cancel_task)

        done, pending = await asyncio.wait(
            wait_set,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        cancelled = cancel_event is not None and cancel_event.is_set()
        timed_out = not done

        if cancelled or timed_out:
            reason = "Command cancelled" if cancelled else (
                f"Command timed out ({args[0]}) after {timeout_seconds}s"
            )
            await _terminate_process_group(process)
            communicate_task.cancel()
            try:
                await communicate_task
            except (asyncio.CancelledError, Exception):
                pass
            if cancelled:
                raise asyncio.CancelledError(reason)
            raise TimeoutError(reason)

        # Normal completion.
        stdout, stderr = communicate_task.result()
    except (asyncio.CancelledError, TimeoutError):
        # Outer cancellation (e.g. job task cancelled) or timeout already
        # handled above. Ensure the process group is reaped.
        await _terminate_process_group(process)
        communicate_task.cancel()
        try:
            await communicate_task
        except (asyncio.CancelledError, Exception):
            pass
        raise
    finally:
        # Always clean up the helper task so nothing lingers.
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            try:
                await cancel_task
            except (asyncio.CancelledError, Exception):
                pass

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        if len(message) > 500:
            message = f"{message[:500]}..."
        raise RuntimeError(f"Command failed ({args[0]}): {message}")
    return stdout.decode("utf-8", errors="replace").strip()


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 2.0,
) -> None:
    """Terminate the child's process group with bounded grace and SIGKILL
    fallback, then reap the process. Avoids double-kill and masks no
    ``ProcessLookupError``."""
    import os
    import signal

    if process.returncode is not None:
        return

    pgid: int | None = None
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        # Already reaped.
        return
    except OSError:
        pgid = None

    # SIGTERM the whole group.
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            # Fallback: kill just the process.
            try:
                process.terminate()
            except ProcessLookupError:
                return
            except OSError:
                pass
    else:
        try:
            process.terminate()
        except ProcessLookupError:
            return
        except OSError:
            pass

    # Bounded grace: wait for the process to exit.
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    except ProcessLookupError:
        return

    # SIGKILL the group.
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            except OSError:
                pass
    else:
        try:
            process.kill()
        except ProcessLookupError:
            return
        except OSError:
            pass

    # Reap.
    try:
        await process.wait()
    except (ProcessLookupError, asyncio.CancelledError, Exception):
        pass


async def _probe_duration_seconds(
    file_path: Path,
    timeout_seconds: int,
    *,
    cancel_event: asyncio.Event | None = None,
) -> float:
    output = await _run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ],
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )
    try:
        duration = float(output)
    except ValueError as exc:
        raise ValueError(f"Could not determine audio duration for '{file_path}'") from exc
    if duration <= 0:
        raise ValueError(f"Audio duration must be positive for '{file_path}'")
    return duration


def _chunk_ranges(
    *,
    duration_seconds: float,
    chunk_seconds: int,
    overlap_seconds: int,
) -> list[tuple[float, float]]:
    if duration_seconds <= chunk_seconds:
        return [(0.0, duration_seconds)]

    step = chunk_seconds - overlap_seconds
    ranges: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_seconds:
        end = min(start + chunk_seconds, duration_seconds)
        ranges.append((start, end))
        if end >= duration_seconds:
            break
        start += step
    return ranges


async def _normalize_audio(
    *,
    source_file: Path,
    output_file: Path,
    start_seconds: float | None = None,
    duration_seconds: float | None = None,
    timeout_seconds: int,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """Normalize any audio input to 16 kHz mono PCM WAV with one-pass
    ffmpeg loudnorm.

    This is the single cancellation-safe helper reused by both the
    short/single-shot path and each long-audio chunk. When ``start_seconds``
    and ``duration_seconds`` are provided, the source is seeked/sliced
    (chunk extraction + normalization in one ffmpeg pass). Otherwise the
    whole file is normalized.

    Cancellation is delegated to ``_run_command`` which terminates the
    ffmpeg process group on cancel/timeout.
    """
    args: list[str] = [
        "ffmpeg",
        "-nostdin",
        "-y",
    ]
    if start_seconds is not None:
        args.extend(["-ss", f"{start_seconds:.3f}"])
    if duration_seconds is not None:
        args.extend(["-t", f"{duration_seconds:.3f}"])
    args.extend(
        [
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source_file),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            LOUDNORM_FILTER,
            "-ac",
            "1",
            "-ar",
            str(NORMALIZED_SAMPLE_RATE),
            "-acodec",
            "pcm_s16le",
            str(output_file),
        ]
    )
    await _run_command(
        args,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )


def _merge_pair(left: str, right: str) -> str:
    left_words = left.split()
    right_words = right.split()
    if not left:
        return right
    if not right:
        return left

    overlap_words = _find_overlap_word_count(left_words, right_words)
    if overlap_words:
        right_overlap = right_words[:overlap_words]
        punctuation = _terminal_punctuation(right_overlap[-1]) if right_overlap else ""
        if punctuation and left_words and _terminal_punctuation(left_words[-1]) == "":
            left_words[-1] = f"{left_words[-1]}{punctuation}"
        return " ".join(left_words + right_words[overlap_words:])

    return f"{left.rstrip()} {right.lstrip()}"


def _terminal_punctuation(word: str) -> str:
    stripped = word.rstrip('"\')]}')
    if stripped.endswith((".", "?", "!", ",", ":", ";")):
        return stripped[-1]
    return ""


def _find_overlap_word_count(left_words: list[str], right_words: list[str]) -> int:
    """Return how many prefix words from right are safe to drop as boundary overlap."""
    if len(left_words) < MIN_OVERLAP_WORDS or len(right_words) < MIN_OVERLAP_WORDS:
        return 0

    left_norm = _normalized_words(left_words[-BOUNDARY_SCAN_WORDS:])
    right_norm = _normalized_words(right_words[:BOUNDARY_SCAN_WORDS])
    max_right_size = min(MAX_OVERLAP_WORDS, len(right_norm))
    best_left_size = 0
    best_right_size = 0
    best_score = 0.0

    for right_size in range(max_right_size, MIN_OVERLAP_WORDS - 1, -1):
        min_left_size = max(MIN_OVERLAP_WORDS, right_size - MAX_OVERLAP_SIZE_DELTA)
        max_left_size = min(len(left_norm), right_size + MAX_OVERLAP_SIZE_DELTA)
        right_slice = right_norm[:right_size]
        for left_size in range(max_left_size, min_left_size - 1, -1):
            left_slice = left_norm[-left_size:]
            if not all(left_slice) or not all(right_slice):
                continue
            if left_slice == right_slice:
                return right_size
            score = SequenceMatcher(None, left_slice, right_slice, autojunk=False).ratio()
            if score >= MIN_OVERLAP_SIMILARITY and score > best_score:
                best_score = score
                best_left_size = left_size
                best_right_size = right_size

    if not best_right_size:
        return 0
    return min(best_left_size, best_right_size)


def _collapse_repeated_phrase_loops(text: str) -> str:
    """Collapse only obvious model loops: 5+ consecutive repeated phrases."""
    words = text.split()
    if len(words) < MIN_LOOP_PHRASE_WORDS * MIN_LOOP_REPETITIONS:
        return text

    normalized = _normalized_words(words)
    output: list[str] = []
    index = 0
    while index < len(words):
        collapsed = False
        max_phrase_words = min(MAX_LOOP_PHRASE_WORDS, (len(words) - index) // MIN_LOOP_REPETITIONS)
        for phrase_size in range(max_phrase_words, MIN_LOOP_PHRASE_WORDS - 1, -1):
            phrase = normalized[index:index + phrase_size]
            if not all(phrase):
                continue

            repetitions = 1
            cursor = index + phrase_size
            while cursor + phrase_size <= len(words):
                if normalized[cursor:cursor + phrase_size] != phrase:
                    break
                repetitions += 1
                cursor += phrase_size

            if repetitions >= MIN_LOOP_REPETITIONS:
                output.extend(words[index:index + phrase_size])
                index = cursor
                collapsed = True
                break

        if not collapsed:
            output.append(words[index])
            index += 1

    return " ".join(output)


def _merge_transcripts(texts: list[str]) -> str:
    merged = ""
    for text in texts:
        cleaned = _collapse_repeated_phrase_loops(text.strip())
        if cleaned:
            merged = _merge_pair(merged, cleaned)
    return merged.strip()


def _build_prompt(prompt: str | None, model_spec: ModelSpec) -> str:
    effective_prompt = (prompt or model_spec.default_prompt).strip()
    if not effective_prompt:
        return "transcribe the speech with proper punctuation and capitalization."
    return effective_prompt.replace("<|audio|>", "").strip()


async def run_transcription_task(
    *,
    file_path: str,
    model_spec: ModelSpec,
    model_id: str,
    prompt: str | None,
    timeout_seconds: int,
    max_audio_bytes: int,
    max_duration_seconds: int,
    max_chunks: int,
    chunk_seconds: int,
    overlap_seconds: int,
    ffmpeg_timeout_seconds: int,
    allowed_roots: tuple[Path, ...],
    router: LlamaRouterClient,
    cancel_event: asyncio.Event | None = None,
) -> dict:
    resolved_file = _resolve_safe_input_path(file_path, allowed_roots)
    suffix = resolved_file.suffix.lower()
    if suffix not in SUPPORTED_AUDIO_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
        raise ValueError(f"Unsupported audio extension '{suffix}'. Supported: {supported}")

    file_size = resolved_file.stat().st_size
    if file_size > max_audio_bytes:
        raise ValueError(
            f"Audio file is too large ({file_size} bytes). Maximum allowed: {max_audio_bytes} bytes."
        )

    effective_prompt = _build_prompt(prompt, model_spec)
    duration_seconds = await _probe_duration_seconds(
        resolved_file,
        timeout_seconds=ffmpeg_timeout_seconds,
        cancel_event=cancel_event,
    )
    if duration_seconds > max_duration_seconds:
        raise ValueError(
            f"Audio duration is too long ({duration_seconds:.3f}s). "
            f"Maximum allowed: {max_duration_seconds}s."
        )

    chunks = _chunk_ranges(
        duration_seconds=duration_seconds,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
    )
    if len(chunks) > max_chunks:
        raise ValueError(
            f"Audio would produce too many chunks ({len(chunks)}). Maximum allowed: {max_chunks}."
        )

    chunk_results: list[dict] = []
    chunk_texts: list[str] = []
    if len(chunks) == 1:
        # Normalize even short/single-shot input to 16 kHz mono PCM WAV with
        # loudnorm before llama.cpp. Temporary file is cleaned in finally.
        with tempfile.TemporaryDirectory(prefix="aux-ml-transcribe-") as temp_dir:
            normalized_file = Path(temp_dir) / "normalized.wav"
            await _normalize_audio(
                source_file=resolved_file,
                output_file=normalized_file,
                timeout_seconds=ffmpeg_timeout_seconds,
                cancel_event=cancel_event,
            )
            try:
                completion = await router.audio_transcription(
                    file_path=normalized_file,
                    model_id=model_id,
                    prompt=effective_prompt,
                    mime_type="audio/x-wav",
                    timeout_seconds=timeout_seconds,
                )
            finally:
                normalized_file.unlink(missing_ok=True)
        text = _extract_text_from_completion(completion)
        mode = "single-shot"
    else:
        with tempfile.TemporaryDirectory(prefix="aux-ml-transcribe-") as temp_dir:
            temp_root = Path(temp_dir)
            for index, (start, end) in enumerate(chunks, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError("Transcription cancelled between chunks")
                chunk_file = temp_root / f"chunk-{index:04d}.wav"
                # Each chunk is extracted AND normalized in one ffmpeg pass
                # using the same loudnorm helper as the single-shot path.
                await _normalize_audio(
                    source_file=resolved_file,
                    output_file=chunk_file,
                    start_seconds=start,
                    duration_seconds=end - start,
                    timeout_seconds=ffmpeg_timeout_seconds,
                    cancel_event=cancel_event,
                )
                try:
                    completion = await router.audio_transcription(
                        file_path=chunk_file,
                        model_id=model_id,
                        prompt=effective_prompt,
                        mime_type="audio/x-wav",
                        timeout_seconds=timeout_seconds,
                    )
                finally:
                    chunk_file.unlink(missing_ok=True)
                chunk_text = _extract_text_from_completion(completion)
                chunk_texts.append(chunk_text)
                chunk_results.append(
                    {
                        "index": index,
                        "start_seconds": round(start, 3),
                        "end_seconds": round(end, 3),
                        "text": chunk_text,
                    }
                )
        text = _merge_transcripts(chunk_texts)
        mode = "chunked"

    return {
        "source_file": str(resolved_file),
        "source_type": "audio",
        "mime_type": _guess_mime_type(resolved_file),
        "size_bytes": file_size,
        "duration_seconds": round(duration_seconds, 3),
        "mode": mode,
        "chunk_count": len(chunks),
        "chunks": chunk_results,
        "text": text,
    }


__all__ = ["run_transcription_task"]
