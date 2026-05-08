#!/bin/bash
sleep 2
curl -s http://127.0.0.1:8080/api/health
echo
curl -s http://127.0.0.1:8080/api/vitals > /tmp/vitals_check.json
python3 - <<'EOF'
import json
d = json.load(open("/tmp/vitals_check.json"))
print("STATUS:", d["status"])
c = d["canary"]
print("NATIVE:", c.get("native_mode"))
print("DONOR:", c.get("donor_addr"))
print("RESPONSE:", c.get("response_preview", "")[:80])
EOF
