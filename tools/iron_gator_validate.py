#!/usr/bin/env python3
"""Iron-Gator validation suite.

Pings every endpoint surfaced by the Vitals UI, and simulates the OpenClaw
transport layer by pushing a prompt through the Gator bridge using the same
payload shape that telegram_hive.py uses (see TelegramHiveGateway._handle_message,
src/interfaces/telegram_hive.py:181).

Exit code 0 == all green; non-zero == at least one failure.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WEBUI = "http://127.0.0.1:8080"
BRIDGE = "http://127.0.0.1:8090"

# (method, url, body_or_none, accept_non_200)
CHECKS: list[tuple[str, str, dict | None, bool]] = [
    ("GET", f"{BRIDGE}/health", None, False),
    ("GET", f"{WEBUI}/api/health", None, False),
    ("GET", f"{WEBUI}/api/vitals", None, False),
    ("GET", f"{WEBUI}/graph", None, False),
    ("GET", f"{WEBUI}/api/voice/status", None, False),
    ("GET", f"{WEBUI}/api/telegram/status", None, False),
    ("GET", f"{WEBUI}/api/config/telegram", None, False),
    ("GET", f"{WEBUI}/htmx/vitals", None, False),
    ("GET", f"{WEBUI}/htmx/greenlight", None, False),
    ("GET", f"{WEBUI}/htmx/vram", None, False),
    ("GET", f"{WEBUI}/htmx/hive", None, False),
    ("GET", f"{WEBUI}/htmx/cron_status", None, False),
    ("GET", f"{WEBUI}/htmx/tools_stream", None, False),
    ("GET", f"{WEBUI}/htmx/debug", None, False),
    ("GET", f"{WEBUI}/api/ingest_status", None, False),
]


def _hit(method: str, url: str, body: dict | None, allow_non_200: bool) -> tuple[bool, int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            txt = resp.read().decode("utf-8", errors="replace")[:200]
            return True, resp.status, txt
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace")[:200] if exc.fp else ""
        return allow_non_200, exc.code, body_txt
    except Exception as exc:
        return False, 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    failures: list[str] = []
    print(f"=== Iron-Gator Validation Suite @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    for method, url, body, allow in CHECKS:
        ok, code, snippet = _hit(method, url, body, allow)
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {method:4s} {code:>3d}  {url}")
        if not ok:
            failures.append(f"{method} {url} -> {code} {snippet}")

    # OpenClaw transport simulation: same payload shape as telegram_hive uses.
    print("--- OpenClaw transport simulation (bridge /generate via Telegram-shaped payload) ---")
    payload = {"prompt": "iron gator validation ping", "max_tokens": 24, "temperature": 0.4, "top_k": 40}
    ok, code, snippet = _hit("POST", f"{BRIDGE}/generate", payload, False)
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] POST {code:>3d}  /generate  body={snippet}")
    if not ok:
        failures.append(f"OpenClaw transport simulation -> {code}")

    # Vitals payload semantic check.
    try:
        with urllib.request.urlopen(f"{WEBUI}/api/vitals", timeout=30) as resp:
            vitals = json.loads(resp.read().decode())
        status = vitals.get("status")
        tg = vitals.get("telegram", {})
        print(f"--- vitals.status={status!r}  telegram.state={tg.get('state')!r}  alive={tg.get('alive')} ---")
        if status not in ("PASS", "OK", "NOMINAL"):
            failures.append(f"vitals.status={status!r}")
        if not tg.get("alive"):
            failures.append("telegram not alive")
    except Exception as exc:
        failures.append(f"vitals semantic check: {exc}")

    print()
    if failures:
        print(f"=== RESULT: {len(failures)} FAILURE(S) ===")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("=== RESULT: ALL SYSTEMS NOMINAL ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
