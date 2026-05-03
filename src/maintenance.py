#!/home/user/Gator/venv/bin/python3
"""Phase 5 Immune System: maintenance, dream cycle, and rollback logic."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lancedb
from event_bus import EventBusClient

GATOR_ROOT = Path.home() / "Gator"
STATE_FILE = GATOR_ROOT / "bin" / "maintenance_state.json"


class MaintenanceError(RuntimeError):
    pass


@dataclass
class PriorityTask:
    name: str
    priority: int
    created_ts: float
    payload: dict[str, Any] | None = None


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        raise MaintenanceError(f"cmd failed: {' '.join(cmd)}\n{proc.stderr or proc.stdout}")
    return proc


class GatorMaintenance:
    def __init__(self, root: Path = GATOR_ROOT) -> None:
        self.root = root
        self.db = lancedb.connect(str(root / "db"))
        self.bus = EventBusClient()
        (self.root / "bin").mkdir(parents=True, exist_ok=True)

    def init_git_mirror(self) -> dict[str, Any]:
        git_dir = self.root / ".git"
        if not git_dir.exists():
            _run(["git", "init"], cwd=self.root)

        # Local-only identity for autonomous snapshots.
        _run(["git", "config", "user.name", "Gator Immune"], cwd=self.root)
        _run(["git", "config", "user.email", "gator@local"], cwd=self.root)

        return {"git_initialized": True, "git_dir": str(git_dir)}

    def snapshot_state(self, message: str = "immune snapshot") -> dict[str, Any]:
        self.init_git_mirror()

        paths = ["src", "config", "wakeup", "update.md"]
        existing = [p for p in paths if (self.root / p).exists()]
        if existing:
            _run(["git", "add", *existing], cwd=self.root)

        status = _run(["git", "status", "--porcelain"], cwd=self.root, check=False)
        if not status.stdout.strip():
            head = _run(["git", "rev-parse", "--short", "HEAD"], cwd=self.root, check=False)
            return {"committed": False, "head": (head.stdout or "").strip()}

        commit = _run(["git", "commit", "-m", message], cwd=self.root, check=False)
        combined = (commit.stdout + commit.stderr).lower()
        if commit.returncode != 0 and "nothing to commit" not in combined and "nothing added to commit" not in combined:
            raise MaintenanceError(f"git commit failed: {commit.stderr or commit.stdout}")

        head = _run(["git", "rev-parse", "--short", "HEAD"], cwd=self.root, check=False)
        committed = "nothing to commit" not in combined and "nothing added to commit" not in combined
        return {"committed": committed, "head": (head.stdout or "").strip()}

    def _load_state(self) -> dict[str, Any]:
        if not STATE_FILE.exists():
            return {"last_activity_ts": time.time()}
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"last_activity_ts": time.time()}

    def _save_state(self, data: dict[str, Any]) -> None:
        STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def touch_activity(self) -> dict[str, Any]:
        data = self._load_state()
        data["last_activity_ts"] = time.time()
        self._save_state(data)
        return data

    def run_dream_cycle(self, idle_minutes: int = 30) -> dict[str, Any]:
        self.snapshot_state("immune pre-dream snapshot")
        data = self._load_state()
        now = time.time()
        idle_for = (now - float(data.get("last_activity_ts", now))) / 60.0
        if idle_for < idle_minutes:
            return {"dream_ran": False, "idle_minutes": round(idle_for, 2)}

        queue = self.build_circadian_queue()
        queue_exec = self.execute_priority_queue(queue)

        data["last_dream_ts"] = now
        data["last_activity_ts"] = now
        self._save_state(data)

        return {
            "dream_ran": True,
            "idle_minutes": round(idle_for, 2),
            "queue": queue_exec,
            "vectors_pruned": int(queue_exec["results"].get("lancedb_prune", {}).get("vectors_pruned", 0)),
            "graph_updated": bool(queue_exec["results"].get("graphify_promotion", {}).get("graph_updated", False)),
        }

    def sort_priority_queue(self, tasks: list[PriorityTask]) -> list[PriorityTask]:
        # BabyAGI-style lightweight array sorting by urgency then insertion time.
        return sorted(tasks, key=lambda t: (int(t.priority), float(t.created_ts)))

    def build_circadian_queue(self) -> list[PriorityTask]:
        now = time.time()
        return self.sort_priority_queue(
            [
                PriorityTask(name="rollback_checks", priority=1, created_ts=now),
                PriorityTask(name="graphify_promotion", priority=2, created_ts=now + 0.01),
                PriorityTask(name="lancedb_prune", priority=3, created_ts=now + 0.02),
                PriorityTask(name="pulse_checks", priority=4, created_ts=now + 0.03),
            ]
        )

    def _task_rollback_checks(self) -> dict[str, Any]:
        snap = self.snapshot_state("circadian rollback check")
        return {"ok": True, "head": snap.get("head"), "committed": bool(snap.get("committed", False))}

    def _task_graphify_promotion(self) -> dict[str, Any]:
        graphify = Path.home() / ".local" / "bin" / "graphify"
        if not graphify.exists():
            return {"ok": False, "graph_updated": False, "reason": "graphify_missing"}
        proc = _run([str(graphify), "update", str(self.root / "research")], cwd=self.root, check=False)
        return {"ok": proc.returncode == 0, "graph_updated": proc.returncode == 0}

    def _task_lancedb_prune(self) -> dict[str, Any]:
        pruned = 0
        tables = self.db.list_tables()
        if hasattr(tables, "tables"):
            tables = tables.tables
        if "scholar_memory" in tables:
            table = self.db.open_table("scholar_memory")
            rows = table.to_arrow().to_pylist()
            seen: set[str] = set()
            keep = []
            for r in rows:
                key = str(r.get("text", "")).strip()
                if key in seen:
                    pruned += 1
                    continue
                seen.add(key)
                keep.append(r)
            if pruned > 0:
                self.db.drop_table("scholar_memory")
                self.db.create_table("scholar_memory", data=keep, mode="overwrite")
        return {"ok": True, "vectors_pruned": pruned}

    def _task_pulse_checks(self) -> dict[str, Any]:
        py = self.root / "venv" / "bin" / "python"
        pulse = self.root / "src" / "pulse_check.py"
        proc = _run([str(py), str(pulse)], cwd=self.root, check=False)
        body = (proc.stdout or proc.stderr or "")[-800:]
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output_tail": body}

    def execute_priority_queue(self, tasks: list[PriorityTask]) -> dict[str, Any]:
        ordered = self.sort_priority_queue(tasks)
        order = [t.name for t in ordered]
        results: dict[str, Any] = {}

        for task in ordered:
            if task.name == "rollback_checks":
                results[task.name] = self._task_rollback_checks()
            elif task.name == "graphify_promotion":
                results[task.name] = self._task_graphify_promotion()
            elif task.name == "lancedb_prune":
                results[task.name] = self._task_lancedb_prune()
            elif task.name == "pulse_checks":
                results[task.name] = self._task_pulse_checks()
            elif task.name.startswith("mock_"):
                results[task.name] = {
                    "ok": True,
                    "executed": True,
                    "payload": task.payload or {},
                    "priority": task.priority,
                }
            else:
                results[task.name] = {"ok": False, "reason": "unknown_task"}

        return {"ordered": order, "results": results}

    def doctor_query(self) -> dict[str, Any]:
        return self.bus.doctor_query()

    def test_restart_attach(self) -> dict[str, Any]:
        # Simulate crash + autonomous bridge restart and require recovery within 5 seconds.
        _run(["bash", "-lc", "pkill -f '/home/user/Gator/src/gator_bridge.py' 2>/dev/null || true"], check=False)
        t0 = time.perf_counter()

        _run(
            [
                "bash",
                "-lc",
                "nohup env GATOR_DEBUG=${GATOR_DEBUG:-false} /home/user/Gator/venv/bin/python /home/user/Gator/src/gator_bridge.py --mode api --server http://127.0.0.1:8081 --host 127.0.0.1 --port 8090 >/home/user/Gator/logs/gator_bridge.log 2>&1 & echo $! >/home/user/Gator/bin/gator_bridge.pid",
            ],
            check=False,
        )

        ok = False
        for _ in range(30):
            probe = _run(["bash", "-lc", "curl -s http://127.0.0.1:8090/health"], check=False)
            if '"ok":true' in (probe.stdout or ""):
                ok = True
                break
            time.sleep(0.15)

        elapsed = time.perf_counter() - t0
        return {"recovered": ok, "seconds": round(elapsed, 3), "target_max_seconds": 5.0}

    def execute_with_rollback(self, command: list[str]) -> dict[str, Any]:
        snap = self.snapshot_state("immune pre-change snapshot")
        proc = _run(command, cwd=self.root, check=False)
        if proc.returncode == 0:
            return {"rolled_back": False, "exit_code": 0, "head": snap.get("head")}

        # Roll back to last stable commit on failure.
        _run(["git", "reset", "--hard", "HEAD"], cwd=self.root, check=True)
        return {
            "rolled_back": True,
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or proc.stdout)[-500:],
            "head": snap.get("head"),
        }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator maintenance daemon")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--touch-activity", action="store_true")
    parser.add_argument("--dream", action="store_true")
    parser.add_argument("--idle-minutes", type=int, default=30)
    parser.add_argument("--test-rollback", action="store_true")
    parser.add_argument("--doctor-query", action="store_true")
    parser.add_argument("--test-restart", action="store_true")
    args = parser.parse_args()

    m = GatorMaintenance()
    out: dict[str, Any] = {}

    if args.snapshot:
        out["snapshot"] = m.snapshot_state("phase5 snapshot")
    if args.touch_activity:
        out["touch_activity"] = m.touch_activity()
    if args.dream:
        out["dream"] = m.run_dream_cycle(idle_minutes=args.idle_minutes)
    if args.test_rollback:
        bad_script = GATOR_ROOT / "bin" / "phase5_mock_fail.sh"
        bad_script.write_text("#!/bin/bash\necho 'mock failure' 1>&2\nexit 42\n", encoding="utf-8")
        os.chmod(bad_script, 0o755)
        out["rollback_test"] = m.execute_with_rollback(["bash", str(bad_script)])
    if args.doctor_query:
        out["doctor_query"] = m.doctor_query()
    if args.test_restart:
        out["restart_test"] = m.test_restart_attach()

    if not out:
        parser.error("Provide at least one action flag")

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
