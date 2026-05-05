#!/usr/bin/env python3
"""Project Gator - Step 6 validation harness.

Checks:
1) VRAM check for llama-server GPU allocation
2) Memory substrate check (ingest + LanceDB write)
3) Logic graft check (bias application from logic_map.gate)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, request

GATOR_ROOT = Path.home() / "Gator"
WAKEUP = GATOR_ROOT / "wakeup"
MEMORY_CORE = GATOR_ROOT / "src" / "memory_core.py"
SERVER_URL = "http://127.0.0.1:8081"
BRIDGE_URL = "http://127.0.0.1:8090"


class TestFailure(RuntimeError):
    pass


def http_get_json(url: str, timeout: float = 5.0) -> dict:
    try:
        with request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        raise TestFailure(f"GET failed for {url}: {exc}") from exc


def http_post_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise TestFailure(f"POST {url} HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise TestFailure(f"POST failed for {url}: {exc}") from exc


def wait_for_stack(timeout_s: int = 180) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        ok_server = False
        ok_bridge = False
        try:
            http_get_json(f"{SERVER_URL}/health", timeout=2)
            ok_server = True
        except Exception:
            pass
        try:
            http_get_json(f"{BRIDGE_URL}/health", timeout=2)
            ok_bridge = True
        except Exception:
            pass
        if ok_server and ok_bridge:
            return
        time.sleep(1)
    raise TestFailure("Timed out waiting for server and bridge health endpoints.")


def parse_pid_file(path: Path) -> int:
    if not path.exists():
        raise TestFailure(f"Missing pid file: {path}")
    return int(path.read_text(encoding="utf-8").strip())


def terminate_stack() -> None:
    for name in ("gator_bridge.pid", "llama_server.pid"):
        p = GATOR_ROOT / "bin" / name
        if not p.exists():
            continue
        try:
            pid = int(p.read_text(encoding="utf-8").strip())
            subprocess.run(["bash", "-lc", f"kill {pid} 2>/dev/null || true"], check=False)
        except Exception:
            pass

    # Also clear stale processes that may not match current pid files.
    subprocess.run(["bash", "-lc", "pkill -f '/home/user/Gator/src/gator_bridge.py' 2>/dev/null || true"], check=False)
    subprocess.run(["bash", "-lc", "pkill -f 'llama-server.*--port 8081' 2>/dev/null || true"], check=False)


def vram_check(server_pid: int) -> dict:
    out = subprocess.check_output(
        [
            "bash",
            "-lc",
            "nvidia-smi --query-compute-apps=pid,used_gpu_memory --format=csv,noheader,nounits",
        ],
        text=True,
    ).strip().splitlines()

    matched = None
    for line in out:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        mem_str = parts[1]
        if not mem_str.isdigit():
            continue
        mem = int(mem_str)
        if pid == server_pid:
            matched = mem
            break

    if matched is None:
        total_used = subprocess.check_output(
            [
                "bash",
                "-lc",
                "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1",
            ],
            text=True,
        ).strip()
        if not total_used.isdigit():
            raise TestFailure(
                f"No per-process GPU memory record and total memory parse failed: '{total_used}'"
            )
        matched = int(total_used)

    if matched < 300:
        raise TestFailure(f"GPU memory too low for full-offload intent: {matched} MiB")

    return {"pid": server_pid, "gpu_mem_mib": matched}


def memory_check() -> dict:
    before = int(
        subprocess.check_output(
            [str(GATOR_ROOT / "venv/bin/python"), str(MEMORY_CORE), "--count"],
            text=True,
        ).strip()
    )

    dummy = (
        "Gator memory test paragraph: local chassis embedding path is active. "
        "This document verifies LanceDB direct-link ingestion with llama-server embeddings."
    )
    ingest_raw = subprocess.check_output(
        [str(GATOR_ROOT / "venv/bin/python"), str(MEMORY_CORE), "--ingest", dummy],
        text=True,
    )
    ingest_json = json.loads(ingest_raw)

    after = int(
        subprocess.check_output(
            [str(GATOR_ROOT / "venv/bin/python"), str(MEMORY_CORE), "--count"],
            text=True,
        ).strip()
    )

    if after != before + 1:
        raise TestFailure(f"LanceDB row count mismatch: before={before} after={after}")

    return {
        "rows_before": before,
        "rows_after": after,
        "embedding_endpoint": ingest_json.get("endpoint_used"),
        "dimension": ingest_json.get("dimension"),
    }


def logic_check() -> dict:
    prompt = (
        "Prove by structured reasoning whether a policy that increases minimum wage "
        "can still reduce poverty under differing elasticities; include causal analysis "
        "and fact-check assumptions."
    )
    data = http_post_json(
        f"{BRIDGE_URL}/generate",
        {"prompt": prompt, "max_tokens": 96, "temperature": 0.6, "top_p": 0.9},
        timeout=240,
    )

    if int(data.get("logic_records_loaded", 0)) <= 0:
        raise TestFailure("Bridge did not load any logic_map.gate records.")
    if float(data.get("bias_weight", 0.0)) != 0.4:
        raise TestFailure("Bridge bias weight does not match required static weight 0.4.")
    if int(data.get("biases_applied_total", 0)) <= 0:
        raise TestFailure("No donor-pathway biases were applied during generation.")

    return {
        "category": data.get("category"),
        "bias_weight": data.get("bias_weight"),
        "biases_applied_total": data.get("biases_applied_total"),
        "logic_records_loaded": data.get("logic_records_loaded"),
        "sample_output": (data.get("text") or "")[:200],
    }


def main() -> None:
    terminate_stack()
    print("[1/4] Starting Gator stack via wakeup ...")
    if not WAKEUP.exists():
        raise TestFailure(f"Missing wakeup script: {WAKEUP}")

    wakeup_proc = subprocess.Popen(
        ["bash", str(WAKEUP)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    banner_seen = False
    t0 = time.time()
    while time.time() - t0 < 240:
        line = wakeup_proc.stdout.readline()
        if not line:
            time.sleep(0.1)
            continue
        print(line.rstrip())
        if "GATOR IS AWAKE. VRAM CONSTRAINTS NOMINAL. LOGIC GRAFT ACTIVE." in line:
            banner_seen = True
            break

    if not banner_seen:
        raise TestFailure("wakeup did not report active status banner.")

    print("[2/4] Waiting for health endpoints ...")
    wait_for_stack()

    server_pid = parse_pid_file(GATOR_ROOT / "bin" / "llama_server.pid")

    print("[3/4] Running VRAM check ...")
    vram = vram_check(server_pid)

    try:
        print("[4/4] Running memory + logic checks ...")
        mem = memory_check()
        logic = logic_check()
        print(
            json.dumps(
                {
                    "vram_check": vram,
                    "memory_check": mem,
                    "logic_check": logic,
                    "status": "PASS",
                },
                indent=2,
            )
        )
    finally:
        terminate_stack()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"TEST FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
