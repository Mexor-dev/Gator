#!/usr/bin/env python3
"""Agentic cron runner with zero-overhead hard-off semantics."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from maintenance import GatorMaintenance

GATOR_ROOT = Path(__file__).resolve().parents[1]
BIN_ROOT = GATOR_ROOT / "bin"
LOG_ROOT = GATOR_ROOT / "logs"
PID_FILE = BIN_ROOT / "agentic_cron.pid"
STATE_FILE = BIN_ROOT / "agentic_cron_state.json"
STATUS_FILE = LOG_ROOT / "agentic_cron_status.json"

DEFAULT_SCHEDULE = {
    "enabled": False,
    "interval_seconds": 15,
    "dream_idle_minutes": 30,
    "dream_every_seconds": 120,
    "process_dream_every_seconds": 180,
    "defrag_every_seconds": 300,
    "architect_every_seconds": 420,
}


class AgenticCronError(RuntimeError):
    pass


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return dict(DEFAULT_SCHEDULE)
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    merged = dict(DEFAULT_SCHEDULE)
    merged.update(payload)
    return merged


def _save_state(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_SCHEDULE)
    merged.update(payload)
    BIN_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _write_status(payload: dict[str, Any]) -> None:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def cron_status() -> dict[str, Any]:
    state = _load_state()
    pid = _read_pid()
    alive = _pid_alive(pid)
    status_payload: dict[str, Any] = {}
    if STATUS_FILE.exists():
        try:
            status_payload = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            status_payload = {}
    return {
        "enabled": bool(state.get("enabled", False)),
        "pid": pid,
        "alive": alive,
        "state": state,
        "status": status_payload,
    }


def cron_stop() -> dict[str, Any]:
    state = _save_state({"enabled": False})
    pid = _read_pid()
    killed = False
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed = True
        except Exception:
            pass
        for _ in range(30):
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.kill(int(pid), signal.SIGKILL)
                killed = True
            except Exception:
                pass
    subprocess.run(["pkill", "-f", str(GATOR_ROOT / "src" / "agentic_cron.py")], check=False)
    time.sleep(0.1)
    subprocess.run(["pkill", "-9", "-f", str(GATOR_ROOT / "src" / "agentic_cron.py")], check=False)
    PID_FILE.unlink(missing_ok=True)
    _write_status(
        {
            "state": "off",
            "enabled": False,
            "killed": killed,
            "updated_at": time.time(),
            "message": "Agentic cron disabled. Zero background overhead requested.",
        }
    )
    return {"ok": True, "enabled": False, "pid": pid, "killed": killed, "state": state}


def cron_start() -> dict[str, Any]:
    state = _save_state({"enabled": True})
    existing = _read_pid()
    if _pid_alive(existing):
        return {"ok": True, "already_running": True, "pid": existing, "state": state}

    py = GATOR_ROOT / "venv" / "bin" / "python"
    script = GATOR_ROOT / "src" / "agentic_cron.py"
    log_file = LOG_ROOT / "agentic_cron.log"
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as log_fp:
        proc = __import__("subprocess").Popen(
            [str(py), str(script), "--run-loop"],
            stdout=log_fp,
            stderr=log_fp,
            start_new_session=True,
            env=os.environ.copy(),
        )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    _write_status(
        {
            "state": "booting",
            "enabled": True,
            "pid": proc.pid,
            "updated_at": time.time(),
            "message": "Agentic cron boot requested.",
        }
    )
    return {"ok": True, "enabled": True, "pid": proc.pid, "state": state}


class AgenticCronRunner:
    def __init__(self) -> None:
        self.maintenance = GatorMaintenance()
        self.running = True
        self.last_run = {
            "dream": 0.0,
            "process_dream": 0.0,
            "defrag": 0.0,
            "architect": 0.0,
        }

    def _handle_signal(self, _signum: int, _frame: Any) -> None:
        self.running = False

    def _run_task(self, name: str, state: dict[str, Any]) -> dict[str, Any]:
        if name == "dream":
            return self.maintenance.run_dream_cycle(idle_minutes=int(state.get("dream_idle_minutes", 30)))
        if name == "process_dream":
            return self.maintenance.process_dream_cycle()
        if name == "defrag":
            return self.maintenance.defrag_and_housekeeping()
        if name == "architect":
            return self.maintenance.architect_loop()
        return {"ok": False, "reason": "unknown_task"}

    def loop(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        _write_status({"state": "running", "enabled": True, "pid": os.getpid(), "updated_at": time.time(), "last_results": {}})

        while self.running:
            state = _load_state()
            if not bool(state.get("enabled", False)):
                break

            now = time.time()
            results: dict[str, Any] = {}

            if now - self.last_run["dream"] >= int(state.get("dream_every_seconds", 120)):
                results["dream"] = self._run_task("dream", state)
                self.last_run["dream"] = now
            if now - self.last_run["process_dream"] >= int(state.get("process_dream_every_seconds", 180)):
                results["process_dream"] = self._run_task("process_dream", state)
                self.last_run["process_dream"] = now
            if now - self.last_run["defrag"] >= int(state.get("defrag_every_seconds", 300)):
                results["defrag"] = self._run_task("defrag", state)
                self.last_run["defrag"] = now
            if now - self.last_run["architect"] >= int(state.get("architect_every_seconds", 420)):
                results["architect"] = self._run_task("architect", state)
                self.last_run["architect"] = now

            _write_status(
                {
                    "state": "running",
                    "enabled": True,
                    "pid": os.getpid(),
                    "updated_at": now,
                    "last_results": results,
                    "schedule": state,
                }
            )
            time.sleep(max(1, int(state.get("interval_seconds", 15))))

        PID_FILE.unlink(missing_ok=True)
        _write_status(
            {
                "state": "off",
                "enabled": False,
                "updated_at": time.time(),
                "message": "Agentic cron stopped.",
            }
        )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Agentic cron runner")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--run-loop", action="store_true")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(cron_status(), indent=2))
        return
    if args.start:
        print(json.dumps(cron_start(), indent=2))
        return
    if args.stop:
        print(json.dumps(cron_stop(), indent=2))
        return
    if args.run_loop:
        AgenticCronRunner().loop()
        return
    parser.error("Provide --status, --start, --stop, or --run-loop")


if __name__ == "__main__":
    _main()