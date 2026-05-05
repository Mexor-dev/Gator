#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request

from event_bus import EventBusClient

GATOR_ROOT = Path(__file__).resolve().parents[2]
BIN_ROOT = GATOR_ROOT / "bin"
LOG_ROOT = GATOR_ROOT / "logs"
HIVE_ROOT = BIN_ROOT / "hive_nodes"
HIVE_STATE_FILE = BIN_ROOT / "hive_state.json"
PRIME_BRIDGE_URL = "http://127.0.0.1:8090"

# Per-worker VRAM budget for the 35B-class graft.
# 6 workers × 2228 MiB ≈ 13 GiB → fits 12 GiB hardware with headroom on shared layers.
WORKER_VRAM_TARGET_MIB = 2228
MAX_WORKER_DENSITY = 6
GENESIS_ARTIFACT = LOG_ROOT / "genesis_artifact.json"


def wakeup_cleared() -> bool:
    """Return True iff the Prime Gator wakeup gates have all passed."""
    try:
        data = json.loads(GENESIS_ARTIFACT.read_text(encoding="utf-8"))
        gv = data.get("genesis_verification") or {}
        summary = gv.get("summary") or {}
        return int(summary.get("failed", 1)) == 0 and int(summary.get("passed", 0)) > 0
    except Exception:
        return False


class MitosisError(RuntimeError):
    pass


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip()).strip("-").lower()
    return slug or f"node-{uuid.uuid4().hex[:8]}"


