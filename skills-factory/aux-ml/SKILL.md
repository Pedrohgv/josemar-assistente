---
name: aux-ml
description: Queue-based auxiliary ML processing via llama.cpp router. Supports long-running OCR jobs and async polling.
categories:
  - ml
  - ocr
  - batch
  - llama.cpp
---

# Aux ML Skill

Submits and tracks long-running auxiliary ML jobs in the `aux-ml` container.

## Service Assumptions

- `AUX_ML_ENABLED=true`
- `AUX_ML_URL` points to orchestrator API (default `http://aux-ml:8091`)
- The `aux-ml` container is running (enable via `COMPOSE_PROFILES=aux-ml`)

## Important Notes

- **File location:** Input files must reside inside `/root/.openclaw/workspace/` (e.g. `uploads/`). Files outside this root are rejected.
- **Processing time:** OCR jobs can take **30+ minutes** depending on page count, column split, and model load. Set `timeout_seconds` accordingly (recommend ≥ 1800).
- **Queue system:** Jobs are processed sequentially. Use `queue_status` to check depth before submitting. Avoid submitting duplicate or unnecessary jobs to prevent queue buildup.

## Actions

### `ocr_file`

Submit an OCR job and optionally wait for completion.

```bash
echo '{
  "action": "ocr_file",
  "file_path": "/root/.openclaw/workspace/uploads/invoice.pdf",
  "model": "glm-ocr",
  "wait": true
}' | aux-ml
```

Optional fields:
- `prompt` (string, default `Text Recognition:`; for table layouts you can pass `Table Recognition:`)
- `timeout_seconds` (integer, when `wait=true`)
- `column_split` (integer, default `1`; set `2` to OCR left/right columns separately)
- `column_split_pages` (array of page numbers to split; when omitted and `column_split > 1`, split applies to all PDF pages)

Note: `column_split_pages` is ignored when `column_split=1`.

### `submit_job`

Submit a generic async job.

```bash
echo '{
  "action": "submit_job",
  "task": "ocr",
  "model": "glm-ocr",
  "file_path": "/root/.openclaw/workspace/uploads/invoice.pdf"
}' | aux-ml
```

### `job_status`

Fetch current job state.

```bash
echo '{"action": "job_status", "job_id": "<job-id>"}' | aux-ml
```

### `wait_for_job`

Poll until terminal state (`succeeded` or `failed`).

```bash
echo '{"action": "wait_for_job", "job_id": "<job-id>", "timeout_seconds": 1800}' | aux-ml
```

### `queue_status`

Inspect queue depth, running job, and currently loaded model.

```bash
echo '{"action": "queue_status"}' | aux-ml
```

### `health`

Read orchestrator health and memory policy state.

```bash
echo '{"action": "health"}' | aux-ml
```
