#!/usr/bin/env python3
"""
Gator Command Center — focused, isolated UI on port 8000.

Independent of the legacy webui (port 8080). This UI owns three first-class
panels:

    * Chat        — native virtualized chat with SSE streaming
    * Cron Manager — view/edit schedule in agentic_cron.py state
    * Dream Engine — interval slider + live tail of logs/dream.log via SSE

Process-isolation contract:
    * Binds 127.0.0.1:8000 only.
    * Survives bridge restarts: every bridge call is wrapped, errors return
      structured JSON instead of crashing the worker.
    * No imports from gator_bridge / inference / kernel modules → cannot be
      taken down by their failures.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

GATOR_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATOR_ROOT / "src"))
STATIC_DIR = GATOR_ROOT / "src" / "interfaces" / "static"

# Local imports (kept narrow so a bridge crash cannot import-fail this UI).
from agentic_cron import (  # type: ignore  # noqa: E402
    DEFAULT_SCHEDULE,
    cron_start,
    cron_status,
    cron_stop,
    _load_state as cron_load_state,
    _save_state as cron_save_state,
)
from dream_engine import (  # type: ignore  # noqa: E402
    DREAM_LOG,
    run_dream_once,
    tail_dream_log,
)

BRIDGE_URL = os.environ.get("GATOR_BRIDGE_URL", "http://127.0.0.1:8090")
GENERATE_URL = f"{BRIDGE_URL}/generate"
HEALTH_URL = f"{BRIDGE_URL}/health"
SESSION_RESET_URL = f"{BRIDGE_URL}/api/session_reset"
TASKS_URL = f"{BRIDGE_URL}/api/tasks"
TASK_METRICS_URL = f"{BRIDGE_URL}/api/tasks/metrics"
SKILLS_URL = f"{BRIDGE_URL}/api/skills"
SKILLS_RECENT_URL = f"{BRIDGE_URL}/api/skills/recent"
SKILLS_SEARCH_URL = f"{BRIDGE_URL}/api/skills/search"
HISTORY_URL = f"{BRIDGE_URL}/api/history/completions"

# ---------------------------------------------------------------------------
# Bridge helpers (all wrapped — never raise into the request handler)
# ---------------------------------------------------------------------------

def _bridge_post(url: str, payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            detail = {"error": str(exc)}
        return {"ok": False, "http_status": exc.code, "detail": detail}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"bridge_unreachable: {exc}"}


def _bridge_get(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    prompt: str
    max_tokens: int = 700
    temperature: float = 0.65


class CronUpdateRequest(BaseModel):
    interval_seconds: int | None = None
    dream_idle_minutes: int | None = None
    dream_every_seconds: int | None = None
    process_dream_every_seconds: int | None = None
    defrag_every_seconds: int | None = None
    architect_every_seconds: int | None = None
    enabled: bool | None = None


class DreamConfigRequest(BaseModel):
    dream_every_seconds: int


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Gator Command Center", version="1.0")

# Mount static files for logo and assets
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Logic confidence storage (in-memory, persists during UI session)
LOGIC_CONFIDENCE = {"value": 85}


@app.get("/api/logic_confidence")
def api_logic_confidence_get() -> dict[str, Any]:
    return {"ok": True, "confidence": LOGIC_CONFIDENCE["value"]}


@app.post("/api/logic_confidence")
async def api_logic_confidence_set(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        value = int(body.get("value", 85))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid request body")
    if not 50 <= value <= 100:
        raise HTTPException(status_code=400, detail="confidence must be 50-100")
    LOGIC_CONFIDENCE["value"] = value
    return {"ok": True, "confidence": value}


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    bridge = _bridge_get(HEALTH_URL, timeout=2.0)
    return {
        "ok": True,
        "ui": "command_center",
        "port": 8000,
        "bridge_reachable": bool(bridge.get("ok") is not False and "error" not in bridge),
        "bridge": bridge,
        "ts": time.time(),
    }


# ---- CHAT (SSE) -----------------------------------------------------------

@app.post("/api/chat")
def api_chat(req: ChatRequest) -> JSONResponse:
    """Non-streaming fallback for clients that don't support SSE."""
    result = _bridge_post(
        GENERATE_URL,
        {
            "prompt": req.prompt,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        },
        timeout=180.0,
    )
    return JSONResponse(result)


@app.get("/api/chat/stream")
async def api_chat_stream(prompt: str, max_tokens: int = 700, temperature: float = 0.65) -> StreamingResponse:
    """SSE stream. The bridge is non-streaming, so we run the call in a worker
    thread and emit a lightweight progress heartbeat until the response lands.
    The final payload arrives as a single ``message`` event with the full text.
    """

    async def generator() -> AsyncIterator[bytes]:
        loop = asyncio.get_event_loop()
        started = time.time()
        # Start the bridge call in a thread.
        future = loop.run_in_executor(
            None,
            _bridge_post,
            GENERATE_URL,
            {"prompt": prompt, "max_tokens": max_tokens, "temperature": temperature},
            300.0,
        )
        # Heartbeat every 1.5s until the bridge call finishes.
        while not future.done():
            elapsed = time.time() - started
            payload = json.dumps({"elapsed_s": round(elapsed, 1)})
            yield f"event: heartbeat\ndata: {payload}\n\n".encode("utf-8")
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=1.5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
        try:
            result = await future
        except Exception as exc:
            err = json.dumps({"ok": False, "error": str(exc)})
            yield f"event: error\ndata: {err}\n\n".encode("utf-8")
            return
        msg = json.dumps(
            {
                "text": (result or {}).get("text", ""),
                "ok": (result or {}).get("ok", True),
                "elapsed_s": round(time.time() - started, 2),
                "pipeline": (result or {}).get("pipeline"),
                "raw": result,
            },
            ensure_ascii=False,
        )
        yield f"event: message\ndata: {msg}\n\n".encode("utf-8")
        yield b"event: done\ndata: {}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.post("/api/chat/reset")
def api_chat_reset() -> dict[str, Any]:
    return _bridge_post(SESSION_RESET_URL, {}, timeout=10.0)


@app.get("/api/tasks")
def api_tasks() -> dict[str, Any]:
  return _bridge_get(TASKS_URL, timeout=5.0)


@app.post("/api/tasks/clear")
def api_tasks_clear() -> dict[str, Any]:
  return _bridge_post(f"{TASKS_URL}/clear", {}, timeout=10.0)


@app.get("/api/tasks/metrics")
def api_tasks_metrics() -> dict[str, Any]:
  return _bridge_get(TASK_METRICS_URL, timeout=5.0)


# ---- SKILLS & HISTORY (Gator-Flywheel) ------------------------------------

@app.get("/api/skills")
def api_skills() -> dict[str, Any]:
    return _bridge_get(SKILLS_URL, timeout=5.0)


@app.get("/api/skills/recent")
def api_skills_recent(limit: int = 5) -> dict[str, Any]:
    return _bridge_get(f"{SKILLS_RECENT_URL}?limit={limit}", timeout=5.0)


@app.get("/api/history/completions")
def api_history() -> dict[str, Any]:
    return _bridge_get(HISTORY_URL, timeout=5.0)


@app.get("/api/tools")
def api_tools() -> dict[str, Any]:
    return _bridge_get(f"{BRIDGE_URL}/api/tools", timeout=5.0)


@app.get("/api/tools/activity")
def api_tools_activity(limit: int = 20) -> dict[str, Any]:
    return _bridge_get(f"{BRIDGE_URL}/api/tools/activity?limit={limit}", timeout=5.0)


# ---- LOGIC ALIGNMENT (Iron Law Enforcement) -------------------------------

