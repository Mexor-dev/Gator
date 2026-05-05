#!/usr/bin/env python3
"""
tests/test_lance_scratchpad.py

Lance of Larger Thinking — Scratchpad Test Suite
=================================================

Three tests covering state isolation, coherence stitching, and VRAM stability.

Requirements:
  - llama-server running at 127.0.0.1:8081
  - gator-bridge running at 127.0.0.1:8090
  - nvidia-smi available (Test 3 degrades gracefully without it)

Run:
    cd ~/Gator
    venv/bin/python tests/test_lance_scratchpad.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

# Allow running from project root or tests/ directory
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from memory_core import GatorMemoryCore, MemoryCoreError  # noqa: E402

LLAMA_URL = "http://127.0.0.1:8081"
BRIDGE_URL = "http://127.0.0.1:8090"

_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_RST = "\033[0m"
PASS = f"{_GREEN}PASS{_RST}"
FAIL = f"{_RED}FAIL{_RST}"
WARN = f"{_YELLOW}WARN{_RST}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _post(url: str, payload: dict, timeout: float = 180.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_vram_mib() -> int | None:
    """Poll current GPU VRAM usage via nvidia-smi. Returns None if unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            timeout=5,
        ).strip().splitlines()[0]
        return int(out.strip())
    except Exception:
        return None


