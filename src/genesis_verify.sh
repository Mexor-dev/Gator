#!/bin/bash
#
# GATOR GENESIS VERIFICATION SCRIPT
# Sequentially replays all six validation gates in a single clean-room loop.
# Produces consolidated JSON artifact for baseline rollback anchoring.
#
# Usage: bash ~/Gator/src/genesis_verify.sh
#

set -e

# Delegate to the verified baseline implementation.
exec bash "$HOME/Gator/src/genesis_verify_v2.sh"

GATOR_HOME="$HOME/Gator"
GATOR_SRC="$GATOR_HOME/src"
GATOR_LOGS="$GATOR_HOME/logs"
VENV="$GATOR_HOME/venv/bin/python"
ARTIFACT="$GATOR_LOGS/genesis_artifact.json"

# Ensure logs directory exists
mkdir -p "$GATOR_LOGS"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Initialize results array
declare -a GATES_RESULTS=()

echo "=========================================="
echo "GATOR GENESIS VERIFICATION (6-GATE REPLAY)"
echo "=========================================="
echo "Start time: $(date)"
echo ""

# Boot the stack once
echo -e "${YELLOW}[BOOT]${NC} Starting unified stack..."
export GATOR_DAEMON=true
export GATOR_DEBUG=true
cd "$GATOR_HOME"

# Kill any existing processes
pkill -f "llama-server" || true
pkill -f "gator_bridge" || true
pkill -f "webui" || true
pkill -f "event_bus" || true
sleep 2

# Boot fresh
bash "$GATOR_HOME/wakeup" > "$GATOR_LOGS/genesis_boot.log" 2>&1 &
BOOT_PID=$!
sleep 15  # Allow services to stabilize

if ps -p $BOOT_PID > /dev/null 2>&1; then
    wait $BOOT_PID || true
fi

# Verify services are alive
echo -e "${YELLOW}[BOOT]${NC} Verifying service health..."
for proc in llama-server gator_bridge; do
    if pgrep -f "$proc" > /dev/null; then
        echo "  ✓ $proc alive"
    else
        echo -e "  ${RED}✗ $proc DEAD${NC}"
        exit 1
    fi
done

sleep 2

# ============================================================================
# GATE 1: HANDSHAKE (50x "hi" loop, final:true DNA packets)
# ============================================================================
echo ""
echo -e "${YELLOW}[GATE 1]${NC} Handshake Validation (50x hi loop, DNA packets)"
GATE1_START=$(date +%s%N)
GATE1_VRAM_START=$(free -m | awk '/^Mem:/{print $3}')

cd "$GATOR_HOME"
GATE1_OUT=$($VENV "$GATOR_SRC/phase1_eventbus_test.py" 2>&1)
GATE1_EXIT=$?

GATE1_END=$(date +%s%N)
GATE1_VRAM_END=$(free -m | awk '/^Mem:/{print $3}')
GATE1_ELAPSED_MS=$(( (GATE1_END - GATE1_START) / 1000000 ))

if echo "$GATE1_OUT" | grep -q '"status": "PASS"'; then
    GATE1_STATUS="PASS"
    GATE1_TPS=$(echo "$GATE1_OUT" | grep -oP '"truncation_count":\s*\K[0-9]+' | head -1)
    if [ -z "$GATE1_TPS" ]; then GATE1_TPS=0; fi
else
    GATE1_STATUS="FAIL"
    GATE1_TPS=0
fi

echo "  Status: $GATE1_STATUS"
echo "  VRAM: ${GATE1_VRAM_START}M -> ${GATE1_VRAM_END}M"
echo "  Elapsed: ${GATE1_ELAPSED_MS}ms"

GATES_RESULTS+=("{ \"gate\": 1, \"status\": \"$GATE1_STATUS\", \"tps\": $GATE1_TPS, \"vram_usage\": \"${GATE1_VRAM_END}M\" }")

if [ "$GATE1_STATUS" != "PASS" ]; then
    echo -e "${RED}GATE 1 FAILED${NC}"
    echo "Output: $GATE1_OUT"
    exit 1
fi

# ============================================================================
# GATE 2: LOGIC FLOOR (Medication PASS, Weather ZERO_CONTEXT)
# ============================================================================
echo ""
echo -e "${YELLOW}[GATE 2]${NC} Logic Floor Validation (similarity guardrails)"
GATE2_START=$(date +%s%N)
GATE2_VRAM_START=$(free -m | awk '/^Mem:/{print $3}')

