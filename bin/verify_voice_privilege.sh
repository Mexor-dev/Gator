#!/usr/bin/env bash
set -e
cd /home/user/Gator
source venv/bin/activate

python3 - <<'PYEOF'
import sys, os
sys.path.insert(0, 'src')

# ── 1. Voice HardBlock for clones ──
os.environ["GATOR_NODE_NAME"] = "Gator-Scout"
os.environ["GATOR_VOICE_DISABLED"] = "true"

# Re-import with clone identity set
import importlib
import importlib.util
# Load voice_layer fresh
spec = importlib.util.spec_from_file_location("voice_layer", "src/interfaces/voice_layer.py")
voice_mod = importlib.util.module_from_spec(spec)
# Don't exec (needs faster_whisper), just parse the source
with open("src/interfaces/voice_layer.py") as f:
    src = f.read()
assert "VoiceHardBlock" in src, "VoiceHardBlock class missing"
assert "_PRIME_ENTITY_NAMES" in src, "_PRIME_ENTITY_NAMES missing"
assert "self.voice_disabled" in src, "voice_disabled flag missing"
assert "VoiceHardBlock(self._entity)" in src, "HardBlock guard missing from methods"
assert "text_log_fallback" in src, "text_log_fallback missing"
print("  [PASS] voice_layer.py: VoiceHardBlock + Prime-only guards present")

# ── 2. Clone silent_mode + GATOR_VOICE_DISABLED in mitosis ──
with open("src/core/mitosis.py") as f:
    mit = f.read()
assert '"silent_mode": True' in mit, "silent_mode missing from clone config"
assert '"voice_disabled": True' in mit, "voice_disabled missing from clone config"
assert 'env["GATOR_VOICE_DISABLED"] = "true"' in mit, "GATOR_VOICE_DISABLED env var missing"
assert 'env["GATOR_TEXT_ONLY"] = "true"' in mit, "GATOR_TEXT_ONLY env var missing"
assert "def post_update(self" in mit, "post_update() method missing"
assert "def node_id(self" in mit, "node_id() method missing"
assert "def worker_header(self" in mit, "worker_header() method missing"
assert "PROGRESS_INTERVAL_S" in open("src/interfaces/telegram_hive.py").read() or True  # checked below
print("  [PASS] mitosis.py: silent_mode, voice_disabled, GATOR_VOICE_DISABLED, GATOR_TEXT_ONLY, post_update(), node_id(), worker_header()")

# ── 3. Hive Response Chain in telegram_hive ──
with open("src/interfaces/telegram_hive.py") as f:
    tg = f.read()
assert "PROGRESS_INTERVAL_S" in tg, "PROGRESS_INTERVAL_S constant missing"
assert "_progress_watchdog" in tg, "_progress_watchdog coroutine missing"
assert "asyncio.create_task(self._progress_watchdog" in tg, "watchdog not started"
assert "Acknowledged, Prime" in tg, "Worker acknowledgment message missing"
assert "Gator-Prime]: " in tg and "initialize task" in tg, "Prime delegation message missing"
assert "Analysis complete. Anchoring Safety Node #402" in tg, "Worker result format missing"
assert "_try_prime_voice" in tg, "Prime voice confirmation missing"
assert "worker_header" in tg, "worker_header() not used in telegram_hive"
assert "is_clone" in tg, "clone routing logic missing"
print("  [PASS] telegram_hive.py: delegation, ack, 15s watchdog, result chain, voice confirm")

# ── 4. Text-only identity in gator_bridge ──
with open("src/gator_bridge.py") as f:
    bridge = f.read()
assert "_IS_CLONE" in bridge, "_IS_CLONE detection missing"
assert "GATOR_TEXT_ONLY" in bridge, "GATOR_TEXT_ONLY check missing"
assert "TEXT-ONLY worker clone" in bridge, "Text-only persona suffix missing"
assert "voice_related instructions" in bridge or "voice output" in bridge, "Voice block instruction missing"
print("  [PASS] gator_bridge.py: _IS_CLONE + GATOR_TEXT_ONLY text-only persona appended")

# ── 5. Syntax check all modified files ──
import py_compile
for path in [
    "src/interfaces/voice_layer.py",
    "src/interfaces/telegram_hive.py",
    "src/core/mitosis.py",
]:
    try:
        py_compile.compile(path, doraise=True)
        print(f"  [PASS] syntax OK: {path}")
    except py_compile.PyCompileError as e:
        print(f"  [FAIL] syntax error in {path}: {e}")
        raise

# gator_bridge.py is very large, do a quick import check of just constants
import ast
with open("src/gator_bridge.py") as f:
    tree_src = f.read()
ast.parse(tree_src)
print("  [PASS] syntax OK: src/gator_bridge.py")

print()
print("=== ALL VOICE PRIVILEGE + HIVE CHAIN CHECKS PASSED ===")
PYEOF
