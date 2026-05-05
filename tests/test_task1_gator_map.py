#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

import sys

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from core.gator_map import GatorMap  # noqa: E402


def test_snapshot_and_rollback_cycle() -> None:
    with tempfile.TemporaryDirectory(prefix="gator_map_") as tmpdir:
        root = Path(tmpdir)
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        file_path = src / "example.py"
        file_path.write_text("value = 1\n", encoding="utf-8")

        gm = GatorMap(root=root)
        snap = gm.snapshot_system_state(reason="unit")
        assert snap["module_count"] == 1

        file_path.write_text("value = 99\n", encoding="utf-8")
        out = gm.rollback_to_snapshot(snap["snapshot_id"])
        assert out["rolled_back"] is True
        assert "value = 1" in file_path.read_text(encoding="utf-8")


def test_guard_reverts_on_vram_spike() -> None:
    with tempfile.TemporaryDirectory(prefix="gator_map_guard_") as tmpdir:
        root = Path(tmpdir)
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "worker.py").write_text("token = 1\n", encoding="utf-8")

        gm = GatorMap(root=root)
        gm.snapshot_system_state(reason="stable")
        (src / "worker.py").write_text("token = 2\n", encoding="utf-8")

        out = gm.guard_and_revert(crashed=False, vram_used_mib=5900)
        assert out["rolled_back"] is True
        assert "token = 1" in (src / "worker.py").read_text(encoding="utf-8")
