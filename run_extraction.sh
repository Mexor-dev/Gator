#!/usr/bin/env bash
# Project Gator — Extraction launcher
# Activates the Gator venv and runs the donor logit extraction pipeline.
# Usage: bash ~/Gator/run_extraction.sh [extra args passed to extract_logic.py]

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

# Activate and run
source "$VENV/bin/activate"
echo "=== Gator Extraction — $(date) ==="
echo "=== Python: $(python --version) ==="
echo "=== GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'no nvidia-smi') ==="
echo "=== RAM: $(free -h | awk '/Mem:/{print $4" free / "$2" total"}') ==="
echo ""

exec python "$EXTRACT" "$@"
