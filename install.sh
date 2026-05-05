#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/venv"
REQ="$ROOT/requirements.txt"
BUILD_DIR="$ROOT/build"
ENV_FILE="$ROOT/.env"

detect_backend() {
  if command -v nvidia-smi >/dev/null 2>&1; then
    echo cuda
    return
  fi
  if command -v rocminfo >/dev/null 2>&1; then
    echo rocm
    return
  fi
  if command -v sycl-ls >/dev/null 2>&1; then
    echo oneapi
    return
  fi
  echo cpu
}

BACKEND="$(detect_backend)"
echo "[INFO] backend=$BACKEND"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[FATAL] python3 is required"
  exit 1
fi

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/pip" install -r "$REQ"
"$VENV/bin/pip" uninstall -y llama-cpp-python >/dev/null 2>&1 || true
rm -f "$ROOT/bin/llama-server" "$ROOT/bin/llama-cli" 2>/dev/null || true

if [[ -z "${GATOR_TG_BOT_TOKEN:-}" ]] || [[ -z "${GATOR_TG_AUTH_CHAT_ID:-}" ]]; then
  echo "[INFO] Telegram hive gateway can be configured now or later via .env"
  if [[ -t 0 ]]; then
    read -r -p "Telegram Bot Token (optional, press Enter to skip): " _tg_token || true
    read -r -p "Telegram Authorized Chat ID (optional, press Enter to skip): " _tg_chat || true
    if [[ -n "${_tg_token:-}" ]]; then
      export GATOR_TG_BOT_TOKEN="$_tg_token"
    fi
    if [[ -n "${_tg_chat:-}" ]]; then
      export GATOR_TG_AUTH_CHAT_ID="$_tg_chat"
    fi
  fi
fi

touch "$ENV_FILE"
if [[ -n "${GATOR_TG_BOT_TOKEN:-}" ]]; then
  grep -q '^GATOR_TG_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null && sed -i "s|^GATOR_TG_BOT_TOKEN=.*|GATOR_TG_BOT_TOKEN=${GATOR_TG_BOT_TOKEN}|" "$ENV_FILE" || echo "GATOR_TG_BOT_TOKEN=${GATOR_TG_BOT_TOKEN}" >> "$ENV_FILE"
fi
if [[ -n "${GATOR_TG_AUTH_CHAT_ID:-}" ]]; then
  grep -q '^GATOR_TG_AUTH_CHAT_ID=' "$ENV_FILE" 2>/dev/null && sed -i "s|^GATOR_TG_AUTH_CHAT_ID=.*|GATOR_TG_AUTH_CHAT_ID=${GATOR_TG_AUTH_CHAT_ID}|" "$ENV_FILE" || echo "GATOR_TG_AUTH_CHAT_ID=${GATOR_TG_AUTH_CHAT_ID}" >> "$ENV_FILE"
fi

cmake -S "$ROOT" -B "$BUILD_DIR"
cmake --build "$BUILD_DIR" --target gator_kern

if [[ -f "$BUILD_DIR/src/inference/libgator_kern.so" ]]; then
  cp "$BUILD_DIR/src/inference/libgator_kern.so" "$ROOT/src/inference/libgator_kern.so"
elif [[ -f "$BUILD_DIR/libgator_kern.so" ]]; then
  cp "$BUILD_DIR/libgator_kern.so" "$ROOT/src/inference/libgator_kern.so"
fi

"$VENV/bin/python" "$ROOT/src/core/gator_map.py" --snapshot --reason install_bootstrap

GATOR_DAEMON=true "$ROOT/wakeup"

echo "[OK] Project Gator installed and wakeup sequence started"
