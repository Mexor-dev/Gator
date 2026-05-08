#!/usr/bin/env python3
"""
Dream Engine — autonomous reasoning during idle windows.

The Dream Cycle is when Gator's 35B donor processes accumulated context
(recent log lines, scratchpad rows, the persona reflection journal) and
writes a structured JSON record describing what it noticed, hypothesized,
or planned. Records are appended to ``logs/dream.log`` as JSON-lines so
the Command Center can stream them via SSE.

This module is invoked by ``agentic_cron.AgenticCronRunner`` when the
``dream`` task fires; it is also runnable directly:

    python src/dream_engine.py --once
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

GATOR_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = GATOR_ROOT / "logs"
DREAM_LOG = LOG_DIR / "dream.log"
# Accept either a base URL ("http://127.0.0.1:8090") or an explicit
# /generate URL — strip a trailing /generate so we always derive the right path.
_BRIDGE_RAW = os.environ.get("GATOR_BRIDGE_URL", "http://127.0.0.1:8090").rstrip("/")
if _BRIDGE_RAW.endswith("/generate"):
    _BRIDGE_RAW = _BRIDGE_RAW[: -len("/generate")]
BRIDGE_URL = f"{_BRIDGE_RAW}/generate"

# Inputs the dream prompt scans. Truncated to keep the donor context small.
LOG_INPUTS = (
    LOG_DIR / "gator_bridge.log",
    LOG_DIR / "gator_server.log",
    LOG_DIR / "webui.log",
    LOG_DIR / "command_center.log",
)
TAIL_LINES_PER_LOG = 60
SCRATCHPAD_ROWS = 12
DREAM_MAX_TOKENS = 380
DREAM_TEMPERATURE = 0.55


DREAM_SYSTEM_HINT = (
    "DREAM_CYCLE: you are reflecting during an idle window. Output ONE JSON "
    "object on a single line with these keys: observation (str, what you "
    "noticed in recent activity), hypothesis (str, an engineering theory), "
    "next_action (str, one concrete follow-up to try), confidence (float "
    "0..1). No prose, no preamble, no code fences — just the JSON object."
)


def _tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _gather_recent_activity() -> str:
    parts: list[str] = []
    for log in LOG_INPUTS:
        lines = _tail(log, TAIL_LINES_PER_LOG)
        if lines:
            parts.append(f"### {log.name}\n" + "\n".join(lines[-TAIL_LINES_PER_LOG:]))
    if not parts:
        return "(no recent log activity)"
    blob = "\n\n".join(parts)
    # Hard cap so the donor context window stays bounded.
    return blob[-12000:]


def _bridge_call(prompt: str, timeout: float = 90.0) -> dict[str, Any]:
    payload = json.dumps(
        {
            "prompt": prompt,
            "max_tokens": DREAM_MAX_TOKENS,
            "temperature": DREAM_TEMPERATURE,
            "top_k": 40,
            "top_p": 0.9,
            "min_p": 0.05,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _parse_donor_json(text: str) -> dict[str, Any]:
    """Best-effort: pull the first {...} JSON object out of donor output."""
    if not text:
        return {}
    s = text.strip()
    # Strip code fences if model added them
    for fence in ("```json", "```"):
        if s.startswith(fence):
            s = s[len(fence):].lstrip()
            if s.endswith("```"):
                s = s[:-3].rstrip()
    # Find first {...} balanced span
    start = s.find("{")
    if start < 0:
        return {"raw_text": text[:600]}
    depth = 0
    end = -1
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return {"raw_text": text[:600]}
    try:
        return json.loads(s[start:end])
    except json.JSONDecodeError:
        return {"raw_text": text[:600]}


def _append_dream(record: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with DREAM_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_dream_once(*, trigger: str = "scheduled") -> dict[str, Any]:
    """Execute one dream cycle. Always writes a record to dream.log."""
    started = time.time()
    activity = _gather_recent_activity()
    prompt = (
        f"{DREAM_SYSTEM_HINT}\n\n"
        f"=== Recent system activity (truncated tails) ===\n{activity}\n"
        f"=== End activity ===\n\n"
        f"Reflect now. Produce the JSON object."
    )

    record: dict[str, Any] = {
        "ts": started,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "trigger": trigger,
        "observation": "",
        "hypothesis": "",
        "next_action": "",
        "confidence": 0.0,
        "ok": False,
        "elapsed_s": 0.0,
    }

    try:
        resp = _bridge_call(prompt)
        text = (resp or {}).get("text", "") or ""
        parsed = _parse_donor_json(text)
        record.update(
            {
                "observation": str(parsed.get("observation", ""))[:600],
                "hypothesis": str(parsed.get("hypothesis", ""))[:600],
                "next_action": str(parsed.get("next_action", ""))[:300],
                "confidence": float(parsed.get("confidence", 0.0) or 0.0),
                "ok": True,
                "raw_text": parsed.get("raw_text"),
                "pipeline_trace": resp.get("pipeline_trace"),
            }
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        record["ok"] = False
        record["error"] = f"bridge_unreachable: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        record["ok"] = False
        record["error"] = f"dream_failure: {exc}"
    finally:
        record["elapsed_s"] = round(time.time() - started, 3)
        # Drop any None values so the JSON-line stays compact.
        record = {k: v for k, v in record.items() if v is not None}
        _append_dream(record)

    return record


def tail_dream_log(n: int = 50) -> Iterable[dict[str, Any]]:
    """Yield the last ``n`` dream records (newest last)."""
    if not DREAM_LOG.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in _tail(DREAM_LOG, n):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line[:400]})
    return out


def _main() -> int:
    parser = argparse.ArgumentParser(description="Gator Dream Engine")
    parser.add_argument("--once", action="store_true", help="Run a single dream cycle")
    parser.add_argument("--tail", type=int, default=0, help="Print last N dream records")
    parser.add_argument("--trigger", default="manual", help="Trigger label written into the record")
    args = parser.parse_args()

    if args.tail > 0:
        for rec in tail_dream_log(args.tail):
            print(json.dumps(rec, ensure_ascii=False))
        return 0
    if args.once:
        rec = run_dream_once(trigger=args.trigger)
        print(json.dumps(rec, indent=2))
        return 0 if rec.get("ok") else 1
    parser.error("provide --once or --tail N")
    return 2


if __name__ == "__main__":
    sys.exit(_main())
