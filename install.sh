#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$ROOT/.env"

if [[ ! -x "$ROOT/bootstrap.sh" && ! -f "$ROOT/bootstrap.sh" ]]; then
  echo "[FATAL] bootstrap.sh missing at $ROOT/bootstrap.sh"
  exit 1
fi

echo "[INFO] running bootstrap pipeline (models + kernel + logic gate + scrub + wakeup validation)"
bash "$ROOT/bootstrap.sh"

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

echo "[OK] Project Gator installed via bootstrap pipeline"
