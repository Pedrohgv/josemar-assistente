# Bundled Models

Place auxiliary model artifacts in this directory before building the `aux-ml` image.

## Current expected models

- `glm-ocr.gguf` -> mapped to model key `glm-ocr`
- `mmproj-glm-ocr.gguf` -> multimodal projector for GLM-OCR

The image build copies this directory into `/models` inside the container.

If local files are not present here, compose defaults download and bundle both files.

You can override URLs/checksums with:

- `AUX_ML_GLM_OCR_URL`
- `AUX_ML_GLM_OCR_SHA256`
- `AUX_ML_GLM_OCR_MMPROJ_URL`
- `AUX_ML_GLM_OCR_MMPROJ_SHA256`

## Notes

- Model files are intentionally gitignored due size and licensing constraints.
- Keep `aux-ml/config/models.yaml` aligned with filenames and memory requirements.