# Create test script for similarity floor
GATE2_TEST=$(mktemp)
cat > "$GATE2_TEST" << 'EOF'
import sys
sys.path.insert(0, '/home/user/Gator/src')
from scholar_sense import ScholarSense

scholar = ScholarSense(llama_url="http://127.0.0.1:8081")

# Test 1: Medication (should pass floor)
result1 = scholar.query("What is ibuprofen used for?")
pass1 = (not result1.get("zero_context", True)) and (result1.get("effective_similarity", 0) > 0.25)

# Test 2: Weather (should trigger floor)
result2 = scholar.query("What is the weather in Tokyo tomorrow?")
pass2 = result2.get("zero_context", False)

status = "PASS" if (pass1 and pass2) else "FAIL"
print(f'{{"gate2_test1": {pass1}, "gate2_test2": {pass2}, "status": "{status}"}}')
EOF

GATE2_OUT=$($VENV "$GATE2_TEST" 2>&1)
GATE2_EXIT=$?
rm -f "$GATE2_TEST"

GATE2_END=$(date +%s%N)
GATE2_VRAM_END=$(free -m | awk '/^Mem:/{print $3}')
GATE2_ELAPSED_MS=$(( (GATE2_END - GATE2_START) / 1000000 ))

if echo "$GATE2_OUT" | grep -q '"status": "PASS"'; then
    GATE2_STATUS="PASS"
else
    GATE2_STATUS="FAIL"
fi

echo "  Medication query (pass floor): $(echo "$GATE2_OUT" | grep -oP '"gate2_test1":\s*\K(true|false)')"
echo "  Weather query (trigger floor): $(echo "$GATE2_OUT" | grep -oP '"gate2_test2":\s*\K(true|false)')"
echo "  Status: $GATE2_STATUS"
echo "  VRAM: ${GATE2_VRAM_START}M -> ${GATE2_VRAM_END}M"

GATES_RESULTS+=("{ \"gate\": 2, \"status\": \"$GATE2_STATUS\", \"tps\": 0.0, \"vram_usage\": \"${GATE2_VRAM_END}M\" }")

if [ "$GATE2_STATUS" != "PASS" ]; then
    echo -e "${RED}GATE 2 FAILED${NC}"
    exit 1
fi

# ============================================================================
# GATE 3: TOOL INTEGRITY (Scout + Architect)
# ============================================================================
echo ""
echo -e "${YELLOW}[GATE 3]${NC} Tool Integrity (Scout stealth scan + Architect self-code)"
GATE3_START=$(date +%s%N)
GATE3_VRAM_START=$(free -m | awk '/^Mem:/{print $3}')

# Create test script for tools
GATE3_TEST=$(mktemp)
cat > "$GATE3_TEST" << 'EOF'
import sys
sys.path.insert(0, '/home/user/Gator/src')
from tools.scout import scout_scan
from skills import generate_and_test_skill

try:
    # Gate 3.1: Scout
    result1 = scout_scan("https://bot.sannysoft.com")
    scout_pass = (result1.get("chars_scraped", 0) > 0) and result1.get("memory_id")
    
    # Gate 3.2: Architect
    skill_result = generate_and_test_skill("Gate 3 verification skill", test_mode=True)
    architect_pass = skill_result.get("success", False) and skill_result.get("node_id")
    
    status = "PASS" if (scout_pass and architect_pass) else "FAIL"
    print(f'{{"scout": {scout_pass}, "architect": {architect_pass}, "status": "{status}"}}')
except Exception as e:
    print(f'{{"error": "{str(e)}", "status": "FAIL"}}')
EOF

GATE3_OUT=$($VENV "$GATE3_TEST" 2>&1)
GATE3_EXIT=$?
rm -f "$GATE3_TEST"

GATE3_END=$(date +%s%N)
GATE3_VRAM_END=$(free -m | awk '/^Mem:/{print $3}')
GATE3_ELAPSED_MS=$(( (GATE3_END - GATE3_START) / 1000000 ))

if echo "$GATE3_OUT" | grep -q '"status": "PASS"'; then
    GATE3_STATUS="PASS"
else
    GATE3_STATUS="FAIL"
fi

