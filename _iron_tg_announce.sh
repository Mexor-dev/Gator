#!/usr/bin/env bash
set -u
cd /home/user/Gator
set -a
. ./.env
set +a
RESP=$(curl -sS -X POST "https://api.telegram.org/bot${GATOR_TG_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${GATOR_TG_AUTH_CHAT_ID}" \
  --data-urlencode 'text=[Gator-Prime] Iron-Gator hardening complete. Hive gateway Awake. All Systems Nominal.')
echo "$RESP" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('telegram_ok=', d.get('ok'), 'message_id=', (d.get('result') or {}).get('message_id'), 'desc=', d.get('description'))"
