#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

def run(task: str) -> dict:
    return {
        "tool": "jit_tool_1777911621_1",
        "task": task,
        "status": "ok",
        "origin_gap": "ERROR unmet goal: missing skill for thermal ledger conversion",
    }

def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="pulse-check")
    args = parser.parse_args()
    print(json.dumps(run(args.task)))

if __name__ == "__main__":
    _main()
