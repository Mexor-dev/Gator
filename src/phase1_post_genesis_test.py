#!/home/user/Gator/venv/bin/python3
"""Phase 1 post-genesis verification for Hermes-style scaffolding."""

from __future__ import annotations

import json
import subprocess
import sys
from urllib import request

BRIDGE_URL = "http://127.0.0.1:8090"


def _post_json(url: str, payload: dict) -> dict:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _vram_mib() -> int:
    out = subprocess.check_output(
        ["bash", "-lc", "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1"],
        text=True,
    ).strip()
    return int(out)


def main() -> None:
    payload = {
        "prompt": (
            "A 4-step logic puzzle: A farmer has 17 sheep. "
            "All but 9 die. Then he buys 4 goats and sells 2 sheep. "
            "How many sheep remain and why?"
        ),
        "max_tokens": 180,
        "temperature": 0.4,
        "top_p": 0.9,
    }

    data = _post_json(f"{BRIDGE_URL}/generate", payload)
    text = data.get("text", "")
    headers = [
        "STEP_1_UNDERSTAND",
        "STEP_2_PLAN",
        "STEP_3_EXECUTE",
        "STEP_4_VERIFY",
        "FINAL_ANSWER",
    ]
    format_ok = all(h in text for h in headers)
    scaffold = bool(data.get("reasoning_scaffold", False))
    vram = _vram_mib()

    report = {
        "phase": 1,
        "status": "PASS" if scaffold and format_ok and vram < 6144 else "FAIL",
        "reasoning_scaffold": scaffold,
        "format_ok": format_ok,
        "vram_mib": vram,
        "answer_preview": text[:260],
    }
    print(json.dumps(report, indent=2))

    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"phase": 1, "status": "FAIL", "error": str(exc)}, indent=2), file=sys.stderr)
        raise
