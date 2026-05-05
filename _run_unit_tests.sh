#!/bin/bash
cd /home/user/Gator
source venv/bin/activate
export PYTHONPATH=/home/user/Gator/src

python3 - << 'PY'
import sys
sys.path.insert(0, '/home/user/Gator/src')
from tests.test_lance_scratchpad import test_state_isolation, test_vram_heisenberg
test_state_isolation()
test_vram_heisenberg()
print('\nBoth unit/stress tests passed.')
PY
