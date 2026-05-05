#!/usr/bin/env python3
"""Phase 6 Surgical Lab UI (native FastAPI + JS)."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import dotenv_values, load_dotenv, set_key
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from event_bus import EventBusClient
from pulse_check import run_pulse
from scholar_sense import ScholarSense
from discovery.cluster_namer import ClusterNamer, ClusterNamerError
from core.mitosis import MitosisEngine, wakeup_cleared, WORKER_VRAM_TARGET_MIB, MAX_WORKER_DENSITY, wakeup_cleared, WORKER_VRAM_TARGET_MIB, MAX_WORKER_DENSITY
from decommission_node import decommission_clone
from agentic_cron import cron_start, cron_status, cron_stop
from persona_engine import PersonaEngine

GATOR_ROOT = Path(__file__).resolve().parents[2]
GRAPH_HTML = GATOR_ROOT / "research" / "graphify-out" / "graph.html"
GRAPH_JSON = GATOR_ROOT / "research" / "graphify-out" / "graph.json"
DEBUG_FILE = GATOR_ROOT / "logs" / "debug.json"
ENV_FILE = GATOR_ROOT / ".env"
TG_PID_FILE = GATOR_ROOT / "bin" / "telegram_gateway.pid"  # shared: wakeup writes hive PID here
TG_LOG_FILE = GATOR_ROOT / "logs" / "telegram_hive.log"
TG_STATUS_FILE = GATOR_ROOT / "logs" / "telegram_hive_status.json"
INGEST_STATUS_FILE = GATOR_ROOT / "logs" / "ingest_status.json"
PRIME_BRIDGE_URL = os.environ.get("PRIME_BRIDGE_URL", "http://127.0.0.1:8090")

app = FastAPI(title="Gator Surgical Lab", version="1.0")
MITOSIS = MitosisEngine(root=GATOR_ROOT)
CLUSTER_NAMER = ClusterNamer(graph_json=GRAPH_JSON)
PERSONA = PersonaEngine(root=GATOR_ROOT)


def _set_ingest_status(state: str, percent: int, detail: dict[str, Any] | None = None) -> dict[str, Any]:
  payload = {
    "state": state,
    "percent": percent,
    "detail": detail or {},
    "updated_at": time.time(),
  }
  INGEST_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
  INGEST_STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
  return payload


def _get_ingest_status() -> dict[str, Any]:
  if not INGEST_STATUS_FILE.exists():
    return {"state": "idle", "percent": 0, "detail": {}, "updated_at": 0.0}
  try:
    return json.loads(INGEST_STATUS_FILE.read_text(encoding="utf-8"))
  except Exception:
    return {"state": "error", "percent": 0, "detail": {"reason": "status_parse_failed"}, "updated_at": time.time()}


def _ingest_worker(pdf_path: str, job_id: str) -> None:
  try:
    _set_ingest_status("queued", 5, {"job_id": job_id, "pdf_path": pdf_path})
    ss = ScholarSense()
    result = ss.ingest_pdf(
      Path(pdf_path).expanduser(),
      progress_callback=lambda state, percent, detail: _set_ingest_status(
        state,
        percent,
        {"job_id": job_id, **detail},
      ),
    )
    _set_ingest_status("complete", 100, {"job_id": job_id, **result})
  except Exception as exc:
    _set_ingest_status("error", 100, {"job_id": job_id, "error": str(exc)})


def _read_pid(path: Path) -> int | None:
  try:
    return int(path.read_text(encoding="utf-8").strip())
  except Exception:
    return None


def _pid_alive(pid: int | None) -> bool:
  if not pid:
    return False
  try:
    os.kill(pid, 0)
    return True
  except Exception:
    return False


def _mask(value: str) -> str:
  if not value:
    return ""
  if len(value) <= 6:
    return "*" * len(value)
  return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _telegram_config() -> dict[str, str]:
  if not ENV_FILE.exists():
    return {"token": "", "username": "", "chat_id": ""}
  env = dotenv_values(ENV_FILE)
  return {
    "token": str(env.get("GATOR_TG_BOT_TOKEN") or "").strip(),
    "username": str(env.get("GATOR_TG_BOT_USERNAME") or "").strip(),
    "chat_id": str(env.get("GATOR_TG_AUTH_CHAT_ID") or "").strip(),
  }


def _telegram_status() -> dict[str, Any]:
  cfg = _telegram_config()
  pid = _read_pid(TG_PID_FILE)
  pid_alive = _pid_alive(pid)
  bus_ok = False
  try:
    bus_ok = bool(EventBusClient().doctor_query().get("ok"))
  except Exception:
    bus_ok = False

  status_payload: dict[str, Any] = {}
  if TG_STATUS_FILE.exists():
    try:
      status_payload = json.loads(TG_STATUS_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
      status_payload = {}

  configured = bool(cfg["token"] and cfg["username"] and cfg["chat_id"])
  connected = bool(pid_alive and configured and bus_ok and status_payload.get("authenticated", False))
  return {
    "configured": configured,
    "pid": pid,
    "alive": pid_alive,
    "connected_event_bus": bus_ok,
    "authenticated": bool(status_payload.get("authenticated", False)),
    "indicator": "🟢" if connected else "🔴",
    "state": "connected" if connected else "disconnected",
  }


def _restart_telegram_gateway() -> dict[str, Any]:
  py_bin = GATOR_ROOT / "venv" / "bin" / "python"
  hive_script = GATOR_ROOT / "src" / "interfaces" / "telegram_hive.py"
  legacy_script = GATOR_ROOT / "src" / "interfaces" / "telegram_gateway.py"
  gw_script = hive_script if hive_script.exists() else legacy_script

  subprocess.run(["pkill", "-f", str(hive_script)], check=False)
  subprocess.run(["pkill", "-f", str(legacy_script)], check=False)
  for _ in range(50):
    probe = subprocess.run(["pgrep", "-f", str(gw_script)], capture_output=True, text=True, check=False)
    if probe.returncode != 0 or not probe.stdout.strip():
      break
    time.sleep(0.1)

  probe = subprocess.run(["pgrep", "-f", str(gw_script)], capture_output=True, text=True, check=False)
  if probe.returncode == 0 and probe.stdout.strip():
    subprocess.run(["pkill", "-9", "-f", str(gw_script)], check=False)
    time.sleep(0.2)

  TG_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
  log_fp = open(TG_LOG_FILE, "ab")
  try:
    proc = subprocess.Popen(
      [str(py_bin), str(gw_script)],
      stdout=log_fp,
      stderr=subprocess.STDOUT,
      start_new_session=True,
      env=os.environ.copy(),
    )
  finally:
    log_fp.close()

  TG_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
  TG_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
  return {"ok": True, "pid": proc.pid}


def _dynamic_graph_assets() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, dict[str, Any]]]:
  html = GRAPH_HTML.read_text(encoding="utf-8", errors="replace")
  nodes_match = re.search(r"const RAW_NODES = (\[.*?\]);", html, re.S)
  edges_match = re.search(r"const RAW_EDGES = (\[.*?\]);", html, re.S)
  legend_match = re.search(r"const LEGEND = (\[.*?\]);", html, re.S)
  if not nodes_match or not edges_match or not legend_match:
    raise HTTPException(status_code=500, detail="graph.html payload markers missing")

  nodes = json.loads(nodes_match.group(1))
  edges = json.loads(edges_match.group(1))
  legend = json.loads(legend_match.group(1))
  labels = CLUSTER_NAMER.refresh_community_labels()

  def _community_id(value: Any) -> int:
    if value is None or value == "":
      return -1
    return int(value)

  for node in nodes:
    cid = _community_id(node.get("community", -1))
    label_entry = labels.get(cid)
    if label_entry:
      node["community_name"] = str(label_entry["label"])

  for item in legend:
    cid = _community_id(item.get("cid", -1))
    label_entry = labels.get(cid)
    if label_entry:
      item["label"] = str(label_entry["label"])

  return nodes, edges, labels


@app.get("/api/refresh_graph")
def api_refresh_graph() -> dict[str, Any]:
  if not GRAPH_HTML.exists() or not GRAPH_JSON.exists():
    raise HTTPException(status_code=404, detail="Graph assets missing")
  try:
    nodes, edges, labels = _dynamic_graph_assets()
  except ClusterNamerError as exc:
    raise HTTPException(status_code=500, detail=str(exc)) from exc
  except ValueError as exc:
    raise HTTPException(status_code=500, detail=f"graph asset parse failed: {exc}") from exc

  legend = []
  counts: dict[int, int] = {}
  colors: dict[int, str] = {}
  for node in nodes:
    cid = -1 if node.get("community") is None or node.get("community") == "" else int(node.get("community"))
    counts[cid] = counts.get(cid, 0) + 1
    color = node.get("color") or {}
    colors[cid] = str((color.get("background") if isinstance(color, dict) else None) or colors.get(cid) or "#4E79A7")
  for cid, count in sorted(counts.items()):
    legend.append(
      {
        "cid": cid,
        "label": str(labels.get(cid, {}).get("label") or f"Community {cid}"),
        "count": count,
        "color": colors.get(cid, "#4E79A7"),
      }
    )
  return {
    "ok": True,
    "nodes": nodes,
    "edges": edges,
    "legend": legend,
    "community_labels": labels,
    "hybrid_sql": CLUSTER_NAMER.hybrid.audit_counts(),
  }


@app.get("/api/graph/semantic_search")
def api_graph_semantic_search(q: str = Query(default="", min_length=1), top_k: int = 5) -> dict[str, Any]:
  try:
    hits = CLUSTER_NAMER.semantic_lookup(query=q, top_k=max(1, min(int(top_k), 20)))
  except Exception as exc:
    raise HTTPException(status_code=500, detail=f"semantic search failed: {exc}") from exc

  graph_payload = {}
  try:
    graph_payload = json.loads(GRAPH_JSON.read_text(encoding="utf-8", errors="replace"))
  except Exception:
    graph_payload = {"nodes": []}

  nodes = graph_payload.get("nodes", []) if isinstance(graph_payload, dict) else []
  related: dict[int, list[dict[str, Any]]] = {}
  for hit in hits:
    cid = int(hit.get("community_id", -1))
    related[cid] = [node for node in nodes if int(node.get("community", -1) or -1) == cid][:12]

  return {
    "ok": True,
    "query": q,
    "hits": hits,
    "related_nodes": related,
  }


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/vitals")
def api_vitals() -> JSONResponse:
  data = run_pulse()
  data["telegram"] = _telegram_status()
  return JSONResponse(data)


@app.get("/api/config/telegram")
def api_config_telegram() -> dict[str, Any]:
  cfg = _telegram_config()
  return {
    "ok": True,
    "token": cfg["token"],
    "username": cfg["username"],
    "chat_id": cfg["chat_id"],
    "chat_locked": False,
    "status": _telegram_status(),
  }


@app.post("/api/config/telegram")
def api_save_config_telegram(payload: dict[str, Any]) -> dict[str, Any]:
  current = _telegram_config()
  token = str(payload.get("token") or "").strip() or current["token"]
  username = str(payload.get("username") or "").strip() or current["username"]
  chat_id = str(payload.get("chat_id") or "").strip() or current["chat_id"]

  if not token or not username or not chat_id:
    raise HTTPException(status_code=400, detail="token, username and chat_id are required")

  set_key(str(ENV_FILE), "GATOR_TG_BOT_TOKEN", token)
  set_key(str(ENV_FILE), "GATOR_TG_BOT_USERNAME", username)
  set_key(str(ENV_FILE), "GATOR_TG_AUTH_CHAT_ID", chat_id)
  load_dotenv(dotenv_path=ENV_FILE, override=True)

  restart = _restart_telegram_gateway()
  return {
    "ok": True,
    "saved": True,
    "restart": restart,
    "status": _telegram_status(),
  }

@app.post("/api/interrupt")
def api_interrupt() -> dict[str, Any]:
    try:
        return EventBusClient().interrupt()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/session_reset")
def api_session_reset() -> dict[str, Any]:
    try:
        req = urllib.request.Request(
            f"{PRIME_BRIDGE_URL}/api/session_reset",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/debug_tail")
def api_debug_tail() -> dict[str, Any]:
    if not DEBUG_FILE.exists():
        return {"lines": []}
    lines = DEBUG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    parsed = []
    for ln in lines:
        try:
            parsed.append(json.loads(ln))
        except Exception:
            parsed.append({"raw": ln})
    return {"lines": parsed}



@app.get("/api/ingest_status")
def api_ingest_status() -> dict[str, Any]:
    return _get_ingest_status()


@app.post("/api/ingest_pdf")
def api_ingest_pdf(payload: dict[str, Any]) -> dict[str, Any]:
    pdf_path = str(payload.get("pdf_path") or "").strip()
    if not pdf_path:
        raise HTTPException(status_code=400, detail="pdf_path is required")
    job_id = uuid.uuid4().hex
    threading.Thread(target=_ingest_worker, args=(pdf_path, job_id), daemon=True).start()
    return {"ok": True, "job_id": job_id, "status": _get_ingest_status()}


@app.get("/api/hive/status")
def api_hive_status() -> dict[str, Any]:
  try:
    return {"ok": True, "hive": MITOSIS.hive_status()}
  except Exception as exc:
    return {"ok": False, "error": str(exc)}


@app.post("/api/hive/spawn")
def api_hive_spawn(payload: dict[str, Any]) -> dict[str, Any]:
  name = str(payload.get("name") or "").strip()
  if not name:
    raise HTTPException(status_code=400, detail="name is required")
  try:
    node = MITOSIS.spawn_clone(name)
    return {"ok": True, "node": node, "hive": MITOSIS.hive_status()}
  except Exception as exc:
    raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/hive/decommission")
def api_hive_decommission(payload: dict[str, Any]) -> dict[str, Any]:
  name = str(payload.get("name") or "").strip()
  if not name:
    raise HTTPException(status_code=400, detail="name is required")
  try:
    result = decommission_clone(name)
    return {"ok": result.get("ok"), **result, "hive": MITOSIS.hive_status()}
  except Exception as exc:
    raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/spawn-worker")
def api_spawn_worker(payload: dict[str, Any]) -> dict[str, Any]:
  """Sovereign Build v1.0 alias for /api/hive/spawn.

  Wraps MitosisEngine.spawn_clone() with the worker-clone contract:
  - 2228 MiB VRAM target enforced via GATOR_WORKER_VRAM_MIB env var
  - 6× density cap enforced before subprocess fork
  - Per-clone Lance scratchpad at db/transient_scratchpad.lance/<slug>
  - Wakeup gate: refuses if Prime Gator has not cleared genesis verification
  """
  if not wakeup_cleared():
    raise HTTPException(
      status_code=409,
      detail="Wakeup gate not cleared - Prime Gator has not passed ignition.",
    )
  name = str(payload.get("name") or "").strip()
  if not name:
    raise HTTPException(status_code=400, detail="name is required")
  try:
    node = MITOSIS.spawn_clone(name)
    return {
      "ok": True,
      "node": node,
      "hive": MITOSIS.hive_status(),
      "vram_target_mib": WORKER_VRAM_TARGET_MIB,
      "density_cap": MAX_WORKER_DENSITY,
    }
  except Exception as exc:
    raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/hive/greenlight")
def api_hive_greenlight() -> dict[str, Any]:
  """Greenlight protocol status: wakeup + density + VRAM contract."""
  status = MITOSIS.hive_status()
  return {
    "ok": True,
    "wakeup_cleared": status.get("wakeup_cleared", False),
    "greenlight": status.get("greenlight", False),
    "worker_density": status.get("worker_density", {}),
    "vram_target_mib": WORKER_VRAM_TARGET_MIB,
    "density_cap": MAX_WORKER_DENSITY,
  }


@app.get("/api/cron/status")
def api_cron_status() -> dict[str, Any]:
  return {"ok": True, **cron_status()}


@app.post("/api/cron/start")
def api_cron_start() -> dict[str, Any]:
  return {"ok": True, **cron_start()}


@app.post("/api/cron/stop")
def api_cron_stop() -> dict[str, Any]:
  return {"ok": True, **cron_stop()}


@app.get("/api/persona")
def api_persona_get() -> dict[str, Any]:
  traits = PERSONA.current_traits()
  recent = PERSONA.get_reflections(limit=5)
  return {"ok": True, "traits": traits, "recent_reflections": recent}


@app.post("/api/persona")
async def api_persona_post(request: Request) -> dict[str, Any]:
  body: dict[str, Any] = {}
  try:
    body = await request.json()
  except Exception:
    pass
  updates = body.get("traits", {})
  if not isinstance(updates, dict):
    raise HTTPException(status_code=400, detail="traits must be a JSON object")
  updated = PERSONA.set_traits({k: float(v) for k, v in updates.items()})
  return {"ok": True, "traits": updated}


@app.get("/api/persona/reflections")
def api_persona_reflections(limit: int = Query(10, ge=1, le=100)) -> dict[str, Any]:
  return {"ok": True, "reflections": PERSONA.get_reflections(limit=limit)}


# ---------------------------------------------------------------------------
# HTMX fragment endpoints — return HTML partials for attribute-driven polling
# ---------------------------------------------------------------------------

@app.get("/htmx/vitals", response_class=HTMLResponse)
def htmx_vitals() -> str:
  try:
    data = run_pulse()
    data["telegram"] = _telegram_status()
  except Exception as exc:
    return f'<span class="pill" style="background:#8b1f2a">Vitals error: {exc}</span>'
  perf = data.get("performance", {})
  canary = data.get("canary", {})
  tg = data.get("telegram", {})
  icon = "🟢" if data.get("status") == "PASS" else "🔴"
  mode = "NATIVE" if canary.get("native_mode") else (str(data.get("status") or "UNKNOWN"))
  donor = f" [{canary['donor_addr']}]" if canary.get("donor_addr") else ""
  pills = [
    f"{icon} {mode}{donor}",
    f"TPS~ {perf.get('tps_est', 'n/a')}",
    f"VRAM: {data.get('vram', 'n/a')}",
    f"Telegram: {tg.get('indicator', '🔴')}",
    f"Bias: {canary.get('biases_applied_total', 0)}",
  ]
  if canary.get("native_mode"):
    pills.append("native://gator_kern 🟢")
  return "".join(f'<span class="pill">{p}</span>' for p in pills)


@app.get("/htmx/vram", response_class=HTMLResponse)
def htmx_vram() -> str:
  try:
    data = run_pulse()
    vram_str = str(data.get("vram") or "n/a")
  except Exception:
    vram_str = "n/a"
  pct = 0
  try:
    num = float(re.search(r"[\d.]+", vram_str).group())  # type: ignore[union-attr]
    limit = 8.0 if "GiB" in vram_str else 8192.0
    pct = min(100, int(num / limit * 100))
  except Exception:
    pct = 0
  bar_color = "#1b6b3a" if pct < 60 else "#d8742f" if pct < 85 else "#8b1f2a"
  return (
    f'<div style="display:flex;align-items:center;gap:12px;">'
    f'<span style="min-width:52px;font-size:12px;color:#7ab3d4;font-weight:700">VRAM</span>'
    f'<div style="flex:1;height:12px;background:#0a1016;border-radius:999px;overflow:hidden;border:1px solid #24384a;">'
    f'<div style="height:100%;width:{pct}%;background:{bar_color};transition:width 0.6s;"></div></div>'
    f'<span class="pill" style="min-width:80px;text-align:center">{vram_str} ({pct}%)</span>'
    f'</div>'
  )


@app.get("/htmx/cron_status", response_class=HTMLResponse)
def htmx_cron_status() -> str:
  cs = cron_status()
  enabled = bool(cs.get("enabled")) and bool(cs.get("alive"))
  pid = cs.get("pid", "—")
  badge_bg = "#1b6b3a" if enabled else "#334a60"
  badge_text = f"ON [pid={pid}]" if enabled else "OFF"
  status_payload = cs.get("status") or {}
  current_task = status_payload.get("current_task") or "—"
  last_results = status_payload.get("last_results") or {}
  rows = [
    f'<span class="pill" style="background:{badge_bg}">{badge_text}</span>',
    f'<div style="margin-top:6px;font-size:12px;color:#8ea2b8">Active task: '
    f'<strong style="color:#d6e2ef">{current_task}</strong></div>',
  ]
  if last_results:
    rows.append('<div style="margin-top:4px;font-size:11px;color:#6a8898">Last cycle:</div>')
    for task_name, result in list(last_results.items())[:4]:
      r_str = str(result)[:80]
      rows.append(
        f'<div style="font-size:11px;margin-left:8px;color:#8ea2b8;padding:1px 0">'
        f'• {task_name}: {r_str}</div>'
      )
  return "\n".join(rows)


@app.get("/htmx/tools_stream", response_class=HTMLResponse)
def htmx_tools_stream() -> str:
  import datetime
  entries: list[dict[str, Any]] = []
  if DEBUG_FILE.exists():
    try:
      lines = DEBUG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
      for ln in lines:
        try:
          obj = json.loads(ln)
          stage = obj.get("stage", "")
          if stage:
            entries.append({
              "ts": obj.get("ts", 0),
              "type": "stage",
              "label": str(stage),
              "ok": bool(obj.get("ok", True)),
            })
        except Exception:
          pass
    except Exception:
      pass
  cs = cron_status()
  status_payload = cs.get("status") or {}
  current_task = status_payload.get("current_task")
  if current_task:
    entries.append({
      "ts": time.time(),
      "type": "cron",
      "label": f"[cron] {current_task}",
      "ok": True,
    })
  entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
  if not entries:
    return (
      '<span style="color:#6a8898;font-size:12px">'
      'No activity yet — awaiting first generation or cron task.</span>'
    )
  rows = []
  for e in entries[:14]:
    bg = "#1b3a28" if e.get("ok", True) else "#3a1b1b"
    ts = e.get("ts")
    t_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""
    rows.append(
      f'<div style="display:flex;align-items:center;gap:8px;padding:3px 0;'
      f'border-bottom:1px solid #1a2736;">'
      f'<span style="font-size:10px;color:#6a8898;min-width:58px">{t_str}</span>'
      f'<span class="pill" style="background:{bg};font-size:11px">{e["label"]}</span>'
      f'</div>'
    )
  return "\n".join(rows)


@app.get("/htmx/hive", response_class=HTMLResponse)
def htmx_hive() -> str:
  try:
    hive_data = MITOSIS.hive_status()
  except Exception as exc:
    return f'<span class="pill" style="background:#8b1f2a">Hive error: {exc}</span>'
  prime = hive_data.get("prime", {})
  clones_raw = hive_data.get("clones", {})
  clones = list(clones_raw.values()) if isinstance(clones_raw, dict) else (clones_raw or [])
  rows = [
    '<table style="width:100%;font-size:12px;border-collapse:collapse">',
    '<tr>'
    '<th style="text-align:left;padding:4px;color:#7ab3d4">Node</th>'
    '<th style="text-align:left;padding:4px;color:#7ab3d4">Status</th>'
    '<th style="text-align:right;padding:4px;color:#7ab3d4">VRAM</th>'
    '</tr>',
    f'<tr><td style="padding:4px">Gator-Prime</td>'
    f'<td style="padding:4px"><span class="pill">{prime.get("status", "UNKNOWN")}</span></td>'
    f'<td style="padding:4px;text-align:right">{prime.get("vram_mib", 0)} MiB</td></tr>',
  ]
  opts = ['<option value="">-- Select a clone to decommission --</option>']
  for node in clones:
    name = node.get("name", "?")
    rows.append(
      f'<tr><td style="padding:4px">{name}</td>'
      f'<td style="padding:4px"><span class="pill">{node.get("status", "?")}</span></td>'
      f'<td style="padding:4px;text-align:right">{node.get("vram_mib", 0)} MiB</td></tr>'
    )
    opts.append(f'<option value="{name}">{name}</option>')
  rows.append('</table>')
  # OOB-swap the clone select options alongside the table
  select_oob = (
    f'<select id="cloneSelect" hx-swap-oob="true"'
    f' style="flex:1;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef">'
    + "".join(opts) +
    '</select>'
  )
  return "\n".join(rows) + "\n" + select_oob


@app.get("/htmx/debug", response_class=HTMLResponse)
def htmx_debug() -> str:
  if not DEBUG_FILE.exists():
    return "No debug data yet."
  lines = DEBUG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
  out = []
  for ln in lines:
    try:
      out.append(json.dumps(json.loads(ln), ensure_ascii=True))
    except Exception:
      out.append(ln)
  return "\n".join(out) or "No debug data yet."


@app.get("/htmx/greenlight", response_class=HTMLResponse)
def htmx_greenlight() -> str:
  """HTMX fragment: SYSTEM GREENLIGHT pill + auto-enables Spawn 35B Worker btn."""
  status = MITOSIS.hive_status()
  cleared = bool(status.get("wakeup_cleared", False))
  greenlight = bool(status.get("greenlight", False))
  density = status.get("worker_density", {})
  live = int(density.get("live", 0))
  cap = int(density.get("cap", MAX_WORKER_DENSITY))
  vram = int(density.get("per_worker_vram_mib", WORKER_VRAM_TARGET_MIB))
  if greenlight:
    bg, fg, label = "#1b6b3a", "#fff", f"✅ SYSTEM GREENLIGHT · {live}/{cap} workers · {vram} MiB ea."
    enable_js = (
      "<script>(function(){var b=document.getElementById('mitosisBtn');"
      "if(b){b.disabled=false;b.style.opacity='1';b.style.cursor='pointer';}})();</script>"
    )
  elif cleared:
    bg, fg, label = "#a07020", "#fff", "⚠️ DENSITY CAP REACHED"
    enable_js = ""
  else:
    bg, fg, label = "#3a3a3a", "#aaa", "⏳ AWAITING IGNITION"
    enable_js = (
      "<script>(function(){var b=document.getElementById('mitosisBtn');"
      "if(b){b.disabled=true;b.style.opacity='0.5';b.style.cursor='not-allowed';}})();</script>"
    )
  return (
    f'<span id="greenlightPill" class="pill" '
    f'hx-get="/htmx/greenlight" hx-trigger="every 6s" hx-swap="outerHTML" '
    f'style="background:{bg};color:{fg};font-weight:700">{label}</span>{enable_js}'
  )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    graph_url = "/graph" if GRAPH_HTML.exists() else ""
    graph_frame = (
        f'<iframe id="graphFrame" src="{graph_url}"></iframe>'
        if graph_url
        else '<pre>graph.html not found. Run scholar/graphify update first.</pre>'
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Gator Surgical Lab</title>
  <script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"></script>
  <style>
    :root {{ --bg:#0c1218; --card:#131d27; --ink:#d6e2ef; --muted:#8ea2b8; }}
    body {{ margin:0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: radial-gradient(1200px 600px at 20% -20%, #1a2836, var(--bg)); color:var(--ink); }}
    .wrap {{ padding:16px; display:grid; grid-template-columns: 1.1fr 1fr; gap:12px; }}
    .card {{ background:var(--card); border:1px solid #1f2c39; border-radius:12px; padding:12px; box-shadow: 0 8px 24px rgba(0,0,0,.25); }}
    h1 {{ margin:0 0 8px; font-size:18px; letter-spacing:.3px; }}
    h2 {{ margin:0 0 8px; font-size:14px; color:var(--muted); }}
    .row {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .pill {{ background:#0f1720; border:1px solid #24384a; border-radius:999px; padding:4px 10px; font-size:12px; }}
    button {{ background:#8b1f2a; color:#fff; border:0; border-radius:8px; padding:8px 12px; cursor:pointer; font-weight:700; }}
    iframe {{ width:100%; min-height:420px; border:0; border-radius:10px; background:#0a1016; }}
    pre {{ max-height:280px; overflow:auto; white-space:pre-wrap; font-size:11px; background:#0f1720; border-radius:8px; padding:10px; }}
    .vram-strip {{ padding:8px 16px 4px; }}
    .stream-entry {{ display:flex; align-items:center; gap:8px; padding:3px 0; border-bottom:1px solid #1a2736; }}
    @media (max-width: 900px) {{ .wrap {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>

  <!-- VRAM monitor strip (HTMX-polled every 5s) -->
  <div class="vram-strip"
       hx-get="/htmx/vram"
       hx-trigger="load, every 5s"
       hx-swap="innerHTML">
    <span style="color:#6a8898;font-size:12px">VRAM loading…</span>
  </div>

  <div class="wrap">

    <!-- ── Vitals card ──────────────────────────────────────── -->
    <section class="card">
      <h1>Vitals</h1>
      <h2>Live TPS · native kernel · VRAM · Telegram</h2>
      <div id="pills" class="row"
           hx-get="/htmx/vitals"
           hx-trigger="load, every 7s"
           hx-swap="innerHTML">
        <span class="pill">Loading…</span>
      </div>
      <div class="row" style="margin:8px 0">
        <button hx-post="/api/interrupt"
                hx-swap="none"
                hx-on::after-request="alert('Interrupt sent')">Interrupt</button>
        <button id="voiceOnBtn" style="background:#1b6b3a">🎙️ Voice ON</button>
        <button id="voiceOffBtn" style="background:#8b1f2a">🔇 Voice OFF</button>
        <button id="setupTelegramBtn">⚙️ Setup Telegram</button>
        <button id="mitosisBtn"
                title="Spawn a 35B Worker clone (capped at 2228 MiB VRAM, 6× max density)"
                disabled
                style="background:#1b6b3a">Spawn</button>
        <span id="greenlightPill" class="pill"
              hx-get="/htmx/greenlight"
              hx-trigger="load, every 6s"
              hx-swap="outerHTML"
              style="background:#3a3a3a;color:#aaa">⏳ AWAITING IGNITION</span>
        <button style="background:#1e4060"
                hx-post="/api/session_reset"
                hx-swap="none"
                hx-on::after-request="alert('Session reset sent')">↺ Reset Session</button>
      </div>
    </section>

    <!-- ── Graph Map card ───────────────────────────────────── -->
    <section class="card">
      <h1>Graph Map</h1>
      <h2>Graphify structural map</h2>
      {graph_frame}
    </section>

    <!-- ── Cron / Heartbeat card ────────────────────────────── -->
    <section class="card">
      <h1>Cron / Heartbeat</h1>
      <h2>Dreaming &amp; maintenance runner — hard kill switch</h2>
      <div class="row" style="margin-bottom:8px">
        <button onclick="setCronEnabled(true)"  style="background:#1b6b3a">Cron ON</button>
        <button onclick="setCronEnabled(false)" style="background:#8b1f2a">Cron OFF</button>
      </div>
      <div id="cronBody"
           hx-get="/htmx/cron_status"
           hx-trigger="load, every 3s"
           hx-swap="innerHTML">
        <span class="pill">Loading…</span>
      </div>
    </section>

    <!-- ── Task / Tool Stream widget ────────────────────────── -->
    <section class="card">
      <h1>Task / Tool Stream</h1>
      <h2>Live generation stages &amp; cron task heartbeat</h2>
      <div id="toolStream"
           hx-get="/htmx/tools_stream"
           hx-trigger="load, every 3s"
           hx-swap="innerHTML">
        <span style="color:#6a8898;font-size:12px">Loading…</span>
      </div>
    </section>

    <!-- ── Persona Engine (full width) ──────────────────────── -->
    <section class="card" style="grid-column:1/-1;">
      <h1>Persona Engine</h1>
      <h2>Neural trait sliders — steer the 35B logic donor in real time</h2>
      <div id="personaSliders" style="display:grid;gap:10px;margin:10px 0;">
        <div><label style="display:flex;align-items:center;gap:10px;font-size:13px;">
          <span style="min-width:90px;color:#7ab3d4">Curiosity</span>
          <input type="range" id="traitCuriosity" min="0" max="100" value="50" style="flex:1" oninput="updatePersonaLabel(this,'curiosity')" />
          <span id="lblCuriosity" class="pill" style="min-width:60px;text-align:center">50%</span>
          <span style="font-size:11px;color:#6a8898;min-width:170px">focused → exploratory</span>
        </label></div>
        <div><label style="display:flex;align-items:center;gap:10px;font-size:13px;">
          <span style="min-width:90px;color:#7ab3d4">Directness</span>
          <input type="range" id="traitDirectness" min="0" max="100" value="50" style="flex:1" oninput="updatePersonaLabel(this,'directness')" />
          <span id="lblDirectness" class="pill" style="min-width:60px;text-align:center">50%</span>
          <span style="font-size:11px;color:#6a8898;min-width:170px">verbose → surgical</span>
        </label></div>
        <div><label style="display:flex;align-items:center;gap:10px;font-size:13px;">
          <span style="min-width:90px;color:#7ab3d4">Caution</span>
          <input type="range" id="traitCaution" min="0" max="100" value="50" style="flex:1" oninput="updatePersonaLabel(this,'caution')" />
          <span id="lblCaution" class="pill" style="min-width:60px;text-align:center">50%</span>
          <span style="font-size:11px;color:#6a8898;min-width:170px">bold → careful</span>
        </label></div>
        <div><label style="display:flex;align-items:center;gap:10px;font-size:13px;">
          <span style="min-width:90px;color:#7ab3d4">Creativity</span>
          <input type="range" id="traitCreativity" min="0" max="100" value="50" style="flex:1" oninput="updatePersonaLabel(this,'creativity')" />
          <span id="lblCreativity" class="pill" style="min-width:60px;text-align:center">50%</span>
          <span style="font-size:11px;color:#6a8898;min-width:170px">conventional → inventive</span>
        </label></div>
        <div><label style="display:flex;align-items:center;gap:10px;font-size:13px;">
          <span style="min-width:90px;color:#7ab3d4">Empathy</span>
          <input type="range" id="traitEmpathy" min="0" max="100" value="50" style="flex:1" oninput="updatePersonaLabel(this,'empathy')" />
          <span id="lblEmpathy" class="pill" style="min-width:60px;text-align:center">50%</span>
          <span style="font-size:11px;color:#6a8898;min-width:170px">clinical → warm</span>
        </label></div>
        <div><label style="display:flex;align-items:center;gap:10px;font-size:13px;">
          <span style="min-width:90px;color:#7ab3d4">Precision</span>
          <input type="range" id="traitPrecision" min="0" max="100" value="50" style="flex:1" oninput="updatePersonaLabel(this,'precision')" />
          <span id="lblPrecision" class="pill" style="min-width:60px;text-align:center">50%</span>
          <span style="font-size:11px;color:#6a8898;min-width:170px">approximate → exact</span>
        </label></div>
      </div>
      <div class="row" style="margin-bottom:8px;">
        <button id="personaSaveBtn" style="background:#1b6b3a">Apply Traits</button>
        <button id="personaLoadBtn" style="background:#1e4060">Reload</button>
        <span id="personaSaveStatus" class="pill">idle</span>
      </div>
      <pre id="personaReflections" style="max-height:160px;overflow:auto">Loading reflections…</pre>
    </section>

    <!-- ── Debug Decisions (full width, HTMX-polled) ─────────── -->
    <section class="card" style="grid-column:1/-1;">
      <h1>Debug Decisions</h1>
      <h2>High-fidelity logit decisions (when GATOR_DEBUG=true)</h2>
      <pre id="debug"
           hx-get="/htmx/debug"
           hx-trigger="load, every 9s"
           hx-swap="innerHTML">No debug data yet.</pre>
    </section>

    <!-- ── Ingestion (full width) ────────────────────────────── -->
    <section class="card" style="grid-column:1/-1;">
      <h1>Ingestion</h1>
      <h2>Shredding and Distillation progress</h2>
      <div class="row" style="margin-bottom:8px;">
        <input id="pdfPath" type="text"
               placeholder="/home/user/Gator/research/sample.pdf"
               style="flex:1;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef" />
        <button id="ingestBtn">Start Ingest</button>
      </div>
      <div style="height:16px;background:#0f1720;border-radius:999px;overflow:hidden;border:1px solid #24384a;">
        <div id="ingestBar" style="height:100%;width:0%;background:linear-gradient(90deg,#8b1f2a,#d8742f);"></div>
      </div>
      <div class="row" style="margin-top:8px;">
        <span class="pill">Phase: <span id="ingestPhase">idle</span></span>
        <span class="pill">Progress: <span id="ingestPercent">0</span>%</span>
        <span class="pill">Markers: Shredding / Distillation / Indexing</span>
      </div>
      <pre id="ingestStatus">Idle</pre>
    </section>

    <!-- ── Hive Status (full width, HTMX-polled) ─────────────── -->
    <section class="card" style="grid-column:1/-1;">
      <h1>Hive Status</h1>
      <h2>Active clones and per-node VRAM footprint</h2>
      <div class="row" style="margin-bottom:8px;">
        <select id="cloneSelect"
                style="flex:1;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef">
          <option value="">-- Select a clone to decommission --</option>
        </select>
        <button id="decommissionBtn" style="background:#8b4a4a">Decommission</button>
      </div>
      <div id="hiveBody"
           hx-get="/htmx/hive"
           hx-trigger="load, every 4s"
           hx-swap="innerHTML">
        <span style="color:#6a8898;font-size:12px">Loading hive…</span>
      </div>
    </section>

  </div><!-- /.wrap -->

  <!-- ── Modals ──────────────────────────────────────────────── -->
  <div id="tgModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);align-items:center;justify-content:center;z-index:9999;">
    <div class="card" style="width:min(520px,92vw)">
      <h1>Telegram Setup</h1>
      <h2>Credentials are stored in ~/Gator/.env</h2>
      <div style="display:grid;gap:8px;">
        <label>Telegram Bot Token<br><input id="tgToken" type="text" style="width:100%;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef" /></label>
        <label>Bot Username<br><input id="tgUsername" type="text" style="width:100%;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef" /></label>
        <label>Authorized Chat ID (locked)<br><input id="tgChatId" type="text" style="width:100%;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef" /></label>
      </div>
      <div class="row" style="margin-top:10px;">
        <button id="tgSaveBtn">Save</button>
        <button id="tgCloseBtn" style="background:#334a60">Close</button>
      </div>
      <pre id="tgModalStatus" style="margin-top:8px;">Idle</pre>
    </div>
  </div>
  <div id="mitosisModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);align-items:center;justify-content:center;z-index:9999;">
    <div class="card" style="width:min(520px,92vw)">
      <h1>Mitosis</h1>
      <h2>Enter name for New Worker Node:</h2>
      <div style="display:grid;gap:8px;">
        <input id="cloneName" type="text" placeholder="Gator-Scout" style="width:100%;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef" />
      </div>
      <div class="row" style="margin-top:10px;">
        <button id="cloneSpawnBtn">Spawn</button>
        <button id="cloneCloseBtn" style="background:#334a60">Close</button>
      </div>
      <pre id="mitosisStatus" style="margin-top:8px;">Idle</pre>
    </div>
  </div>
  <div id="decommissionModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);align-items:center;justify-content:center;z-index:9999;">
    <div class="card" style="width:min(520px,92vw)">
      <h1>Decommission Clone</h1>
      <h2 id="decommissionPrompt">Are you sure?</h2>
      <p style="color:#d6e2ef;font-size:13px;line-height:1.6;">
        The clone process will be terminated and its environment files cleaned up.<br><br>
        <strong>Scholar Sense data will be PRESERVED</strong> — the clone's knowledge contribution remains in the shared memory.<br><br>
        <span id="decommissionWarning" style="color:#f4a46e;">⚠️ This action cannot be undone.</span>
      </p>
      <div class="row" style="margin-top:10px;">
        <button id="decommissionConfirmBtn" style="background:#8b1f2a">Confirm Decommission</button>
        <button id="decommissionCancelBtn" style="background:#334a60">Cancel</button>
      </div>
      <pre id="decommissionStatus" style="margin-top:8px;">Idle</pre>
    </div>
  </div>

  <script>
    // ── Modal helpers ──────────────────────────────────────────
    function showModal() {{ document.getElementById('tgModal').style.display = 'flex'; }}
    function hideModal() {{ document.getElementById('tgModal').style.display = 'none'; }}
    function showMitosisModal() {{ document.getElementById('mitosisModal').style.display = 'flex'; }}
    function hideMitosisModal() {{ document.getElementById('mitosisModal').style.display = 'none'; }}
    function showDecommissionModal(cloneName) {{
      document.getElementById('decommissionModal').style.display = 'flex';
      document.getElementById('decommissionPrompt').textContent = 'Decommission ' + cloneName + '?';
      document.getElementById('decommissionStatus').textContent = 'Ready to confirm.';
      document.getElementById('decommissionConfirmBtn').dataset.cloneName = cloneName;
    }}
    function hideDecommissionModal() {{ document.getElementById('decommissionModal').style.display = 'none'; }}

    // ── Telegram config modal ──────────────────────────────────
    async function loadTelegramConfig() {{
      const r = await fetch('/api/config/telegram');
      const d = await r.json();
      document.getElementById('tgToken').value = d.token || '';
      document.getElementById('tgUsername').value = d.username || '';
      const chat = document.getElementById('tgChatId');
      chat.value = d.chat_id || '';
      chat.readOnly = !!d.chat_locked;
      document.getElementById('tgModalStatus').textContent = JSON.stringify(d.status || {{}}, null, 2);
    }}
    async function saveTelegramConfig() {{
      const payload = {{
        token: document.getElementById('tgToken').value.trim(),
        username: document.getElementById('tgUsername').value.trim(),
        chat_id: document.getElementById('tgChatId').value.trim(),
      }};
      const r = await fetch('/api/config/telegram', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const d = await r.json();
      document.getElementById('tgModalStatus').textContent = JSON.stringify(d, null, 2);
    }}

    // ── Cron on/off (triggers HTMX reload after action) ────────
    async function setCronEnabled(enabled) {{
      await fetch(enabled ? '/api/cron/start' : '/api/cron/stop', {{ method: 'POST' }});
      htmx.trigger(document.getElementById('cronBody'), 'load');
    }}

    // ── Voice chat on/off ───────────────────────────────────────
    async function setVoiceEnabled(enabled) {{
      const endpoint = enabled ? '/api/voice/on' : '/api/voice/off';
      try {{
        const r = await fetch(endpoint, {{ method: 'POST' }});
        const d = await r.json();
        if (d.ok) {{
          const msg = enabled ? 'Voice chat enabled (Piper TTS + Whisper STT)' : 'Voice chat disabled (freed VRAM)';
          alert(msg);
          updateVoiceButtonStates();
        }}
      }} catch (err) {{
        alert('Voice toggle error: ' + String(err));
      }}
    }}

    async function updateVoiceButtonStates() {{
      try {{
        const r = await fetch('/api/voice/status');
        const d = await r.json();
        document.getElementById('voiceOnBtn').style.opacity = d.enabled ? '1' : '0.5';
        document.getElementById('voiceOffBtn').style.opacity = d.enabled ? '0.5' : '1';
      }} catch (err) {{
        console.error('Voice status update failed:', err);
      }}
    }}

    document.getElementById('voiceOnBtn').addEventListener('click', () => setVoiceEnabled(true));
    document.getElementById('voiceOffBtn').addEventListener('click', () => setVoiceEnabled(false));

    // ── Ingest bar (JS-driven for CSS width animation) ─────────
    let lastGraphRefreshTs = 0;
    function reloadGraphFrame() {{
      const frame = document.getElementById('graphFrame');
      if (frame) frame.src = '/graph?ts=' + Date.now();
    }}
    async function refreshIngestStatus() {{
      const r = await fetch('/api/ingest_status');
      const d = await r.json();
      document.getElementById('ingestPhase').textContent = d.state;
      document.getElementById('ingestPercent').textContent = d.percent || 0;
      document.getElementById('ingestBar').style.width = (d.percent || 0) + '%';
      document.getElementById('ingestStatus').textContent = JSON.stringify(d, null, 2);
      if (d.state === 'complete' && (d.updated_at || 0) > lastGraphRefreshTs) {{
        lastGraphRefreshTs = d.updated_at || Date.now() / 1000;
        reloadGraphFrame();
      }}
    }}

    // ── Persona Engine sliders ─────────────────────────────────
    const TRAIT_KEYS = ['curiosity','directness','caution','creativity','empathy','precision'];
    function updatePersonaLabel(slider, trait) {{
      const pct = parseInt(slider.value);
      const cap = trait.charAt(0).toUpperCase() + trait.slice(1);
      document.getElementById('lbl' + cap).textContent = pct + '%';
    }}
    async function loadPersonaTraits() {{
      try {{
        const r = await fetch('/api/persona');
        const d = await r.json();
        const traits = d.traits || {{}};
        TRAIT_KEYS.forEach(k => {{
          const el = document.getElementById('trait' + k.charAt(0).toUpperCase() + k.slice(1));
          if (el) {{
            const val = Math.round((traits[k] ?? 0.5) * 100);
            el.value = val;
            updatePersonaLabel(el, k);
          }}
        }});
        const reflData = d.recent_reflections || [];
        document.getElementById('personaReflections').textContent =
          reflData.length ? JSON.stringify(reflData.slice(0,3), null, 2) : 'No reflections yet.';
      }} catch (err) {{
        document.getElementById('personaReflections').textContent = 'Load error: ' + String(err);
      }}
    }}
    document.getElementById('personaSaveBtn').addEventListener('click', async () => {{
      const updates = {{}};
      TRAIT_KEYS.forEach(k => {{
        const el = document.getElementById('trait' + k.charAt(0).toUpperCase() + k.slice(1));
        if (el) updates[k] = parseInt(el.value) / 100;
      }});
      try {{
        const r = await fetch('/api/persona', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{traits: updates}}),
        }});
        const d = await r.json();
        document.getElementById('personaSaveStatus').textContent = d.ok ? 'saved' : 'error';
        setTimeout(() => {{ document.getElementById('personaSaveStatus').textContent = 'idle'; }}, 2000);
      }} catch (err) {{
        document.getElementById('personaSaveStatus').textContent = 'err: ' + String(err);
      }}
    }});
    document.getElementById('personaLoadBtn').addEventListener('click', loadPersonaTraits);

    // ── Hive spawn / decommission ──────────────────────────────
    async function spawnClone() {{
      const name = document.getElementById('cloneName').value.trim();
      if (!name) {{ alert('Worker name required'); return; }}
      const r = await fetch('/api/spawn-worker', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name }}),
      }});
      const d = await r.json();
      if (!r.ok) {{
        document.getElementById('mitosisStatus').textContent =
          '❌ ' + (d.detail || JSON.stringify(d));
        return;
      }}
      document.getElementById('mitosisStatus').textContent =
        '✅ Spawned ' + name + ' · pid=' + (d.node && d.node.pid) +
        ' · VRAM target=' + d.vram_target_mib + ' MiB' +
        ' · density=' + (d.hive && d.hive.worker_density && d.hive.worker_density.live) +
        '/' + d.density_cap;
      htmx.trigger(document.getElementById('hiveBody'), 'load');
      htmx.trigger(document.getElementById('greenlightPill'), 'load');
    }}
    async function decommissionClone(cloneName) {{
      if (!cloneName) {{ alert('Please select a clone to decommission.'); return; }}
      const r = await fetch('/api/hive/decommission', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name: cloneName }}),
      }});
      const d = await r.json();
      document.getElementById('decommissionStatus').textContent = JSON.stringify(d, null, 2);
      await new Promise(res => setTimeout(res, 1000));
      hideDecommissionModal();
      document.getElementById('cloneSelect').value = '';
      htmx.trigger(document.getElementById('hiveBody'), 'load');
    }}

    // ── Event listeners ────────────────────────────────────────
    document.getElementById('setupTelegramBtn').addEventListener('click', async () => {{
      showModal(); await loadTelegramConfig();
    }});
    document.getElementById('tgCloseBtn').addEventListener('click', hideModal);
    document.getElementById('tgSaveBtn').addEventListener('click', saveTelegramConfig);
    document.getElementById('mitosisBtn').addEventListener('click', () => {{
      showMitosisModal();
      document.getElementById('mitosisStatus').textContent = 'Enter name for New Worker Node:';
    }});
    document.getElementById('cloneCloseBtn').addEventListener('click', hideMitosisModal);
    document.getElementById('cloneSpawnBtn').addEventListener('click', spawnClone);
    document.getElementById('ingestBtn').addEventListener('click', async () => {{
      const pdfPath = document.getElementById('pdfPath').value.trim();
      const r = await fetch('/api/ingest_pdf', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ pdf_path: pdfPath }}),
      }});
      const d = await r.json();
      document.getElementById('ingestStatus').textContent = JSON.stringify(d, null, 2);
      refreshIngestStatus();
    }});
    document.getElementById('decommissionBtn').addEventListener('click', () => {{
      const cloneName = document.getElementById('cloneSelect').value;
      if (!cloneName) {{ alert('Please select a clone to decommission.'); return; }}
      showDecommissionModal(cloneName);
    }});
    document.getElementById('decommissionCancelBtn').addEventListener('click', hideDecommissionModal);
    document.getElementById('decommissionConfirmBtn').addEventListener('click', async () => {{
      const cloneName = document.getElementById('decommissionConfirmBtn').dataset.cloneName;
      await decommissionClone(cloneName);
    }});

    // ── Init ───────────────────────────────────────────────────
    loadPersonaTraits();
    refreshIngestStatus();
    updateVoiceButtonStates();
    setInterval(refreshIngestStatus, 1500);
    // Vitals, cron, hive, debug, tools stream, vram — all driven by HTMX hx-trigger="load, every Xs"
  </script>
</body>
</html>"""
def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Surgical Lab web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    _main()
