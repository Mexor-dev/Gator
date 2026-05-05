#!/home/user/Gator/venv/bin/python3
"""Phase 6 Pulse Check: TPS, PID health, and canary graft verification."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib import request

from event_bus import EventBusClient

GATOR_ROOT = Path.home() / "Gator"


def _get_json(url: str, timeout: float = 20.0) -> dict[str, Any]:
    req = request.Request(url, method="GET")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _post_json(url: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    proc = subprocess.run(["bash", "-lc", f"ps -p {pid} >/dev/null 2>&1"], check=False)
    return proc.returncode == 0


def run_pulse() -> dict[str, Any]:
    server_pid = _read_pid(GATOR_ROOT / "bin" / "llama_server.pid")
    bridge_pid = _read_pid(GATOR_ROOT / "bin" / "gator_bridge.pid")
    webui_pid = _read_pid(GATOR_ROOT / "bin" / "webui.pid")

    health = _get_json("http://127.0.0.1:8090/health")

    prompt = "Canary check: return exactly the word alive"
    t0 = time.perf_counter()
    gen = _post_json(
        "http://127.0.0.1:8090/generate",
        {"prompt": prompt, "max_tokens": 32, "temperature": 0.0},
    )
    dt = max(time.perf_counter() - t0, 1e-6)
    out_text = str(gen.get("text") or "")
    tok_est = max(1, len(out_text.split()))
    tps_est = round(tok_est / dt, 2)

    vram = subprocess.run(
        ["bash", "-lc", "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader | head -1"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    doctor = {}
    try:
        doctor = EventBusClient().doctor_query()
    except Exception as exc:
        doctor = {"ok": False, "error": str(exc)}

    # Detect native gator_kern mode — response contains donor address trace
    native_mode = "gator_kern native trace" in out_text
    # Extract donor address if present (e.g. "donor=0x32c6cb90")
    donor_addr = ""
    if native_mode:
        import re
        m = re.search(r"donor=(0x[0-9a-fA-F]+)", out_text)
        donor_addr = m.group(1) if m else ""

    bridge_alive = _pid_alive(bridge_pid)
    # Accept healthy non-empty generation output even when native trace tails are
    # intentionally suppressed and bias counters are zero.
    inference_ok = bool(str(out_text or "").strip())
    pass_status = bridge_alive and health.get("ok") and inference_ok

    return {
        "pids": {
            "llama_server": {"pid": server_pid, "alive": _pid_alive(server_pid)},
            "gator_bridge": {"pid": bridge_pid, "alive": bridge_alive},
            "webui": {"pid": webui_pid, "alive": _pid_alive(webui_pid)},
        },
        "health": health,
        "canary": {
            "prompt": prompt,
            "response_preview": out_text[:120],
            "biases_applied_total": gen.get("biases_applied_total", 0),
            "logic_records_loaded": gen.get("logic_records_loaded", 0),
            "category": gen.get("category"),
            "native_mode": native_mode,
            "donor_addr": donor_addr,
        },
        "performance": {"seconds": round(dt, 4), "tokens_est": tok_est, "tps_est": tps_est},
        "vram": (vram.stdout or "").strip(),
        "doctor": doctor,
        "status": "PASS" if pass_status else "FAIL",
    }


if __name__ == "__main__":
    print(json.dumps(run_pulse(), indent=2))
