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
cat > /tmp/gator_check_query.json <<'JSON'
{"prompt":"What is the memory bus of this machine?","max_tokens":180,"temperature":0.2,"top_k":40}
JSON
curl -sS "http://127.0.0.1:${PORT}/generate" -H "Content-Type: application/json" --data-binary @/tmp/gator_check_query.json