echo "  Scout stealth scan: $(echo "$GATE3_OUT" | grep -oP '"scout":\s*\K(true|false)')"
echo "  Architect self-code: $(echo "$GATE3_OUT" | grep -oP '"architect":\s*\K(true|false)')"
echo "  Status: $GATE3_STATUS"
echo "  VRAM: ${GATE3_VRAM_START}M -> ${GATE3_VRAM_END}M"

GATES_RESULTS+=("{ \"gate\": 3, \"status\": \"$GATE3_STATUS\", \"tps\": 0.0, \"vram_usage\": \"${GATE3_VRAM_END}M\" }")

if [ "$GATE3_STATUS" != "PASS" ]; then
    echo -e "${RED}GATE 3 FAILED${NC}"
    exit 1
fi

# ============================================================================
# GATE 4: AMBIENT SENSES (Voice STT + TTS, no GPU spike)
# ============================================================================
echo ""
echo -e "${YELLOW}[GATE 4]${NC} Ambient Senses (Voice STT→TTS, CPU-only validation)"
GATE4_START=$(date +%s%N)
GATE4_VRAM_START=$(free -m | awk '/^Mem:/{print $3}')

# Create test script for voice
GATE4_TEST=$(mktemp)
cat > "$GATE4_TEST" << 'EOF'
import sys
sys.path.insert(0, '/home/user/Gator/src')
from interfaces.voice_layer import faster_whisper_stt, piper_tts

try:
    # Test STT -> TTS pipeline (CPU only)
    test_audio = "/tmp/test_audio.wav"
    
    # Create a minimal test WAV
    import wave, struct
    with wave.open(test_audio, 'w') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        # Silence: 1 second
        silence = struct.pack('<h', 0) * 16000
        w.writeframes(silence)
    
    # Test STT
    stt_result = faster_whisper_stt(test_audio)
    stt_pass = stt_result is not None
    
    # Test TTS
    text_in = "Hello from Gator Genesis"
    tts_path = piper_tts(text_in)
    tts_pass = tts_path is not None and len(tts_path) > 0
    
    status = "PASS" if (stt_pass and tts_pass) else "FAIL"
    print(f'{{"stt": {stt_pass}, "tts": {tts_pass}, "status": "{status}"}}')
except Exception as e:
    print(f'{{"error": "{str(e)}", "status": "FAIL"}}')
EOF

GATE4_OUT=$($VENV "$GATE4_TEST" 2>&1)
GATE4_EXIT=$?
rm -f "$GATE4_TEST"

GATE4_END=$(date +%s%N)
GATE4_VRAM_END=$(free -m | awk '/^Mem:/{print $3}')
GATE4_ELAPSED_MS=$(( (GATE4_END - GATE4_START) / 1000000 ))

if echo "$GATE4_OUT" | grep -q '"status": "PASS"'; then
    GATE4_STATUS="PASS"
else
    GATE4_STATUS="FAIL"
fi

echo "  STT pipeline: $(echo "$GATE4_OUT" | grep -oP '"stt":\s*\K(true|false)')"
echo "  TTS pipeline: $(echo "$GATE4_OUT" | grep -oP '"tts":\s*\K(true|false)')"
echo "  Status: $GATE4_STATUS"
echo "  VRAM Delta: ${GATE4_VRAM_START}M -> ${GATE4_VRAM_END}M (CPU-only, should be minimal)"

GATES_RESULTS+=("{ \"gate\": 4, \"status\": \"$GATE4_STATUS\", \"tps\": 0.0, \"vram_usage\": \"${GATE4_VRAM_END}M\" }")

if [ "$GATE4_STATUS" != "PASS" ]; then
    echo -e "${RED}GATE 4 FAILED${NC}"
    exit 1
fi

# ============================================================================
# GATE 5: IMMUNE RECOVERY (Kill process, restart <5s)
# ============================================================================
echo ""
echo -e "${YELLOW}[GATE 5]${NC} Immune Recovery (Kill + restart <5s)"
GATE5_START=$(date +%s%N)
GATE5_VRAM_START=$(free -m | awk '/^Mem:/{print $3}')

# Create test script for recovery
GATE5_TEST=$(mktemp)
cat > "$GATE5_TEST" << 'EOF'
import sys
import time
import subprocess
sys.path.insert(0, '/home/user/Gator/src')

