FROM ghcr.io/ggml-org/llama.cpp:server

USER root

ARG GLM_OCR_MODEL_URL=
ARG GLM_OCR_MODEL_SHA256=
ARG GLM_OCR_MMPROJ_URL=
ARG GLM_OCR_MMPROJ_SHA256=

RUN apt-get update && apt-get install -y --no-install-recommends curl python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY aux-ml/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY aux-ml/app /app/app
COPY aux-ml/config /app/config
COPY aux-ml/models /models
COPY aux-ml/entrypoint.sh /usr/local/bin/aux-ml-entrypoint.sh

RUN if [ ! -f /models/glm-ocr.gguf ]; then \
        if [ -z "$GLM_OCR_MODEL_URL" ]; then \
            echo "ERROR: Missing /models/glm-ocr.gguf and GLM_OCR_MODEL_URL build arg is empty."; \
            echo "Provide local file aux-ml/models/glm-ocr.gguf or set AUX_ML_GLM_OCR_URL in .env."; \
            exit 1; \
        fi; \
        echo "Downloading glm-ocr model from build arg URL..."; \
        curl -fL "$GLM_OCR_MODEL_URL" -o /models/glm-ocr.gguf; \
    fi \
    && if [ -n "$GLM_OCR_MODEL_SHA256" ]; then \
        echo "$GLM_OCR_MODEL_SHA256  /models/glm-ocr.gguf" | sha256sum -c -; \
    fi \
    && if [ ! -f /models/mmproj-glm-ocr.gguf ]; then \
        if [ -z "$GLM_OCR_MMPROJ_URL" ]; then \
            echo "ERROR: Missing /models/mmproj-glm-ocr.gguf and GLM_OCR_MMPROJ_URL build arg is empty."; \
            echo "Provide local file aux-ml/models/mmproj-glm-ocr.gguf or set AUX_ML_GLM_OCR_MMPROJ_URL in .env."; \
            exit 1; \
        fi; \
        echo "Downloading GLM-OCR mmproj from build arg URL..."; \
        curl -fL "$GLM_OCR_MMPROJ_URL" -o /models/mmproj-glm-ocr.gguf; \
    fi \
    && if [ -n "$GLM_OCR_MMPROJ_SHA256" ]; then \
        echo "$GLM_OCR_MMPROJ_SHA256  /models/mmproj-glm-ocr.gguf" | sha256sum -c -; \
    fi

RUN chmod +x /usr/local/bin/aux-ml-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    AUX_ML_PORT=8091 \
    AUX_ML_BIND_HOST=0.0.0.0 \
    AUX_ML_LLAMACPP_URL=http://127.0.0.1:8080 \
    AUX_ML_MODEL_REGISTRY=/app/config/models.yaml \
    AUX_ML_MAX_QUEUE=50 \
    AUX_ML_JOB_TIMEOUT_SECONDS=1800 \
    AUX_ML_POLL_INTERVAL_SECONDS=1.0 \
    AUX_ML_ALLOWED_INPUT_DIRS=/root/.openclaw/workspace \
    AUX_ML_ENFORCE_MEMORY_LIMIT=true \
    AUX_ML_MEMORY_LIMIT_MB=8192 \
    AUX_ML_OCR_MAX_PAGES=50 \
    LLAMA_ROUTER_HOST=127.0.0.1 \
    LLAMA_ROUTER_PORT=8080 \
    LLAMA_MODELS_DIR=/models \
    LLAMA_PARALLEL=1 \
    LLAMA_CONT_BATCHING=false

ENTRYPOINT ["aux-ml-entrypoint.sh"]
