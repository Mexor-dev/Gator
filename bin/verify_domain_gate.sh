#!/usr/bin/env bash
set -e
cd /home/user/Gator
source venv/bin/activate

python3 - <<'PYEOF'
import sys
sys.path.insert(0, 'src')
from core.validation import HardwareValidator, DOMAIN_DRIFT_RETRY_PROMPT

v = HardwareValidator()

# Should fail: domain drift
r = v.validate_text("Use Node.js and Express to build your REST API")
assert not r["ok"], "Expected domain_drift failure"
assert r["failures"][0]["type"] == "domain_drift", f"Wrong type: {r['failures'][0]['type']}"
assert "Node.js" in r["retry_prompt"] or "domain" in r["retry_prompt"].lower(), f"Bad retry_prompt: {r['retry_prompt']}"
print(f"  [PASS] domain_drift caught: term='{r['failures'][0]['value']}'")
print(f"         retry_prompt: {r['retry_prompt'][:80]}")

# Should also fail: npm mention
r2 = v.validate_text("Run npm install and start the server")
assert not r2["ok"], "Expected npm drift failure"
assert r2["failures"][0]["type"] == "domain_drift"
print(f"  [PASS] npm drift caught: term='{r2['failures'][0]['value']}'")

# Should pass: clean C++/CUDA text
r3 = v.validate_text("The CUDA kernel allocates 128-bit memory using RTX-Direct VRAM.")
assert r3["ok"], f"Expected pass but got failures: {r3['failures']}"
print("  [PASS] clean C++/CUDA text passes validator")

# Verify SYSTEM_IDENTITY constant
import importlib.util
spec = importlib.util.spec_from_file_location("gator_bridge", "src/gator_bridge.py")
# Just read the file and grep for SYSTEM_IDENTITY
with open("src/gator_bridge.py") as f:
    src = f.read()
assert 'SYSTEM_IDENTITY = "cpp_rtx_direct"' in src, "SYSTEM_IDENTITY constant missing"
assert "C++/RTX-Direct" in src, "C++/RTX-Direct identity missing from persona"
assert "NEVER reference Node.js" in src, "Node.js block missing from persona"
print("  [PASS] SYSTEM_IDENTITY + SYSTEM_PERSONA_PROMPT verified in gator_bridge.py")

# Verify session_reset() method
assert "def session_reset(self)" in src, "session_reset() missing from bridge"
assert "scholar_sense_retained" in src, "session_reset missing scholar_sense_retained key"
assert '"/api/session_reset"' in src, "/api/session_reset endpoint missing"
print("  [PASS] session_reset() + /api/session_reset endpoint present")

# Verify mitosis clone env
with open("src/core/mitosis.py") as f:
    mit = f.read()
assert 'GATOR_SYSTEM_IDENTITY' in mit, "GATOR_SYSTEM_IDENTITY env var missing from mitosis"
assert '"system_identity": "cpp_rtx_direct"' in mit, "system_identity missing from clone config"
print("  [PASS] mitosis injects GATOR_SYSTEM_IDENTITY + system_identity in sandbox config")

# Verify gator_map identity_constraint
with open("src/core/gator_map.py") as f:
    gmap = f.read()
assert '"identity_constraint"' in gmap, "identity_constraint missing from GatorMap blueprint"
assert '"donor_bound": True' in gmap, "donor_bound missing from GatorMap"
assert '"cpp_rtx_direct"' in gmap, "cpp_rtx_direct missing from GatorMap"
print("  [PASS] GatorMap blueprint includes identity_constraint with donor_bound")

print()
print("=== ALL DOMAIN GATE + CONTEXT ISOLATION CHECKS PASSED ===")
PYEOF
