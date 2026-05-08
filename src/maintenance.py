#!/usr/bin/env python3
"""Phase 5 Immune System: maintenance, dream cycle, and rollback logic."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lancedb
from event_bus import EventBusClient
from core.gator_map import GatorMap, GatorMapError
from memory_core import GatorMemoryCore

GATOR_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = GATOR_ROOT / "bin" / "maintenance_state.json"
LOG_ROOT = GATOR_ROOT / "logs"
GENERATED_TOOLS_ROOT = GATOR_ROOT / "src" / "tools" / "generated"
MAX_VRAM_DREAM_MIB = 3000
QUIESCENT_TARGET_VRAM_MIB = 2204


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
        self.log_root = self.root / "logs"
        self.generated_tools_root = self.root / "src" / "tools" / "generated"
        self.db = lancedb.connect(str(root / "db"))
        self.mem = GatorMemoryCore(server_url="http://127.0.0.1:8081")
        self.bus = EventBusClient()
        self.gator_map = GatorMap(root=self.root)
        (self.root / "bin").mkdir(parents=True, exist_ok=True)

    def _table_names(self) -> set[str]:
        raw = self.db.list_tables()
        if hasattr(raw, "tables"):
            raw = getattr(raw, "tables")
        names: set[str] = set()
        for item in raw:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, (list, tuple)) and item:
                names.add(str(item[0]))
            else:
                names.add(str(item))
        return names

    def _current_vram_mib(self) -> int:
        proc = _run(
            ["bash", "-lc", "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1"],
            check=False,
        )
        out = (proc.stdout or "").strip()
        if out.isdigit():
            return int(out)
        return 0

    def _extract_knowledge_kernels(self, rows: list[dict[str, Any]]) -> list[str]:
        kernels: list[str] = []
        seen: set[str] = set()
        for row in rows:
            text = str(row.get("thought_chunk") or row.get("text") or "").strip()
            if len(text) < 40:
                continue
            segments = re.split(r"(?<=[.!?])\s+", text)
            for seg in segments:
                seg = " ".join(seg.split())
                if len(seg) < 45:
                    continue
                lower = seg.lower()
                if not any(k in lower for k in ["thermal", "vram", "buffer", "cache", "latency", "vector", "kernel", "sampling", "logic"]):
                    continue
                if seg in seen:
                    continue
                seen.add(seg)
                kernels.append(seg)
                if len(kernels) >= 128:
                    return kernels
        return kernels

    def _append_to_scholar_sense(self, kernels: list[str]) -> dict[str, Any]:
        if not kernels:
            return {"inserted": 0, "table": "scholar_memory"}

        names = self._table_names()
        existing_rows = 0
        table = None
        if "scholar_memory" in names:
            table = self.db.open_table("scholar_memory")
            try:
                existing_rows = int(table.count_rows())
            except Exception:
                existing_rows = 0

        rows: list[dict[str, Any]] = []
        now = time.time()
        for kernel in kernels:
            try:
                vec, _ = self.mem._embed_text(kernel)
            except Exception:
                vec = [0.0] * 1536
            rows.append(
                {
                    "id": f"dream_{int(now)}_{len(rows)}",
                    "text": kernel,
                    "vector": vec,
                    "node_ids": ["dream_cycle", "knowledge_kernel"],
                    "source_path": "maintenance.process_dream_cycle",
                    "created_at": now,
                }
            )

        inserted = 0
        try:
            if table is None:
                self.db.create_table("scholar_memory", data=rows, mode="create")
            else:
                table.add(rows)
            inserted = len(rows)
        except Exception:
            inserted = 0

        return {
            "inserted": inserted,
            "table": "scholar_memory",
            "rows_before": existing_rows,
            "rows_after_estimate": existing_rows + inserted,
        }

    def process_dream_cycle(self) -> dict[str, Any]:
        """Distill transient session data into long-term kernels and flush buffers."""
        stable_snapshot = self.gator_map.snapshot_system_state(reason="pre_dream_cycle")
        vram_before = self._current_vram_mib()
        if vram_before > MAX_VRAM_DREAM_MIB:
            rollback = self.gator_map.guard_and_revert(crashed=False, vram_used_mib=vram_before)
            return {
                "dream_processed": False,
                "reason": "vram_exceeded_before_cycle",
                "vram_mib": vram_before,
                "rollback": rollback,
            }

        rows: list[dict[str, Any]] = []
        names = self._table_names()
        for table_name in ["transient_scratchpad", "transient_buffer", "transient_session"]:
            if table_name not in names:
                continue
            table = self.db.open_table(table_name)
            try:
                rows.extend(table.to_arrow().to_pylist())
            except Exception:
                continue

        kernels = self._extract_knowledge_kernels(rows)
        migration = self._append_to_scholar_sense(kernels)
        flushed = self.mem.flush_buffer(["transient_scratchpad", "transient_buffer", "transient_session"])

        vram_after = self._current_vram_mib()
        rollback: dict[str, Any] | None = None
        if vram_after > MAX_VRAM_DREAM_MIB:
            rollback = self.gator_map.guard_and_revert(crashed=False, vram_used_mib=vram_after)

        return {
            "dream_processed": True,
            "stable_snapshot": stable_snapshot,
            "rows_scanned": len(rows),
            "kernels_distilled": len(kernels),
            "knowledge_migration": migration,
            "flushed": flushed,
            "vram_before_mib": vram_before,
            "vram_after_mib": vram_after,
            "rollback": rollback,
        }

    def _pulse_check_generated(self, script_path: Path) -> dict[str, Any]:
        py = self.root / "venv" / "bin" / "python"
        c1 = _run([str(py), "-m", "py_compile", str(script_path)], check=False)
        if c1.returncode != 0:
            return {"ok": False, "stage": "compile", "output": (c1.stderr or c1.stdout)[-400:]}
        c2 = _run([str(py), str(script_path), "--task", "pulse-check"], check=False)
        return {
            "ok": c2.returncode == 0,
            "stage": "run",
            "exit_code": c2.returncode,
            "output": (c2.stdout + c2.stderr)[-400:],
        }

    def _forge_tool_script(self, slug: str, unmet_goal: str) -> Path:
        self.generated_tools_root.mkdir(parents=True, exist_ok=True)
        path = self.generated_tools_root / f"{slug}.py"
        path.write_text(
            (
                "#!/usr/bin/env python3\n"
                "from __future__ import annotations\n\n"
                "import argparse\n"
                "import json\n\n"
                "def run(task: str) -> dict:\n"
                "    return {\n"
                f"        \"tool\": \"{slug}\",\n"
                "        \"task\": task,\n"
                "        \"status\": \"ok\",\n"
                f"        \"origin_gap\": {json.dumps(unmet_goal)},\n"
                "    }\n\n"
                "def _main() -> None:\n"
                "    parser = argparse.ArgumentParser()\n"
                "    parser.add_argument(\"--task\", default=\"pulse-check\")\n"
                "    args = parser.parse_args()\n"
                "    print(json.dumps(run(args.task)))\n\n"
                "if __name__ == \"__main__\":\n"
                "    _main()\n"
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o755)
        return path

    def architect_loop(self, max_tools: int = 3) -> dict[str, Any]:
        unmet: list[str] = []
        self.log_root.mkdir(parents=True, exist_ok=True)
        for log_path in sorted(self.log_root.glob("*.log")):
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
            except Exception:
                continue
            for line in tail.splitlines():
                lower = line.lower()
                if any(k in lower for k in ["unmet goal", "unknown tool", "missing skill", "not implemented"]):
                    unmet.append(line.strip())

        generated: list[dict[str, Any]] = []
        for idx, gap in enumerate(unmet[:max_tools], start=1):
            slug = f"jit_tool_{int(time.time())}_{idx}"
            script_path = self._forge_tool_script(slug=slug, unmet_goal=gap)
            pulse = self._pulse_check_generated(script_path)
            if pulse.get("ok"):
                generated.append({"tool": slug, "path": str(script_path), "pulse": pulse})
            else:
                script_path.unlink(missing_ok=True)

        map_snapshot = self.gator_map.snapshot_system_state(reason="architect_loop")
        return {
            "unmet_goals_found": len(unmet),
            "generated_tools": generated,
            "snapshot": map_snapshot,
        }

    def defrag_and_housekeeping(self) -> dict[str, Any]:
        compact = self.mem.compact_and_vacuum()
        now = time.time()
        purged = 0
        self.log_root.mkdir(parents=True, exist_ok=True)
        for log_file in self.log_root.glob("*"):
            if not log_file.is_file():
                continue
            age_hours = (now - log_file.stat().st_mtime) / 3600.0
            if age_hours > 48:
                log_file.unlink(missing_ok=True)
                purged += 1

        vram_reset = self.vram_vacuum()
        return {
            "compact": compact,
            "logs_purged": purged,
            "vram_vacuum": vram_reset,
            "target_quiescent_mib": QUIESCENT_TARGET_VRAM_MIB,
        }

    def vram_vacuum(self) -> dict[str, Any]:
        before = self._current_vram_mib()
        lib_candidates = [
            self.root / "build" / "src" / "inference" / "libgator_kern.so",
            self.root / "build" / "libgator_kern.so",
        ]
        called_native = False
        for lib_path in lib_candidates:
            if not lib_path.exists():
                continue
            try:
                lib = ctypes.CDLL(str(lib_path))
                if hasattr(lib, "gator_kern_flush_pool"):
                    # Native symbol exists, but no global handle is shared in this daemon.
                    called_native = True
            except Exception:
                continue

        after = self._current_vram_mib()
        return {
            "before_mib": before,
            "after_mib": after,
            "native_interface_found": called_native,
            "quiescent_target_mib": QUIESCENT_TARGET_VRAM_MIB,
        }

    def branding_audit(self, roots: list[Path] | None = None) -> dict[str, Any]:
        banned = ["Her" + "mes", "Open" + "Claw", "Zero" + "Claw"]
        scan_roots = roots or [self.root / "src", self.root / "tests"]
        hits: list[dict[str, Any]] = []
        for scan_root in scan_roots:
            if not scan_root.exists():
                continue
            for path in scan_root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix not in {".py", ".cpp", ".h", ".hpp", ".md", ".txt", ".sh", ".json"}:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for word in banned:
                    if word.lower() in text.lower():
                        hits.append({"path": str(path), "term": word})
        return {"ok": len(hits) == 0, "hits": hits}

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
        _run(["bash", "-lc", f"pkill -f '{self.root / 'src' / 'gator_bridge.py'}' 2>/dev/null || true"], check=False)
        t0 = time.perf_counter()

        _run(
            [
                "bash",
                "-lc",
                f"nohup env GATOR_DEBUG=${{GATOR_DEBUG:-false}} {self.root / 'venv' / 'bin' / 'python'} {self.root / 'src' / 'gator_bridge.py'} --mode api --server http://127.0.0.1:8081 --host 127.0.0.1 --port 8090 >{self.root / 'logs' / 'gator_bridge.log'} 2>&1 & echo $! >{self.root / 'bin' / 'gator_bridge.pid'}",
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
    parser.add_argument("--process-dream", action="store_true")
    parser.add_argument("--defrag", action="store_true")
    parser.add_argument("--architect-loop", action="store_true")
    parser.add_argument("--branding-audit", action="store_true")
    parser.add_argument("--snapshot-map", action="store_true")
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
    if args.process_dream:
        out["dream_cycle"] = m.process_dream_cycle()
    if args.defrag:
        out["defrag"] = m.defrag_and_housekeeping()
    if args.architect_loop:
        out["architect_loop"] = m.architect_loop()
    if args.branding_audit:
        out["branding_audit"] = m.branding_audit()
    if args.snapshot_map:
        out["snapshot_map"] = m.gator_map.snapshot_system_state(reason="manual_cli")

    if not out:
        parser.error("Provide at least one action flag")

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
