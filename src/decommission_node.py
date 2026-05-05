#!/usr/bin/env python3
"""Surgical clone decommissioning: SIGTERM process, clean environment, preserve Scholar Sense."""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import sys
import time
from pathlib import Path
from typing import Any

GATOR_ROOT = Path(__file__).resolve().parents[1]
HIVE_STATE_FILE = GATOR_ROOT / "bin" / "hive_state.json"
HIVE_ROOT = GATOR_ROOT / "bin" / "hive_nodes"


class DecommissionError(RuntimeError):
    pass


def _load_state() -> dict[str, Any]:
    if not HIVE_STATE_FILE.exists():
        return {"prime": {"name": "Gator-Prime"}, "clones": {}}
    try:
        return json.loads(HIVE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DecommissionError(f"Failed to load hive_state.json: {exc}") from exc


def _save_state(payload: dict[str, Any]) -> None:
    try:
        HIVE_STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        raise DecommissionError(f"Failed to save hive_state.json: {exc}") from exc


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def decommission_clone(clone_name: str) -> dict[str, Any]:
    """
    Decommission a clone:
    1. SIGTERM the bridge process
    2. Clean up environment files (sandbox config, architect_prune.json)
    3. Mark as OFFLINE in hive_state.json or remove entirely
    4. Preserve .db and scholar_sense data
    """
    clone_name = (clone_name or "").strip()
    if not clone_name:
        raise DecommissionError("Clone name is required")

    state = _load_state()
    clones = state.get("clones", {})
    
    # Find the clone by name or slug
    clone_slug = None
    clone_entry = None
    for slug, entry in clones.items():
        if entry.get("name", "").lower() == clone_name.lower() or slug.lower() == clone_name.lower():
            clone_slug = slug
            clone_entry = entry
            break
    
    if not clone_entry:
        raise DecommissionError(f"Clone not found: {clone_name}")

    result = {
        "ok": True,
        "clone_name": clone_entry.get("name", "Unknown"),
        "slug": clone_slug,
        "operations": [],
    }

    # Step 1: SIGTERM the process
    pid = clone_entry.get("pid")
    if pid and _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
            result["operations"].append({"op": "sigterm", "pid": pid, "status": "sent"})
            # Wait up to 3 seconds for graceful shutdown
            for _ in range(30):
                if not _pid_alive(pid):
                    result["operations"][-1]["status"] = "terminated"
                    break
                time.sleep(0.1)
            # If still alive, force kill
            if _pid_alive(pid):
                os.kill(int(pid), signal.SIGKILL)
                result["operations"][-1]["status"] = "force_killed"
        except Exception as exc:
            result["operations"].append({"op": "sigterm", "pid": pid, "status": "error", "error": str(exc)})
    else:
        result["operations"].append({"op": "sigterm", "pid": pid, "status": "not_alive"})

    # Step 2: Clean up environment files (but NOT db/ or scholar_sense/)
    sandbox = HIVE_ROOT / clone_slug if clone_slug else None
    if sandbox and sandbox.exists():
        try:
            # Remove only the config files, not the entire sandbox
            config_file = sandbox / "config.json"
            architect_file = sandbox / "architect_prune.json"
            if config_file.exists():
                config_file.unlink()
                result["operations"].append({"op": "delete_config", "path": str(config_file), "status": "ok"})
            if architect_file.exists():
                architect_file.unlink()
                result["operations"].append({"op": "delete_architect", "path": str(architect_file), "status": "ok"})
            
            # Optionally remove the entire sandbox if empty, but preserve any data files
            try:
                if sandbox.exists() and not any(sandbox.iterdir()):
                    sandbox.rmdir()
                    result["operations"].append({"op": "remove_sandbox", "path": str(sandbox), "status": "ok"})
            except Exception:
                # Sandbox may not be empty or other files exist; don't force removal
                pass
        except Exception as exc:
            result["operations"].append({"op": "cleanup_env", "status": "error", "error": str(exc)})

    # Step 3: Update hive_state.json - mark as OFFLINE or remove
    # For now, remove from active clones dict entirely
    if clone_slug in clones:
        del clones[clone_slug]
        state["clones"] = clones
        _save_state(state)
        result["operations"].append({"op": "update_hive_state", "status": "removed"})

    result["hive_after"] = state

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Decommission a Gator clone node")
    parser.add_argument("clone_name", help="Name or slug of the clone to decommission")
    args = parser.parse_args()

    try:
        result = decommission_clone(args.clone_name)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)
    except DecommissionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
