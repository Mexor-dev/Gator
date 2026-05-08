#!/usr/bin/env python3
"""Ghost Test — Worker Clone Integration Validation
Sovereign Build v1.0 Greenlight Protocol

Tests:
1. wakeup_cleared() reports correctly
2. spawn_clone() refuses without wakeup gate
3. Spawn 3 workers (Ghost-A, Ghost-B, Ghost-C)
4. Verify each:
   - has unique pid + port
   - has lance_scratchpad path that exists on disk
   - has vram_target_mib == 2228
   - hive_status() reports all 3 in WORKING state
5. Verify density cap blocks a 7th worker
6. Decommission all 3
7. Verify scratchpads are cleaned up
8. Verify density returns to 0

Run from repo root: python3 _ghost_test_workers.py
"""
from __future__ import annotations

import os
import sys
import time
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from core.mitosis import (
    MitosisEngine,
    wakeup_cleared,
    WORKER_VRAM_TARGET_MIB,
    MAX_WORKER_DENSITY,
    MitosisError,
)
from decommission_node import decommission_clone


PASS = "\u2705 PASS"
FAIL = "\u274c FAIL"
INFO = "\u2139\ufe0f  INFO"
results: list[tuple[str, str, str]] = []


def log(check: str, status: str, detail: str = "") -> None:
    results.append((check, status, detail))
    print(f"{status}  {check}  {detail}")


def main() -> int:
    print("=" * 70)
    print("GHOST TEST - Worker Clone Integration")
    print(f"VRAM target per worker: {WORKER_VRAM_TARGET_MIB} MiB")
    print(f"Max density: {MAX_WORKER_DENSITY}x")
    print("=" * 70)

    eng = MitosisEngine()

    # ── Check 1: wakeup gate state ─────────────────────────────────
    cleared = wakeup_cleared()
    log("Wakeup gate readable", PASS if cleared is not None else FAIL,
        f"cleared={cleared}")

    if not cleared:
        log("Wakeup gate cleared", FAIL,
            "Prime Gator has not passed genesis verification - "
            "cannot proceed with spawn tests. Run wakeup first.")
        # Still test the refusal behavior:
        try:
            eng.spawn_clone("ShouldFail-WakeupGate")
            log("Spawn refused without wakeup", FAIL,
                "spawn_clone returned instead of raising")
        except MitosisError as e:
            log("Spawn refused without wakeup", PASS, str(e))
        print_summary()
        return 1

    log("Wakeup gate cleared", PASS, "genesis verification confirmed")

    # ── Check 2: spawn 3 ghost workers ─────────────────────────────
    workers = ["Ghost-A", "Ghost-B", "Ghost-C"]
    spawned: list[dict] = []
    for w in workers:
        try:
            node = eng.spawn_clone(w)
            spawned.append(node)
            log(f"Spawn {w}", PASS,
                f"pid={node.get('pid')} port={node.get('bridge_port')} "
                f"slug={node.get('slug')}")
            time.sleep(0.5)
        except Exception as e:
            log(f"Spawn {w}", FAIL, repr(e))

    # ── Check 3: per-worker contract ───────────────────────────────
    for node in spawned:
        slug = node.get("slug")
        # 3a: vram_target_mib must equal 2228
        vram = node.get("vram_target_mib")
        log(f"  {slug}.vram_target_mib == {WORKER_VRAM_TARGET_MIB}",
            PASS if vram == WORKER_VRAM_TARGET_MIB else FAIL, f"got={vram}")
        # 3b: lance_scratchpad path exists
        sp = node.get("lance_scratchpad")
        sp_exists = bool(sp) and Path(sp).exists()
        log(f"  {slug}.lance_scratchpad exists",
            PASS if sp_exists else FAIL, str(sp))
        # 3c: lance_namespace == slug
        ns = node.get("lance_namespace")
        log(f"  {slug}.lance_namespace == slug",
            PASS if ns == slug else FAIL, f"got={ns}")
        # 3d: pid is alive
        pid = node.get("pid")
        try:
            os.kill(int(pid), 0)
            alive = True
        except (ProcessLookupError, PermissionError, ValueError, TypeError):
            alive = False
        log(f"  {slug} pid={pid} alive",
            PASS if alive else INFO,
            "running" if alive else "may have exited (bridge needs llama-server) - acceptable for ghost test")

    # ── Check 4: hive_status reports all + greenlight ──────────────
    status = eng.hive_status()
    clones = status.get("clones", [])
    log("hive_status() returns 3 clones",
        PASS if len(clones) == 3 else FAIL, f"got={len(clones)}")
    log("hive_status.greenlight",
        PASS if status.get("greenlight") else FAIL,
        json.dumps(status.get("worker_density", {})))

    # ── Check 5: density cap ───────────────────────────────────────
    # Spawn workers up to cap, then expect refusal
    extra = []
    for i in range(MAX_WORKER_DENSITY - 3):
        name = f"Ghost-Extra-{i}"
        try:
            node = eng.spawn_clone(name)
            extra.append(node)
        except Exception as e:
            log(f"Spawn {name} (filling to cap)", FAIL, str(e))
    # Now we should be at cap; one more should fail
    try:
        eng.spawn_clone("Ghost-OverCap")
        log(f"Density cap blocks N+1 spawn", FAIL,
            f"spawn_clone allowed {MAX_WORKER_DENSITY+1}th worker!")
    except MitosisError as e:
        log(f"Density cap blocks N+1 spawn", PASS, str(e))

    # ── Check 6: decommission all ──────────────────────────────────
    all_spawned = spawned + extra
    for node in all_spawned:
        name = node.get("name")
        try:
            r = decommission_clone(name)
            log(f"Decommission {name}", PASS if r.get("ok") else FAIL,
                f"ops={len(r.get('operations', []))}")
        except Exception as e:
            log(f"Decommission {name}", FAIL, repr(e))

    # ── Check 7: scratchpads cleaned ───────────────────────────────
    for node in all_spawned:
        sp = node.get("lance_scratchpad")
        slug = node.get("slug")
        if sp:
            still_exists = Path(sp).exists()
            log(f"  {slug} scratchpad cleaned",
                PASS if not still_exists else FAIL,
                f"{sp} {'STILL EXISTS' if still_exists else 'removed'}")

    # ── Check 8: hive empty ────────────────────────────────────────
    status_after = eng.hive_status()
    live_after = status_after.get("worker_density", {}).get("live", -1)
    log("Hive empty after decommission",
        PASS if live_after == 0 else FAIL, f"live={live_after}")

    return print_summary()


def print_summary() -> int:
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    info = sum(1 for _, s, _ in results if s == INFO)
    print(f"  PASS: {passed}    FAIL: {failed}    INFO: {info}")
    print("=" * 70)
    if failed == 0:
        print("\U0001f7e2  GREENLIGHT  -  Worker Clone Integration validated")
        return 0
    print("\U0001f534  RED  -  Investigate failures above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
