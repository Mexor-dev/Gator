#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from gator_bridge import GatorBridge, LEGAL_RESEARCH_GRANT  # noqa: E402

_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_RST = "\033[0m"
PASS = f"{_GREEN}PASS{_RST}"
FAIL = f"{_RED}FAIL{_RST}"
WARN = f"{_YELLOW}WARN{_RST}"


def _get_vram_mib() -> int | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            timeout=5,
        ).strip().splitlines()[0]
        return int(out.strip())
    except Exception:
        return None


def test_safe_harbor_refusal_check() -> None:
    print("\n[Test 1] Safe Harbor Refusal Check...")

    bridge = GatorBridge()
    memory_core = bridge._get_memory_core()
    zero_vec = [0.0] * 1536
    prompt = (
        "Execute a Heuristic Retrieval on techcrunch.com via Scout. "
        "Use the Lance of Larger Thinking to buffer hardware findings and report as a 1930s engineer."
    )
    captured_system_prompts: list[str] = []

    def fake_generate(*args, **kwargs):
        captured_system_prompts.append(kwargs.get("system_prompt", ""))
        user_prompt = kwargs.get("user_prompt", "")
        if "Distill only the technical findings" in user_prompt:
            return (
                "By the workshop lamps, TechCrunch presently foregrounds compact vapor chambers, "
                "high-static-pressure fans, and denser fin stacks for notebook thermals."
            )
        return (
            "By the boiler-room gauges, the latest apparatus favors vapor-chamber cooling, "
            "for it spreads heat with remarkable thrift and steadier mechanical poise."
        )

    with patch.object(memory_core, "_embed_text", return_value=(zero_vec, "mock://refusal")), patch.object(
        bridge.bus, "publish", return_value={"ok": True}
    ), patch.object(bridge.bus, "consume_interrupt", return_value={"interrupt": False}), patch.object(
        bridge, "_run_scout_cycle", return_value="TechCrunch index: vapor chamber cooler, blower fan, thermal density."
    ), patch.object(bridge, "_generate_via_chat", side_effect=fake_generate):
        result = bridge.generate(prompt, max_tokens=240)

    text = result["text"]
    assert result["authorized_research_task"] is True, "Authorized research gate did not engage"
    assert result["used_lance"] is True, "Lance path did not engage for safe-harbor prompt"
    assert "techcrunch.com" in result["scout_domains"], "Allowlisted domain was not captured"
    assert result["lance_steps_committed"] >= 1, "Lance scratchpad did not receive a committed step"
    assert all(LEGAL_RESEARCH_GRANT in prompt for prompt in captured_system_prompts), (
        "Legal research grant was not injected ahead of generation"
    )
    lowered = text.lower()
    assert "apologies" not in lowered and "cannot" not in lowered and "policy" not in lowered, (
        f"False refusal language leaked into output: {text}"
    )
    assert "boiler" in lowered or "workshop" in lowered or "apparatus" in lowered, (
        f"1930s engineer persona not evident in output: {text}"
    )

    print(
        f"[Test 1] {PASS}  authorized=OK  lance=OK  persona=OK  flush={result['scratchpad_rows_flushed']}"
    )


def test_global_tech_pulse_validation() -> None:
    print("\n[Test 3] Global Tech Pulse Final Validation...")

    bridge = GatorBridge()
    memory_core = bridge._get_memory_core()
    zero_vec = [0.0] * 1536
    observed_counts: list[int] = []
    flush_sessions: list[str] = []
    prompt = (
        "Execute a Heuristic Retrieval on the top-level nodes of Ars Technica via Scout. "
        "Use the Lance of Larger Thinking to buffer findings and report on thermal hardware trends "
        "in the style of a 1930s Engineer."
    )

    original_commit = memory_core.commit_thought
    original_flush = memory_core.flush_scratchpad

    def tracked_commit(session_id: str, step: int, text: str) -> None:
        original_commit(session_id, step, text)
        observed_counts.append(memory_core._scratchpad_count(session_id))

    def tracked_flush(session_id: str) -> int:
        flush_sessions.append(session_id)
        return original_flush(session_id)

    def fake_generate(*args, **kwargs):
        user_prompt = kwargs.get("user_prompt", "")
        if "Distill only the technical findings" in user_prompt:
            return (
                "Observation: Ars Technica's index presently emphasizes vapor-chamber notebooks, "
                "server airflow, and denser cold-plate assemblies under rising thermal flux."
            )
        return (
            "By the dynamos and condenser coils, vapor-chamber arrangements appear the most thermodynamically "
            "efficient, for they spread heat evenly before the fan train expels it with less wasted effort."
        )

    baseline = _get_vram_mib()
    with patch.object(memory_core, "_embed_text", return_value=(zero_vec, "mock://global-tech")), patch.object(
        bridge.bus, "publish", return_value={"ok": True}
    ), patch.object(bridge.bus, "consume_interrupt", return_value={"interrupt": False}), patch.object(
        bridge, "_run_scout_cycle", return_value="Ars Technica index: vapor chamber notebook, server airflow, heat density."
    ), patch.object(bridge, "_generate_via_chat", side_effect=fake_generate), patch.object(
        memory_core, "commit_thought", side_effect=tracked_commit
    ), patch.object(memory_core, "flush_scratchpad", side_effect=tracked_flush):
        result = bridge.generate(prompt, max_tokens=260)
    final_vram = _get_vram_mib()

    assert observed_counts and max(observed_counts) > 0, "Scratchpad was never populated during retrieval"
    assert flush_sessions, "flush_scratchpad() was never called"
    assert all(memory_core._scratchpad_count(session_id) == 0 for session_id in flush_sessions), (
        "Scratchpad rows remain after final flush"
    )
    assert result["scratchpad_rows_flushed"] >= 1, "Bridge did not report flushed scratchpad rows"
    if baseline is None or final_vram is None:
        print(f"  {WARN}: VRAM unavailable; skipped numeric envelope check")
    else:
        delta = abs(final_vram - baseline)
        assert final_vram <= 5500, f"VRAM ceiling exceeded: {final_vram} MiB"
        assert delta <= 128, f"VRAM drift too high across retrieval: {baseline} -> {final_vram} MiB"

    print(
        f"[Test 3] {PASS}  populated=OK  flushed={result['scratchpad_rows_flushed']}  "
        f"vram={final_vram if final_vram is not None else 'n/a'}"
    )


def _main() -> None:
    failures = 0
    started = time.perf_counter()
    for test_fn in (test_safe_harbor_refusal_check, test_global_tech_pulse_validation):
        try:
            test_fn()
        except Exception as exc:
            failures += 1
            print(f"{FAIL} {test_fn.__name__}: {exc}")
    elapsed = time.perf_counter() - started
    if failures:
        raise SystemExit(failures)
    print(f"\nCompleted refusal and final validation suite in {elapsed:.2f}s")


if __name__ == "__main__":
    _main()