#!/usr/bin/env python3
"""Comprehensive stress test for Gator bridge chat + tool execution paths."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8090"


def post(path: str, payload: dict, timeout: int = 90) -> tuple[int, dict | str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def get(path: str, timeout: int = 30) -> tuple[int, dict | str]:
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def gen(prompt: str, max_tokens: int = 500) -> tuple[int, dict | str]:
    return post("/generate", {"prompt": prompt, "max_tokens": max_tokens})


PASS = "PASS"
FAIL = "FAIL"
rows: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, note: str) -> None:
    rows.append((PASS if ok else FAIL, name, note))
    print(f"[{PASS if ok else FAIL}] {name}: {note}")


print("=" * 78)
print("GATOR FULL STRESS TEST: CHAT + TASKS + TOOL-CALLING")
print("=" * 78)

# 1) Health
code, payload = get("/health")
record("health endpoint", code == 200 and isinstance(payload, dict) and payload.get("ok") is True, str(payload)[:180])

# 2) Session reset (compat route)
code, payload = post("/session/reset", {})
record("session reset compat", code == 200 and isinstance(payload, dict) and payload.get("ok") is True, str(payload)[:180])

# 3) Human chat memory test
_, r1 = gen("Hey, call me Jordan. I am working on hardening an API gateway.")
text1 = r1.get("text", "") if isinstance(r1, dict) else str(r1)
record("human greeting", len(text1.split()) >= 4, text1[:180])

_, r2 = gen("What is my name?")
text2 = r2.get("text", "") if isinstance(r2, dict) else str(r2)
record("name memory recall", "jordan" in text2.lower(), text2[:180])

_, r3 = gen("What is my current goal?")
text3 = r3.get("text", "") if isinstance(r3, dict) else str(r3)
record("goal memory recall", "api gateway" in text3.lower() or "hardening" in text3.lower(), text3[:180])

# 4) Complex reasoning tasks
_, r4 = gen("Give me a concise threat model for a JWT-auth REST API with PostgreSQL.")
text4 = r4.get("text", "") if isinstance(r4, dict) else str(r4)
record("complex reasoning response", len(text4.split()) >= 20 and "plan locked" not in text4.lower(), text4[:220])

# 5) Ask it to code something
_, r5 = gen("Write Python code for fibonacci sequence generation.")
text5 = r5.get("text", "") if isinstance(r5, dict) else str(r5)
record("coding request", "def fibonacci" in text5.lower() or "```python" in text5.lower(), text5[:220])

# 6) Tool list endpoint
code, tools_payload = get("/api/tools")
ok_tools = (
    code == 200
    and isinstance(tools_payload, dict)
    and isinstance(tools_payload.get("tools"), list)
    and len(tools_payload.get("tools", [])) >= 4
)
record("tool inventory", ok_tools, str(tools_payload)[:220])

# 7) Direct API tool execution: file_write
code, tw = post("/api/tools/execute", {
    "tool": "file_write",
    "args": {
        "path": "tmp/stress_tool.txt",
        "content": "alpha beta gamma",
        "mode": "overwrite",
    },
    "issued_by": "Gator-Prime",
})
record("tool execute file_write", code == 200 and isinstance(tw, dict) and tw.get("ok") is True, str(tw)[:180])

# 8) Direct API tool execution: file_read
code, tr = post("/api/tools/execute", {
    "tool": "file_read",
    "args": {
        "path": "tmp/stress_tool.txt",
        "start_line": 1,
        "end_line": 20,
    },
    "issued_by": "Gator-Prime",
})
content = tr.get("content", "") if isinstance(tr, dict) else ""
record("tool execute file_read", code == 200 and "alpha beta gamma" in content, str(tr)[:220])

# 9) Direct API tool execution: file_edit
code, te = post("/api/tools/execute", {
    "tool": "file_edit",
    "args": {
        "path": "tmp/stress_tool.txt",
        "find": "beta",
        "replace": "BETA",
        "count": 1,
    },
    "issued_by": "Gator-Prime",
})
record("tool execute file_edit", code == 200 and isinstance(te, dict) and te.get("ok") is True, str(te)[:180])

# 10) In-chat tool directive path: write/read/edit
_, gtw = gen('tool:file_write path=tmp/chat_tool.txt mode=overwrite content="hello from chat tools"')
text_gtw = gtw.get("text", "") if isinstance(gtw, dict) else str(gtw)
record("chat tool directive file_write", "write complete" in text_gtw.lower(), text_gtw[:180])

_, gtr = gen('tool:file_read path=tmp/chat_tool.txt start_line=1 end_line=10')
text_gtr = gtr.get("text", "") if isinstance(gtr, dict) else str(gtr)
record("chat tool directive file_read", "hello from chat tools" in text_gtr.lower(), text_gtr[:220])

_, gte = gen('tool:file_edit path=tmp/chat_tool.txt find=hello replace=HELLO count=1')
text_gte = gte.get("text", "") if isinstance(gte, dict) else str(gte)
record("chat tool directive file_edit", "edit complete" in text_gte.lower(), text_gte[:180])

# 11) Optional web sensor checks (direct + chat). This may fail if camoufox is unavailable.
code, ws = post("/api/tools/execute", {
    "tool": "web_sensor",
    "args": {
        "url": "http://127.0.0.1:8090/health",
        "mode": "markdown",
        "max_chars": 1200,
    },
    "issued_by": "Gator-Prime",
}, timeout=120)
ws_ok = code == 200 and isinstance(ws, dict) and ws.get("ok") is True
record("tool execute web_sensor", ws_ok, str(ws)[:220])

_, gws = gen('tool:web_sensor url=http://127.0.0.1:8090/health mode=markdown max_chars=900', max_tokens=700)
text_gws = gws.get("text", "") if isinstance(gws, dict) else str(gws)
record("chat tool directive web_sensor", "web sensor" in text_gws.lower() or "snapshot" in text_gws.lower() or "tool execution failed" in text_gws.lower(), text_gws[:220])

# 12) Regression checks for banned loop templates
_, r6 = gen("List three principles of reliable systems design.")
text6 = r6.get("text", "") if isinstance(r6, dict) else str(r6)
bad = any(k in text6.lower() for k in ("task acknowledged", "moving to execution", "working on:"))
record("template regression guard", not bad, text6[:220])

print("\n" + "=" * 78)
passed = sum(1 for x in rows if x[0] == PASS)
total = len(rows)
print(f"SUMMARY: {passed}/{total} checks passed")
print("=" * 78)
for status, name, note in rows:
    if status == FAIL:
        print(f" - FAIL {name}: {note}")

# Non-zero if any required non-web tests fail.
required_fail = False
for status, name, _ in rows:
    if status == FAIL and "web_sensor" not in name:
        required_fail = True

raise SystemExit(1 if required_fail else 0)
