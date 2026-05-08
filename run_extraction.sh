#!/usr/bin/env bash
# Project Gator — Deep Extraction launcher
#
# Spins up the decommissioned 32B donor on its own port, streams the
# calibration corpus through it, harvests top-K logprobs per token,
# and emits bin/logic_map.gate. Donor process is killed on completion.
#
# Usage:
#   bash ~/Gator/run_extraction.sh                 # full 5k+ record run
#   EXTRACT_TARGET=500 bash ~/Gator/run_extraction.sh   # smaller run
#   bash ~/Gator/run_extraction.sh --max-prompts 4      # smoke test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"
EXTRACT="$SCRIPT_DIR/src/extract_logic.py"

if [[ ! -f "$VENV/bin/python" ]]; then
    echo "[ERROR] Venv not found at $VENV"
    echo "        Run: python3 ~/Gator/setup_env.py"
    exit 1
fi

if [[ ! -f "$EXTRACT" ]]; then
    echo "[ERROR] extract_logic.py not found at $EXTRACT"
    exit 1
fi

# Tunables (override via env). Defaults are calibrated for the
# decommissioned 32B donor (14GB Q3) on a 6GB-VRAM RTX 3050:
#   GPU_LAYERS=14   -> fits the 6GB budget; remainder spills to CPU.
#   CTX=2048        -> enough for 256-char hash window + 22 generated tokens.
#   TOP_K=64        -> matches the gate format the bridge expects.
#   TARGET=5000     -> master-prompt directive.
export EXTRACT_GPU_LAYERS="${EXTRACT_GPU_LAYERS:-14}"
export EXTRACT_CTX="${EXTRACT_CTX:-2048}"
export EXTRACT_TOP_K="${EXTRACT_TOP_K:-64}"
export EXTRACT_TOKENS_PER_PROMPT="${EXTRACT_TOKENS_PER_PROMPT:-22}"
export EXTRACT_TARGET="${EXTRACT_TARGET:-5000}"
export EXTRACT_DONOR_PORT="${EXTRACT_DONOR_PORT:-8181}"
export EXTRACT_HEALTH_TIMEOUT="${EXTRACT_HEALTH_TIMEOUT:-300}"

source "$VENV/bin/activate"
echo "=== Gator Deep Extraction — $(date) ==="
echo "=== Python: $(python --version) ==="
echo "=== GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'no nvidia-smi') ==="
echo "=== Target records: $EXTRACT_TARGET   tokens/prompt: $EXTRACT_TOKENS_PER_PROMPT   top_k: $EXTRACT_TOP_K ==="
echo ""

exec python "$EXTRACT" "$@"
