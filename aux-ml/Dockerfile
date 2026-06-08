FROM ghcr.io/ggml-org/llama.cpp:server

USER root

ARG LLAMA_CPP_RELEASE_URL=
ARG LLAMA_CPP_RELEASE_SHA256=
ARG ENABLE_GRANITE_SPEECH=true
ARG GLM_OCR_MODEL_URL=
ARG GLM_OCR_MODEL_SHA256=
ARG GLM_OCR_MMPROJ_URL=
ARG GLM_OCR_MMPROJ_SHA256=
ARG GRANITE_SPEECH_MODEL_URL=
ARG GRANITE_SPEECH_MODEL_SHA256=
ARG GRANITE_SPEECH_MMPROJ_URL=
ARG GRANITE_SPEECH_MMPROJ_SHA256=

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg python3 python3-pip tar \
    && rm -rf /var/lib/apt/lists/*

RUN if [ -n "$LLAMA_CPP_RELEASE_URL" ]; then \
        if [ -z "$LLAMA_CPP_RELEASE_SHA256" ]; then \
            echo "ERROR: LLAMA_CPP_RELEASE_SHA256 is required when downloading llama.cpp release."; \
            exit 1; \
        fi; \
        echo "Downloading pinned llama.cpp runtime..."; \
        curl -fL "$LLAMA_CPP_RELEASE_URL" -o /tmp/llama-cpp-release.tar.gz; \
        echo "$LLAMA_CPP_RELEASE_SHA256  /tmp/llama-cpp-release.tar.gz" | sha256sum -c -; \
        mkdir -p /tmp/llama-cpp-release; \
        tar -xzf /tmp/llama-cpp-release.tar.gz -C /tmp/llama-cpp-release --strip-components=1; \
        cp -a /tmp/llama-cpp-release/. /app/; \
        rm -rf /tmp/llama-cpp-release /tmp/llama-cpp-release.tar.gz; \
    fi

WORKDIR /app

COPY aux-ml/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY aux-ml/app /app/app
COPY aux-ml/config /app/config
COPY aux-ml/models /models
COPY aux-ml/entrypoint.sh /usr/local/bin/aux-ml-entrypoint.sh

RUN if [ "$ENABLE_GRANITE_SPEECH" != "true" ]; then \
        rm -f /models/granite-speech-4.1-2b-Q8_0.gguf /models/mmproj-granite-speech-4.1-2b-f16.gguf; \
    fi \
    && if [ ! -f /models/glm-ocr.gguf ]; then \
        if [ -z "$GLM_OCR_MODEL_URL" ]; then \
            echo "ERROR: Missing /models/glm-ocr.gguf and GLM_OCR_MODEL_URL build arg is empty."; \
            echo "Provide local file aux-ml/models/glm-ocr.gguf or set AUX_ML_GLM_OCR_URL in .env."; \
            exit 1; \
        fi; \
        if [ -z "$GLM_OCR_MODEL_SHA256" ]; then \
            echo "ERROR: GLM_OCR_MODEL_SHA256 is required when downloading GLM-OCR model."; \
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
        if [ -z "$GLM_OCR_MMPROJ_SHA256" ]; then \
            echo "ERROR: GLM_OCR_MMPROJ_SHA256 is required when downloading GLM-OCR mmproj."; \
            exit 1; \
        fi; \
        echo "Downloading GLM-OCR mmproj from build arg URL..."; \
        curl -fL "$GLM_OCR_MMPROJ_URL" -o /models/mmproj-glm-ocr.gguf; \
    fi \
    && if [ -n "$GLM_OCR_MMPROJ_SHA256" ]; then \
        echo "$GLM_OCR_MMPROJ_SHA256  /models/mmproj-glm-ocr.gguf" | sha256sum -c -; \
    fi \
    && if [ "$ENABLE_GRANITE_SPEECH" = "true" ]; then \
        if [ ! -f /models/granite-speech-4.1-2b-Q8_0.gguf ]; then \
            if [ -z "$GRANITE_SPEECH_MODEL_URL" ]; then \
                echo "ERROR: Missing /models/granite-speech-4.1-2b-Q8_0.gguf and GRANITE_SPEECH_MODEL_URL build arg is empty."; \
                echo "Provide local file aux-ml/models/granite-speech-4.1-2b-Q8_0.gguf or set AUX_ML_GRANITE_SPEECH_URL in .env."; \
                exit 1; \
            fi; \
            if [ -z "$GRANITE_SPEECH_MODEL_SHA256" ]; then \
                echo "ERROR: GRANITE_SPEECH_MODEL_SHA256 is required when downloading Granite Speech model."; \
                exit 1; \
            fi; \
            echo "Downloading Granite Speech model from build arg URL..."; \
            curl -fL "$GRANITE_SPEECH_MODEL_URL" -o /models/granite-speech-4.1-2b-Q8_0.gguf; \
        fi; \
        if [ -n "$GRANITE_SPEECH_MODEL_SHA256" ]; then \
            echo "$GRANITE_SPEECH_MODEL_SHA256  /models/granite-speech-4.1-2b-Q8_0.gguf" | sha256sum -c -; \
        fi; \
        if [ ! -f /models/mmproj-granite-speech-4.1-2b-f16.gguf ]; then \
            if [ -z "$GRANITE_SPEECH_MMPROJ_URL" ]; then \
                echo "ERROR: Missing /models/mmproj-granite-speech-4.1-2b-f16.gguf and GRANITE_SPEECH_MMPROJ_URL build arg is empty."; \
                echo "Provide local file aux-ml/models/mmproj-granite-speech-4.1-2b-f16.gguf or set AUX_ML_GRANITE_SPEECH_MMPROJ_URL in .env."; \
                exit 1; \
            fi; \
            if [ -z "$GRANITE_SPEECH_MMPROJ_SHA256" ]; then \
                echo "ERROR: GRANITE_SPEECH_MMPROJ_SHA256 is required when downloading Granite Speech mmproj."; \
                exit 1; \
            fi; \
            echo "Downloading Granite Speech mmproj from build arg URL..."; \
            curl -fL "$GRANITE_SPEECH_MMPROJ_URL" -o /models/mmproj-granite-speech-4.1-2b-f16.gguf; \
        fi; \
        if [ -n "$GRANITE_SPEECH_MMPROJ_SHA256" ]; then \
            echo "$GRANITE_SPEECH_MMPROJ_SHA256  /models/mmproj-granite-speech-4.1-2b-f16.gguf" | sha256sum -c -; \
        fi; \
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
    AUX_ML_ALLOWED_INPUT_DIRS=/opt/data/workspace \
    AUX_ML_ENFORCE_MEMORY_LIMIT=true \
    AUX_ML_MEMORY_LIMIT_MB=8192 \
    AUX_ML_OCR_MAX_PAGES=50 \
    LLAMA_ROUTER_HOST=127.0.0.1 \
    LLAMA_ROUTER_PORT=8080 \
    LLAMA_MODELS_DIR=/models \
    LLAMA_PARALLEL=1 \
    LLAMA_CONT_BATCHING=false

ENTRYPOINT ["aux-ml-entrypoint.sh"]
