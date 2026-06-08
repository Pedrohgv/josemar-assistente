# Auxiliary ML Service (`aux-ml`)

This document describes the optional `aux-ml` container used for long-running, queue-based ML tasks (OCR first, transcription later).

## Goals

- Run auxiliary models separately from Hermes/Josemar.
- Process jobs as batch workloads (minute-scale jobs are acceptable).
- Keep strict FIFO ordering and process one job at a time.
- Load models on demand and unload them when not needed.
- Keep memory predictable by sizing the container for the largest supported model.

## Service Layout

- **Container:** `josemar-aux-ml`
- **Inference backend:** pinned llama.cpp `b9045` `llama-server` in router mode (local-only inside container)
- **Orchestrator API:** FastAPI on `8091`
- **Network exposure:** internal Docker network only (`http://aux-ml:8091`)

## Enabling the Service

Set these values in `.env`:

```bash
AUX_ML_ENABLED=true
COMPOSE_PROFILES=aux-ml
AUX_ML_MEMORY_LIMIT=8192m
AUX_ML_MEMORY_LIMIT_MB=8192
AUX_ML_LLAMACPP_TIMEOUT_SECONDS=1800
```

Then start:

```bash
docker compose up -d --build
```

If `COMPOSE_PROFILES` does not include `aux-ml`, the service is not started.

## Model Packaging

`aux-ml` ships model files inside the image. Place model files in `aux-ml/models/` before build.

Current expected files:

- `aux-ml/models/glm-ocr.gguf`
- `aux-ml/models/mmproj-glm-ocr.gguf`
- `aux-ml/models/granite-speech-4.1-2b-Q8_0.gguf`
- `aux-ml/models/mmproj-granite-speech-4.1-2b-f16.gguf`

If required model files are missing, `aux-ml` fails fast on startup.
Granite Speech is optional: if its model or mmproj file is absent, it is not registered and OCR can still run.

Build fallback behavior:

- Compose overlays the verified llama.cpp `b9045` Ubuntu x64 release during build because newer moving Docker tags produced unusable Granite Speech transcripts in testing.
- `AUX_ML_ENABLE_GRANITE_SPEECH=false` removes copied Granite artifacts from the image and falls back to an OCR-only llama.cpp preset.
- If local files are absent, compose defaults download and bundle:
  - `GLM-OCR-Q8_0.gguf`
  - `mmproj-GLM-OCR-Q8_0.gguf`
  - `granite-speech-4.1-2b-Q8_0.gguf`
  - `mmproj-model-f16.gguf` as `mmproj-granite-speech-4.1-2b-f16.gguf`
- To override download sources/checksums, set:
  - `AUX_ML_ENABLE_GRANITE_SPEECH` (`true` by default; set `false` for OCR-only/offline builds)
  - `AUX_ML_LLAMA_CPP_RELEASE_URL`
  - `AUX_ML_LLAMA_CPP_RELEASE_SHA256`
  - `AUX_ML_GLM_OCR_URL`
  - `AUX_ML_GLM_OCR_SHA256`
  - `AUX_ML_GLM_OCR_MMPROJ_URL`
  - `AUX_ML_GLM_OCR_MMPROJ_SHA256`
  - `AUX_ML_GRANITE_SPEECH_URL`
  - `AUX_ML_GRANITE_SPEECH_SHA256`
  - `AUX_ML_GRANITE_SPEECH_MMPROJ_URL`
  - `AUX_ML_GRANITE_SPEECH_MMPROJ_SHA256`

Model metadata lives in `aux-ml/config/models.yaml`.

## Queue and Model Lifecycle

1. Job is submitted to `/jobs`.
2. Job is appended to FIFO queue.
3. Worker pops first queued job (single worker only).
4. Required model is loaded if not already loaded.
5. Job runs to completion.
6. Worker checks next queued job:
   - Same model: keep loaded.
   - Different model or empty queue: unload current model.

This behavior intentionally prioritizes predictable memory and correctness over low latency.

For longer OCR runs (for example, page splits or low thread counts), increase
`AUX_ML_LLAMACPP_TIMEOUT_SECONDS` to avoid `proxy error: Failed to read connection`
from llama-router child requests.

## API Endpoints

- `GET /health` - service status, queue depth, memory policy summary
- `GET /queue` - queued job ids, running job id, loaded model key
- `POST /jobs` - submit asynchronous job
- `GET /jobs/{job_id}` - fetch job status/result
- `POST /run` - submit job and wait until terminal state

## Job Schema (OCR)

`POST /jobs` request body:

```json
{
  "task": "ocr",
  "model": "glm-ocr",
  "file_path": "/opt/data/workspace/uploads/invoice.pdf",
  "prompt": "Text Recognition:",
  "column_split": 1,
  "column_split_pages": [2]
}
```

