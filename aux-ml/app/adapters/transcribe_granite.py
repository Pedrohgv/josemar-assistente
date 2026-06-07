from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
import tempfile

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
    max_overlap = min(80, len(left_words), len(right_words))
    for size in range(max_overlap, 7, -1):
        if left_words[-size:] == right_words[:size]:
            return " ".join(left_words + right_words[size:])
    if not left:
        return right
    if not right:
        return left
    return f"{left.rstrip()} {right.lstrip()}"


def _merge_transcripts(texts: list[str]) -> str:
    merged = ""
    for text in texts:
        cleaned = text.strip()
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
