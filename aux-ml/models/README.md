# Bundled Models

Place auxiliary model artifacts in this directory before building the `aux-ml` image.

## Current expected models

- `glm-ocr.gguf` -> mapped to model key `glm-ocr`
- `mmproj-glm-ocr.gguf` -> multimodal projector for GLM-OCR
- `granite-speech-4.1-2b-Q8_0.gguf` -> mapped to model key `granite-speech-4.1-2b`
- `mmproj-granite-speech-4.1-2b-f16.gguf` -> multimodal projector for Granite Speech 4.1

The image build copies this directory into `/models` inside the container.

If local files are not present here, compose defaults download and bundle the required model files.
Granite Speech files are optional at runtime. If they are not present, the transcription model is not registered and OCR can still run. Set `AUX_ML_ENABLE_GRANITE_SPEECH=false` to remove copied Granite files during image build and use the OCR-only llama.cpp preset.

The `aux-ml` image also overlays the verified llama.cpp `b9045` release by default. Granite Speech GGUF produced coherent transcripts with `b9045`; later moving Docker tags produced unusable audio output in testing.

You can override URLs/checksums with:

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

## Notes

- Model files are intentionally gitignored due size and licensing constraints.
- Keep `aux-ml/config/models.yaml` aligned with filenames and memory requirements.
