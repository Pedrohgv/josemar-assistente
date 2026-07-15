# Auxiliary ML Service (`aux-ml`)

This document describes the optional `aux-ml` container used for long-running, queue-based ML tasks (OCR first, transcription later).

## Goals

- Run auxiliary models separately from Hermes/Josemar.
- Process jobs as batch workloads (minute-scale jobs are acceptable).
- Keep strict FIFO ordering and process one job at a time.
- Load models on demand and unload them when not needed.
- Keep memory predictable by sizing the container for the largest supported model.

## Service Layout

- **Container:** `${JOSEMAR_CONTAINER_PREFIX:-josemar}-aux-ml`
- **Inference backend:** pinned llama.cpp `b9585` `llama-server` in router mode (local-only inside container)
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

- Compose overlays the verified llama.cpp `b9585` Ubuntu x64 release during build (target commit `d73cd076740db9c111d0e58ddd4486904469e75e`). `b9585` contains an embedding-scale fix relevant to Granite Speech; runtime validation in a real container is still required before relying on it. Earlier moving Docker tags produced unusable Granite Speech transcripts in testing.
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
- `POST /jobs/{job_id}/cancel` - cancel a queued or running job
- `POST /run` - submit job and wait until terminal state

## Job Cancellation

`POST /jobs/{job_id}/cancel` cancels a job that is currently `queued` or `running`. The endpoint only records intent and returns promptly; the worker alone owns model cleanup and the final status.

HTTP semantics:

- `queued` job: `200`, status=`cancelled` (removed from FIFO and marked cancelled atomically).
- `running` job: `202`, status=`cancelling` (intent recorded; worker finalizes).
- `cancelling` job: `202`, status=`cancelling` (idempotent).
- `cancelled` job: `200`, status=`cancelled` (idempotent).
- `succeeded`/`failed` job: `409`, `cancelled: false`.
- unknown job: `404`.

Cancellation behavior:

- **Queued jobs:** removed from the FIFO queue and marked `cancelled` atomically under one scheduler lock. No model interaction occurs.
- **Running jobs:** the endpoint transitions the job to `cancelling`, sets the cancellation event, and cancels the active task. The worker then: awaits the task unwind, unloads the model (or the in-flight loading target), confirms the unload, and marks the job `cancelled`. A late `succeeded`/`failed` result cannot overwrite `cancelling`/`cancelled` (compare-and-set transitions).
- **Cancellation during model load:** the service tracks the model id being loaded before issuing `/models/load`, so a cancel during loading unloads the correct target.
- **Cleanup failure:** if the model unload fails during cancellation cleanup, the job is marked `failed` with a cleanup error, dispatch is paused (degraded), and the reason is exposed in `/queue` and `/health`. Restart is the accepted recovery path; replacement work cannot start unsafely.
- **ffmpeg safety:** the transcription adapter starts subprocesses in their own session (`start_new_session=True`), terminates the process group with a bounded grace period and SIGKILL fallback, reaps the process, and always cancels/awaits the helper task. Both the short/single-shot normalization pass and each long-audio chunk normalization pass use the same cancellation-safe helper, so cancellation can stop normalization ffmpeg as well as chunked transcription. Chunked transcription also checks the cancellation event between chunks.
- **Worker survival:** the worker loop survives a successful cancellation and processes the next queued job. If dispatch is blocked due to a cleanup failure, the worker pauses until restart.

Note: active cancellation cancels the local inference request and unloads the model child; it is considered cancelled only after the router confirms the unload. The pinned llama.cpp `b9585` runtime behavior still needs real-container validation.

## File Handoff

aux-ml only mounts the `aux-ml-shared` volume at `/shared` (read-only). Files elsewhere in the Hermes container (e.g. `/opt/data/image_cache/`) are not visible to aux-ml.

The `aux-ml` skill handles this transparently: it copies any file outside `/shared/` into `/shared/staged/` before submitting the job. You can pass any path visible to the Hermes container; no manual copying is needed. Files already under `/shared/` are submitted as-is.

When calling the API directly (not through the skill), you must stage files into `/shared/` yourself.

## Job Schema (OCR)

`POST /jobs` request body:

```json
{
  "task": "ocr",
  "model": "glm-ocr",
  "file_path": "/shared/invoice.pdf",
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
  "file_path": "/shared/meeting.mp3",
  "prompt": "transcribe the speech with proper punctuation and capitalization."
}
```

Current transcription behavior:

- Uses official `ibm-granite/granite-speech-4.1-2b-GGUF:Q8_0` through llama.cpp native `/v1/audio/transcriptions`.
- Requires the pinned llama.cpp `b9585` runtime. `b9585` contains an embedding-scale fix relevant to Granite Speech; runtime validation in a real container is still required. Earlier later llama.cpp builds tested with Q4, Q8, BF16, CLI, and server paths produced empty or hallucinated transcripts.
- The rejected `granite-speech-4.1-2b-plus` GGUF currently fails in llama.cpp server with `unknown model architecture: granite_speech`.
- Audio input in llama.cpp is experimental; validate quality and latency before relying on it.
- Granite Speech currently works best for English. Portuguese and other languages may produce lower-quality or unreliable transcripts; important non-English transcripts should be reviewed by a human.
- Every transcription request is normalized to 16 kHz mono PCM WAV with one-pass ffmpeg loudnorm (`loudnorm=I=-16:TP=-1.5:LRA=11`) before llama.cpp, including short/single-shot input and each long-audio chunk. Temporary normalized files are cleaned in finally blocks.
- Audio longer than `AUX_ML_TRANSCRIBE_CHUNK_SECONDS` is split with ffmpeg into 16 kHz mono WAV chunks (extracted and normalized in one ffmpeg pass) and processed sequentially.
- Chunk overlap is controlled by `AUX_ML_TRANSCRIBE_OVERLAP_SECONDS`; chunk text is merged with conservative fuzzy overlap cleanup. Overlap must be strictly less than the chunk size; a minimum chunk size of 10 seconds is enforced at settings load.
- Long-form transcripts are draft quality. Spot-check important sections, expect occasional duplicated overlap or model repetition, and do not treat transcripts as authoritative without human review.
- Audio files larger than `AUX_ML_TRANSCRIBE_MAX_BYTES` are rejected before being sent to llama.cpp. Default: `104857600` bytes.

Transcription tuning:

- `AUX_ML_TRANSCRIBE_CHUNK_SECONDS` default: `30`
- `AUX_ML_TRANSCRIBE_OVERLAP_SECONDS` default: `2`
- `AUX_ML_TRANSCRIBE_MAX_DURATION_SECONDS` default: `1800`
- `AUX_ML_TRANSCRIBE_MAX_CHUNKS` default: `72`
- `AUX_ML_TRANSCRIBE_FFMPEG_TIMEOUT_SECONDS` default: `300`
- Keep chunks small enough for Granite's llama.cpp audio context. A 30-minute input at the 30/2 defaults produces 65 chunks, under the default `max_chunks=72`.

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