Prompt behavior:

- If omitted, OCR uses default prompt `Text Recognition:`
- For table-heavy layouts, you can pass `Table Recognition:` explicitly

Column split controls (optional):

- `column_split`: number of vertical segments per selected page (`1` = disabled, `2` = left/right)
- `column_split_pages`: list of 1-based page numbers to split; when omitted and `column_split > 1`, split applies to all PDF pages

Successful OCR job result includes:

- `text` - merged extracted text
- `page_count` - number of processed pages/images
- `pages` - per-page text chunks
- `layout` - PDF-only layout metadata (column split settings); image OCR keeps the simpler response shape

## Job Schema (Transcription)

`POST /jobs` request body:

```json
{
  "task": "transcribe",
  "model": "granite-speech-4.1-2b",
  "file_path": "/opt/data/workspace/uploads/meeting.mp3",
  "prompt": "transcribe the speech with proper punctuation and capitalization."
}
```

Current transcription behavior:

- Uses official `ibm-granite/granite-speech-4.1-2b-GGUF:Q8_0` through llama.cpp native `/v1/audio/transcriptions`.
- Requires the pinned llama.cpp `b9045` runtime. Current later llama.cpp builds tested with Q4, Q8, BF16, CLI, and server paths produced empty or hallucinated transcripts.
- The rejected `granite-speech-4.1-2b-plus` GGUF currently fails in llama.cpp server with `unknown model architecture: granite_speech`.
- Audio input in llama.cpp is experimental; validate quality and latency before relying on it.
- Granite Speech currently works best for English. Portuguese and other languages may produce lower-quality or unreliable transcripts; important non-English transcripts should be reviewed by a human.
- Telegram voice notes commonly arrive as OGG/Opus. If aux-ml rejects them or output quality is poor, convert them to 16 kHz mono WAV before submitting transcription.
- Audio longer than `AUX_ML_TRANSCRIBE_CHUNK_SECONDS` is split with ffmpeg into 16 kHz mono WAV chunks and processed sequentially.
- Chunk overlap is controlled by `AUX_ML_TRANSCRIBE_OVERLAP_SECONDS`; chunk text is merged with conservative fuzzy overlap cleanup.
- Long-form transcripts are draft quality. Spot-check important sections, expect occasional duplicated overlap or model repetition, and do not treat transcripts as authoritative without human review.
- Audio files larger than `AUX_ML_TRANSCRIBE_MAX_BYTES` are rejected before being sent to llama.cpp. Default: `104857600` bytes.

Transcription tuning:

- `AUX_ML_TRANSCRIBE_CHUNK_SECONDS` default: `240`
- `AUX_ML_TRANSCRIBE_OVERLAP_SECONDS` default: `20`
- `AUX_ML_TRANSCRIBE_MAX_DURATION_SECONDS` default: `1800`
- `AUX_ML_TRANSCRIBE_MAX_CHUNKS` default: `16`
- `AUX_ML_TRANSCRIBE_FFMPEG_TIMEOUT_SECONDS` default: `300`
- Keep chunks small enough for Granite's llama.cpp audio context. In testing, a 20-minute MP3 exceeded the native 4096-token audio context as a single request.

Successful transcription result includes:

- `text` - extracted transcript
- `source_file` - resolved input path
- `source_type` - `audio`
- `mime_type` - detected MIME type
- `mode` - `single-shot` or `chunked`
- `chunk_count` - number of audio chunks processed
- `chunks` - per-chunk text and timing metadata when chunked

## Memory Policy

- `AUX_ML_MEMORY_LIMIT` controls Docker container memory limit.
- `AUX_ML_MEMORY_LIMIT_MB` is checked at runtime against `required_memory_mb` in model registry.
- If runtime check is enabled (`AUX_ML_ENFORCE_MEMORY_LIMIT=true`) and memory is insufficient, service fails fast.

When adding bigger models, update both:

1. `aux-ml/config/models.yaml` (`required_memory_mb`)
2. `.env` (`AUX_ML_MEMORY_LIMIT` and `AUX_ML_MEMORY_LIMIT_MB`)

## Security Controls

- OCR file paths are restricted to roots declared by `AUX_ML_ALLOWED_INPUT_DIRS`.
- Service has no host port mapping by default.
- Hermes calls the service via internal Docker networking.

## Extending to New Models

To add a new model/task:

1. Add model files under `aux-ml/models/`.
2. Add model entry in `aux-ml/config/models.yaml`.
3. Add or reuse task adapter in `aux-ml/app/adapters/`.
4. Rebuild with `docker compose build` (and active `aux-ml` profile).
