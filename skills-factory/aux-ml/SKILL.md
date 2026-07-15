---
name: aux-ml
description: Queue-based auxiliary ML processing via llama.cpp router. Supports long-running OCR/transcription jobs and async polling.
categories:
  - ml
  - ocr
  - transcription
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

- **File location:** Input files should be staged into the aux-ml shared handoff path mounted at `/shared/` in both Hermes and aux-ml. The skill automatically copies files outside `/shared/` into `/shared/staged/` before submitting, so you can pass any path visible to the Hermes container (e.g. `/opt/data/image_cache/photo.jpg`). Files already inside `/shared/` are submitted as-is.
- **Processing time:** OCR/transcription jobs can take **30+ minutes** depending on page count, audio length, and model load. Set `timeout_seconds` accordingly (recommend >= 1800).
- **Queue system:** Jobs are processed sequentially. Use `queue_status` to check depth before submitting. Avoid submitting duplicate or unnecessary jobs to prevent queue buildup.

## Actions

### `ocr_file`

Submit an OCR job and optionally wait for completion.

```bash
echo '{
  "action": "ocr_file",
  "file_path": "/opt/data/image_cache/invoice.jpg",
  "model": "glm-ocr",
  "wait": true
}' | aux-ml
```

The skill stages the file into `/shared/staged/` automatically. You can also pass a path already under `/shared/` directly.

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
  "file_path": "/shared/invoice.pdf"
}' | aux-ml
```

For transcription:

```bash
echo '{
  "action": "submit_job",
  "task": "transcribe",
  "model": "granite-speech-4.1-2b",
  "file_path": "/shared/meeting.mp3",
  "prompt": "transcribe the speech with proper punctuation and capitalization."
}' | aux-ml
```

The generic `submit_job` action accepts the optional `prompt` field for both OCR and transcription. For Granite Speech, use one of the model's task prompts rather than free-form instructions:

- Raw transcript: `can you transcribe the speech into a written format?`
- Transcript with punctuation and capitalization (default): `transcribe the speech with proper punctuation and capitalization.`
- Keyword-biased transcript: `transcribe the speech to text. Keywords: <keyword1>, <keyword2>, ...`
- Speech translation: `translate the speech to <language>.`
- Speech translation with punctuation: `translate the speech to <language> with proper punctuation and capitalization.`

Do not include the `<|audio|>` token; the service removes it before calling llama.cpp's audio transcription endpoint. Unfamiliar or malformed prompts may be ignored by the model, which then falls back to plain transcription.

Note: Granite Speech support uses pinned llama.cpp `b9585` experimental audio input. `b9585` contains an embedding-scale fix relevant to Granite Speech; runtime validation in a real container is still required. Every request is normalized to 16 kHz mono PCM WAV with one-pass ffmpeg loudnorm before llama.cpp, including short/single-shot input and each long-audio chunk. Long files are chunked with ffmpeg and merged with conservative fuzzy overlap cleanup. Treat long-form transcripts as draft output: spot-check important sections, expect occasional duplicated overlap or model repetition, and do not treat transcripts as authoritative without human review.

Language/format caveats:
- Supported transcription languages are English, French, German, Spanish, Portuguese, and Japanese. Do not imply support for arbitrary languages.
- Portuguese is officially supported, but local Q8_0 tests on multi-speaker meeting audio remained unreliable with llama.cpp `b9585`, loudness normalization, and 30-second chunks. Some segments mixed Portuguese and English or collapsed to the repeated phrase `Thank you very much.` Treat that phrase as a likely transcription failure, not valid output.
- Prefer clean, single-speaker segments of roughly 15–30 seconds when accuracy matters. The base model does not provide speaker attribution, and overlapping or far-field meeting speech requires careful human review.
- For punctuated or keyword-biased transcription of non-English audio, keep the task prompt in English. Multilingual prompts are supported only for the raw-transcript task.
- Speech translation is supported between English and French, German, Spanish, Portuguese, or Japanese, plus English-to-Italian and English-to-Mandarin. Translation is distinct from same-language transcription.
- The model does not document a language-identification task or API language parameter; use a supported task prompt and review important non-English output.
- Telegram voice notes commonly arrive as OGG/Opus. The service normalizes every request to 16 kHz mono PCM WAV with ffmpeg loudnorm before llama.cpp, so you can submit OGG/Opus directly. If output quality is poor, review the prompt and language settings.

Prompt and language guidance is based on the [official Granite Speech 4.1 2B model card](https://huggingface.co/ibm-granite/granite-speech-4.1-2b).

### `job_status`

Fetch current job state.

```bash
echo '{"action": "job_status", "job_id": "<job-id>"}' | aux-ml
```

### `wait_for_job`

Poll until terminal state (`succeeded`, `failed`, or `cancelled`).

```bash
echo '{"action": "wait_for_job", "job_id": "<job-id>", "timeout_seconds": 1800}' | aux-ml
```

### `cancel_job`

Cancel a queued or running job. Queued jobs are removed from the queue and marked cancelled immediately. Running jobs transition to `cancelling`: the endpoint records intent and returns promptly, and the worker unloads the model and marks the job cancelled once the router confirms the unload.

```bash
echo '{"action": "cancel_job", "job_id": "<job-id>"}' | aux-ml
```

Returns `cancelled: true` with `status` `cancelled` (queued) or `cancelling` (running), and `cancelled: false` with the current `status` when the job was already in a terminal state (`succeeded`, `failed`, or `cancelled`).

Note: active cancellation cancels the local inference request and unloads the model child; it is considered cancelled only after the router confirms the unload. The pinned llama.cpp `b9585` runtime behavior still needs real-container validation.

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
