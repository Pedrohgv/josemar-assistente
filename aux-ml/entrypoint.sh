#!/bin/sh

set -eu

LLAMA_ROUTER_HOST="${LLAMA_ROUTER_HOST:-127.0.0.1}"
LLAMA_ROUTER_PORT="${LLAMA_ROUTER_PORT:-8080}"
LLAMA_MODELS_DIR="${LLAMA_MODELS_DIR:-/models}"
LLAMA_MODELS_PRESET="${LLAMA_MODELS_PRESET:-/app/config/models-preset.ini}"
LLAMA_PARALLEL="${LLAMA_PARALLEL:-1}"
LLAMA_CONT_BATCHING="${LLAMA_CONT_BATCHING:-false}"
LLAMA_MMPROJ_PATH="${LLAMA_MMPROJ_PATH:-/models/mmproj-glm-ocr.gguf}"

AUX_ML_BIND_HOST="${AUX_ML_BIND_HOST:-0.0.0.0}"
AUX_ML_PORT="${AUX_ML_PORT:-8091}"

if [ "$LLAMA_CONT_BATCHING" = "true" ]; then
    CONT_BATCHING_FLAG="--cont-batching"
else
    CONT_BATCHING_FLAG="--no-cont-batching"
fi

echo "Starting llama-server router on ${LLAMA_ROUTER_HOST}:${LLAMA_ROUTER_PORT}..."
if [ -f "$LLAMA_MODELS_PRESET" ]; then
    echo "Using model preset file: ${LLAMA_MODELS_PRESET}"
    /app/llama-server \
        --host "$LLAMA_ROUTER_HOST" \
        --port "$LLAMA_ROUTER_PORT" \
        --models-preset "$LLAMA_MODELS_PRESET" \
        --parallel "$LLAMA_PARALLEL" \
        "$CONT_BATCHING_FLAG" &
else
    if [ -f "$LLAMA_MMPROJ_PATH" ]; then
        echo "Detected multimodal projector: ${LLAMA_MMPROJ_PATH}"
    fi
    /app/llama-server \
        --host "$LLAMA_ROUTER_HOST" \
        --port "$LLAMA_ROUTER_PORT" \
        --models-dir "$LLAMA_MODELS_DIR" \
        --parallel "$LLAMA_PARALLEL" \
        "$CONT_BATCHING_FLAG" &
fi
LLAMA_PID=$!

echo "Starting aux-ml orchestrator on ${AUX_ML_BIND_HOST}:${AUX_ML_PORT}..."
uvicorn app.main:app --host "$AUX_ML_BIND_HOST" --port "$AUX_ML_PORT" &
API_PID=$!

cleanup() {
    kill "$API_PID" "$LLAMA_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
    wait "$LLAMA_PID" 2>/dev/null || true
}

trap cleanup INT TERM

EXIT_CODE=1
while :; do
    if ! kill -0 "$LLAMA_PID" 2>/dev/null; then
        set +e
        wait "$LLAMA_PID"
        EXIT_CODE=$?
        set -e
        break
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        set +e
        wait "$API_PID"
        EXIT_CODE=$?
        set -e
        break
    fi
    sleep 1
done

cleanup
exit "$EXIT_CODE"
