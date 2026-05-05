#!/usr/bin/env bash
set -euo pipefail
cd /home/user/Gator
PORT=$(python3 - <<'PY'
import json
from pathlib import Path
p = Path('/home/user/Gator/bin/hive_state.json')
data = json.loads(p.read_text()) if p.exists() else {}
node = (data.get('clones') or {}).get('gator-check') or {}
print(int(node.get('bridge_port') or 8100))
PY
)
printf '%s' '{"prompt":"Could this GPU be a 24-bit bus at 3.6GHz memory?","max_tokens":220,"temperature":0.2,"top_k":40}' > /tmp/gator_unit_query.json
curl -sS "http://127.0.0.1:${PORT}/generate" -H "Content-Type: application/json" --data-binary @/tmp/gator_unit_query.json
