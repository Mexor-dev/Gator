#!/usr/bin/env bash
set -u
cd /home/user/Gator
pkill -f telegram_hive.py 2>/dev/null || true
sleep 1
nohup ./venv/bin/python src/interfaces/telegram_hive.py >> logs/telegram_hive.log 2>&1 &
TG_PID=$!
echo "$TG_PID" > bin/telegram_gateway.pid
sleep 7
echo "PID=$TG_PID"
ps -p "$TG_PID" -o pid,cmd 2>&1 || true
echo "---LOG---"
tail -n 30 logs/telegram_hive.log
echo "---STATUS---"
cat logs/telegram_hive_status.json 2>&1
echo
echo "---VITALS TG---"
curl -sS http://127.0.0.1:8080/api/vitals 2>&1 | python3 -c "import json,sys;d=json.load(sys.stdin);print(json.dumps(d.get('telegram'),indent=2))"
