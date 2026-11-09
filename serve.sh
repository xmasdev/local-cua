#!/bin/bash
# Start the llama.cpp router with every model in $CUA_MODELS_DIR (one model loaded at a time).
LLAMA_SERVER=${LLAMA_SERVER:-$(command -v llama-server || echo ~/llama.cpp/build/bin/llama-server)}
MODELS_DIR=${CUA_MODELS_DIR:-~/models/llama.cpp}
PRESET=${CUA_MODELS_PRESET:-$(dirname "$(readlink -f "$0")")/config/models.ini}
exec "$LLAMA_SERVER" --models-dir "$MODELS_DIR" --models-preset "$PRESET" --models-max 1 --port 8080 "$@"
