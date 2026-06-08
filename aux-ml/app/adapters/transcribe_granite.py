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


async def _run_command(args: list[str], timeout_seconds: int) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise TimeoutError(f"Command timed out ({args[0]}) after {timeout_seconds}s") from exc
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        if len(message) > 500:
            message = f"{message[:500]}..."
        raise RuntimeError(f"Command failed ({args[0]}): {message}")
    return stdout.decode("utf-8", errors="replace").strip()


async def _probe_duration_seconds(file_path: Path, timeout_seconds: int) -> float:
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

    overlap = min(overlap_seconds, max(chunk_seconds - 1, 0))
    step = chunk_seconds - overlap
    ranges: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_seconds:
        end = min(start + chunk_seconds, duration_seconds)
        ranges.append((start, end))
        if end >= duration_seconds:
            break
        start += step
    return ranges


async def _create_chunk(
    *,
    source_file: Path,
    output_file: Path,
    start_seconds: float,
    duration_seconds: float,
    timeout_seconds: int,
) -> None:
    await _run_command(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-t",
            f"{duration_seconds:.3f}",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source_file),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            str(output_file),
        ],
        timeout_seconds=timeout_seconds,
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
        completion = await router.audio_transcription(
            file_path=resolved_file,
            model_id=model_id,
            prompt=effective_prompt,
            mime_type=_guess_mime_type(resolved_file),
            timeout_seconds=timeout_seconds,
        )
        text = _extract_text_from_completion(completion)
        mode = "single-shot"
    else:
        with tempfile.TemporaryDirectory(prefix="aux-ml-transcribe-") as temp_dir:
            temp_root = Path(temp_dir)
            for index, (start, end) in enumerate(chunks, start=1):
                chunk_file = temp_root / f"chunk-{index:04d}.wav"
                await _create_chunk(
                    source_file=resolved_file,
                    output_file=chunk_file,
                    start_seconds=start,
                    duration_seconds=end - start,
                    timeout_seconds=ffmpeg_timeout_seconds,
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
