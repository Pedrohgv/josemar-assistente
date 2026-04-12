from __future__ import annotations

from pathlib import Path
import base64
import mimetypes

import pymupdf

from ..llama_router import LlamaRouterClient
from ..model_registry import ModelSpec


SUPPORTED_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}

COLUMN_SPLIT_OVERLAP_RATIO = 0.08


def _extract_text_from_completion(response: dict) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0]
    if not isinstance(first, dict):
        return ""

    message = first.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if isinstance(item, dict) and item.get("type") == "text":
                text_value = item.get("text")
                if isinstance(text_value, str):
                    chunks.append(text_value)
        return "\n".join(chunk.strip() for chunk in chunks if chunk.strip())

    return ""


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


def _should_split_pdf_page(
    *,
    page_index: int,
    column_split: int,
    column_split_pages: tuple[int, ...] | None,
) -> bool:
    if column_split <= 1:
        return False
    if not column_split_pages:
        return True
    return page_index in column_split_pages


def _build_column_clips(
    *,
    page_rect: pymupdf.Rect,
    column_split: int,
    overlap_ratio: float,
) -> list[pymupdf.Rect]:
    column_width = page_rect.width / column_split
    overlap = column_width * overlap_ratio

    clips: list[pymupdf.Rect] = []
    for index in range(column_split):
        left = page_rect.x0 + (index * column_width)
        right = page_rect.x0 + ((index + 1) * column_width)

        if index > 0:
            left -= overlap
        if index < (column_split - 1):
            right += overlap

        clip = pymupdf.Rect(
            max(page_rect.x0, left),
            page_rect.y0,
            min(page_rect.x1, right),
            page_rect.y1,
        )
        clips.append(clip)

    return clips


def _merge_column_parts(column_parts: list[str]) -> str:
    cleaned = [part.strip() for part in column_parts if part.strip()]
    return "\n\n".join(cleaned)


async def _ocr_image_bytes(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    model_id: str,
    max_tokens: int,
    timeout_seconds: int,
    router: LlamaRouterClient,
) -> str:
    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_base64}",
                        },
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }

    completion = await router.chat_completion(payload, timeout_seconds=timeout_seconds)
    return _extract_text_from_completion(completion)


async def run_ocr_task(
    *,
    file_path: str,
    model_spec: ModelSpec,
    model_id: str,
    prompt: str | None,
    timeout_seconds: int,
    max_pages: int,
    column_split: int,
    column_split_pages: tuple[int, ...] | None,
    allowed_roots: tuple[Path, ...],
    router: LlamaRouterClient,
) -> dict:
    resolved_file = _resolve_safe_input_path(file_path, allowed_roots)
    effective_prompt = (prompt or model_spec.default_prompt).strip()
    if not effective_prompt:
        effective_prompt = "Extract all text preserving reading order."

    suffix = resolved_file.suffix.lower()
    if suffix == ".pdf":
        document = pymupdf.open(str(resolved_file))
        pages: list[dict] = []
        merged_parts: list[str] = []
        try:
            for page_index, page in enumerate(document, start=1):
                if page_index > max_pages:
                    raise ValueError(
                        f"PDF has more than {max_pages} pages. Increase AUX_ML_OCR_MAX_PAGES if needed."
                    )

                split_mode = _should_split_pdf_page(
                    page_index=page_index,
                    column_split=column_split,
                    column_split_pages=column_split_pages,
                )

                if split_mode:
                    clips = _build_column_clips(
                        page_rect=page.rect,
                        column_split=column_split,
                        overlap_ratio=COLUMN_SPLIT_OVERLAP_RATIO,
                    )
                    parts: list[str] = []
                    for clip in clips:
                        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip)
                        image_bytes = pixmap.tobytes("png")
                        part_text = await _ocr_image_bytes(
                            image_bytes=image_bytes,
                            mime_type="image/png",
                            prompt=effective_prompt,
                            model_id=model_id,
                            max_tokens=model_spec.max_tokens,
                            timeout_seconds=timeout_seconds,
                            router=router,
                        )
                        parts.append(part_text)
                    page_text = _merge_column_parts(parts)
                    page_mode = f"columns-{column_split}"
                else:
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
                    image_bytes = pixmap.tobytes("png")
                    page_text = await _ocr_image_bytes(
                        image_bytes=image_bytes,
                        mime_type="image/png",
                        prompt=effective_prompt,
                        model_id=model_id,
                        max_tokens=model_spec.max_tokens,
                        timeout_seconds=timeout_seconds,
                        router=router,
                    )
                    page_mode = "full-page"

                pages.append({"page": page_index, "mode": page_mode, "text": page_text})
                merged_parts.append(page_text)
        finally:
            document.close()

        merged_text = "\n\n".join(part for part in merged_parts if part.strip())
        return {
            "source_file": str(resolved_file),
            "source_type": "pdf",
            "page_count": len(pages),
            "layout": {
                "column_split": column_split,
                "column_split_pages": list(column_split_pages) if column_split_pages else None,
            },
            "text": merged_text,
            "pages": pages,
        }

    if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
        extensions = ", ".join(sorted(SUPPORTED_IMAGE_EXTENSIONS))
        raise ValueError(
            f"Unsupported file extension '{suffix}'. Supported image extensions: {extensions}, plus .pdf"
        )

    image_bytes = resolved_file.read_bytes()
    page_text = await _ocr_image_bytes(
        image_bytes=image_bytes,
        mime_type=_guess_mime_type(resolved_file),
        prompt=effective_prompt,
        model_id=model_id,
        max_tokens=model_spec.max_tokens,
        timeout_seconds=timeout_seconds,
        router=router,
    )
    return {
        "source_file": str(resolved_file),
        "source_type": "image",
        "page_count": 1,
        "text": page_text,
        "pages": [{"page": 1, "text": page_text}],
    }
