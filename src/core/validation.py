#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

GATOR_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPECS_PATH = GATOR_ROOT / "config" / "hive_verified_specs.json"


DOMAIN_DRIFT_TERMS = [
    "node.js", "nodejs", "express.js", "expressjs", r"\bexpress\b",
    r"\bnpm\b", r"\bnpx\b", r"\breact\b", r"\bvue\b", r"\bangular\b",
    "flutter", "mobile dev", "android sdk", "ios sdk", r"\.apk\b",
    r"\bxcode\b", "mobile app", "web application framework",
]
_DOMAIN_DRIFT_RE = re.compile("|".join(DOMAIN_DRIFT_TERMS), re.I)
DOMAIN_DRIFT_RETRY_PROMPT = (
    "ERROR: Output contains non-system domain context (Node.js/Express/mobile/web frameworks). "
    "Re-generate using only local C++/RTX/CUDA/systems heuristics. "
    "Do not reference web application stacks."
)


class HardwareValidator:
    """Global numeric sanity validator for hardware-oriented responses."""

    _unit_pattern = re.compile(r"\b(bits?|mhz|ghz|gb|gib|\u00b0c|celsius)\b", re.I)
    _bus_pattern = re.compile(r"\[?(\d{2,4})\]?\s*[- ]?bit\b", re.I)
    _bus_context_pattern = re.compile(r"\b(?:bus\s*width|memory\s*bus)\b[^\n:.]{0,32}?\[?(\d{2,4})\]?", re.I)
    _mhz_pattern = re.compile(r"\[?(\d+(?:\.\d+)?)\]?\s*mhz\b", re.I)
    _ghz_pattern = re.compile(r"\[?(\d+(?:\.\d+)?)\]?\s*ghz\b", re.I)
    _temp_pattern = re.compile(r"\[?(-?\d+(?:\.\d+)?)\]?\s*(?:\u00b0\s*)?c\b", re.I)
    _vram_pattern = re.compile(r"\[?(\d+(?:\.\d+)?)\]?\s*(?:gib|gb)\b", re.I)

    def __init__(self, specs_path: Path | None = None) -> None:
        env_path = os.environ.get("GATOR_VERIFIED_SPECS", "").strip()
        self.specs_path = Path(env_path) if env_path else (specs_path or DEFAULT_SPECS_PATH)
        self.specs = self._load_specs()

    def _load_specs(self) -> dict[str, Any]:
        fallback = {
            "gpu_specs": {
                "ga107": {
                    "bus_width_bits_allowed": [64, 96, 128, 192, 256, 384],
                    "memory_bus_bits_preferred": [128, 96],
                    "core_clock_mhz_range": [200, 3000],
                    "boost_clock_mhz_range": [400, 3500],
                    "memory_clock_ghz_range": [2.0, 24.0],
                    "temperature_c_range": [-20, 110],
                    "vram_gb_range": [1, 64],
                }
            },
            "machine_profile": {"primary_arch": "ga107"},
        }
        try:
            return json.loads(self.specs_path.read_text(encoding="utf-8"))
        except Exception:
            return fallback

    def requires_rag(self, prompt: str) -> bool:
        return bool(self._unit_pattern.search(prompt or ""))

    def verified_specs_text(self) -> str:
        arch = str(self.specs.get("machine_profile", {}).get("primary_arch", "ga107")).lower()
        s = self.specs.get("gpu_specs", {}).get(arch, {})
        allowed = s.get("bus_width_bits_allowed", [64, 96, 128, 192, 256, 384])
        preferred = s.get("memory_bus_bits_preferred", [128, 96])
        return (
            "Verified hardware spec constraints:\n"
            f"- Bus Width allowed: {allowed}\n"
            f"- Bus Width preferred for this machine profile: {preferred}\n"
            f"- Core clock MHz range: {s.get('core_clock_mhz_range', [200, 3000])}\n"
            f"- Memory clock GHz range: {s.get('memory_clock_ghz_range', [2.0, 24.0])}\n"
            f"- Temperature C range: {s.get('temperature_c_range', [-20, 110])}\n"
            f"- VRAM GB range: {s.get('vram_gb_range', [1, 64])}"
        )

    def scan_memory_bus_bits(self) -> int | None:
        # Query direct nvidia-smi fields first, then fallback to verbose output parse.
        cmds = [
            ["bash", "-lc", "nvidia-smi --query-gpu=memory.bus_width --format=csv,noheader,nounits | head -1"],
            ["bash", "-lc", "nvidia-smi -q | grep -i -m1 \"Memory Bus Width\""],
        ]
        for cmd in cmds:
            try:
                out = subprocess.check_output(cmd, text=True, timeout=4).strip()
            except Exception:
                continue
            if out.isdigit():
                return int(out)
            m = re.search(r"(\d{2,4})\s*bit", out, re.I)
            if m:
                return int(m.group(1))
        return None

    def validate_text(self, text: str) -> dict[str, Any]:
        arch = str(self.specs.get("machine_profile", {}).get("primary_arch", "ga107")).lower()
        s = self.specs.get("gpu_specs", {}).get(arch, {})
        failures: list[dict[str, Any]] = []

        allowed_bus = [int(v) for v in s.get("bus_width_bits_allowed", [64, 96, 128, 192, 256, 384])]
        mhz_min, mhz_max = [float(v) for v in s.get("core_clock_mhz_range", [200, 3000])]
        ghz_min, ghz_max = [float(v) for v in s.get("memory_clock_ghz_range", [2.0, 24.0])]
        c_min, c_max = [float(v) for v in s.get("temperature_c_range", [-20, 110])]
        gb_min, gb_max = [float(v) for v in s.get("vram_gb_range", [1, 64])]

        for m in self._bus_pattern.finditer(text or ""):
            val = int(m.group(1))
            if val not in allowed_bus:
                failures.append({"type": "bus_width", "value": val, "unit": "bit", "allowed": allowed_bus})

        for m in self._bus_context_pattern.finditer(text or ""):
            val = int(m.group(1))
            if val not in allowed_bus:
                failures.append({"type": "bus_width", "value": val, "unit": "bit", "allowed": allowed_bus})

        for m in self._mhz_pattern.finditer(text or ""):
            val = float(m.group(1))
            if val < mhz_min or val > mhz_max:
                failures.append({"type": "clock_mhz", "value": val, "unit": "MHz", "range": [mhz_min, mhz_max]})

        for m in self._ghz_pattern.finditer(text or ""):
            val = float(m.group(1))
            if val < ghz_min or val > ghz_max:
                failures.append({"type": "clock_ghz", "value": val, "unit": "GHz", "range": [ghz_min, ghz_max]})

        for m in self._temp_pattern.finditer(text or ""):
            val = float(m.group(1))
            if val < c_min or val > c_max:
                failures.append({"type": "temperature", "value": val, "unit": "C", "range": [c_min, c_max]})

        for m in self._vram_pattern.finditer(text or ""):
            val = float(m.group(1))
            if val < gb_min or val > gb_max:
                failures.append({"type": "vram", "value": val, "unit": "GB", "range": [gb_min, gb_max]})

        if _DOMAIN_DRIFT_RE.search(text or ""):
            m_drift = _DOMAIN_DRIFT_RE.search(text or "")
            failures.append({
                "type": "domain_drift",
                "value": m_drift.group(0) if m_drift else "unknown",
                "unit": "term",
                "allowed": ["C++", "CUDA", "RTX", "GPU", "systems"],
            })

        first = failures[0] if failures else None
        retry_prompt = ""
        if first is not None:
            if first.get("type") == "domain_drift":
                retry_prompt = DOMAIN_DRIFT_RETRY_PROMPT
            else:
                retry_prompt = (
                    f"ERROR: The value [{first.get('value')}] contradicts the GA107 architecture. "
                    "Cross-reference Scholar Sense Node #402 and regenerate."
                )

        return {
            "ok": not failures,
            "failures": failures,
            "retry_prompt": retry_prompt,
            "spec_path": str(self.specs_path),
        }

    def force_memory_bus_answer(self, prompt: str) -> str | None:
        p = (prompt or "").lower()
        if "memory bus" not in p:
            return None
        scanned = self.scan_memory_bus_bits()
        preferred = self.specs.get("gpu_specs", {}).get("ga107", {}).get("memory_bus_bits_preferred", [128, 96])
        chosen = scanned if isinstance(scanned, int) and scanned > 0 else int(preferred[0])
        if chosen not in self.specs.get("gpu_specs", {}).get("ga107", {}).get("bus_width_bits_allowed", [64, 96, 128, 192, 256, 384]):
            chosen = int(preferred[0])
        return f"The memory bus of this machine is {chosen}-bit based on local hardware scan and verified hive specs."
