#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import sys

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from maintenance import GatorMaintenance  # noqa: E402


def test_process_dream_cycle_path() -> None:
    with tempfile.TemporaryDirectory(prefix="maint_dream_") as tmpdir:
        root = Path(tmpdir)
        (root / "db").mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(parents=True, exist_ok=True)

        m = GatorMaintenance(root=root)

        with patch.object(m.gator_map, "snapshot_system_state", return_value={"snapshot_id": "s1"}), patch.object(
            m, "_current_vram_mib", side_effect=[2200, 2240]
        ), patch.object(m, "_table_names", return_value=set()), patch.object(
            m, "_extract_knowledge_kernels", return_value=["kernel insight"]
        ), patch.object(
            m, "_append_to_scholar_sense", return_value={"inserted": 1}
        ), patch.object(
            m.mem, "flush_buffer", return_value={"transient_scratchpad": 2}
        ):
            out = m.process_dream_cycle()

        assert out["dream_processed"] is True
        assert out["kernels_distilled"] == 1
        assert out["rollback"] is None


def test_defrag_and_architect_loop() -> None:
    with tempfile.TemporaryDirectory(prefix="maint_arch_") as tmpdir:
        root = Path(tmpdir)
        (root / "db").mkdir(parents=True, exist_ok=True)
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)

        stale = logs / "stale.log"
        stale.write_text("old", encoding="utf-8")
        old_ts = time.time() - (72 * 3600)
        stale.touch()
        import os

        os.utime(stale, (old_ts, old_ts))

        active = logs / "active.log"
        active.write_text("error: unmet goal tool missing skill", encoding="utf-8")

        m = GatorMaintenance(root=root)

        with patch.object(m.mem, "compact_and_vacuum", return_value={"gator_memory": "optimized"}), patch.object(
            m, "vram_vacuum", return_value={"before_mib": 2204, "after_mib": 2204}
        ):
            defrag = m.defrag_and_housekeeping()

        assert defrag["logs_purged"] == 1

        with patch.object(m.gator_map, "snapshot_system_state", return_value={"snapshot_id": "s2"}), patch.object(
            m, "_pulse_check_generated", return_value={"ok": True, "stage": "run", "exit_code": 0}
        ):
            arch = m.architect_loop(max_tools=1)

        assert arch["unmet_goals_found"] >= 1
        assert len(arch["generated_tools"]) == 1


def test_branding_audit_reports_hits() -> None:
    with tempfile.TemporaryDirectory(prefix="branding_") as tmpdir:
        root = Path(tmpdir)
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        banned_marker = "Her" + "mes"
        (src / "x.py").write_text(f"name = '{banned_marker}'\\n", encoding="utf-8")
        (root / "db").mkdir(parents=True, exist_ok=True)

        m = GatorMaintenance(root=root)
        audit = m.branding_audit(roots=[src])
        assert audit["ok"] is False
        assert audit["hits"], "expected branding audit hits"
