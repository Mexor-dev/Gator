#!/usr/bin/env bash
set -uo pipefail

cd /home/user/Gator

for i in 1 2 3; do
  echo "=== CYCLE $i START ==="
  GATOR_DAEMON=true bash wakeup >/tmp/gator_wakeup_${i}.log 2>&1
  wrc=$?
  bash /home/user/verify_gateway.sh >/tmp/gator_verify_${i}.log 2>&1
  vrc=$?

  health="$(curl -s http://127.0.0.1:8090/health | tr -d '\n')"
  gen_json="$(curl -s -X POST http://127.0.0.1:8090/generate -H 'Content-Type: application/json' -d '{"prompt":"Give me three concrete steps to reduce API latency"}')"
  gen_text="$(printf '%s' "$gen_json" | sed -n 's/.*"text":"\([^"]*\)".*/\1/p' | head -1)"

  echo "wakeup_exit=$wrc"
  echo "verify_exit=$vrc"
  echo "health=$health"
  echo "gen_preview=${gen_text:0:220}"
  echo "=== CYCLE $i END ==="
done
