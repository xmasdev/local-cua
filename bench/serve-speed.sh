#!/bin/bash
# serve-speed.sh NAME [extra llama-server args]  - Holo-4B with the production settings on :8091
name=$1; shift
M=${CUA_MODELS_DIR:-~/models/llama.cpp}/Holo-3.1-4B
LLAMA_SERVER=${LLAMA_SERVER:-$(command -v llama-server || echo ~/llama.cpp/build/bin/llama-server)}
pid=$(pgrep -x llama-server); [ -n "$pid" ] && kill $pid && while pgrep -x llama-server >/dev/null; do sleep 0.5; done
nohup "$LLAMA_SERVER" -m $M/Holo-3.1-4B.Q5_K_M.gguf --mmproj $M/mmproj-Q8_0.gguf --port 8091 -c 32768 -np 2 \
  -fa on -ctk q8_0 -ctv q8_0 --fit-target 200 --image-min-tokens 1024 --image-max-tokens 2304 "$@" \
  > ~/cua/bench/server-speed-$name.log 2>&1 &
for i in $(seq 90); do curl -sf localhost:8091/health >/dev/null && break; sleep 1; done
curl -sf localhost:8091/health >/dev/null && echo "up ($(nvidia-smi --query-gpu=memory.used --format=csv,noheader))" || { echo DOWN; tail -15 ~/cua/bench/server-speed-$name.log; exit 1; }