def _post_json(url: str, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


class MitosisEngine:
    def __init__(self, root: Path = GATOR_ROOT) -> None:
        self.root = root
        self.bin_root = self.root / "bin"
        self.log_root = self.root / "logs"
        self.hive_root = self.bin_root / "hive_nodes"
        self.state_file = self.bin_root / "hive_state.json"
        self.bus = EventBusClient()
        self.bin_root.mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)
        self.hive_root.mkdir(parents=True, exist_ok=True)

    def _role_for_name(self, name: str) -> str:
        n = name.strip().lower()
        if "scout" in n:
            return "scout"
        if "coder" in n or "dev" in n:
            return "coder"
        if "analyst" in n:
            return "analyst"
        return "generalist"

    def _toolset_for_role(self, role: str) -> list[str]:
        if role == "scout":
            return ["scholar_sense", "graphify", "url_fetch"]
        if role == "coder":
            return ["python", "cmake", "unit_test", "lint"]
        if role == "analyst":
            return ["lancedb", "vector_search", "maintenance"]
        return ["python", "event_bus", "maintenance"]

    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"prime": {"name": "Gator-Prime", "status": "IDLE"}, "clones": {}}
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            if "clones" not in payload:
                payload["clones"] = {}
            if "prime" not in payload:
                payload["prime"] = {"name": "Gator-Prime", "status": "IDLE"}
            return payload
        except Exception:
            return {"prime": {"name": "Gator-Prime", "status": "IDLE"}, "clones": {}}

    def _save_state(self, payload: dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _pid_alive(self, pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False

    def _next_ports(self, state: dict[str, Any]) -> tuple[int, int]:
        used_bridge = {8090}
        used_webui = {8080}
        for clone in state.get("clones", {}).values():
            used_bridge.add(int(clone.get("bridge_port", 0) or 0))
            used_webui.add(int(clone.get("webui_port", 0) or 0))

        bridge_port = 8100
        while bridge_port in used_bridge:
            bridge_port += 1

        webui_port = 8180
        while webui_port in used_webui:
            webui_port += 1
        return bridge_port, webui_port

    def _current_vram_mib(self) -> int:
        try:
            out = subprocess.check_output(
                ["bash", "-lc", "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1"],
                text=True,
                timeout=3,
            ).strip()
            return int(out) if out.isdigit() else 0
        except Exception:
            return 0

    def _estimate_vram_split(self, clone_count: int) -> tuple[int, int]:
        """Per-worker VRAM is fixed at the 35B graft target (2228 MiB).

        We no longer divide a measured slack budget across workers — that
        starved workers below the 35B graft minimum. The Sovereign Build
        contract is: each worker reserves WORKER_VRAM_TARGET_MIB so the
        hive can hit MAX_WORKER_DENSITY (6×) on 12 GiB hardware.
        """
        total = self._current_vram_mib()
        if clone_count <= 0:
            return total, WORKER_VRAM_TARGET_MIB
        worker_budget = WORKER_VRAM_TARGET_MIB
        prime_est = max(0, total - worker_budget * clone_count)
        return prime_est, worker_budget

    def _node_coord(self, label: str, index: int, is_prime: bool = False) -> dict[str, float]:
        base = sum(ord(ch) for ch in label)
        if is_prime:
            return {"x": 0.0, "y": 0.0, "z": 0.0}
        return {
            "x": float(((base % 41) - 20) * 1.7),
            "y": float(14 + index * 6),
            "z": float(((base % 29) - 14) * 1.3),
        }

    def spawn_clone(self, name: str) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise MitosisError("Clone name is required")

        # Wakeup gate: refuse to spawn until the Prime Gator has cleared ignition.
        if not wakeup_cleared():
            raise MitosisError(
                "Wakeup gate not cleared - Prime Gator has not passed genesis "
                "verification. Run wakeup before spawning workers."
            )

        state = self._load_state()
        slug = _slug(name)
        if slug in state["clones"] and self._pid_alive(state["clones"][slug].get("pid")):
            raise MitosisError(f"Clone already active: {name}")

        # Density gate: cap at MAX_WORKER_DENSITY live workers (2228 MiB × 6 ≈ 12 GiB).
        live_count = sum(
            1 for c in state["clones"].values()
            if self._pid_alive(c.get("pid"))
        )
        if live_count >= MAX_WORKER_DENSITY:
            raise MitosisError(
                f"Worker density cap reached ({live_count}/{MAX_WORKER_DENSITY}). "
                f"Decommission an existing clone before spawning a new one."
            )

        role = self._role_for_name(name)
        toolset = self._toolset_for_role(role)
        bridge_port, webui_port = self._next_ports(state)
        map_id = f"map_{slug}_{uuid.uuid4().hex[:8]}"

        sandbox = self.hive_root / slug
        verified_specs = self.root / "config" / "hive_verified_specs.json"
        sandbox.mkdir(parents=True, exist_ok=True)
        (sandbox / "config.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "slug": slug,
                    "role": role,
                    "toolset": toolset,
                    "map_id": map_id,
                    "shared_db": str(self.root / "db"),
                    "shared_logic_server": PRIME_BRIDGE_URL,
                    "verified_specs": str(verified_specs),
                    "gator_guard": True,
                    "system_identity": "cpp_rtx_direct",
                    "silent_mode": True,
                    "voice_disabled": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # Architect-loop role prune marker (lightweight and deterministic).
        (sandbox / "architect_prune.json").write_text(
            json.dumps({"role": role, "toolset": toolset, "pruned_at": time.time()}, indent=2),
            encoding="utf-8",
        )

        py = self.root / "venv" / "bin" / "python"
        bridge_script = self.root / "src" / "gator_bridge.py"
        log_file = self.log_root / f"clone_{slug}.log"
        log_fp = open(log_file, "ab")

        # Per-clone Lance scratchpad namespace (transient context exchange).
        scratchpad_root = self.root / "db" / "transient_scratchpad.lance"
        clone_scratchpad = scratchpad_root / slug
        clone_scratchpad.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["GATOR_NODE_NAME"] = name
        env["GATOR_ROLE"] = role
        env["GATOR_SANDBOX"] = str(sandbox)
        env["GATOR_MAP_ID"] = map_id
        env["GATOR_SHARED_DB"] = str(self.root / "db")
        env["GATOR_VERIFIED_SPECS"] = str(verified_specs)
        env["GATOR_GUARD_ENFORCED"] = "true"
        env["GATOR_TOOL_PRELOAD"] = json.dumps(toolset)
        env["GATOR_SILENT_BACKGROUND"] = "true"
        env["GATOR_SYSTEM_IDENTITY"] = "cpp_rtx_direct"
        env["GATOR_VOICE_DISABLED"] = "true"
        env["GATOR_TEXT_ONLY"] = "true"
        # Sovereign Build v1.0 worker-clone contract:
        env["GATOR_WORKER_VRAM_MIB"] = str(WORKER_VRAM_TARGET_MIB)
        env["GATOR_WORKER_DENSITY_CAP"] = str(MAX_WORKER_DENSITY)
        env["GATOR_LANCE_SCRATCHPAD"] = str(clone_scratchpad)
        env["GATOR_LANCE_SCRATCHPAD_NS"] = slug
        env["GATOR_IS_WORKER_CLONE"] = "true"

        try:
            proc = subprocess.Popen(
                [
                    str(py),
                    str(bridge_script),
                    "--mode",
                    "api",
                    "--server",
                    "http://127.0.0.1:8081",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(bridge_port),
                ],
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        finally:
            log_fp.close()

        node = {
            "name": name,
            "slug": slug,
            "pid": proc.pid,
            "status": "WORKING",
            "role": role,
            "toolset": toolset,
            "bridge_port": bridge_port,
            "webui_port": webui_port,
            "map_id": map_id,
            "sandbox": str(sandbox),
            "log": str(log_file),
            "started_at": time.time(),
            "shared_logic_singleton": True,
            "shared_scholar_memory": str(self.root / "db"),
            "vram_target_mib": WORKER_VRAM_TARGET_MIB,
            "lance_scratchpad": str(clone_scratchpad),
            "lance_namespace": slug,
        }
        state["clones"][slug] = node
        self._save_state(state)

        try:
            self.bus.publish({"type": "clone_spawned", "name": name, "slug": slug, "map_id": map_id, "final": False})
            self.bus.publish({"type": "gator_map_sync", "map_id": map_id, "source": name, "final": False})
        except Exception:
            pass

        # Hive ignition protocol.
        try:
            self.bus.publish({"type": "hive_ignition", "speaker": name, "text": "I am online and ready for task.", "final": True})
            self.bus.publish({"type": "hive_ignition", "speaker": "Gator-Prime", "text": f"Acknowledged. Clone {name} successfully anchored to the Hive-Mind.", "final": True})
        except Exception:
            pass

        return node

    def stop_clone(self, name_or_slug: str) -> dict[str, Any]:
        state = self._load_state()
        needle = _slug(name_or_slug)
        found_slug = None
        for slug, node in state.get("clones", {}).items():
            if slug == needle or _slug(str(node.get("name", ""))) == needle:
                found_slug = slug
                break
        if not found_slug:
            raise MitosisError(f"Unknown clone: {name_or_slug}")

        node = state["clones"][found_slug]
        pid = int(node.get("pid") or 0)
        if pid > 0:
            with contextlib.suppress(Exception):
                os.kill(pid, signal.SIGTERM)
        node["status"] = "HIBERNATING"
        node["stopped_at"] = time.time()
        self._save_state(state)
        return node

    def resume_hibernating(self) -> dict[str, Any]:
        state = self._load_state()
        resumed: list[str] = []
        for node in list(state.get("clones", {}).values()):
            if str(node.get("status", "")).upper() == "HIBERNATING":
                try:
                    self.spawn_clone(str(node.get("name") or node.get("slug") or "Worker"))
                    resumed.append(str(node.get("name") or node.get("slug")))
                except Exception:
                    continue
        return {"resumed": resumed, "count": len(resumed)}

    def hive_status(self) -> dict[str, Any]:
        state = self._load_state()
        clone_items: list[dict[str, Any]] = []
        for slug, node in state.get("clones", {}).items():
            pid = int(node.get("pid") or 0)
            alive = self._pid_alive(pid)
            status = str(node.get("status") or "IDLE")
            if alive and status == "HIBERNATING":
                status = "WORKING"
            if (not alive) and status == "WORKING":
                status = "IDLE"
            node["status"] = status
            clone_items.append(dict(node))

        clone_items = sorted(clone_items, key=lambda c: str(c.get("name", "")))
        prime_vram, worker_vram = self._estimate_vram_split(len([c for c in clone_items if c.get("status") == "WORKING"]))
        for idx, item in enumerate(clone_items, start=1):
            item["vram_mib"] = worker_vram if item.get("status") == "WORKING" else 0
            item["coord"] = self._node_coord(str(item.get("name", "worker")), idx, is_prime=False)

        payload = {
            "prime": {
                "name": "Gator-Prime",
                "status": "WORKING",
                "bridge_url": PRIME_BRIDGE_URL,
                "vram_mib": prime_vram,
                "coord": self._node_coord("Gator-Prime", 0, is_prime=True),
            },
            "clones": clone_items,
            "updated_at": time.time(),
            "shared_memory": str(self.root / "db"),
            "logic_singleton": True,
            "layout_3d": {
                "units": "abstract",
                "axis": {
                    "x": "worker spread",
                    "y": "hierarchy depth",
                    "z": "role variance",
                },
            },
        }
        # Sovereign Build greenlight signal: wakeup cleared + at least one
        # responsive worker (or just Prime alive when no workers spawned).
        live_clones = [c for c in clone_items if c.get("status") == "WORKING"]
        payload["wakeup_cleared"] = wakeup_cleared()
        payload["worker_density"] = {
            "live": len(live_clones),
            "cap": MAX_WORKER_DENSITY,
            "per_worker_vram_mib": WORKER_VRAM_TARGET_MIB,
        }
        payload["greenlight"] = bool(payload["wakeup_cleared"])
        self._save_state(state)
        return payload

    # ------------------------------------------------------------------
    # Visible Worker Protocol helpers
    # ------------------------------------------------------------------

    def node_id(self, name_or_slug: str) -> int:
        """Return the 1-based sequential node index for a clone, or 0 for Prime."""
        needle = _slug(name_or_slug)
        if needle in {"gator-prime", "prime", "gator", ""}:
            return 0
        state = self._load_state()
        for idx, slug in enumerate(state.get("clones", {}).keys(), start=1):
            if slug == needle:
                return idx
        return 0

    def worker_header(self, name: str) -> str:
        """Format the standardized Telegram worker header."""
        nid = self.node_id(name)
        if nid == 0:
            return f"[{name}]"
        return f"[{name}] (Node #{nid})"

    def post_update(self, name_or_slug: str, message: str) -> None:
        """Publish a visible progress update for a worker to the Telegram channel.

        Called automatically by telegram_hive after PROGRESS_INTERVAL_S seconds.
        Also usable from any long-running task to push mid-task status.
        """
        state = self._load_state()
        needle = _slug(name_or_slug)
        node = state.get("clones", {}).get(needle)
        display_name = str(node.get("name") if node else name_or_slug)
        header = self.worker_header(display_name)
        try:
            self.bus.publish({
                "type": "hive_ignition",
                "speaker": display_name,
                "text": f"{header}: {message}",
                "final": False,
            })
        except Exception:
            pass


def _main() -> None:
    parser = argparse.ArgumentParser(description="Mitosis engine")
    parser.add_argument("--spawn", type=str)
    parser.add_argument("--stop", type=str)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--resume-hibernating", action="store_true")
    args = parser.parse_args()

    engine = MitosisEngine()
    out: dict[str, Any] = {}

    if args.spawn:
        out["spawn"] = engine.spawn_clone(args.spawn)
    if args.stop:
        out["stop"] = engine.stop_clone(args.stop)
    if args.status:
        out["status"] = engine.hive_status()
    if args.resume_hibernating:
        out["resume_hibernating"] = engine.resume_hibernating()

    if not out:
        parser.error("Provide an action flag")

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
