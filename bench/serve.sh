#!/bin/bash
# serve.sh NAME MODEL MMPROJ [extra args...]  -> llama-server on :8091, waits for health
name=$1 model=$2 mmproj=$3; shift 3
pid=$(pgrep -x llama-server); [ -n "$pid" ] && kill $pid && while pgrep -x llama-server >/dev/null; do sleep 0.5; done
nohup "${LLAMA_SERVER:-$(command -v llama-server || echo ~/llama.cpp/build/bin/llama-server)}" -m "$model" --mmproj "$mmproj" --port 8091 -c 16384 -np 1 -fa on \
  -ctk q8_0 -ctv q8_0 --fit-target ${FIT:-1024} --image-min-tokens 1024 "$@" > ~/cua/bench/server-$name.log 2>&1 &
for i in $(seq 90); do curl -sf localhost:8091/health >/dev/null && break; sleep 2; done
curl -sf localhost:8091/health && echo " up" || { echo DOWN; tail -20 ~/cua/bench/server-$name.log; }
nvidia-smi --query-gpu=memory.used --format=csv,noheader
