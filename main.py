#!/usr/bin/env python3
"""Gator top-level CLI dispatcher.

Usage:
    python main.py pull <model-ref>     # download a model
        # examples:  llama3              (Ollama library)
        #            llama3:8b           (Ollama library, tag)
        #            ollama:phi3:mini    (explicit Ollama)
        #            hf:Qwen/Qwen2.5-7B-Instruct-GGUF
        #            hf:org/repo:filename.gguf
    python main.py list                 # list local models
    python main.py wakeup               # boot the full stack via ./wakeup
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

GATOR_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(GATOR_ROOT / "src"))


def _cmd_pull(argv: list[str]) -> int:
    from registry_manager import _main as registry_main  # type: ignore
    if not argv:
        sys.stderr.write("usage: main.py pull <model-ref>\n")
        return 2
    return registry_main(["pull", *argv])


def _cmd_list(_argv: list[str]) -> int:
    from registry_manager import _main as registry_main  # type: ignore
    return registry_main(["list"])


def _cmd_wakeup(_argv: list[str]) -> int:
    wakeup = GATOR_ROOT / "wakeup"
    if not wakeup.exists():
        sys.stderr.write(f"missing wakeup script at {wakeup}\n")
        return 2
    env = os.environ.copy()
    env.setdefault("GATOR_DAEMON", "true")
    return subprocess.call(["bash", str(wakeup)], env=env)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(__doc__ or "")
        return 2
    cmd, rest = argv[0], argv[1:]
    table = {
        "pull": _cmd_pull,
        "list": _cmd_list,
        "wakeup": _cmd_wakeup,
    }
    if cmd not in table:
        sys.stderr.write(f"unknown command: {cmd}\n{__doc__ or ''}")
        return 2
    return table[cmd](rest)


if __name__ == "__main__":
    raise SystemExit(main())
