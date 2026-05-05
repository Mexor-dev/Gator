#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

GATOR_ROOT = Path(__file__).resolve().parents[1]
if str(GATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(GATOR_ROOT))
if str(GATOR_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(GATOR_ROOT / "src"))

from src.core.gator_map import GatorMap
from src.inference.gator_kern import GatorKernRuntime
from src.maintenance import GatorMaintenance
from src.memory_core import GatorMemoryCore
from src.scholar_sense import ScholarSense
BRIDGE_URL = "http://127.0.0.1:8090"
WEBUI_URL = "http://127.0.0.1:8080"


def _post(url: str, payload: dict, timeout: float = 300.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(url: str, timeout: float = 60.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _vram_mib() -> int:
    out = subprocess.check_output(
        ["bash", "-lc", "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1"],
        text=True,
    ).strip()
    return int(out) if out.isdigit() else 0


def _make_pdf(path: Path, target_bytes: int = 5_000_000) -> None:
    page_count = 180
    while True:
        c = canvas.Canvas(str(path), pagesize=letter, pageCompression=0)
        c.setTitle("Gator Gauntlet Thermal Corpus")
        dense_line = (
            "Thermal engineering dossier: vapor chamber saturation, fin-stack pressure curves, fan duty cycles, "
            "memory bandwidth contention, and VRAM residency hysteresis across inference bursts. "
        ) * 6
        for page in range(page_count):
            y = 768
            c.setFont("Helvetica", 8)
            for row in range(64):
                c.drawString(24, y, f"P{page:04d} R{row:02d} {dense_line}")
                y -= 11
            c.showPage()
        c.save()
        if path.stat().st_size >= target_bytes:
            return
        page_count += 60


def inference_check() -> dict:
    with GatorKernRuntime() as kern:
        tokens = kern.sample_tokens(count=128)
        before = kern.resize_kv(512)
        after = kern.resize_kv(128)
        kern.flush_pool()
    assert len(tokens) == 128
    assert after < before
    return {"sampled_tokens": len(tokens), "kv_bytes_before": before, "kv_bytes_after": after}


def authorization_check() -> dict:
    allowed = _post(f"{BRIDGE_URL}/generate", {"prompt": "Execute a Heuristic Retrieval on the top-level nodes of Ars Technica via Scout.", "max_tokens": 200})
    general = _post(f"{BRIDGE_URL}/generate", {"prompt": "What is the capital of France?", "max_tokens": 32})
    assert allowed.get("authorized_research_task") is True
    assert general.get("authorized_research_task") is False
    return {"allowed": allowed.get("authorized_research_task"), "general": general.get("authorized_research_task")}


def silmc_check() -> dict:
    mc = GatorMemoryCore()
    sid = f"gauntlet_{int(time.time())}"
    mc.init_scratchpad(sid)
    mc.commit_thought(sid, 0, "Thermal kernel: vapor chamber spread improves heat flux and lowers fan duty.")
    maint = GatorMaintenance(root=GATOR_ROOT)
    out = maint.process_dream_cycle()
    assert out["knowledge_migration"]["inserted"] >= 1
    assert out["flushed"]["transient_scratchpad"] >= 1
    assert abs(out["vram_after_mib"] - 2204) <= 256
    return out


def architect_check() -> dict:
    log_file = GATOR_ROOT / "logs" / "gauntlet_architect.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("ERROR unmet goal: missing skill for thermal ledger conversion\n", encoding="utf-8")
    maint = GatorMaintenance(root=GATOR_ROOT)
    out = maint.architect_loop(max_tools=1)
    assert out["generated_tools"], "No tool generated"
    return out


def ingestion_check() -> dict:
    with tempfile.TemporaryDirectory(prefix="gator_pdf_") as tmpdir:
        pdf_path = Path(tmpdir) / "thermal_large.pdf"
        _make_pdf(pdf_path)
        assert pdf_path.stat().st_size >= 5_000_000

        html = urllib.request.urlopen(f"{WEBUI_URL}/", timeout=60).read().decode()
        assert "Shredding" in html and "Distillation" in html

        kickoff = _post(f"{WEBUI_URL}/api/ingest_pdf", {"pdf_path": str(pdf_path)})
        deadline = time.time() + 300
        last = kickoff.get("status", {})
        while time.time() < deadline:
            last = _get(f"{WEBUI_URL}/api/ingest_status")
            if last.get("state") == "complete":
                break
            if last.get("state") == "error":
                raise RuntimeError(last)
            time.sleep(1.0)
        assert last.get("state") == "complete", last

        ss = ScholarSense()
        query = ss.query("thermal vapor chamber airflow fin stack heat flux", top_k=4)
        assert not query.get("zero_context", True)
        return {"pdf_bytes": pdf_path.stat().st_size, "ingest": last, "query": {"selected_chunks": len(query.get('selected_chunks', []))}}


def finalize_blueprint(report: dict) -> dict:
    gm = GatorMap(root=GATOR_ROOT)
    snapshot = gm.snapshot_system_state(reason="gauntlet_green")
    baseline = gm.seal_master_baseline(label="github_release", gauntlet_report=report)
    return {"snapshot": snapshot, "master_baseline": str(baseline)}


def main() -> None:
    report = {
        "inference": inference_check(),
        "authorization": authorization_check(),
        "silmc": silmc_check(),
        "architect": architect_check(),
        "ingestion": ingestion_check(),
    }
    max_vram = _vram_mib()
    assert max_vram <= 5500, f"VRAM ceiling exceeded: {max_vram}"
    report["max_vram_mib"] = max_vram
    report["blueprint"] = finalize_blueprint(report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
