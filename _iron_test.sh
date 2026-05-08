#!/usr/bin/env bash
set -u
cd /home/user/Gator
sleep 2
echo "=== GENERATE TEST ==="
curl -sS -X POST http://127.0.0.1:8090/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"say hi briefly","max_tokens":24,"temperature":0.5}' \
  -w '\nHTTP %{http_code}\n' 2>&1 | tail -40
echo
echo "=== VITALS BEFORE TG ==="
curl -sS http://127.0.0.1:8080/api/vitals -w '\nHTTP %{http_code}\n' 2>&1 | head -20
echo
echo "=== TG RESTART ==="
curl -sS -X POST http://127.0.0.1:8080/api/telegram/restart -w '\nHTTP %{http_code}\n'
echo
sleep 6
echo "=== TG STATUS FILE ==="
cat logs/telegram_hive_status.json 2>&1
echo
echo "=== TG LOG TAIL ==="
tail -n 30 logs/telegram_hive.log 2>&1
echo
echo "=== TG PID ALIVE ==="
TGPID=$(cat bin/telegram_gateway.pid 2>/dev/null || true)
echo "pid_file=$TGPID"
ps -p "$TGPID" -o pid,cmd 2>&1 || true
