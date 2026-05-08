#!/usr/bin/env python3
"""GATOR_CTL — Iron-Gator control tool.

Commands:
  status               — bridge + server health + VRAM + gate record count
  health               — quick HTTP check
  revert               — atomic swap logic_map.gate ← logic_map.gate.prev
                         (RCU-safe; bridge re-installs bias on next request)
  abort [--timeout MS] — engine-abort + revert-gate-prev in one shot
  vram                 — current VRAM usage / guard threshold
  ttft                 — measure first-token latency

Flag aliases (Technical-Manual style accepted by --abort):
  --engine-abort  --revert-gate-prev  --timeout 5ms
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pickle
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "bin" / "logic_map.gate"
GATE_PREV = ROOT / "bin" / "logic_map.gate.prev"
BRIDGE_PID = ROOT / "bin" / "gator_bridge.pid"
SERVER_PID = ROOT / "bin" / "gator_server.pid"
BRIDGE_URL = os.environ.get("GATOR_BRIDGE_URL", "http://127.0.0.1:8090")
SERVER_URL = os.environ.get("GATOR_SERVER_URL", "http://127.0.0.1:8081")
VRAM_GUARD = int(os.environ.get("GATOR_VRAM_GUARD_MIB", "2200"))


def _http_get_status(url: str, timeout: float = 2.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception:
        return 0


def _vram_used_mib() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True, timeout=4,
        )
        return int(out.strip().splitlines()[0])
    except Exception:
        return -1


def _gate_records(path: Path) -> int:
    try:
        with gzip.open(path, "rb") as fh:
            d = pickle.load(fh)
        return len(d.get("records") or [])
    except Exception:
        return 0


def cmd_status(_args) -> int:
    bridge = _http_get_status(f"{BRIDGE_URL}/health")
    server = _http_get_status(f"{SERVER_URL}/health")
    vram = _vram_used_mib()
    rec = _gate_records(GATE)
    prev = _gate_records(GATE_PREV)
    print(f"bridge       : HTTP {bridge}  ({BRIDGE_URL})")
    print(f"server       : HTTP {server}  ({SERVER_URL})")
    print(f"vram_used    : {vram} MiB   (guard {VRAM_GUARD} MiB)")
    print(f"gate_records : {rec}   (.prev={prev})")
    return 0 if (bridge == 200 and rec > 0) else 1


def cmd_health(_args) -> int:
    return cmd_status(_args)


def cmd_vram(_args) -> int:
    vram = _vram_used_mib()
    print(f"{vram} MiB used / guard {VRAM_GUARD} MiB")
    return 0 if 0 <= vram <= VRAM_GUARD else 2


def cmd_revert(_args) -> int:
    if not GATE_PREV.exists():
        print("[revert] no .prev fallback present", file=sys.stderr)
        return 2
    # Atomic swap: write new file then rename. ext4 rename is atomic.
    tmp = GATE.with_suffix(".gate.swap")
    shutil.copy2(GATE_PREV, tmp)
    os.replace(tmp, GATE)
    print(f"[revert] logic_map.gate ← .prev   records={_gate_records(GATE)}")
    # Nudge bridge to reload (bridge VRAM guard reloads on next request anyway)
    try:
        urllib.request.urlopen(f"{BRIDGE_URL}/reload_gate", timeout=4).read()
        print("[revert] bridge /reload_gate acknowledged")
    except Exception:
        print("[revert] bridge will pick up gate on next request")
    return 0


def cmd_abort(args) -> int:
    timeout_ms = args.timeout
    if BRIDGE_PID.exists():
        try:
            pid = int(BRIDGE_PID.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"[abort] sent SIGTERM to bridge pid={pid}")
            deadline = time.time() + (timeout_ms / 1000.0)
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    print(f"[abort] bridge exited within {timeout_ms}ms")
                    break
                time.sleep(0.05)
            else:
                os.kill(pid, signal.SIGKILL)
                print(f"[abort] SIGKILL escalated after {timeout_ms}ms")
        except Exception as exc:
            print(f"[abort] warn: {exc}", file=sys.stderr)
    if args.revert_gate_prev:
        return cmd_revert(args)
    return 0


def cmd_ttft(_args) -> int:
    body = json.dumps({"prompt": "ping", "max_tokens": 1, "temperature": 0.0}).encode()
    req = urllib.request.Request(
        f"{BRIDGE_URL}/generate", data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(1)
        ms = (time.perf_counter() - t0) * 1000.0
        print(f"TTFT: {ms:.0f} ms  (target <100 ms)")
        return 0 if ms < 100 else 1
    except Exception as exc:
        print(f"[ttft] FAIL: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="GATOR_CTL", description="Iron-Gator control tool")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("health").set_defaults(fn=cmd_health)
    sub.add_parser("vram").set_defaults(fn=cmd_vram)
    sub.add_parser("revert").set_defaults(fn=cmd_revert)
    sub.add_parser("ttft").set_defaults(fn=cmd_ttft)

    a = sub.add_parser("abort")
    a.add_argument("--timeout", type=int, default=5, help="ms grace before SIGKILL (default 5)")
    a.add_argument("--revert-gate-prev", action="store_true", default=True)
    a.set_defaults(fn=cmd_abort)

    # Flat technical-manual style:
    #   GATOR_CTL --engine-abort --revert-gate-prev --timeout 5ms
    p.add_argument("--engine-abort", action="store_true")
    p.add_argument("--revert-gate-prev", action="store_true", dest="flat_revert")
    p.add_argument("--timeout", help="e.g. 5ms / 5 / 5000us")
    return p


def _parse_timeout(raw: str | None) -> int:
    if not raw:
        return 5
    s = str(raw).strip().lower()
    if s.endswith("ms"):
        return int(s[:-2])
    if s.endswith("us"):
        return max(1, int(s[:-2]) // 1000)
    if s.endswith("s"):
        return int(s[:-1]) * 1000
    return int(s)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Flat-flag mode dispatch
    if args.engine_abort:
        ns = argparse.Namespace(
            timeout=_parse_timeout(args.timeout),
            revert_gate_prev=args.flat_revert,
        )
        return cmd_abort(ns)

    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_help()
        return 2
    if args.cmd == "abort":
        args.timeout = _parse_timeout(str(args.timeout))
    return fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
