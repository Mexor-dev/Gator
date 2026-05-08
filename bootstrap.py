#!/usr/bin/env python3
"""Iron-Gator Brain-Start sequence.

Four-phase boot:
  1. Kernel Map     — load libgator_kern.so, verify mmap interface
  2. Gate Loading   — load logic_map.gate, self-heal to .prev on corruption
  3. Bridge Ignition— start GatorBridge with Cold-Path Boost + VRAM Guard
  4. Chassis Link   — connect 1.5B model, run Deep-Logic self-test

Abort policy: any phase failure prints a Technical Manual log line and exits
with a non-zero status. Phase 4 failure also rolls the bridge back.
"""
from __future__ import annotations

import ctypes
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

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

CONF = ROOT / "config" / "iron_gator.env"
LIB_INTREE = ROOT / "src" / "inference" / "libgator_kern.so"
LIB_SYSTEM = Path("/usr/local/lib/libgator_kern.so")
GATE = ROOT / "bin" / "logic_map.gate"
GATE_PREV = ROOT / "bin" / "logic_map.gate.prev"
BRIDGE_PID = ROOT / "bin" / "gator_bridge.pid"
BRIDGE_URL = os.environ.get("GATOR_BRIDGE_URL", "http://127.0.0.1:8090")
SERVER_URL = os.environ.get("GATOR_SERVER_URL", "http://127.0.0.1:8081")
DEEP_LOGIC_PROMPT = (
    "In one paragraph: explain the relationship between libgator_kern.so "
    "and logic_map.gate, and how the bridge mediates them."
)
DEEP_LOGIC_KEYWORDS = ("libgator_kern", "logic_map", "bridge")


def log(phase: str, msg: str) -> None:
    print(f"[brain-start][{phase}] {msg}", flush=True)


