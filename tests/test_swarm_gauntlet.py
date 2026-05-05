#!/usr/bin/env python3
from __future__ import annotations

import json
import os
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

from src.core.mitosis import MitosisEngine
from src.inference.gator_kern import GatorKernRuntime
from src.scholar_sense import ScholarSense

PRIME_URL = "http://127.0.0.1:8090"


def _post(url: str, payload: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _make_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter, pageCompression=0)
    dense = (
        "Vapor chamber thermal transfer, phase transition limits, heatsink delta-T mapping, fan curve envelopes, "
        "and VRAM pressure hysteresis under sustained inference load. "
    ) * 5
    for page in range(50):
        y = 760
        c.setFont("Helvetica", 9)
        for row in range(60):
            c.drawString(24, y, f"P{page:03d} R{row:02d} {dense}")
            y -= 12
        c.showPage()
    c.save()


def _vram_mib() -> int:
    out = subprocess.check_output(
        ["bash", "-lc", "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1"],
        text=True,
    ).strip()
    return int(out) if out.isdigit() else 0


def main() -> None:
    report: dict[str, object] = {}

    # Shared logic singleton check.
    with GatorKernRuntime() as a, GatorKernRuntime() as b:
        addr_a = a.logic_singleton_addr()
        addr_b = b.logic_singleton_addr()
    assert addr_a == addr_b and addr_a != 0
    report["shared_logic_singleton_addr"] = addr_a

    engine = MitosisEngine(root=GATOR_ROOT)
    try:
        node = engine.spawn_clone("Gator-Scout")
    except Exception:
        # Reuse existing worker if already active.
        hive = engine.hive_status()
        match = None
        for clone in hive.get("clones", []):
            if str(clone.get("name", "")).lower() == "gator-scout":
                match = clone
                break
        if not match:
            raise
        node = match
    report["spawn"] = {"name": node["name"], "pid": node["pid"], "port": node["bridge_port"]}

    # Wait for worker bridge to come online.
    scout_url = f"http://127.0.0.1:{int(node['bridge_port'])}"
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{scout_url}/health", timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if data.get("ok"):
                    break
        except Exception:
            pass
        time.sleep(0.5)

    # Shared logic test through worker.
    scout_answer = _post(
        f"{scout_url}/generate",
        {
            "prompt": "Explain vapor chamber saturation and why fin density impacts thermal runaway in constrained VRAM systems.",
            "max_tokens": 220,
        },
        timeout=180,
    )
    scout_text = str(scout_answer.get("text") or "")
    assert len(scout_text) > 80
    report["shared_logic_test_chars"] = len(scout_text)

    # Cross-pollination test: ingest as scout, query as prime.
    with tempfile.TemporaryDirectory(prefix="swarm_pdf_") as tmp:
        pdf = Path(tmp) / "scout_ingest.pdf"
        _make_pdf(pdf)
        os.environ["GATOR_NODE_NAME"] = "Gator-Scout"
        scholar = ScholarSense()
        ingest = scholar.ingest_pdf(pdf)
        report["ingest"] = {"chunks": ingest["chunks"], "vector_dim": ingest["vector_dim"]}

    prime = ScholarSense()
    query_attempts = [
        "P000 R00 vapor chamber thermal transfer phase transition limits",
        "heatsink delta-T mapping fan curve envelopes VRAM pressure hysteresis",
        "Summarize the thermal transfer findings from scout ingest",
    ]
    prime_query = None
    for q in query_attempts:
        candidate = prime.query(q, top_k=6)
        if not candidate.get("zero_context", True):
            prime_query = candidate
            break
    assert prime_query is not None, "Prime could not retrieve scout-ingested content"
    report["cross_pollination_selected_chunks"] = len(prime_query.get("selected_chunks", []))

    # VRAM ceiling for 1 prime + 1 worker.
    vram = _vram_mib()
    assert vram <= 4800, f"VRAM ceiling exceeded: {vram}"
    report["vram_mib"] = vram

    # Emit hive map snapshot for final render artifact.
    hive = engine.hive_status()
    hive_map = GATOR_ROOT / "bin" / "gator_map" / "gator_hive_map.json"
    hive_map.parent.mkdir(parents=True, exist_ok=True)
    hive_map.write_text(json.dumps(hive, indent=2), encoding="utf-8")
    report["hive_map"] = str(hive_map)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