def _bridge_alive() -> bool:
    try:
        req = urllib.request.Request(f"{BRIDGE_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Test 1: State Isolation Test (Unit)
# ---------------------------------------------------------------------------

def test_state_isolation() -> None:
    """
    Goal: Prove LanceDB is not leaking memory between turns.

    Approach: write 3 thought chunks for session A, verify they exist, flush,
    assert 0 rows remain. Also verify that flushing session A does not affect
    a simultaneously-open session B.
    """
    print("\n[Test 1] State Isolation Test (Unit)...")

    mc = GatorMemoryCore(server_url=LLAMA_URL)
    session_a = f"iso_A_{uuid.uuid4().hex[:12]}"
    session_b = f"iso_B_{uuid.uuid4().hex[:12]}"

    # Patch _embed_text so the unit test is independent of embedding server
    zero_vec = [0.0] * 1536
    with patch.object(mc, "_embed_text", return_value=(zero_vec, "mock://unit")):

        # --- Session A: write 3 chunks and verify ---
        mc.init_scratchpad(session_a)
        mc.commit_thought(session_a, step=0, text="Input preprocessing: tokenise and normalise the incoming data stream.")
        mc.commit_thought(session_a, step=1, text="Intermediate computation: apply logit bias matrix to hidden states.")
        mc.commit_thought(session_a, step=2, text="Conclusion: verify output distribution against expected entropy bounds.")

        before = mc._scratchpad_count(session_a)
        assert before == 3, f"Expected 3 rows before flush, got {before}"

        # Verify retrieve_context respects step ordering
        ctx_step1 = mc.retrieve_context(session_a, current_step=1)
        assert "Step 0" in ctx_step1, "retrieve_context should return step 0 when current_step=1"
        assert "Step 1" not in ctx_step1, "retrieve_context must exclude current_step"

        ctx_all = mc.retrieve_context(session_a, current_step=3)
        assert "Step 0" in ctx_all and "Step 1" in ctx_all and "Step 2" in ctx_all, (
            "retrieve_context(current_step=3) should include all 3 prior steps"
        )

        # --- Session B: open a concurrent session ---
        mc.init_scratchpad(session_b)
        mc.commit_thought(session_b, step=0, text="Session B content — must survive flush of session A.")
        assert mc._scratchpad_count(session_b) == 1, "Session B row should exist before A is flushed"

        # --- Flush session A and assert isolation ---
        deleted = mc.flush_scratchpad(session_a)
        assert deleted == 3, f"flush_scratchpad should return 3 (rows deleted), got {deleted}"

        after_a = mc._scratchpad_count(session_a)
        assert after_a == 0, f"Expected 0 rows for session A after flush, got {after_a}"

        after_b = mc._scratchpad_count(session_b)
        assert after_b == 1, (
            f"Cross-session contamination: session B had {after_b} rows after flushing session A "
            f"(expected 1)"
        )

        # --- Double-flush is safe (idempotent) ---
        deleted2 = mc.flush_scratchpad(session_a)
        assert deleted2 == 0, f"Second flush should return 0, got {deleted2}"

        # Cleanup session B
        mc.flush_scratchpad(session_b)

    print(f"[Test 1] {PASS}  isolation=OK  ordering=OK  idempotent_flush=OK  cross_session=clean")


# ---------------------------------------------------------------------------
# Test 2: Coherence Stitch Test (Integration)
# ---------------------------------------------------------------------------

def test_coherence_stitch() -> None:
    """
    Goal: Prove the 1.5B model can maintain analytical depth and persona
    across a broken context window via the Lance multi-pass loop.

    Assertion criteria:
      1. Output contains mathematical content (equations / exponential notation).
      2. Output contains a conclusion-type statement.
      3. Output contains at least 2 period-appropriate / formal vocabulary words
         that a 1930s scientist would plausibly use.
      4. Output is coherent (alpha-ratio > 0.70, len >= 150 chars).
    """
    print("\n[Test 2] Coherence Stitch Test (Integration)...")

    if not _bridge_alive():
        raise RuntimeError(f"gator-bridge not reachable at {BRIDGE_URL}. Start it first.")

    # Prompt forces chain_of_thought classification (multi-part, analytical, no code keywords)
    # and is >= 6 words, so the Lance routing path is triggered.
    STITCH_PROMPT = (
        "Explain exponential growth in three parts as a 1930s scientist would. "
        "Part 1: the precise mathematical definition with equations. "
        "Part 2: a concrete example from population biology with numbers. "
        "Part 3: a philosophical conclusion about scale and inevitability."
    )

    result = _post(
        f"{BRIDGE_URL}/generate",
        {"prompt": STITCH_PROMPT, "max_tokens": 750, "temperature": 0.75, "top_k": 40},
        timeout=300.0,
    )

    text: str = result.get("text", "")

    # --- Assertion 1: output length and coherence ---
    alpha_ratio = sum(1 for c in text if c.isalpha()) / max(1, len(text))
    assert len(text) >= 150, f"Output too short ({len(text)} chars)"
    assert alpha_ratio >= 0.70, f"Low alpha ratio {alpha_ratio:.2f} — output may be garbage"

    # --- Assertion 2: mathematical content ---
    math_pattern = re.compile(
        r'[=\^×]'                               # explicit operators
        r'|\b\d+\s*[\^*]\s*\d+'                # numeric exponent
        r'|\be\^|\bN_0\b|\bN\(t\)'             # common notation
        r'|\b(exponential|equation|formula|'
        r'growth\s+rate|function|geometric|'
        r'derivative|coefficient|proportion)\b',
        re.I,
    )
    assert math_pattern.search(text), (
        f"No mathematical content detected in output:\n{text[:400]}"
    )

    # --- Assertion 3: conclusion present ---
    conclusion_pattern = re.compile(
        r'\b(therefore|thus|hence|conclude|conclusion|inevitab|ultimately|'
        r'in\s+sum|ergo|thence|wherefore|manifest|it\s+follows|accordingly)\b',
        re.I,
    )
    assert conclusion_pattern.search(text), (
        f"No conclusion-type language found in output:\n{text[:400]}"
    )

    # --- Assertion 4: period-appropriate vocabulary ---
    # Words a 1930s scientist or formal technical writer would use.
    archaic_pattern = re.compile(
        r'\b(hitherto|heretofore|aforementioned|herein|whereby|wherein|thereof|'
        r'thence|therein|verily|propound|whence|naught|nought|endeavour|'
        r'whilst|amongst|upon|indeed|profound|remarkable|extraordinary|'
        r'observation|hypothesis|theorem|postulate|manifest|ascertain|'
        r'henceforth|forthwith|inasmuch|insofar|demonstrate|establish|'
        r'apparent|evident|noted|observed|recorded|derived|calculated|'
        r'therefore|thus|hence|ergo|accordingly|consequently)\b',
        re.I,
    )
    archaic_matches = archaic_pattern.findall(text)
    unique_archaic = list(dict.fromkeys(m.lower() for m in archaic_matches))
    assert len(unique_archaic) >= 2, (
        f"Expected >= 2 period-appropriate vocabulary hits, found {unique_archaic} "
        f"in output:\n{text[:500]}"
    )

    print(
        f"[Test 2] {PASS}  len={len(text)}  alpha={alpha_ratio:.2f}  "
        f"math=OK  conclusion=OK  archaic_words={unique_archaic[:5]}"
    )


# ---------------------------------------------------------------------------
# Test 3: VRAM Heisenberg Stress Test (System)
# ---------------------------------------------------------------------------

def test_vram_heisenberg() -> None:
    """
    Goal: Ensure 50 rapid commit_thought + retrieve_context operations do not
    cause linear VRAM growth that would indicate a GPU memory leak.

    Monitoring: poll nvidia-smi every 10 iterations.
    Failure condition: VRAM growth > 200 MiB AND monotonically increasing.
    """
    print("\n[Test 3] VRAM Heisenberg Stress Test (System)...")

    ITERATIONS = 50
    SAMPLE_EVERY = 10
    LEAK_THRESHOLD_MIB = 200

    mc = GatorMemoryCore(server_url=LLAMA_URL)
    session_id = f"heisenberg_{uuid.uuid4().hex[:12]}"
    zero_vec = [0.0] * 1536

    vram_samples: list[int] = []
    t_start = time.perf_counter()

    baseline = _get_vram_mib()
    if baseline is not None:
        vram_samples.append(baseline)
        print(f"  Baseline VRAM: {baseline} MiB")
    else:
        print(f"  {WARN}: nvidia-smi unavailable \u2014 VRAM monitoring disabled")

    with patch.object(mc, "_embed_text", return_value=(zero_vec, "mock://stress")):
        mc.init_scratchpad(session_id)

        for i in range(ITERATIONS):
            mc.commit_thought(
                session_id,
                step=i,
                text=(
                    f"Stress iteration {i}: the distributed inference pipeline "
                    f"scales horizontally under vectorised multi-pass load. "
                    f"Token trajectory divergence at step {i} is within nominal bounds."
                ),
            )
            # Retrieve context grows with each step (realistic workload)
            _ = mc.retrieve_context(session_id, current_step=i)

            if (i + 1) % SAMPLE_EVERY == 0:
                sample = _get_vram_mib()
                if sample is not None:
                    vram_samples.append(sample)
                    print(f"  [{i + 1:3d}/{ITERATIONS}] VRAM: {sample} MiB")

        mc.flush_scratchpad(session_id)

    # Final VRAM sample post-flush
    final = _get_vram_mib()
    if final is not None:
        vram_samples.append(final)
        print(f"  Post-flush VRAM: {final} MiB")

    elapsed = time.perf_counter() - t_start
    ops_per_sec = ITERATIONS / elapsed
    print(f"  Throughput: {ops_per_sec:.1f} ops/s over {ITERATIONS} iterations ({elapsed:.1f}s)")

    # --- VRAM growth analysis ---
    if len(vram_samples) >= 3:
        vram_min = min(vram_samples)
        vram_max = max(vram_samples)
        growth = vram_max - vram_min

        # Monotonic increase detection: every sample >= the previous
        is_monotonic = all(
            vram_samples[j] <= vram_samples[j + 1]
            for j in range(len(vram_samples) - 1)
        )

        print(
            f"  VRAM range: {vram_min}\u2013{vram_max} MiB  "
            f"growth={growth} MiB  monotonic={is_monotonic}"
        )

        if growth > LEAK_THRESHOLD_MIB and is_monotonic:
            raise MemoryError(
                f"Memory Leak Detected: VRAM grew monotonically by {growth} MiB "
                f"over {ITERATIONS} scratchpad operations "
                f"(threshold {LEAK_THRESHOLD_MIB} MiB). "
                f"Samples: {vram_samples}"
            )

        assert growth <= LEAK_THRESHOLD_MIB, (
            f"Memory Leak Detected: VRAM grew by {growth} MiB "
            f"(threshold {LEAK_THRESHOLD_MIB} MiB). "
            f"Samples: {vram_samples}"
        )
    else:
        print(f"  {WARN}: insufficient VRAM samples \u2014 growth check skipped")

    # Confirm post-flush row count is clean
    final_count = mc._scratchpad_count(session_id)
    assert final_count == 0, f"Scratchpad not clean after flush: {final_count} rows remain"

    print(f"[Test 3] {PASS}  throughput={ops_per_sec:.1f} ops/s  scratchpad_clean=OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 64)
    print("  Lance of Larger Thinking \u2014 Scratchpad Test Suite")
    print("=" * 64)

    tests = [
        ("test_state_isolation",  test_state_isolation),
        ("test_coherence_stitch", test_coherence_stitch),
        ("test_vram_heisenberg",  test_vram_heisenberg),
    ]

    results: dict[str, str] = {}

    for name, fn in tests:
        try:
            fn()
            results[name] = "PASS"
        except AssertionError as exc:
            print(f"  {_RED}ASSERTION{_RST}: {exc}")
            results[name] = "FAIL"
        except MemoryError as exc:
            print(f"  {_RED}MEMORY LEAK{_RST}: {exc}")
            results[name] = "FAIL"
        except Exception as exc:
            print(f"  {_RED}ERROR{_RST}: {type(exc).__name__}: {exc}")
            results[name] = "ERROR"

    print()
    print("=" * 64)
    print("  Results:")
    all_pass = True
    for name, r in results.items():
        icon = "\u2713" if r == "PASS" else "\u2717"
        colour = _GREEN if r == "PASS" else _RED
        print(f"  {colour}{icon}{_RST}  {name}: {r}")
        if r != "PASS":
            all_pass = False
    print("=" * 64)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
