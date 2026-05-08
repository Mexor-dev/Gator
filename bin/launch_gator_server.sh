#!/usr/bin/env bash
set -euo pipefail

GATOR_ROOT="${GATOR_ROOT:-$HOME/Gator}"
SERVER_BIN="${GATOR_SERVER_BIN:-$GATOR_ROOT/bin/gator-server}"
export LD_LIBRARY_PATH="$GATOR_ROOT/lib:${LD_LIBRARY_PATH:-}"
PORT="${GATOR_SERVER_PORT:-8081}"
HOST="${GATOR_SERVER_HOST:-127.0.0.1}"

# Architecture (May 2026): single-engine. The 35B donor.gguf has been
# decommissioned to models/_decommissioned/. The 1.5B chassis is the only
# runtime model. Intelligence is grafted via:
#   1. bin/logic_map.gate  -> live llama-server logit_bias on every token
#      (installed by GatorBridge -> InferenceEngine.install_logit_bias)
#   2. src/inference/libgator_kern.so -> native ctypes runtime, loaded by
#      InferenceEngine for the deterministic-fallback sampling path and the
#      kernel singleton handshake.
MOUTH_MODEL="${GATOR_MOUTH_MODEL:-$GATOR_ROOT/models/chassis.gguf}"
MODEL="$MOUTH_MODEL"
ALIAS="gator-mouth"
CTX="${GATOR_MOUTH_CTX:-8192}"
GPU_LAYERS="${GATOR_MOUTH_GPU_LAYERS:-999}"
BATCH="${GATOR_MOUTH_BATCH:-1024}"

if [[ ! -x "$SERVER_BIN" ]]; then
  echo "[FATAL] gator-server missing or not executable: $SERVER_BIN"
  exit 1
fi
if [[ ! -f "$MODEL" ]]; then
  echo "[FATAL] chassis model missing: $MODEL"
  exit 1
fi
if [[ ! -f "$GATOR_ROOT/bin/logic_map.gate" ]]; then
  echo "[FATAL] logic_map.gate missing at $GATOR_ROOT/bin/logic_map.gate"
  exit 1
fi
if [[ ! -f "$GATOR_ROOT/src/inference/libgator_kern.so" ]]; then
  echo "[FATAL] libgator_kern.so not built at $GATOR_ROOT/src/inference/libgator_kern.so"
  exit 1
fi

# Optional graft hooks for future LoRA / control-vector files. Empty by default.
GRAFT_LORA="${GATOR_GRAFT_LORA:-}"
GRAFT_CV="${GATOR_GRAFT_CONTROL_VECTOR:-}"
GRAFT_SCALE="${GATOR_GRAFT_SCALE:-1.0}"
GRAFT_ARGS=()
if [[ -n "$GRAFT_LORA" && -f "$GRAFT_LORA" ]]; then
  GRAFT_ARGS+=( --lora-scaled "${GRAFT_LORA}:${GRAFT_SCALE}" )
  echo "[GRAFT] LoRA adapter armed: $GRAFT_LORA (scale=$GRAFT_SCALE)"
fi
if [[ -n "$GRAFT_CV" && -f "$GRAFT_CV" ]]; then
  GRAFT_ARGS+=( --control-vector-scaled "${GRAFT_CV}:${GRAFT_SCALE}" )
  echo "[GRAFT] Control vector armed: $GRAFT_CV (scale=$GRAFT_SCALE)"
fi

echo "[GRAFT] Primary steering: bin/logic_map.gate via bridge-side logit_bias"
echo "[KERNEL] Native runtime: src/inference/libgator_kern.so (loaded by bridge)"

exec "$SERVER_BIN" \
  --host "$HOST" \
  --port "$PORT" \
  --model "$MODEL" \
  --alias "$ALIAS" \
  --ctx-size "$CTX" \
  --batch-size "$BATCH" \
  --n-gpu-layers "$GPU_LAYERS" \
  --threads "${GATOR_SERVER_THREADS:-8}" \
  "${GRAFT_ARGS[@]}"

