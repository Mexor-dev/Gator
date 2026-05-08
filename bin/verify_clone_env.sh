#!/usr/bin/env bash
set -euo pipefail
cd /home/user/Gator
PID=$(python3 - <<'PY'
import json
from pathlib import Path
p = Path('/home/user/Gator/bin/hive_state.json')
data = json.loads(p.read_text()) if p.exists() else {}
print(int((data.get('clones') or {}).get('gator-check', {}).get('pid') or 0))
PY
)
if [[ "$PID" -le 0 ]]; then
  echo "missing_pid"
  exit 1
fi
tr '\0' '\n' < "/proc/${PID}/environ" | grep 'GATOR_VERIFIED_SPECS='
tr '\0' '\n' < "/proc/${PID}/environ" | grep 'GATOR_GUARD_ENFORCED='
tr '\0' '\n' < "/proc/${PID}/environ" | grep 'GATOR_SHARED_DB='
