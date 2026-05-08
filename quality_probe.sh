#!/usr/bin/env bash
set -euo pipefail

echo "=== LLAMA SERVER ==="
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/health || true)
echo "llama_health_http=$code"

echo "=== BRIDGE OUTPUT 1 ==="
curl -s -X POST http://127.0.0.1:8090/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Design a 3-step plan to reduce API latency under p95 300ms"}'

echo
echo "=== BRIDGE OUTPUT 2 ==="
curl -s -X POST http://127.0.0.1:8090/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"What is my name?"}'

echo
echo "=== BRIDGE OUTPUT 3 ==="
curl -s -X POST http://127.0.0.1:8090/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain why JWT rotation reduces replay risk in 4 bullets"}'
