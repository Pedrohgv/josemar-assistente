# Bundled Models

Place auxiliary model artifacts in this directory before building the `aux-ml` image.

## Current expected model

- `glm-ocr.gguf` -> mapped to model key `glm-ocr`

The image build copies this directory into `/models` inside the container.

If `glm-ocr.gguf` is not present here, Docker build can download it when
`AUX_ML_GLM_OCR_URL` is set (optional integrity check with `AUX_ML_GLM_OCR_SHA256`).

## Notes

- Model files are intentionally gitignored due size and licensing constraints.
- Keep `aux-ml/config/models.yaml` aligned with filenames and memory requirements.
