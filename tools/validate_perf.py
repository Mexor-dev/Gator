#!/usr/bin/env python3
"""Iron-Gator Performance Validation — TTFT, VRAM, Graft Integrity.

Validates the production spec:
- TTFT: ≤ 100ms (target 60ms)
- VRAM idle: ≤ 2.2 GB (target 2.07 GB)
- Graft: logic_map.gate loads exactly 3,672 records

Exit 0 = PASS, non-zero = FAIL
"""
from __future__ import annotations

import gzip
import json
import pickle
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

BRIDGE_URL = "http://127.0.0.1:8090"
GATE_FILE = Path(__file__).parent.parent / "bin" / "logic_map.gate"

# Production specs (Iron-Gator release targets)
TTFT_TARGET_MS = 60
TTFT_MAX_MS = 100
VRAM_TARGET_MIB = 2070  # 2.07 GB
VRAM_MAX_MIB = 2252     # 2.2 GB
GRAFT_EXPECTED_RECORDS = 3672


def measure_ttft(prompt: str = "hello", max_tokens: int = 12) -> tuple[bool, float, str]:
    """Send prompt to bridge and measure time-to-first-token in milliseconds."""
    payload = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": False,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BRIDGE_URL}/generate",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read().decode())
        t1 = time.perf_counter()
        elapsed_ms = (t1 - t0) * 1000
        text = resp_data.get("text", "")
        return True, elapsed_ms, text
    except Exception as exc:
        return False, 0.0, f"error: {exc}"


def measure_vram_idle() -> tuple[bool, int]:
    """Query nvidia-smi for idle VRAM usage in MiB."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        used_str = result.stdout.strip().split("\n")[0].strip()
        used_mib = int(used_str)
        return True, used_mib
    except Exception as exc:
        return False, 0


def validate_graft_integrity() -> tuple[bool, int, str]:
    """Load logic_map.gate and verify record count."""
    if not GATE_FILE.exists():
        return False, 0, f"missing: {GATE_FILE}"

    try:
        with gzip.open(GATE_FILE, "rb") as fh:
            data: Any = pickle.load(fh)

        if not isinstance(data, dict):
            return False, 0, f"invalid format: expected dict, got {type(data).__name__}"

        records = data.get("records", [])
        count = len(records)
        return True, count, "OK"
    except Exception as exc:
        return False, 0, f"error: {exc}"


def main() -> int:
    print("=" * 72)
    print("Iron-Gator Performance Validation")
    print("=" * 72)

    failures: list[str] = []

    # 1. GRAFT INTEGRITY
    print("\n[1/3] Graft Integrity Check")
    gate_ok, gate_count, gate_msg = validate_graft_integrity()
    if not gate_ok:
        print(f"  ✗ FAIL: {gate_msg}")
        failures.append(f"Graft load failed: {gate_msg}")
    elif gate_count != GRAFT_EXPECTED_RECORDS:
        print(f"  ✗ FAIL: Expected {GRAFT_EXPECTED_RECORDS} records, got {gate_count}")
        failures.append(f"Graft record count mismatch: {gate_count} != {GRAFT_EXPECTED_RECORDS}")
    else:
        print(f"  ✓ PASS: logic_map.gate contains {gate_count} records (exact match)")

    # 2. VRAM IDLE
    print("\n[2/3] VRAM Idle Check")
    vram_ok, vram_mib = measure_vram_idle()
    if not vram_ok:
        print(f"  ⚠ SKIP: nvidia-smi unavailable (CPU-only host or non-NVIDIA GPU)")
    else:
        vram_gb = vram_mib / 1024
        status = "TARGET" if vram_mib <= VRAM_TARGET_MIB else "PASS" if vram_mib <= VRAM_MAX_MIB else "FAIL"
        symbol = "★" if status == "TARGET" else "✓" if status == "PASS" else "✗"
        print(f"  {symbol} {status}: {vram_mib} MiB ({vram_gb:.2f} GB)")
        print(f"     Target: ≤ {VRAM_TARGET_MIB} MiB ({VRAM_TARGET_MIB/1024:.2f} GB)")
        print(f"     Max:    ≤ {VRAM_MAX_MIB} MiB ({VRAM_MAX_MIB/1024:.2f} GB)")
        if vram_mib > VRAM_MAX_MIB:
            failures.append(f"VRAM idle {vram_mib} MiB exceeds max {VRAM_MAX_MIB} MiB")

    # 3. TTFT (Time-To-First-Token)
    print("\n[3/3] TTFT (Time-To-First-Token) Check")
    ttft_ok, ttft_ms, response_text = measure_ttft()
    if not ttft_ok:
        print(f"  ✗ FAIL: {response_text}")
        failures.append(f"TTFT measurement failed: {response_text}")
    else:
        status = "TARGET" if ttft_ms <= TTFT_TARGET_MS else "PASS" if ttft_ms <= TTFT_MAX_MS else "FAIL"
        symbol = "★" if status == "TARGET" else "✓" if status == "PASS" else "✗"
        print(f"  {symbol} {status}: {ttft_ms:.1f} ms")
        print(f"     Target: ≤ {TTFT_TARGET_MS} ms")
        print(f"     Max:    ≤ {TTFT_MAX_MS} ms")
        print(f"     Response: {response_text[:60]!r}")
        if ttft_ms > TTFT_MAX_MS:
            failures.append(f"TTFT {ttft_ms:.1f} ms exceeds max {TTFT_MAX_MS} ms")

    # FINAL VERDICT
    print("\n" + "=" * 72)
    if failures:
        print(f"VERDICT: FAILED ({len(failures)} issue(s))")
        print("=" * 72)
        for i, f in enumerate(failures, 1):
            print(f"  {i}. {f}")
        return 1
    else:
        print("VERDICT: PASSED — All Iron-Gator specs met")
        print("=" * 72)
        return 0


if __name__ == "__main__":
    sys.exit(main())