def fatal(phase: str, msg: str) -> "None":
    print(f"[brain-start][{phase}][ABORT] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if CONF.exists():
        for line in CONF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v
            os.environ.setdefault(k, v)
    return env


# --------------------------------------------------------------------------- #
# Phase 1 — Kernel Map
# --------------------------------------------------------------------------- #
def phase1_kernel() -> ctypes.CDLL:
    log("1/4 KERNEL", "loading libgator_kern.so")
    so_path = LIB_SYSTEM if LIB_SYSTEM.exists() else LIB_INTREE
    if not so_path.exists():
        fatal("1/4 KERNEL", f"libgator_kern.so missing at {LIB_SYSTEM} and {LIB_INTREE}")
    try:
        lib = ctypes.CDLL(str(so_path))
    except OSError as e:
        fatal("1/4 KERNEL", f"dlopen failed: {e}")

    # Verify the mmap interface — we need at least one well-known symbol.
    # The kernel must publish either gator_kern_init OR gator_kern_version.
    have = []
    for sym in ("gator_kern_init", "gator_kern_version", "gator_kern_handshake"):
        if hasattr(lib, sym):
            have.append(sym)
    if not have:
        fatal("1/4 KERNEL", "no recognised gator_kern_* symbol exported")
    log("1/4 KERNEL", f"OK  path={so_path}  symbols={have}")
    return lib


# --------------------------------------------------------------------------- #
# Phase 2 — Gate Loading (with self-heal)
# --------------------------------------------------------------------------- #
def _load_gate(path: Path) -> dict:
    with gzip.open(path, "rb") as fh:
        return pickle.load(fh)


def phase2_gate() -> int:
    log("2/4 GATE", f"loading {GATE}")
    if not GATE.exists():
        log("2/4 GATE", "primary missing — attempting .prev self-heal")
        if not GATE_PREV.exists():
            fatal("2/4 GATE", "no logic_map.gate and no .prev fallback")
        shutil.copy(GATE_PREV, GATE)

    try:
        data = _load_gate(GATE)
    except Exception as exc:
        log("2/4 GATE", f"corruption detected: {exc} — reverting to .prev")
        if not GATE_PREV.exists():
            fatal("2/4 GATE", "corrupted gate and no .prev fallback")
        shutil.copy(GATE_PREV, GATE)
        try:
            data = _load_gate(GATE)
        except Exception as exc2:
            fatal("2/4 GATE", f".prev also unreadable: {exc2}")

    records = data.get("records") or []
    if not records:
        fatal("2/4 GATE", "gate loaded but contains zero records")
    log("2/4 GATE", f"OK  records={len(records)}  vocab={data.get('vocab_size')}  top_k={data.get('top_k')}")
    return len(records)


# --------------------------------------------------------------------------- #
# Phase 3 — Bridge Ignition
# --------------------------------------------------------------------------- #
def _is_bridge_up(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{BRIDGE_URL}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def phase3_bridge() -> None:
    log("3/4 BRIDGE", "ignition")
    os.environ.setdefault("GATOR_GRAFT_BIAS_SCALE", "4.0")
    os.environ.setdefault("GATOR_VRAM_GUARD_MIB", "2200")
    os.environ.setdefault("GATOR_COLD_PATH_BOOST", "1.5")
    os.environ.setdefault("GATOR_REPEAT_PENALTY", "1.18")
    os.environ.setdefault("GATOR_PRESENCE_PENALTY", "0.4")
    os.environ.setdefault("GATOR_FREQUENCY_PENALTY", "0.3")

    if _is_bridge_up():
        log("3/4 BRIDGE", "already alive — reusing")
        return

    launcher = ROOT / "wakeup"
    if not launcher.exists():
        fatal("3/4 BRIDGE", f"missing launcher: {launcher}")

    log("3/4 BRIDGE", f"spawning {launcher}")
    proc = subprocess.Popen(
        ["bash", str(launcher)],
        cwd=ROOT,
        env={**os.environ, "GATOR_DAEMON": "true"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 60.0
    while time.time() < deadline:
        if _is_bridge_up():
            log("3/4 BRIDGE", f"OK  pid={proc.pid}  url={BRIDGE_URL}")
            return
        time.sleep(1.0)
    fatal("3/4 BRIDGE", "bridge failed to come up within 60s")


# --------------------------------------------------------------------------- #
# Phase 4 — Chassis Link + Deep-Logic self-test
# --------------------------------------------------------------------------- #
def _bridge_generate(prompt: str, timeout: float = 30.0) -> str:
    body = json.dumps({"prompt": prompt, "max_tokens": 160, "temperature": 0.3}).encode()
    req = urllib.request.Request(
        f"{BRIDGE_URL}/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return (d.get("text") or d.get("response") or "").strip()


def _rollback_bridge() -> None:
    if BRIDGE_PID.exists():
        try:
            pid = int(BRIDGE_PID.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            log("4/4 CHASSIS", f"rolled back bridge pid={pid}")
        except Exception as exc:
            log("4/4 CHASSIS", f"rollback warn: {exc}")


def phase4_chassis() -> None:
    log("4/4 CHASSIS", "deep-logic self-test")
    try:
        answer = _bridge_generate(DEEP_LOGIC_PROMPT)
    except Exception as exc:
        _rollback_bridge()
        fatal("4/4 CHASSIS", f"chassis unreachable: {exc}")

    lower = answer.lower()
    hits = [k for k in DEEP_LOGIC_KEYWORDS if k in lower]
    if len(hits) < 2:
        _rollback_bridge()
        fatal("4/4 CHASSIS",
              f"chassis cannot articulate kernel⇄gate relationship. hits={hits}\nreply={answer[:240]!r}")
    log("4/4 CHASSIS", f"OK  keywords_hit={hits}  reply_chars={len(answer)}")


# --------------------------------------------------------------------------- #
def main() -> int:
    load_env()
    log("init", f"root={ROOT}")
    phase1_kernel()
    records = phase2_gate()
    phase3_bridge()
    phase4_chassis()
    print()
    print("=" * 60)
    print(" Iron-Gator BRAIN ONLINE")
    print(f"   gate_records : {records}")
    print(f"   bridge       : {BRIDGE_URL}")
    print(f"   server       : {SERVER_URL}")
    print(f"   GATOR_CTL    : status | revert | abort | health")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
