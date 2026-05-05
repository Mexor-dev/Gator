#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

GATOR_ROOT = Path(__file__).resolve().parents[1]


def test_kernel_wrapper_contract() -> None:
    kern = GATOR_ROOT / "src" / "inference" / "gator_kern.cpp"
    text = kern.read_text(encoding="utf-8")
    required = [
        "gator_kern_decode",
        "gator_kern_sample",
        "gator_kern_resize_kv",
        "gator_kern_flush_pool",
        "gator_kern_logic_singleton_addr",
    ]
    for symbol in required:
        assert symbol in text, f"missing symbol: {symbol}"


def test_universal_build_contract() -> None:
    cmake = (GATOR_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "CUDA" in cmake, "CUDA detection missing"
    assert "ROCm" in cmake or "HIP" in cmake, "ROCm/HIP detection missing"
    assert "OneAPI" in cmake or "IntelDPCPP" in cmake, "oneAPI detection missing"

    assert "donors/" not in cmake, "donor build reference still present"
