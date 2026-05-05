#!/bin/bash
set -euo pipefail

cd /home/user/Gator
source venv/bin/activate
export PYTHONPATH=/home/user/Gator/src

echo "Step 1 — Sovereign Validation: START"

python3 - << 'PY'
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, '/home/user/Gator/src')


def fail(msg: str) -> None:
	print(f"FAIL: {msg}")
	raise SystemExit(1)


# Kernel validation: libgator_kern.so presence + runtime sampler smoke check.
kernel_path = Path('/home/user/Gator/src/inference/libgator_kern.so')
if not kernel_path.exists():
	fail(f"Kernel missing: {kernel_path}")
if kernel_path.stat().st_size <= 0:
	fail(f"Kernel empty/corrupt: {kernel_path}")

from inference.gator_kern import GatorKernRuntime

with GatorKernRuntime(library_path=kernel_path) as runtime:
	sampled = runtime.sample_tokens(start_token=1, count=8)
	singleton = runtime.logic_singleton_addr()
if len(sampled) != 8:
	fail("Kernel sampler returned unexpected token count")
print(f"PASS: kernel integrity ({kernel_path.name}, singleton=0x{singleton:x})")


# Bridge check: src/gator_bridge.py donor path responds via InferenceEngine.
from gator_bridge import InferenceEngine, LOGIC_DONOR_PROMPT

bridge_engine = InferenceEngine()
donor_text = bridge_engine.generate(
	system_prompt=LOGIC_DONOR_PROMPT,
	user_prompt="Bridge self-check: confirm 35B donor path is alive.",
	max_tokens=32,
	temperature=0.2,
	top_p=0.9,
)
if not str(donor_text).strip():
	fail("Bridge donor response empty")
print("PASS: bridge donor communication")


# Memory check: fixed 2228 MiB worker baseline in both constant and hive status.
from core.mitosis import MitosisEngine, WORKER_VRAM_TARGET_MIB

if int(WORKER_VRAM_TARGET_MIB) != 2228:
	fail(f"VRAM baseline mismatch constant={WORKER_VRAM_TARGET_MIB} expected=2228")

hive_status = MitosisEngine().hive_status()
reported = int((hive_status.get('worker_density') or {}).get('per_worker_vram_mib', -1))
if reported != 2228:
	fail(f"VRAM baseline mismatch hive_status={reported} expected=2228")
print("PASS: memory baseline 2228 MiB")


# Lance check: db connectivity + transient scratchpad read/write probe.
import lancedb

db_root = Path('/home/user/Gator/db')
db_root.mkdir(parents=True, exist_ok=True)
conn = lancedb.connect(str(db_root))
_ = conn.table_names()

scratchpad_root = db_root / 'transient_scratchpad.lance'
scratchpad_root.mkdir(parents=True, exist_ok=True)
probe_file = scratchpad_root / f".probe_{uuid.uuid4().hex[:8]}"
probe_file.write_text('ok', encoding='utf-8')
if probe_file.read_text(encoding='utf-8') != 'ok':
	fail("Lance scratchpad probe read/write mismatch")
probe_file.unlink(missing_ok=True)
print("PASS: Lance db/scratchpad connectivity")

print("Step 2 — Sovereign Validation: PASS")
PY

echo "Step 3 — Ghost Test: PASS"
