#!/home/user/Gator/venv/bin/python3
"""Phase 6 Pulse Check: lightweight health, vitals, and service telemetry."""

from __future__ import annotations

import json
import os
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


def _safe_get_json(url: str, timeout: float = 4.0) -> dict[str, Any]:
    try:
        payload = _get_json(url, timeout=timeout)
        result = dict(payload) if isinstance(payload, dict) else {"payload": payload}
        result.setdefault("ok", True)
        result["reachable"] = True
        return result
    except Exception as exc:
        return {"ok": False, "reachable": False, "error": str(exc)}


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


def _proc_cpu_percent(pid: int | None) -> float | None:
    if not pid or not _pid_alive(pid):
        return None
    proc = subprocess.run(
        ["bash", "-lc", f"ps -p {pid} -o %cpu= | head -1"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        return round(float((proc.stdout or "").strip()), 1)
    except Exception:
        return None


def _read_cpu_totals() -> tuple[int, int]:
    line = Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines()[0]
    parts = [int(part) for part in line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    return idle, total


def _cpu_usage_percent(sample_seconds: float = 0.15) -> float:
    try:
        idle_a, total_a = _read_cpu_totals()
        time.sleep(sample_seconds)
        idle_b, total_b = _read_cpu_totals()
        total_delta = max(total_b - total_a, 1)
        idle_delta = max(idle_b - idle_a, 0)
        usage = 100.0 * (1.0 - (idle_delta / total_delta))
        return round(max(0.0, min(usage, 100.0)), 1)
    except Exception:
        return 0.0


def _gpu_stats() -> dict[str, Any]:
    query = (
        "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits | head -1"
    )
    proc = subprocess.run(
        ["bash", "-lc", query],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        return {
            "name": "unavailable",
            "utilization_percent": None,
            "memory_used_mib": None,
            "memory_total_mib": None,
            "summary": "unavailable",
        }
    parts = [part.strip() for part in raw.split(",")]
    name = parts[0] if len(parts) > 0 else "unknown"
    try:
        utilization = int(parts[1]) if len(parts) > 1 else None
    except Exception:
        utilization = None
    try:
        used = int(parts[2]) if len(parts) > 2 else None
        total = int(parts[3]) if len(parts) > 3 else None
    except Exception:
        used, total = None, None
    summary = "unavailable"
    if used is not None and total is not None:
        summary = f"{used} MiB / {total} MiB"
    return {
        "name": name,
        "utilization_percent": utilization,
        "memory_used_mib": used,
        "memory_total_mib": total,
        "summary": summary,
    }


def run_pulse() -> dict[str, Any]:
    server_pid = _read_pid(GATOR_ROOT / "bin" / "gator_server.pid")
    bridge_pid = _read_pid(GATOR_ROOT / "bin" / "gator_bridge.pid")
    webui_pid = _read_pid(GATOR_ROOT / "bin" / "webui.pid")
    command_center_pid = _read_pid(GATOR_ROOT / "bin" / "command_center.pid")

    gator_server = _safe_get_json("http://127.0.0.1:8081/health")
    bridge = _safe_get_json("http://127.0.0.1:8090/health")
    webui = _safe_get_json("http://127.0.0.1:8080/api/health")
    command_center = _safe_get_json("http://127.0.0.1:8000/api/health")
    gpu = _gpu_stats()
    cpu_usage = _cpu_usage_percent()

    doctor = {}
    try:
        doctor = EventBusClient().doctor_query()
    except Exception as exc:
        doctor = {"ok": False, "error": str(exc)}

    processes = {
        "gator_server": {
            "pid": server_pid,
            "alive": _pid_alive(server_pid),
            "port": 8081,
            "cpu_percent": _proc_cpu_percent(server_pid),
        },
        "gator_bridge": {
            "pid": bridge_pid,
            "alive": _pid_alive(bridge_pid),
            "port": 8090,
            "cpu_percent": _proc_cpu_percent(bridge_pid),
        },
        "webui": {
            "pid": webui_pid,
            "alive": _pid_alive(webui_pid),
            "port": 8080,
            "cpu_percent": _proc_cpu_percent(webui_pid),
        },
        "command_center": {
            "pid": command_center_pid,
            "alive": _pid_alive(command_center_pid),
            "port": 8000,
            "cpu_percent": _proc_cpu_percent(command_center_pid),
        },
    }

    required_services = [
        processes["gator_server"]["alive"] and bool(gator_server.get("ok")),
        processes["gator_bridge"]["alive"] and bool(bridge.get("ok")),
        processes["webui"]["alive"] and bool(webui.get("ok")),
    ]
    pass_status = all(required_services)
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except Exception:
        load_1m = load_5m = load_15m = 0.0

    return {
        "ok": pass_status,
        "pids": processes,
        "processes": processes,
        "services": {
            "gator_server": {"url": "http://127.0.0.1:8081/health", **gator_server},
            "gator_bridge": {"url": "http://127.0.0.1:8090/health", **bridge},
            "webui": {"url": "http://127.0.0.1:8080/api/health", **webui},
            "command_center": {"url": "http://127.0.0.1:8000/api/health", **command_center},
        },
        "health": bridge,
        "canary": {
            "prompt": "health-only probe",
            "response_preview": "",
            "biases_applied_total": 0,
            "logic_records_loaded": 0,
            "category": "health_probe",
            "native_mode": bool(bridge.get("ok")),
            "donor_addr": "",
        },
        "performance": {
            "seconds": 0.0,
            "tokens_est": 0,
            "tps_est": "n/a",
            "poll_mode": "health",
        },
        "cpu": {
            "usage_percent": cpu_usage,
            "cores": os.cpu_count() or 0,
            "load_average": {
                "1m": round(load_1m, 2),
                "5m": round(load_5m, 2),
                "15m": round(load_15m, 2),
            },
        },
        "gpu": gpu,
        "vram": gpu.get("summary", "unavailable"),
        "vram_stats": gpu,
        "doctor": doctor,
        "status": "PASS" if pass_status else "FAIL",
    }


if __name__ == "__main__":
    print(json.dumps(run_pulse(), indent=2))