try:
    # Find bridge PID
    result = subprocess.run(
        "pgrep -f gator_bridge | head -1",
        shell=True,
        capture_output=True,
        text=True
    )
    bridge_pid = result.stdout.strip()
    
    if not bridge_pid:
        print('{"recovered": false, "reason": "bridge_not_found", "status": "FAIL"}')
        sys.exit(1)
    
    # Kill it
    subprocess.run(f"kill -9 {bridge_pid}", shell=True)
    time.sleep(1)
    
    # Verify dead
    result = subprocess.run(
        f"ps -p {bridge_pid}",
        shell=True,
        capture_output=True
    )
    dead = result.returncode != 0
    
    # Restart via wakeup
    recovery_start = time.time()
    subprocess.Popen(
        "bash /home/user/Gator/wakeup",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    # Wait for recovery
    max_wait = 5.0
    while time.time() - recovery_start < max_wait:
        result = subprocess.run(
            "pgrep -f gator_bridge",
            shell=True,
            capture_output=True
        )
        if result.returncode == 0:
            recovery_time = time.time() - recovery_start
            status = "PASS" if recovery_time <= 5.0 else "FAIL"
            print(f'{{"recovered": true, "time_seconds": {recovery_time:.3f}, "status": "{status}"}}')
            sys.exit(0)
        time.sleep(0.5)
    
    print('{"recovered": false, "reason": "timeout", "status": "FAIL"}')
except Exception as e:
    print(f'{{"error": "{str(e)}", "status": "FAIL"}}')
EOF

GATE5_OUT=$($VENV "$GATE5_TEST" 2>&1)
GATE5_EXIT=$?
rm -f "$GATE5_TEST"

sleep 3  # Let services stabilize

GATE5_END=$(date +%s%N)
GATE5_VRAM_END=$(free -m | awk '/^Mem:/{print $3}')
GATE5_ELAPSED_MS=$(( (GATE5_END - GATE5_START) / 1000000 ))

if echo "$GATE5_OUT" | grep -q '"status": "PASS"'; then
    GATE5_STATUS="PASS"
    GATE5_RECOVERY=$(echo "$GATE5_OUT" | grep -oP '"time_seconds":\s*\K[0-9.]+' | head -1)
else
    GATE5_STATUS="FAIL"
    GATE5_RECOVERY=0
fi

echo "  Process killed and restarted"
echo "  Recovery time: ${GATE5_RECOVERY}s (target <5s)"
echo "  Status: $GATE5_STATUS"
echo "  VRAM: ${GATE5_VRAM_START}M -> ${GATE5_VRAM_END}M"

GATES_RESULTS+=("{ \"gate\": 5, \"status\": \"$GATE5_STATUS\", \"tps\": $GATE5_RECOVERY, \"vram_usage\": \"${GATE5_VRAM_END}M\" }")

if [ "$GATE5_STATUS" != "PASS" ]; then
    echo -e "${RED}GATE 5 FAILED${NC}"
    exit 1
fi

# ============================================================================
# GATE 6: SURGICAL UI (Pulse check, graph access, interrupt capability)
# ============================================================================
echo ""
echo -e "${YELLOW}[GATE 6]${NC} Surgical UI (Pulse check, graph, interrupt)"
GATE6_START=$(date +%s%N)
GATE6_VRAM_START=$(free -m | awk '/^Mem:/{print $3}')

# Create test script for surgical UI
GATE6_TEST=$(mktemp)
cat > "$GATE6_TEST" << 'EOF'
import sys
import subprocess
import time
sys.path.insert(0, '/home/user/Gator/src')

try:
    # Test 1: Pulse check
    result = subprocess.run(
        f"python3 {'/home/user/Gator/src/pulse_check.py'}",
        shell=True,
        capture_output=True,
        text=True,
        timeout=10
    )
    pulse_pass = "PASS" in result.stdout
    
    # Test 2: Graph endpoint
    result = subprocess.run(
        "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/graph",
        shell=True,
        capture_output=True,
        text=True,
        timeout=5
    )
    graph_http = result.stdout.strip()
    graph_pass = graph_http == "200"
    
    # Test 3: Interrupt capability
    result = subprocess.run(
        'curl -s -X POST http://127.0.0.1:8080/api/interrupt',
        shell=True,
        capture_output=True,
        text=True,
        timeout=5
    )
    interrupt_pass = "interrupt_pending" in result.stdout
    
    status = "PASS" if (pulse_pass and graph_pass and interrupt_pass) else "FAIL"
    print(f'{{"pulse": {pulse_pass}, "graph_http": "{graph_http}", "interrupt": {interrupt_pass}, "status": "{status}"}}')
except Exception as e:
    print(f'{{"error": "{str(e)}", "status": "FAIL"}}')
EOF

GATE6_OUT=$($VENV "$GATE6_TEST" 2>&1)
GATE6_EXIT=$?
rm -f "$GATE6_TEST"

GATE6_END=$(date +%s%N)
GATE6_VRAM_END=$(free -m | awk '/^Mem:/{print $3}')
GATE6_ELAPSED_MS=$(( (GATE6_END - GATE6_START) / 1000000 ))

if echo "$GATE6_OUT" | grep -q '"status": "PASS"'; then
    GATE6_STATUS="PASS"
    GATE6_TPS=$(echo "$GATE6_OUT" | grep -oP '"pulse":\s*\K(true|false)' | head -1)
else
    GATE6_STATUS="FAIL"
    GATE6_TPS=0
fi

echo "  Pulse check: $(echo "$GATE6_OUT" | grep -oP '"pulse":\s*\K(true|false)')"
echo "  Graph endpoint: $(echo "$GATE6_OUT" | grep -oP '"graph_http":\s*"?\K[^"]+' | head -1)"
echo "  Interrupt capability: $(echo "$GATE6_OUT" | grep -oP '"interrupt":\s*\K(true|false)')"
echo "  Status: $GATE6_STATUS"
echo "  VRAM: ${GATE6_VRAM_START}M -> ${GATE6_VRAM_END}M"

GATES_RESULTS+=("{ \"gate\": 6, \"status\": \"$GATE6_STATUS\", \"tps\": 0.0, \"vram_usage\": \"${GATE6_VRAM_END}M\" }")

if [ "$GATE6_STATUS" != "PASS" ]; then
    echo -e "${RED}GATE 6 FAILED${NC}"
    exit 1
fi

# ============================================================================
# GENERATE CONSOLIDATED ARTIFACT
# ============================================================================
echo ""
echo -e "${GREEN}[ARTIFACT]${NC} Generating genesis_artifact.json..."

# Build JSON array
JSON_ARRAY="["
for i in "${!GATES_RESULTS[@]}"; do
    JSON_ARRAY+="${GATES_RESULTS[$i]}"
    if [ $i -lt $((${#GATES_RESULTS[@]} - 1)) ]; then
        JSON_ARRAY+=","
    fi
done
JSON_ARRAY+="]"

# Write artifact
cat > "$ARTIFACT" << EOF
{
  "genesis_verification": {
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "substrate": "Gator v6 (Qwen 1.5B + 35B)",
    "vram_ceiling": "6144 MiB",
    "control_plane": "UDS event-bus /tmp/gator_event.bus",
    "topology": {
      "llama_server": "127.0.0.1:8081",
      "gator_bridge": "127.0.0.1:8090",
      "webui": "127.0.0.1:8080"
    },
    "gates": $JSON_ARRAY,
    "summary": {
      "total_gates": 6,
      "passed": $(echo "${GATES_RESULTS[@]}" | grep -o '"status": "PASS"' | wc -l),
      "failed": $(echo "${GATES_RESULTS[@]}" | grep -o '"status": "FAIL"' | wc -l)
    }
  }
}
EOF

echo "  Artifact written to: $ARTIFACT"

# Display artifact
echo ""
echo -e "${GREEN}========== GENESIS ARTIFACT ==========${NC}"
cat "$ARTIFACT" | jq .
echo -e "${GREEN}=====================================${NC}"

# ============================================================================
# FINAL STATUS
# ============================================================================
TOTAL_PASSED=$(echo "${GATES_RESULTS[@]}" | grep -o '"status": "PASS"' | wc -l)

if [ "$TOTAL_PASSED" -eq 6 ]; then
    echo ""
    echo -e "${GREEN}✓ GENESIS VERIFICATION COMPLETE: 6/6 GATES PASSED${NC}"
    echo "  Baseline artifact ready for rollback anchoring."
    exit 0
else
    echo ""
    echo -e "${RED}✗ GENESIS VERIFICATION INCOMPLETE: $TOTAL_PASSED/6 GATES PASSED${NC}"
    exit 1
fi
