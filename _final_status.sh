#!/bin/bash
cd /home/user/Gator
source venv/bin/activate

echo "=== SERVICE_STATUS ==="
curl -s http://127.0.0.1:8090/health
echo
curl -s http://127.0.0.1:8081/health 2>/dev/null && echo "llama-server: OK" || echo "llama-server: no /health endpoint (normal)"

echo ""
echo "=== WAKEUP_CLEAN (should have no embedding/pooling/cache-type-k lines) ==="
grep -n 'embedding\|pooling\|cache-type' wakeup || echo "CLEAN - none found"

echo ""
echo "=== KV_QUANT disabled ==="
grep -n 'KV_QUANT\|KV-Cache\|Quantization' wakeup

echo ""
echo "=== GENERATION_PARAMS ==="
grep -n 'frequency_penalty\|presence_penalty\|repeat_penalty\|max_tokens' src/gator_bridge.py | head -15

echo ""
echo "=== LANCE_ROUTING ==="
grep -n '_LANCE_CATS\|use_lance\|_generate_with_lance\|chain_of_thought.*analysis\|original_cat' src/gator_bridge.py | head -15

echo ""
echo "=== SCRATCHPAD_METHODS ==="
grep -n 'def init_scratchpad\|def commit_thought\|def retrieve_context\|def flush_scratchpad\|def _scratchpad_count' src/memory_core.py

echo ""
echo "=== VRAM ==="
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null
