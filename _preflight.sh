#!/usr/bin/env bash
sleep 4
pgrep -af 'src/gator_bridge.py'
curl -s -o /dev/null -w 'HEALTH=%{http_code}\n' http://127.0.0.1:8090/health
curl -s -o /dev/null -w 'GEN=%{http_code}\n' -X POST http://127.0.0.1:8090/generate -H 'Content-Type: application/json' -d '{"prompt":"hello","max_tokens":60}'