@app.post("/api/logic_alignment/inject")
async def api_logic_alignment_inject(request: Request) -> dict[str, Any]:
    """Atomically commit steering coefficient to config and trigger hot-reload."""
    try:
        body = await request.json()
        coefficient = int(body.get("coefficient", 85))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid request: {exc}")
    
    if not 50 <= coefficient <= 100:
        raise HTTPException(status_code=400, detail="coefficient must be 50-100")
    
    # Update in-memory state
    LOGIC_CONFIDENCE["value"] = coefficient
    
    # Commit to config file (atomic write)
    import json as json_module
    import time as time_module
    
    config_path = GATOR_ROOT / "config" / "logic_constraints.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    t0 = time_module.time()
    try:
        # Load current config
        if config_path.exists():
            config = json_module.loads(config_path.read_text(encoding="utf-8"))
        else:
            config = {
                "version": "1.0.0",
                "enabled": True,
                "scaling_parameters": {
                    "min_bias": 1.5,
                    "max_bias": 5.0,
                    "min_penalty": 0.0,
                    "max_penalty": -10.0,
                    "enforce_top_k_at_100": True
                }
            }
        
        # Update coefficient and timestamp
        config["steering_coefficient"] = coefficient
        config["update_timestamp"] = time_module.time()
        config["last_applied"] = {
            "coefficient": coefficient,
            "timestamp": time_module.time(),
            "applied_by": "command_center_ui"
        }
        
        # Atomic write (write to temp, then rename)
        temp_path = config_path.with_suffix(".tmp")
        temp_path.write_text(json_module.dumps(config, indent=2), encoding="utf-8")
        temp_path.replace(config_path)
        
        apply_time_ms = (time_module.time() - t0) * 1000
        
        # Trigger bridge hot-reload via API
        reload_resp = _bridge_post(
            f"{BRIDGE_URL}/api/logic_alignment/reload",
            {"source": "config_file"},
            timeout=2.0
        )
        
        return {
            "ok": True,
            "coefficient": coefficient,
            "config_path": str(config_path),
            "apply_time_ms": apply_time_ms,
            "bridge_reloaded": reload_resp.get("ok", False),
            "bridge_active_coefficient": reload_resp.get("coefficient", coefficient)
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---- TELEGRAM GATEWAY -----------------------------------------------------

@app.get("/api/telegram/status")
def api_telegram_status() -> dict[str, Any]:
    """Get Telegram gateway process and connection status."""
    import subprocess
    import os as os_module
    from pathlib import Path as Path_class
    
    pid_file = GATOR_ROOT / "bin" / "telegram_gateway.pid"
    status_file = GATOR_ROOT / "logs" / "telegram_hive_status.json"
    
    # Check PID
    pid = None
    pid_alive = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            # Check if process exists
            result = subprocess.run(
                ["ps", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False
            )
            pid_alive = result.returncode == 0
        except Exception:
            pass
    
    # Check status file
    authenticated = False
    if status_file.exists():
        try:
            status_data = json.loads(status_file.read_text(encoding="utf-8"))
            authenticated = bool(status_data.get("authenticated", False))
        except Exception:
            pass
    
    # Check event bus
    bus_ok = False
    try:
        from event_bus import EventBusClient
        bus_ok = bool(EventBusClient().doctor_query().get("ok"))
    except Exception:
        pass
    
    # Check config
    env_file = GATOR_ROOT / ".env"
    configured = False
    if env_file.exists():
        try:
            from dotenv import dotenv_values
            env = dotenv_values(env_file)
            configured = bool(
                env.get("GATOR_TG_BOT_TOKEN") and
                env.get("GATOR_TG_BOT_USERNAME") and
                env.get("GATOR_TG_AUTH_CHAT_ID")
            )
        except Exception:
            pass
    
    connected = bool(pid_alive and configured and bus_ok and authenticated)
    
    return {
        "ok": True,
        "pid": pid,
        "alive": pid_alive,
        "configured": configured,
        "authenticated": authenticated,
        "connected_event_bus": bus_ok,
        "connected": connected
    }


@app.get("/api/telegram/config")
def api_telegram_config_get() -> dict[str, Any]:
    """Get current Telegram configuration from .env."""
    env_file = GATOR_ROOT / ".env"
    if not env_file.exists():
        return {"ok": True, "token": "", "username": "", "chat_id": ""}
    
    try:
        from dotenv import dotenv_values
        env = dotenv_values(env_file)
        return {
            "ok": True,
            "token": str(env.get("GATOR_TG_BOT_TOKEN") or "").strip(),
            "username": str(env.get("GATOR_TG_BOT_USERNAME") or "").strip(),
            "chat_id": str(env.get("GATOR_TG_AUTH_CHAT_ID") or "").strip()
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/telegram/config")
async def api_telegram_config_set(request: Request) -> dict[str, Any]:
    """Save Telegram configuration to .env file."""
    try:
        body = await request.json()
        token = str(body.get("token", "")).strip()
        username = str(body.get("username", "")).strip()
        chat_id = str(body.get("chat_id", "")).strip()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid request: {exc}")
    
    if not token or not username or not chat_id:
        raise HTTPException(status_code=400, detail="all fields required")
    
    env_file = GATOR_ROOT / ".env"
    
    try:
        # Load existing env
        lines = []
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
        
        # Update or add Telegram vars
        def update_or_add(lines_list: list[str], key: str, value: str) -> list[str]:
            found = False
            for i, line in enumerate(lines_list):
                if line.startswith(f"{key}="):
                    lines_list[i] = f"{key}='{value}'"
                    found = True
                    break
            if not found:
                lines_list.append(f"{key}='{value}'")
            return lines_list
        
        lines = update_or_add(lines, "GATOR_TG_BOT_TOKEN", token)
        lines = update_or_add(lines, "GATOR_TG_BOT_USERNAME", username)
        lines = update_or_add(lines, "GATOR_TG_AUTH_CHAT_ID", chat_id)
        
        # Write back
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        
        return {"ok": True, "saved": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/telegram/restart")
def api_telegram_restart() -> dict[str, Any]:
    """Restart the Telegram gateway process."""
    import subprocess
    import time as time_module
    
    hive_script = GATOR_ROOT / "src" / "interfaces" / "telegram_hive.py"
    py_bin = GATOR_ROOT / "venv" / "bin" / "python"
    log_file = GATOR_ROOT / "logs" / "telegram_hive.log"
    pid_file = GATOR_ROOT / "bin" / "telegram_gateway.pid"
    
    try:
        # Kill existing process
        subprocess.run(["pkill", "-f", str(hive_script)], check=False)
        time_module.sleep(1)
        
        # Start new process
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "ab") as log_fp:
            proc = subprocess.Popen(
                [str(py_bin), str(hive_script)],
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
        
        # Save PID
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        
        return {"ok": True, "pid": proc.pid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---- CRON MANAGER ---------------------------------------------------------

@app.get("/api/cron")
def api_cron_get() -> dict[str, Any]:
    return cron_status()


@app.post("/api/cron")
def api_cron_update(req: CronUpdateRequest) -> dict[str, Any]:
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(status_code=400, detail="no fields to update")
    state = cron_save_state(payload)
    return {"ok": True, "state": state, "status": cron_status()}


@app.post("/api/cron/start")
def api_cron_start_route() -> dict[str, Any]:
    return cron_start()


@app.post("/api/cron/stop")
def api_cron_stop_route() -> dict[str, Any]:
    return cron_stop()


# ---- DREAM ENGINE ---------------------------------------------------------

@app.get("/api/dream/config")
def api_dream_config_get() -> dict[str, Any]:
    state = cron_load_state()
    return {
        "ok": True,
        "dream_every_seconds": int(state.get("dream_every_seconds", 120)),
        "dream_idle_minutes": int(state.get("dream_idle_minutes", 30)),
        "process_dream_every_seconds": int(state.get("process_dream_every_seconds", 180)),
        "cron_enabled": bool(state.get("enabled", False)),
    }


@app.post("/api/dream/config")
def api_dream_config_set(req: DreamConfigRequest) -> dict[str, Any]:
    if req.dream_every_seconds < 5 or req.dream_every_seconds > 86400:
        raise HTTPException(status_code=400, detail="dream_every_seconds out of range (5..86400)")
    state = cron_save_state({"dream_every_seconds": int(req.dream_every_seconds)})
    return {"ok": True, "state": state}


@app.post("/api/dream/run")
def api_dream_run_now() -> dict[str, Any]:
    """Fire one dream cycle immediately, bypassing the idle gate."""
    rec = run_dream_once(trigger="manual")
    return {"ok": True, "record": rec}


@app.get("/api/dream/tail")
def api_dream_tail(n: int = 50) -> dict[str, Any]:
    n = max(1, min(500, int(n)))
    return {"ok": True, "records": list(tail_dream_log(n))}


@app.get("/api/dream/stream")
async def api_dream_stream(request: Request) -> StreamingResponse:
    """SSE live-tail of logs/dream.log. New JSON-lines arrive as ``message`` events."""

    async def generator() -> AsyncIterator[bytes]:
        # Seed with the last 20 records so a fresh subscriber has context.
        for rec in list(tail_dream_log(20)):
            data = json.dumps(rec, ensure_ascii=False)
            yield f"event: message\ndata: {data}\n\n".encode("utf-8")
        last_size = DREAM_LOG.stat().st_size if DREAM_LOG.exists() else 0
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(1.0)
            if not DREAM_LOG.exists():
                continue
            try:
                size = DREAM_LOG.stat().st_size
            except OSError:
                continue
            if size <= last_size:
                # Heartbeat so the client connection stays warm through proxies.
                yield b": keepalive\n\n"
                continue
            try:
                with DREAM_LOG.open("rb") as f:
                    f.seek(last_size)
                    new_bytes = f.read(size - last_size)
                last_size = size
            except OSError:
                continue
            for line in new_bytes.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    data = json.dumps(rec, ensure_ascii=False)
                except json.JSONDecodeError:
                    data = json.dumps({"raw": line[:400]})
                yield f"event: message\ndata: {data}\n\n".encode("utf-8")

    return StreamingResponse(generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Gator Command Center</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
  :root {
    --bg:#0a0e12; --panel:#101720; --fg:#e0e6ed; --muted:#7d8ea5;
    --border:#1a2636; --accent:#2d8a4a; --accent2:#8b1f2a; --warn:#c9a227;
    --link:#5fa8d3; --code:#0f1620; --dash-blue:#1e4d7b; --dash-green:#1e4d2b;
    --dash-orange:#5c3a1a; --glow:#2d8a4a88;
  }
  * { box-sizing:border-box; }
  html, body { margin:0; padding:0; background:var(--bg); color:var(--fg);
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size:14px; height:100%; overflow:hidden; }
  #app { display:grid; grid-template-columns:240px minmax(0,1fr); height:100vh; }
  #sidebar { background:linear-gradient(180deg, #0d1218 0%, #0a0e12 100%); 
    border-right:1px solid var(--border); padding:0; display:flex; flex-direction:column;
    box-shadow:2px 0 12px rgba(0,0,0,0.5); }
  #sidebar h1 { font-size:13px; margin:0; letter-spacing:2px;
    color:var(--accent); text-transform:uppercase; font-weight:700; 
    text-shadow:0 0 8px var(--glow); }
  .nav { display:flex; flex-direction:column; flex:1; padding:8px 0; }
  .nav-section { margin-bottom:16px; }
  .nav-label { font-size:10px; color:var(--muted); text-transform:uppercase;
    letter-spacing:1.2px; padding:8px 16px 4px; margin-bottom:4px; font-weight:700; 
    opacity:0.7; }
  .nav button { background:transparent; border:0; color:var(--fg); text-align:left;
    padding:11px 16px 11px 24px; cursor:pointer; font:inherit; 
    border-left:3px solid transparent; transition:all 0.2s ease; position:relative; }
  .nav button:hover { background:rgba(45,138,74,0.1); color:#fff; border-left-color:rgba(45,138,74,0.5); }
  .nav button.active { background:rgba(45,138,74,0.15); border-left-color:var(--accent); 
    color:#fff; font-weight:600; box-shadow:inset 0 0 20px rgba(45,138,74,0.1); }
  #status { margin-top:auto; padding:16px; font-size:11px; color:var(--muted);
    border-top:1px solid var(--border); background:#090d11; }
  #status .status-label { font-size:9px; text-transform:uppercase; letter-spacing:1px;
    margin-bottom:8px; color:#5a6875; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%;
    background:#555; margin-right:8px; vertical-align:middle; }
  .dot.up { background:var(--accent); box-shadow:0 0 8px var(--accent); }
  .dot.down { background:var(--accent2); }
  #main { display:flex; flex-direction:column; min-width:0; height:100vh; overflow:hidden; }
  .panel { flex:1; min-height:0; display:none; flex-direction:column; padding:20px; gap:16px;
    overflow-y:auto; }
  .panel.active { display:flex; }
  h2 { margin:0 0 8px; font-size:16px; color:var(--accent); letter-spacing:0.5px; font-weight:600; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  input, textarea, select, button {
    background:var(--code); color:var(--fg); border:1px solid var(--border);
    padding:8px 12px; border-radius:4px; font:inherit; min-width:0; max-width:100%;
  }
  button { cursor:pointer; transition:all 0.15s ease; }
  button:hover { background:#1a2330; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.primary:hover { background:#35a058; }
  button.danger  { background:var(--accent2); border-color:var(--accent2); color:#fff; }
  button.danger:hover { background:#a52834; }
  button:disabled { opacity:0.5; cursor:not-allowed; }
  .card { background:var(--panel); border:1px solid var(--border);
    border-radius:6px; padding:14px; }
  .muted { color:var(--muted); font-size:12px; }
  pre, .pre { white-space:pre-wrap; word-break:break-word; background:var(--code);
    border:1px solid var(--border); border-radius:4px; padding:12px; margin:0;
    font-size:12px; line-height:1.5; }

  /* Dashboard */
  .dash-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:16px; }
  .dash-card { background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:16px; border-left:4px solid var(--accent); }
  .dash-card.blue { border-left-color:var(--link); }
  .dash-card.green { border-left-color:var(--accent); }
  .dash-card.orange { border-left-color:var(--warn); }
  .dash-card h3 { margin:0 0 12px; font-size:13px; text-transform:uppercase;
    letter-spacing:1px; color:var(--muted); font-weight:600; }
  .dash-metric { display:flex; justify-content:space-between; align-items:center;
    padding:8px 0; border-bottom:1px solid var(--border); }
  .dash-metric:last-child { border-bottom:0; }
  .dash-metric .label { color:var(--muted); font-size:12px; }
  .dash-metric .value { font-size:18px; font-weight:600; color:var(--fg); }
  .dash-status { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
  .dash-status .label { flex:1; }
  .dash-status .value { font-weight:500; }
  .ok-badge { display:inline-block; padding:3px 8px; border-radius:3px; font-size:10px;
    font-weight:600; text-transform:uppercase; letter-spacing:0.5px; }
  .ok-badge.ok { background:#1e4d2b; color:#7ad79a; }
  .ok-badge.bad { background:#4d1e23; color:#e98989; }
  .ok-badge.warn { background:#4d3d1e; color:#f0c869; }

  /* Chat */
  #chat-log { flex:1; min-height:0; overflow-y:auto; padding-right:8px;
    display:flex; flex-direction:column; gap:12px; }
  .msg { padding:12px 14px; border-radius:6px; max-width:75%;
    border:1px solid var(--border); background:var(--panel); box-shadow:0 1px 3px rgba(0,0,0,0.2); }
  .msg.user  { align-self:flex-end; border-color:#1f3a5b; background:#0d1a24; }
  .msg.gator { align-self:flex-start; border-color:#1e3a25; background:#0d1a13; }
  .msg .who { font-size:10px; color:var(--muted); margin-bottom:6px;
    text-transform:uppercase; letter-spacing:1px; font-weight:600; }
  .msg .body { white-space:pre-wrap; word-break:break-word; line-height:1.5; }
  #chat-input { display:flex; gap:10px; }
  #chat-input textarea { flex:1; min-height:70px; resize:vertical; }
  .chat-controls { display:flex; flex-direction:column; gap:8px; }
  .chat-options { display:flex; gap:12px; align-items:center; padding:8px 12px;
    background:var(--code); border:1px solid var(--border); border-radius:4px; margin-top:8px; }
  .chat-options label { font-size:12px; color:var(--muted); cursor:pointer;
    display:flex; align-items:center; gap:6px; user-select:none; }
  .chat-options input[type="checkbox"] { cursor:pointer; }

  /* Tasks Panel */
  .tasks-grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(0,1fr); gap:16px; }
  .task-list { display:flex; flex-direction:column; gap:10px; overflow-y:auto; max-height:calc(100vh - 280px); }
  .task-item { background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:12px;
    border-left:4px solid var(--warn); transition:all 0.15s ease; }
  .task-item:hover { background:#141d2a; }
  .task-item.completed { border-left-color:var(--accent); opacity:0.8; }
  .task-item.in_progress { border-left-color:var(--warn); }
  .task-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:6px; }
  .task-id { font-size:13px; font-weight:600; color:var(--link); }
  .task-title { font-size:13px; color:var(--fg); margin-bottom:6px; white-space:pre-wrap; word-break:break-word; line-height:1.4; }
  .task-sub { color:var(--muted); font-size:11px; margin-bottom:8px; }
  .task-progress { height:8px; border-radius:999px; background:#0d1520; border:1px solid var(--border); overflow:hidden; }
  .task-progress > span { display:block; height:100%; background:linear-gradient(90deg,#c9a227,#2d8a4a); transition:width 0.3s ease; }
  .task-metrics-panel { display:flex; flex-direction:column; gap:10px; }
  .metric-card { background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:12px; }
  .metric-card h3 { margin:0 0 10px; font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--muted); font-weight:600; }
  .metric-row { display:flex; justify-content:space-between; gap:8px; padding:6px 0; border-bottom:1px solid var(--border); }
  .metric-row:last-child { border-bottom:0; padding-bottom:0; }
  .metric-row .label { color:var(--muted); font-size:11px; }
  .metric-row .value { font-weight:600; color:var(--fg); }

  /* Dream live tail */
  #dream-tail { flex:1; min-height:0; overflow-y:auto; display:flex;
    flex-direction:column; gap:8px; }
  .dream-rec { background:var(--panel); border:1px solid var(--border);
    border-left:4px solid var(--accent); border-radius:4px; padding:10px 12px;
    font-size:12px; }
  .dream-rec.skipped { border-left-color:#555; opacity:0.65; }
  .dream-rec.error { border-left-color:var(--accent2); }
  .dream-rec .ts { color:var(--muted); font-size:10px; margin-bottom:4px; }
  .dream-rec .obs { margin-top:6px; }
  .dream-rec .hyp { margin-top:6px; color:#99ccdd; }
  .dream-rec .next { margin-top:6px; color:var(--warn); }

  /* Cron grid */
  .cron-grid { display:grid; grid-template-columns: 240px 1fr; gap:10px 14px; }
  .cron-grid label { color:var(--muted); align-self:center; font-size:12px; }

  .scroll { overflow-y:auto; min-height:0; flex:1; }
  
  /* Tools Panel */
  .tools-grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(0,1fr); gap:16px; }
  .tool-list { display:flex; flex-direction:column; gap:10px; max-height:calc(100vh - 260px); overflow-y:auto; 
    padding:4px; }
  .tool-item { background:linear-gradient(135deg, #101720 0%, #0d1318 100%); 
    border:1px solid var(--border); border-radius:6px;
    padding:12px 14px; border-left:3px solid var(--accent); font-size:12px; 
    transition:all 0.2s ease; box-shadow:0 2px 4px rgba(0,0,0,0.3); }
  .tool-item:hover { border-left-width:4px; box-shadow:0 4px 12px rgba(45,138,74,0.15),
    inset 0 0 20px rgba(45,138,74,0.05); transform:translateX(2px); }
  .tool-item .tool-name { font-weight:700; color:var(--link); margin-bottom:5px; 
    text-transform:uppercase; font-size:11px; letter-spacing:0.5px; }
  .tool-item .tool-desc { color:var(--muted); font-size:11px; line-height:1.4; }
  .activity-log { display:flex; flex-direction:column; gap:8px; max-height:calc(100vh - 260px); 
    overflow-y:auto; padding:4px; }
  .activity-item { background:var(--code); border:1px solid var(--border); border-radius:5px;
    padding:10px 12px; font-size:11px; border-left:3px solid #555; transition:all 0.2s ease;
    box-shadow:0 1px 3px rgba(0,0,0,0.2); }
  .activity-item.success { border-left-color:var(--accent); 
    box-shadow:0 2px 6px rgba(45,138,74,0.15); }
  .activity-item.success:hover { box-shadow:0 3px 10px rgba(45,138,74,0.25); }
  .activity-item.error { border-left-color:var(--accent2); 
    box-shadow:0 2px 6px rgba(139,31,42,0.2); background:#130f10; }
  .activity-item.error:hover { box-shadow:0 3px 10px rgba(139,31,42,0.3); }
  .activity-item .time { color:var(--muted); font-size:10px; margin-bottom:4px; 
    text-transform:uppercase; letter-spacing:0.5px; }
  .activity-item .details { color:var(--fg); font-weight:500; }
  
  @media (max-width: 1200px) {
    #app { grid-template-columns: 180px minmax(0,1fr); }
    .tasks-grid { grid-template-columns: 1fr; }
    .tools-grid { grid-template-columns: 1fr; }
    .dash-grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 768px) {
    #app { grid-template-columns: 1fr; }
    #sidebar { border-right:0; border-bottom:1px solid var(--border); }
  }
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div style="padding:16px;text-align:center;border-bottom:1px solid var(--border);">
      <img src="/static/gator-logo.png" alt="Gator" style="width:120px;height:auto;margin-bottom:8px;border-radius:8px;"/>
      <h1 style="margin:0;font-size:14px;">GATOR COMMAND</h1>
    </div>
    <nav class="nav">
      <div class="nav-section">
        <div class="nav-label">Overview</div>
        <button data-panel="dashboard" class="active">Dashboard</button>
      </div>
      <div class="nav-section">
        <div class="nav-label">Active Work</div>
        <button data-panel="chat">Chat</button>
        <button data-panel="tasks">Tasks</button>
        <button data-panel="skills">Skills</button>
        <button data-panel="tools">Tools</button>
      </div>
      <div class="nav-section">
        <div class="nav-label">Integration</div>
        <button data-panel="telegram">Telegram</button>
      </div>
      <div class="nav-section">
        <div class="nav-label">Configuration</div>
        <button data-panel="cron">Cron Manager</button>
        <button data-panel="dream">Dream Engine</button>
      </div>
    </nav>
    <div style="padding:12px 16px;border-top:1px solid var(--border);border-bottom:1px solid var(--border);background:#090d11;">
      <div class="status-label" style="margin-bottom:8px;">Logic Alignment</div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
        <input type="range" id="logic-confidence-slider" min="50" max="100" value="85" 
          style="flex:1;cursor:pointer;" oninput="updateLogicConfidence(this.value)">
        <strong id="logic-confidence-value" style="min-width:40px;color:var(--accent);font-size:14px;">85%</strong>
      </div>
      <div style="display:flex;gap:6px;margin-bottom:6px;">
        <button onclick="injectLogicAlignment()" class="primary" 
          style="flex:1;padding:6px 8px;font-size:11px;border-radius:3px;">⚡ Inject</button>
        <span id="inject-status" style="font-size:10px;color:var(--muted);line-height:28px;"></span>
      </div>
      <div class="muted" style="font-size:10px;line-height:1.3;">
        <strong style="color:var(--accent);">Iron Law Enforcement:</strong> Scales 35B bias (1.5→5.0) & Top-K at 100%
      </div>
    </div>
    <div id="status">
      <div class="status-label">System Status</div>
      <div style="margin-bottom:6px;"><span class="dot" id="ui-dot"></span>UI :8000</div>
      <div style="margin-bottom:6px;"><span class="dot" id="bridge-dot"></span>Bridge :8090</div>
      <div style="margin-bottom:6px;"><span class="dot" id="cron-dot"></span>Cron <span id="cron-state">?</span></div>
      <div style="margin-bottom:6px;"><span class="dot" id="tasks-dot"></span>Tasks <span id="tasks-state">0</span></div>
      <div style="margin-bottom:6px;"><span class="dot up"></span>Skills <span id="skills-state">0</span></div>
      <div><span class="dot up"></span>Tools <span id="tools-state">5</span></div>
    </div>
  </aside>

  <main id="main">

    <!-- ===== DASHBOARD ===== -->
    <section class="panel active" id="panel-dashboard">
      <h2>Dashboard</h2>
      <div class="muted">Real-time overview of all Gator systems</div>
      <div class="dash-grid">
        <div class="dash-card green">
          <h3>System Health</h3>
          <div class="dash-status">
            <span class="label">UI Server</span>
            <span class="dot" id="dash-ui-dot"></span>
            <span class="value" id="dash-ui-state">Loading...</span>
          </div>
          <div class="dash-status">
            <span class="label">Bridge</span>
            <span class="dot" id="dash-bridge-dot"></span>
            <span class="value" id="dash-bridge-state">Loading...</span>
          </div>
          <div class="dash-status">
            <span class="label">Cron Loop</span>
            <span class="dot" id="dash-cron-dot"></span>
            <span class="value" id="dash-cron-state">Loading...</span>
          </div>
        </div>
        <div class="dash-card orange">
          <h3>Task Management</h3>
          <div class="dash-metric">
            <span class="label">Active Tasks</span>
            <span class="value" id="dash-tasks-count">0</span>
          </div>
          <div class="dash-metric">
            <span class="label">Latest Inject</span>
            <span class="value" id="dash-inject-latest">-- ms</span>
          </div>
          <div class="dash-metric">
            <span class="label">Target Compliance</span>
            <span class="value"><span id="dash-target-badge" class="ok-badge warn">--</span></span>
          </div>
        </div>
        <div class="dash-card blue">
          <h3>Performance Metrics</h3>
          <div class="dash-metric">
            <span class="label">Avg Injection</span>
            <span class="value" id="dash-inject-avg">-- ms</span>
          </div>
          <div class="dash-metric">
            <span class="label">P95 Injection</span>
            <span class="value" id="dash-inject-p95">-- ms</span>
          </div>
          <div class="dash-metric">
            <span class="label">Store Size</span>
            <span class="value" id="dash-store-bytes">-- B</span>
          </div>
        </div>
        <div class="dash-card green" style="border-left-color:#2d8a4a;">
          <h3>Skill Learning (Flywheel)</h3>
          <div class="dash-metric">
            <span class="label">Learned Skills</span>
            <span class="value" id="dash-skills-total">0</span>
          </div>
          <div class="dash-metric">
            <span class="label">Recent Additions</span>
            <span class="value" id="dash-skills-recent">0</span>
          </div>
          <div class="dash-metric">
            <span class="label">Iron Law</span>
            <span class="value"><span class="ok-badge ok">ENFORCED</span></span>
          </div>
        </div>
      </div>
      <div class="card">
        <h2 style="font-size:13px;">Logic Alignment Confidence</h2>
        <div class="muted" style="margin-bottom:10px;">35B → 1.5B steering coherence (higher = tighter control)</div>
        <div style="display:flex;align-items:center;gap:12px;">
          <div style="flex:1;background:var(--code);border:1px solid var(--border);border-radius:6px;height:24px;overflow:hidden;">
            <div id="confidence-bar" style="height:100%;background:linear-gradient(90deg,#2d8a4a,#5fa8d3);width:0%;transition:width 0.5s ease;"></div>
          </div>
          <strong id="confidence-percent" style="min-width:50px;text-align:right;">0%</strong>
        </div>
      </div>
      <div class="card">
        <h2 style="font-size:13px;">Quick Actions</h2>
        <div class="row" style="margin-top:8px;">
          <button class="primary" onclick="switchPanel('chat')">Open Chat</button>
          <button class="primary" onclick="switchPanel('tasks')">View Tasks</button>
          <button class="primary" onclick="switchPanel('skills')">View Skills</button>
          <button onclick="resetChat()">Reset Session</button>
          <button class="danger" onclick="clearTasks()">Clear All Tasks</button>
          <button onclick="runDreamNow()">Dream Now</button>
        </div>
      </div>
    </section>

    <!-- ===== CHAT ===== -->
    <section class="panel" id="panel-chat">
      <div class="card" style="margin-bottom:12px;">
        <div class="row" style="justify-content:space-between;">
          <div>
            <h2 style="margin-bottom:4px;">Chat Interface</h2>
            <div class="muted">Streaming via SSE. The bridge runs the 35B → scratchpad → 1.5B pipeline with persistent task awareness.</div>
          </div>
          <div style="display:flex;gap:8px;">
            <button type="button" onclick="resetChat()">Reset Session</button>
            <button class="danger" onclick="clearChatLog()">Clear Log</button>
          </div>
        </div>
        <div class="chat-options">
          <label>
            <input type="checkbox" id="save-session" checked>
            <span>Save session to memory</span>
          </label>
          <span class="muted" style="margin-left:auto;font-size:11px;">When enabled, completed tasks are persisted and indexed for skill learning</span>
        </div>
      </div>
      <div id="chat-log" style="flex:1;min-height:0;"></div>
      <form id="chat-input" onsubmit="event.preventDefault();sendChat();">
        <textarea id="chat-text" placeholder="Speak to Gator… (Enter to send, Shift+Enter for new line)"
          onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat();}"></textarea>
        <div class="chat-controls">
          <button class="primary" id="chat-send" type="submit">Send Message</button>
        </div>
      </form>
    </section>

    <!-- ===== TASKS ===== -->
    <section class="panel" id="panel-tasks">
      <h2>Task Management</h2>
      <div class="muted">Live task buffer with injection metrics (polling every 2s). Tasks persist across session resets.</div>
      <div class="tasks-grid">
        <div>
          <div class="card" style="margin-bottom:12px;">
            <div class="row" style="justify-content:space-between;">
              <h2 style="font-size:13px;">Active Tasks</h2>
              <button class="danger" onclick="clearTasks()">Clear All</button>
            </div>
          </div>
          <div class="task-list" id="task-list"></div>
        </div>
        <div class="task-metrics-panel">
          <div class="metric-card">
            <h3>Injection Performance</h3>
            <div class="metric-row">
              <span class="label">Latest</span>
              <strong class="value" id="metric-inject-latest">-- ms</strong>
            </div>
            <div class="metric-row">
              <span class="label">Average</span>
              <strong class="value" id="metric-inject-avg">-- ms</strong>
            </div>
            <div class="metric-row">
              <span class="label">P95</span>
              <strong class="value" id="metric-inject-p95">-- ms</strong>
            </div>
            <div class="metric-row">
              <span class="label">Target (&lt;=10ms)</span>
              <strong class="value"><span id="metric-target" class="ok-badge warn">--</span></strong>
            </div>
          </div>
          <div class="metric-card">
            <h3>Task Store</h3>
            <div class="metric-row">
              <span class="label">Store Bytes</span>
              <strong class="value" id="metric-store-bytes">--</strong>
            </div>
            <div class="metric-row">
              <span class="label">Capacity</span>
              <strong class="value">1 MB</strong>
            </div>
            <div class="metric-row">
              <span class="label">Persistence</span>
              <strong class="value">logs/task_store.json</strong>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ===== TOOLS ===== -->
    <section class="panel" id="panel-tools">
      <div class="card" style="margin-bottom:12px;">
        <div class="row" style="justify-content:space-between;">
          <div>
            <h2 style="margin-bottom:4px;">Native Tools</h2>
            <div class="muted">Embedded tools available to the agent. New tools are automatically detected and displayed.</div>
          </div>
          <button onclick="refreshTools()">Refresh</button>
        </div>
      </div>
      <div class="tools-grid">
        <div>
          <div class="card" style="margin-bottom:12px;">
            <h2 style="font-size:13px;">Current Tool Registry</h2>
          </div>
          <div class="tool-list" id="tool-list"></div>
        </div>
        <div>
          <div class="card" style="margin-bottom:12px;">
            <h2 style="font-size:13px;">Tool Activity Log</h2>
            <div class="muted" style="margin-top:4px;">Recent tool executions (live tracking)</div>
          </div>
          <div class="activity-log" id="activity-log">
            <div class="muted" style="text-align:center;padding:20px;">No recent activity</div>
          </div>
        </div>
      </div>
    </section>

    <!-- ===== SKILLS (Gator-Flywheel) ===== -->
    <section class="panel" id="panel-skills">
      <div class="card" style="margin-bottom:12px;">
        <div class="row" style="justify-content:space-between;">
          <div>
            <h2 style="margin-bottom:4px;">Learned Skills</h2>
            <div class="muted">Gator-Flywheel persistent skill extraction. When tasks complete, execution patterns are indexed in LanceDB (semantic) and JSON graph (deterministic). Iron Law: The 1.5B chassis never guesses when a matching skill exists.</div>
          </div>
          <button onclick="refreshSkills()">Refresh</button>
        </div>
      </div>
      <div class="tasks-grid">
        <div>
          <div class="scroll" style="max-height:calc(100vh - 220px);">
            <div id="skills-list" style="display:flex;flex-direction:column;gap:10px;"></div>
          </div>
        </div>
        <div class="task-metrics-panel">
          <div class="metric-card">
            <h3>Skill Graph Stats</h3>
            <div class="metric-row">
              <span class="label">Total Skills</span>
              <strong class="value" id="skill-total">0</strong>
            </div>
            <div class="metric-row">
              <span class="label">Recently Used</span>
              <strong class="value" id="skill-recent-used">0</strong>
            </div>
            <div class="metric-row">
              <span class="label">Storage</span>
              <strong class="value">skills/learned_tools.json</strong>
            </div>
          </div>
          <div class="metric-card">
            <h3>Session History</h3>
            <div id="session-history-list" style="font-size:11px;color:var(--muted);"></div>
          </div>
          <div class="metric-card" style="background:#0d1a13;border:1px solid #1e3a25;">
            <h3 style="color:var(--accent);">Iron Law</h3>
            <div style="font-size:11px;line-height:1.5;color:var(--muted);">
              The 1.5B chassis is FORBIDDEN from guessing. If scratchpad is empty and no learned skill matches, the system returns <code>[WAITING_FOR_LOGIC_MAP]</code> instead of hallucinating a response.
            </div>
            <div style="margin-top:10px;padding:8px;background:#0a0f13;border-radius:4px;font-size:10px;font-family:monospace;color:#7ad79a;">
              STATUS: <strong>ENFORCED</strong>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ===== CRON ===== -->
    <section class="panel" id="panel-cron">
      <h2>Cron Manager</h2>
      <div class="muted">Schedule for <code>src/agentic_cron.py</code>. Changes are saved immediately;
        a restart of the cron loop is required for new intervals to take effect.</div>
      <div class="card">
        <div class="cron-grid" id="cron-form"></div>
        <div class="row" style="margin-top:12px;">
          <button class="primary" onclick="saveCron()">Save schedule</button>
          <button onclick="cronControl('start')">Start cron</button>
          <button class="danger" onclick="cronControl('stop')">Stop cron</button>
          <span class="muted" id="cron-meta"></span>
        </div>
      </div>
      <div class="card scroll">
        <h2 style="font-size:13px;">Last task results</h2>
        <pre id="cron-last">(loading…)</pre>
      </div>
    </section>

    <!-- ===== DREAM ===== -->
    <section class="panel" id="panel-dream">
      <h2>Dream Engine</h2>
      <div class="muted">Background autonomous reasoning. Runs the 35B donor on recent activity
        and writes structured JSON to <code>logs/dream.log</code>.</div>
      <div class="card">
        <div class="row">
          <label for="dream-slider" style="min-width:170px;">Cycle interval (seconds)</label>
          <input type="range" id="dream-slider" min="15" max="3600" step="15" value="120"
            oninput="document.getElementById('dream-val').textContent=this.value+'s';" />
          <strong id="dream-val">120s</strong>
          <button class="primary" onclick="saveDreamInterval()">Apply</button>
          <button onclick="runDreamNow()">Dream now</button>
          <span class="muted" id="dream-meta"></span>
        </div>
        <div class="muted" style="margin-top:6px;">
          Lower = faster learning loop (more VRAM use). Higher = battery-friendly.
        </div>
      </div>
      <div class="card" style="flex:1; min-height:0; display:flex; flex-direction:column;">
        <div class="row" style="justify-content:space-between;">
          <h2 style="font-size:13px;">Live dream log</h2>
          <span class="muted" id="dream-count">0 records</span>
        </div>
        <div id="dream-tail"></div>
      </div>
    </section>

    <!-- ===== TELEGRAM GATEWAY ===== -->
    <section class="panel" id="panel-telegram">
      <h2>Telegram Gateway Setup</h2>
      <div class="muted" style="margin-bottom:16px;">Configure and manage the Telegram bot integration. Requires bot token from @BotFather.</div>
      
      <div class="card" style="margin-bottom:16px;">
        <div class="row" style="justify-content:space-between;margin-bottom:12px;">
          <h2 style="font-size:13px;">Gateway Status</h2>
          <div style="display:flex;align-items:center;gap:8px;">
            <span id="tg-status-indicator" class="dot"></span>
            <strong id="tg-status-text">Checking...</strong>
          </div>
        </div>
        <div class="dash-status">
          <span class="label">Process</span>
          <span id="tg-pid-status">—</span>
        </div>
        <div class="dash-status">
          <span class="label">Authentication</span>
          <span id="tg-auth-status">—</span>
        </div>
        <div class="dash-status">
          <span class="label">Event Bus</span>
          <span id="tg-bus-status">—</span>
        </div>
      </div>

      <div class="card" style="margin-bottom:16px;">
        <h2 style="font-size:13px;margin-bottom:12px;">Configuration</h2>
        <div style="display:flex;flex-direction:column;gap:12px;">
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px;">Bot Token</label>
            <input type=\"password\" id=\"tg-token\" placeholder=\"8441868006:AAE...\" style=\"width:100%;font-family:monospace;\">
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px;">Bot Username</label>
            <input type=\"text\" id=\"tg-username\" placeholder=\"Gator83_bot\" style=\"width:100%;\">
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px;">Authorized Chat ID</label>
            <input type=\"text\" id=\"tg-chat-id\" placeholder=\"6895840459\" style=\"width:100%;\">
          </div>
        </div>
        <div class="row" style="margin-top:16px;gap:8px;">
          <button class="primary" onclick="saveTelegramConfig()">Save Configuration</button>
          <button onclick="loadTelegramConfig()">Load Current</button>
          <button class="danger" onclick="restartTelegramGateway()">Restart Gateway</button>
        </div>
        <div id="tg-save-status" class="muted" style="margin-top:8px;font-size:11px;"></div>
      </div>

      <div class="card">
        <h2 style="font-size:13px;margin-bottom:8px;">Recent Activity</h2>
        <div id="tg-activity-log" style="max-height:200px;overflow-y:auto;">
          <div class="muted" style="text-align:center;padding:20px;">No recent activity</div>
        </div>
      </div>
    </section>

  </main>
</div>

<script>
const $ = (id) => document.getElementById(id);

/* ---------- logic confidence control ---------- */
function updateLogicConfidence(value) {
  $('logic-confidence-value').textContent = value + '%';
  // Save to backend
  fetch('/api/logic_confidence', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: parseInt(value)})
  }).catch(err => console.warn('Failed to save logic confidence:', err));
}

// Inject Logic Alignment (commit to config + hot-reload)
async function injectLogicAlignment() {
  const value = parseInt($('logic-confidence-slider').value);
  const status = $('inject-status');
  status.textContent = 'Injecting...';
  status.style.color = 'var(--warn)';
  
  try {
    const resp = await fetch('/api/logic_alignment/inject', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({coefficient: value})
    });
    const data = await resp.json();
    
    if (data.ok) {
      status.textContent = `\u2713 Injected (${data.apply_time_ms.toFixed(1)}ms)`;
      status.style.color = 'var(--accent)';
      setTimeout(() => { status.textContent = ''; }, 3000);
    } else {
      status.textContent = '\u2717 Failed: ' + (data.error || 'unknown');
      status.style.color = 'var(--accent2)';
    }
  } catch (err) {
    status.textContent = '\u2717 Error: ' + err.message;
    status.style.color = 'var(--accent2)';
  }
}

// Load initial confidence value
fetch('/api/logic_confidence').then(r => r.json()).then(data => {
  if (data.ok) {
    const val = data.confidence || 85;
    $('logic-confidence-slider').value = val;
    $('logic-confidence-value').textContent = val + '%';
  }
}).catch(() => {});

/* ---------- panel switching ---------- */
function switchPanel(name) {
  document.querySelectorAll('.nav button').forEach(x => x.classList.remove('active'));
  document.querySelector('.nav button[data-panel="' + name + '"]').classList.add('active');
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  $('panel-' + name).classList.add('active');
  if (name === 'cron') refreshCron();
  if (name === 'dream') refreshDreamConfig();
  if (name === 'dashboard') refreshDashboard();
  if (name === 'skills') refreshSkills();
  if (name === 'tools') refreshTools();
  if (name === 'telegram') refreshTelegramStatus();
}
  if (name === 'dashboard') refreshDashboard();
  if (name === 'skills') refreshSkills();
  if (name === 'tools') refreshTools();
}
document.querySelectorAll('.nav button').forEach(b => {
  b.onclick = () => switchPanel(b.dataset.panel);
});

/* ---------- dashboard ---------- */
async function refreshDashboard() {
  try {
    const [health, tasks, metrics, skills] = await Promise.all([
      fetch('/api/health').then(r => r.json()).catch(() => null),
      fetch('/api/tasks').then(r => r.json()).catch(() => null),
      fetch('/api/tasks/metrics').then(r => r.json()).catch(() => null),
      fetch('/api/skills/recent?limit=3').then(r => r.json()).catch(() => null),
    ]);
    // System health
    if (health) {
      $('dash-ui-dot').className = 'dot up';
      $('dash-ui-state').textContent = 'Online';
      $('dash-bridge-dot').className = 'dot ' + (health.bridge_reachable ? 'up' : 'down');
      $('dash-bridge-state').textContent = health.bridge_reachable ? 'Connected' : 'Offline';
    }
    const cron = await fetch('/api/cron').then(r => r.json()).catch(() => null);
    if (cron) {
      $('dash-cron-dot').className = 'dot ' + (cron.alive ? 'up' : 'down');
      $('dash-cron-state').textContent = cron.alive ? 'Running' : (cron.enabled ? 'Enabled' : 'Stopped');
    }
    // Task metrics
    if (tasks) {
      const count = Array.isArray(tasks.active_tasks) ? tasks.active_tasks.length : 0;
      $('dash-tasks-count').textContent = String(count);
    }
    if (metrics) {
      const latest = Number((metrics.latest || {}).elapsed_ms || 0);
      const avg = Number(metrics.avg_injection_ms || 0);
      const p95 = Number(metrics.p95_injection_ms || 0);
      const store = Number(((metrics.latest || {}).store_bytes || 0));
      const targetOk = !!metrics.within_target;
      $('dash-inject-latest').textContent = latest.toFixed(2) + ' ms';
      $('dash-inject-avg').textContent = avg.toFixed(2) + ' ms';
      $('dash-inject-p95').textContent = p95.toFixed(2) + ' ms';
      $('dash-store-bytes').textContent = String(store) + ' B';
      const badge = $('dash-target-badge');
      badge.textContent = targetOk ? 'PASS' : 'FAIL';
      badge.className = 'ok-badge ' + (targetOk ? 'ok' : 'bad');
      // Confidence meter (inverse of p95: lower injection time = higher confidence)
      const maxConfidence = 100;
      const confidence = targetOk ? Math.max(0, Math.min(100, maxConfidence - p95 * 3)) : 50;
      $('confidence-bar').style.width = confidence + '%';
      $('confidence-percent').textContent = Math.round(confidence) + '%';
    }
    // Skill learning stats
    if (skills) {
      const total = await fetch('/api/skills').then(r => r.json()).then(d => d.total || 0).catch(() => 0);
      $('dash-skills-total').textContent = String(total);
      $('dash-skills-recent').textContent = String(skills.count || 0);
      $('skills-state').textContent = String(total);
    }
  } catch (err) {
    console.error('Dashboard refresh failed:', err);
  }
}

/* ---------- chat ---------- */
let inflight = null;
function appendMsg(who, text) {
  const log = $('chat-log');
  const m = document.createElement('div');
  m.className = 'msg ' + who;
  m.innerHTML = '<div class="who">' + who + '</div><div class="body"></div>';
  m.querySelector('.body').textContent = text;
  log.appendChild(m);
  log.scrollTop = log.scrollHeight;
  return m;
}
function sendChat() {
  const text = $('chat-text').value.trim();
  if (!text || inflight) return;
  appendMsg('user', text);
  $('chat-text').value = '';
  $('chat-send').disabled = true;
  const placeholder = appendMsg('gator', '…');
  const url = '/api/chat/stream?prompt=' + encodeURIComponent(text);
  inflight = new EventSource(url);
  let started = Date.now();
  inflight.addEventListener('heartbeat', (e) => {
    const d = JSON.parse(e.data);
    placeholder.querySelector('.body').textContent = '… (' + d.elapsed_s + 's)';
  });
  inflight.addEventListener('message', (e) => {
    const d = JSON.parse(e.data);
    placeholder.querySelector('.body').textContent = (d.text || '(no text)').trim();
  });
  inflight.addEventListener('error', (e) => {
    let msg = 'bridge error';
    try { msg = JSON.parse(e.data).error || msg; } catch {}
    placeholder.querySelector('.body').textContent = '[error] ' + msg;
    closeChat();
  });
  inflight.addEventListener('done', () => closeChat());
}
function closeChat() {
  if (inflight) { inflight.close(); inflight = null; }
  $('chat-send').disabled = false;
}
async function resetChat() {
  await fetch('/api/chat/reset', {method:'POST'});
  $('chat-log').innerHTML = '';
}
function clearChatLog() {
  if (confirm('Clear all chat messages? (Session data will be preserved)')) {
    $('chat-log').innerHTML = '';
  }
}

/* ---------- task hive ---------- */
function statusGlyph(status) {
  if (status === 'completed') return '🟢 Done';
  if (status === 'in_progress') return '🟡 Processing';
  return '⚪ Scheduled';
}
function taskNode(task) {
  const wrap = document.createElement('div');
  wrap.className = 'task-item ' + (task.status || 'pending');
  const pct = Math.max(0, Math.min(100, parseInt(task.progress || 0, 10)));
  wrap.innerHTML =
    '<div class="task-head">' +
      '<strong class="task-id">' + escapeHtml(task.id || 'Task-???') + '</strong>' +
      '<span>' + statusGlyph(task.status || 'pending') + '</span>' +
    '</div>' +
    '<div class="task-title">' + escapeHtml(task.title || '(untitled task)') + '</div>' +
    '<div class="task-sub">' + escapeHtml(task.sub_step || 'queued') + '</div>' +
    '<div class="task-progress"><span style="width:' + pct + '%"></span></div>';
  return wrap;
}
async function refreshTasks() {
  try {
    const r = await fetch('/api/tasks').then(r => r.json());
    const list = Array.isArray(r.active_tasks) ? r.active_tasks : [];
    const box = $('task-list');
    box.innerHTML = '';
    if (!list.length) {
      box.innerHTML = '<div class="muted" style="padding:16px;text-align:center;">No active tasks.</div>';
    } else {
      list.forEach(t => box.appendChild(taskNode(t)));
    }
    $('tasks-dot').className = 'dot ' + (list.length ? 'up' : 'down');
    $('tasks-state').textContent = String(list.length);
  } catch {
    $('tasks-dot').className = 'dot down';
    $('tasks-state').textContent = '?';
  }
}
async function clearTasks() {
  await fetch('/api/tasks/clear', {method:'POST'});
  refreshTasks();
  refreshTaskMetrics();
}

async function refreshTaskMetrics() {
  try {
    const m = await fetch('/api/tasks/metrics').then(r => r.json());
    const latest = Number((m.latest || {}).elapsed_ms || 0);
    const avg = Number(m.avg_injection_ms || 0);
    const p95 = Number(m.p95_injection_ms || 0);
    const store = Number(((m.latest || {}).store_bytes || 0));
    const targetOk = !!m.within_target;
    $('metric-inject-latest').textContent = latest.toFixed(3) + ' ms';
    $('metric-inject-avg').textContent = avg.toFixed(3) + ' ms';
    $('metric-inject-p95').textContent = p95.toFixed(3) + ' ms';
    $('metric-store-bytes').textContent = String(store);
    const targetEl = $('metric-target');
    targetEl.textContent = targetOk ? 'PASS' : 'FAIL';
    targetEl.className = 'ok-badge ' + (targetOk ? 'ok' : 'bad');
  } catch {
    $('metric-inject-latest').textContent = '-- ms';
    $('metric-inject-avg').textContent = '-- ms';
    $('metric-inject-p95').textContent = '-- ms';
    $('metric-store-bytes').textContent = '--';
    $('metric-target').textContent = '--';
    $('metric-target').className = 'ok-badge warn';
  }
}

/* ---------- skills (Gator-Flywheel) ---------- */
function skillNode(skill) {
  const wrap = document.createElement('div');
  wrap.className = 'task-item completed';
  wrap.innerHTML =
    '<div class="task-head">' +
      '<strong class="task-id">' + escapeHtml(skill.id || 'Skill-???') + '</strong>' +
      '<span style="font-size:10px;color:var(--muted);">Used ' + (skill.use_count || 0) + 'x</span>' +
    '</div>' +
    '<div class="task-title">' + escapeHtml(skill.title || '(untitled skill)') + '</div>' +
    '<div class="task-sub" style="margin-top:6px;">' + escapeHtml(skill.execution_pattern || 'No pattern') + '</div>';
  return wrap;
}

async function refreshSkills() {
  try {
    const r = await fetch('/api/skills').then(r => r.json());
    const skills = Array.isArray(r.skills) ? r.skills : [];
    const box = $('skills-list');
    box.innerHTML = '';
    if (!skills.length) {
      box.innerHTML = '<div class="muted" style="padding:16px;text-align:center;">No skills learned yet. Complete a task to generate the first skill.</div>';
    } else {
      // Sort by created_at descending
      const sorted = skills.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      sorted.forEach(s => box.appendChild(skillNode(s)));
    }
    $('skill-total').textContent = String(skills.length);
    const recent = skills.filter(s => s.use_count > 0).length;
    $('skill-recent-used').textContent = String(recent);
    
    // Load session history
    const hist = await fetch('/api/history/completions').then(r => r.json()).catch(() => ({completions: []}));
    const histBox = $('session-history-list');
    histBox.innerHTML = '';
    if (hist.completions && hist.completions.length > 0) {
      hist.completions.forEach(entry => {
        const line = document.createElement('div');
        line.style.marginBottom = '6px';
        line.textContent = '✓ ' + (entry.title || entry.task_id || '');
        histBox.appendChild(line);
      });
    } else {
      histBox.innerHTML = '<div class="muted">No recent completions.</div>';
    }
  } catch (err) {
    console.error('Skills refresh failed:', err);
  }
}

/* ---------- tools ---------- */
function toolNode(tool) {
  const wrap = document.createElement('div');
  wrap.className = 'tool-item';
  wrap.innerHTML =
    '<div class="tool-name">' + escapeHtml(tool.name || 'unknown') + '</div>' +
    '<div class="tool-desc">' + escapeHtml(tool.description || 'No description') + '</div>';
  return wrap;
}

function activityNode(act) {
  const wrap = document.createElement('div');
  wrap.className = 'activity-item ' + (act.ok ? 'success' : 'error');
  const ts = new Date(act.ts * 1000);
  const timeStr = ts.toLocaleTimeString();
  const elapsed_ms = ((act.elapsed || 0) * 1000).toFixed(1);
  const detail = act.ok 
    ? escapeHtml(act.tool) + ' (' + elapsed_ms + 'ms)'
    : escapeHtml(act.tool) + ' FAILED: ' + escapeHtml(act.error || 'unknown error');
  wrap.innerHTML =
    '<div class="time">' + timeStr + ' — ' + escapeHtml(act.issued_by || 'system') + '</div>' +
    '<div class="details">' + detail + '</div>';
  return wrap;
}

async function refreshTools() {
  try {
    const [tools, activity] = await Promise.all([
      fetch('/api/tools').then(r => r.json()).catch(() => null),
      fetch('/api/tools/activity?limit=10').then(r => r.json()).catch(() => null),
    ]);
    
    // Update tool list
    if (!tools || !tools.ok) {
      $('tool-list').innerHTML = '<div class="muted" style="padding:16px;text-align:center;">Bridge offline</div>';
    } else {
      const toolsArr = Array.isArray(tools.tools) ? tools.tools : [];
      const box = $('tool-list');
      box.innerHTML = '';
      if (!toolsArr.length) {
        box.innerHTML = '<div class="muted" style="padding:16px;text-align:center;">No tools available</div>';
      } else {
        toolsArr.forEach(t => box.appendChild(toolNode(t)));
      }
      $('tools-state').textContent = String(toolsArr.length);
    }
    
    // Update activity log
    if (activity && activity.ok && Array.isArray(activity.activity)) {
      const acts = activity.activity;
      const actBox = $('activity-log');
      actBox.innerHTML = '';
      if (!acts.length) {
        actBox.innerHTML = '<div class="muted" style="text-align:center;padding:20px;">No recent activity</div>';
      } else {
        // Reverse to show most recent first
        acts.reverse().forEach(a => actBox.appendChild(activityNode(a)));
      }
    }
  } catch (err) {
    console.error('Tools refresh failed:', err);
  }
}

/* ---------- telegram ---------- */
async function refreshTelegramStatus() {
  try {
    const status = await fetch('/api/telegram/status').then(r => r.json()).catch(() => null);
    if (!status) return;
    
    const indicator = $('tg-status-indicator');
    const text = $('tg-status-text');
    
    if (status.connected) {
      indicator.className = 'dot up';
      text.textContent = 'Connected';
      text.style.color = 'var(--accent)';
    } else if (status.alive) {
      indicator.className = 'dot';
      indicator.style.background = 'var(--warn)';
      text.textContent = 'Running (Not Authenticated)';
      text.style.color = 'var(--warn)';
    } else {
      indicator.className = 'dot down';
      text.textContent = 'Offline';
      text.style.color = 'var(--accent2)';
    }
    
    $('tg-pid-status').textContent = status.pid ? `PID ${status.pid} (${status.alive ? 'alive' : 'dead'})` : 'Not running';
    $('tg-auth-status').textContent = status.authenticated ? '\u2713 Authenticated' : '\u2717 Not authenticated';
    $('tg-bus-status').textContent = status.connected_event_bus ? '\u2713 Connected' : '\u2717 Disconnected';
  } catch (err) {
    console.error('Telegram status refresh failed:', err);
  }
}

async function loadTelegramConfig() {
  try {
    const cfg = await fetch('/api/telegram/config').then(r => r.json()).catch(() => null);
    if (cfg && cfg.ok) {
      $('tg-token').value = cfg.token || '';
      $('tg-username').value = cfg.username || '';
      $('tg-chat-id').value = cfg.chat_id || '';
      $('tg-save-status').textContent = 'Configuration loaded';
      $('tg-save-status').style.color = 'var(--accent)';
      setTimeout(() => { $('tg-save-status').textContent = ''; }, 3000);
    }
  } catch (err) {
    console.error('Failed to load telegram config:', err);
  }
}

async function saveTelegramConfig() {
  const token = $('tg-token').value.trim();
  const username = $('tg-username').value.trim();
  const chatId = $('tg-chat-id').value.trim();
  
  if (!token || !username || !chatId) {
    $('tg-save-status').textContent = 'All fields are required';
    $('tg-save-status').style.color = 'var(--accent2)';
    return;
  }
  
  try {
    const resp = await fetch('/api/telegram/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ token, username, chat_id: chatId })
    });
    const data = await resp.json();
    
    if (data.ok) {
      $('tg-save-status').textContent = '\u2713 Configuration saved to .env';
      $('tg-save-status').style.color = 'var(--accent)';
      setTimeout(() => { $('tg-save-status').textContent = ''; }, 3000);
    } else {
      $('tg-save-status').textContent = '\u2717 Failed: ' + (data.error || 'unknown');
      $('tg-save-status').style.color = 'var(--accent2)';
    }
  } catch (err) {
    $('tg-save-status').textContent = '\u2717 Error: ' + err.message;
    $('tg-save-status').style.color = 'var(--accent2)';
  }
}

async function restartTelegramGateway() {
  try {
    $('tg-save-status').textContent = 'Restarting gateway...';
    $('tg-save-status').style.color = 'var(--warn)';
    
    const resp = await fetch('/api/telegram/restart', { method: 'POST' });
    const data = await resp.json();
    
    if (data.ok) {
      $('tg-save-status').textContent = `\u2713 Restarted (PID ${data.pid})`;
      $('tg-save-status').style.color = 'var(--accent)';
      setTimeout(() => {
        refreshTelegramStatus();
        $('tg-save-status').textContent = '';
      }, 2000);
    } else {
      $('tg-save-status').textContent = '\u2717 Failed: ' + (data.error || 'unknown');
      $('tg-save-status').style.color = 'var(--accent2)';
    }
  } catch (err) {
    $('tg-save-status').textContent = '\u2717 Error: ' + err.message;
    $('tg-save-status').style.color = 'var(--accent2)';
  }
}

/* ---------- cron ---------- */
const CRON_FIELDS = [
  ['interval_seconds', 'Loop tick (seconds)'],
  ['dream_idle_minutes', 'Dream idle gate (minutes)'],
  ['dream_every_seconds', 'Dream cycle every (seconds)'],
  ['process_dream_every_seconds', 'Process dream every (seconds)'],
  ['defrag_every_seconds', 'Defrag every (seconds)'],
  ['architect_every_seconds', 'Architect every (seconds)'],
];
async function refreshCron() {
  const r = await fetch('/api/cron').then(r => r.json()).catch(() => null);
  if (!r) return;
  const form = $('cron-form');
  form.innerHTML = '';
  CRON_FIELDS.forEach(([k, label]) => {
    const v = r.state[k] ?? '';
    form.insertAdjacentHTML('beforeend',
      '<label for="c_' + k + '">' + label + '</label>' +
      '<input id="c_' + k + '" type="number" min="1" value="' + v + '" />');
  });
  $('cron-meta').textContent =
    'pid=' + (r.pid || '—') + ' alive=' + r.alive + ' enabled=' + r.enabled;
  $('cron-last').textContent = JSON.stringify(r.status?.last_results || {}, null, 2);
}
async function saveCron() {
  const body = {};
  CRON_FIELDS.forEach(([k]) => {
    const el = $('c_' + k);
    if (el && el.value) body[k] = parseInt(el.value, 10);
  });
  const r = await fetch('/api/cron', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json());
  $('cron-meta').textContent = 'saved at ' + new Date().toLocaleTimeString();
  refreshCron();
}
async function cronControl(action) {
  await fetch('/api/cron/' + action, {method:'POST'});
  setTimeout(refreshCron, 400);
}

/* ---------- dream ---------- */
let dreamStream = null;
let dreamCount = 0;
function dreamNode(rec) {
  const div = document.createElement('div');
  div.className = 'dream-rec';
  if (rec.skipped_reason) div.classList.add('skipped');
  if (rec.error || rec.ok === false) div.classList.add('error');
  const ts = rec.iso || (rec.ts ? new Date(rec.ts*1000).toISOString() : '');
  div.innerHTML =
    '<div class="ts">' + ts + ' · ' + (rec.trigger || '') +
      (rec.confidence != null ? ' · conf=' + rec.confidence : '') + '</div>' +
    (rec.observation ? '<div class="obs">' + escapeHtml(rec.observation) + '</div>' : '') +
    (rec.hypothesis ? '<div class="hyp">↳ ' + escapeHtml(rec.hypothesis) + '</div>' : '') +
    (rec.next_action ? '<div class="next">→ ' + escapeHtml(rec.next_action) + '</div>' : '') +
    (rec.error ? '<div class="next">[error] ' + escapeHtml(rec.error) + '</div>' : '') +
    (rec.skipped_reason ? '<div class="ts">skipped: ' + escapeHtml(rec.skipped_reason) + '</div>' : '');
  return div;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
async function refreshDreamConfig() {
  const r = await fetch('/api/dream/config').then(r => r.json()).catch(() => null);
  if (!r) return;
  $('dream-slider').value = r.dream_every_seconds;
  $('dream-val').textContent = r.dream_every_seconds + 's';
  startDreamStream();
}
async function saveDreamInterval() {
  const v = parseInt($('dream-slider').value, 10);
  const r = await fetch('/api/dream/config', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({dream_every_seconds: v}),
  }).then(r => r.json());
  $('dream-meta').textContent = r.ok
    ? 'saved interval=' + v + 's at ' + new Date().toLocaleTimeString()
    : 'save failed';
}
async function runDreamNow() {
  $('dream-meta').textContent = 'dreaming…';
  const r = await fetch('/api/dream/run', {method:'POST'}).then(r => r.json());
  $('dream-meta').textContent = r.ok ? 'cycle complete' : 'cycle failed';
}
function startDreamStream() {
  if (dreamStream) return;
  $('dream-tail').innerHTML = '';
  dreamCount = 0;
  dreamStream = new EventSource('/api/dream/stream');
  dreamStream.addEventListener('message', (e) => {
    try {
      const rec = JSON.parse(e.data);
      const tail = $('dream-tail');
      tail.appendChild(dreamNode(rec));
      dreamCount++;
      $('dream-count').textContent = dreamCount + ' records';
      while (tail.children.length > 200) tail.removeChild(tail.firstChild);
      tail.scrollTop = tail.scrollHeight;
    } catch {}
  });
  dreamStream.onerror = () => { /* browser auto-reconnects */ };
}

/* ---------- status footer ---------- */
async function refreshStatus() {
  try {
    const r = await fetch('/api/health').then(r => r.json());
    $('ui-dot').className = 'dot up';
    $('bridge-dot').className = 'dot ' + (r.bridge_reachable ? 'up' : 'down');
  } catch {
    $('ui-dot').className = 'dot down';
    $('bridge-dot').className = 'dot down';
  }
  try {
    const c = await fetch('/api/cron').then(r => r.json());
    $('cron-dot').className = 'dot ' + (c.alive ? 'up' : 'down');
    $('cron-state').textContent = c.alive ? 'alive' : (c.enabled ? 'enabled' : 'off');
  } catch {
    $('cron-dot').className = 'dot down';
    $('cron-state').textContent = '?';
  }
}

/* ---------- initialization ---------- */
refreshStatus();
refreshDashboard();
refreshTasks();
refreshTaskMetrics();
refreshSkills();
refreshTools();
setInterval(refreshStatus, 5000);
setInterval(refreshDashboard, 3000);
setInterval(refreshTasks, 2000);
setInterval(refreshTaskMetrics, 2000);
setInterval(refreshSkills, 5000);
setInterval(refreshTools, 5000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(content=INDEX_HTML)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Command Center UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    _main()
