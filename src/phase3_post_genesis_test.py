#!/usr/bin/env python3
"""Phase 3 post-genesis verification for Jarvis-style decomposition."""

from __future__ import annotations

import json
import sys

from skills import SkillArchitect


def main() -> None:
    goal = "Check the current date and write a Python script that calculates days until the next year."
    arch = SkillArchitect(server_url="http://127.0.0.1:8081")
    out = arch.run_jarvis_router(goal)

    tasks = out.tasks
    execution = out.execution
    has_scout_task = any(t.get("action") == "scout" for t in tasks)
    has_code_task = any(t.get("action") == "write_python" for t in tasks)
    scout_ok = any(e.get("action") == "scout" and e.get("ok") for e in execution)
    code_ok = any(e.get("action") == "write_python" and e.get("ok") for e in execution)

    status = "PASS" if (out.status == "PASS" and has_scout_task and has_code_task and scout_ok and code_ok) else "FAIL"
    report = {
        "phase": 3,
        "status": status,
        "goal": goal,
        "planner_tasks": tasks,
        "execution": execution,
        "review": out.review,
    }
    print(json.dumps(report, indent=2))

    if status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"phase": 3, "status": "FAIL", "error": str(exc)}, indent=2), file=sys.stderr)
        raise
