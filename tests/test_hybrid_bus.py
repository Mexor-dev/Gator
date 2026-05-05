#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import sys

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from event_bus import EventBusClient, EventBusDaemon  # noqa: E402

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


def _publish_heartbeat(client: EventBusClient, idx: int) -> float:
    start = time.perf_counter()
    resp = client.publish({"type": "heartbeat", "index": idx, "final": False})
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert resp.get("ok") is True, f"Heartbeat {idx} failed: {resp}"
    return elapsed_ms


def test_hybrid_bus_performance() -> None:
    print("\n[Test 2] Hybrid Bus Performance...")
    with tempfile.TemporaryDirectory(prefix="gator_bus_") as tmpdir:
        bus_path = Path(tmpdir) / "event_bus.sock"
        daemon = EventBusDaemon(bus_path=bus_path)
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()

        for _ in range(50):
            if bus_path.exists():
                break
            time.sleep(0.05)
        if not bus_path.exists():
            raise RuntimeError("Event bus socket did not come up")

        client = EventBusClient(bus_path=bus_path, timeout=2.0)
        _publish_heartbeat(client, -1)
        baseline = _get_vram_mib()
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            latencies = list(pool.map(lambda idx: _publish_heartbeat(client, idx), range(100)))
        final_vram = _get_vram_mib()

        daemon.stop()
        thread.join(timeout=2.0)

    avg_ms = sum(latencies) / len(latencies)
    max_ms = max(latencies)
    assert avg_ms < 5.0, f"Average heartbeat latency too high: {avg_ms:.2f} ms"
    if baseline is None or final_vram is None:
        print(f"  {WARN}: VRAM unavailable; skipped leakage check")
    else:
        delta = abs(final_vram - baseline)
        assert delta <= 16, f"Unexpected VRAM movement during bus test: {baseline} -> {final_vram} MiB"

    print(f"[Test 2] {PASS}  avg_ms={avg_ms:.2f}  max_ms={max_ms:.2f}  vram_delta={0 if baseline is None or final_vram is None else abs(final_vram - baseline)}")


def _main() -> None:
    try:
        test_hybrid_bus_performance()
    except Exception as exc:
        print(f"{FAIL} test_hybrid_bus_performance: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    _main()