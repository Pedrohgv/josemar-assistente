# Auxiliary ML Service (`aux-ml`)

This document describes the optional `aux-ml` container used for long-running, queue-based ML tasks (OCR first, transcription later).

## Goals

- Run auxiliary models separately from OpenClaw/Josemar.
- Process jobs as batch workloads (minute-scale jobs are acceptable).
- Keep strict FIFO ordering and process one job at a time.
- Load models on demand and unload them when not needed.
- Keep memory predictable by sizing the container for the largest supported model.

## Service Layout

- **Container:** `josemar-aux-ml`
- **Inference backend:** `llama-server` in router mode (local-only inside container)
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

If required model files are missing, `aux-ml` fails fast on startup.

Build fallback behavior:

- If local files are absent, compose defaults download and bundle:
  - `GLM-OCR-Q8_0.gguf`
  - `mmproj-GLM-OCR-Q8_0.gguf`
- To override download sources/checksums, set:
  - `AUX_ML_GLM_OCR_URL`
  - `AUX_ML_GLM_OCR_SHA256`
  - `AUX_ML_GLM_OCR_MMPROJ_URL`
  - `AUX_ML_GLM_OCR_MMPROJ_SHA256`

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
  "file_path": "/root/.openclaw/workspace/uploads/invoice.pdf",
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
- OpenClaw calls the service via internal Docker networking.

## Extending to New Models

To add a new model/task:

1. Add model files under `aux-ml/models/`.
2. Add model entry in `aux-ml/config/models.yaml`.
3. Add or reuse task adapter in `aux-ml/app/adapters/`.
4. Rebuild with `docker compose build` (and active `aux-ml` profile).
