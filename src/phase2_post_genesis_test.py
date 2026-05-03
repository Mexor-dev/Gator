#!/home/user/Gator/venv/bin/python3
"""Phase 2 post-genesis verification for BabyAGI-style priority queue."""

from __future__ import annotations

import json
import sys
import time

from maintenance import GatorMaintenance, PriorityTask


def main() -> None:
    m = GatorMaintenance()

    tasks = [
        PriorityTask(name="mock_pulse", priority=4, created_ts=time.time(), payload={"kind": "pulse"}),
        PriorityTask(name="mock_graph", priority=2, created_ts=time.time() + 0.01, payload={"kind": "graph"}),
        PriorityTask(name="mock_prune", priority=3, created_ts=time.time() + 0.02, payload={"kind": "prune"}),
        PriorityTask(name="mock_rollback", priority=1, created_ts=time.time() + 0.03, payload={"kind": "rollback"}),
    ]

    out = m.execute_priority_queue(tasks)
    expected = ["mock_rollback", "mock_graph", "mock_prune", "mock_pulse"]
    ordered = out.get("ordered", [])
    status = "PASS" if ordered == expected else "FAIL"

    report = {
        "phase": 2,
        "status": status,
        "ordered": ordered,
        "expected": expected,
        "results": out.get("results", {}),
    }
    print(json.dumps(report, indent=2))

    if status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"phase": 2, "status": "FAIL", "error": str(exc)}, indent=2), file=sys.stderr)
        raise
