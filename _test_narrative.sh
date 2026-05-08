#!/usr/bin/env bash
set -u
cd /home/user/Gator
bash _restart_bridge.sh 2>&1 | tail -5
sleep 4

run_test() {
  local label="$1"
  local prompt="$2"
  echo "=== ${label} ==="
  curl -s -X POST http://127.0.0.1:8090/generate \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"prompt":sys.argv[1],"max_tokens":250}))' "$prompt")" \
    | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)
  print(d.get("text", d))
except Exception as e:
  print("PARSE_ERR", e)'
  echo
}

run_test TEST_NARRATIVE_STORY "write a short story about a fox crossing a frozen river"
run_test TEST_NARRATIVE_POEM  "compose a haiku about thunder"
run_test TEST_EXECUTE         "summarize the iron-gator validation suite"
run_test TEST_WHATTASK        "What task?"
run_test TEST_GREETING        "hello"

echo "=== RECENT_DEBUG ==="
tail -n 80 /home/user/Gator/logs/bridge.log 2>/dev/null | grep -E "intent|narrative|SanityCheck|StutterCheck" | tail -20
echo "=== DONE ==="
