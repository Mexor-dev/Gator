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
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from event_bus import EventBusClient
from pulse_check import run_pulse
from scholar_sense import ScholarSense
from discovery.cluster_namer import ClusterNamer, ClusterNamerError
from core.mitosis import MitosisEngine
from decommission_node import decommission_clone

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
  return {"ok": True, "nodes": nodes, "edges": edges, "legend": legend, "community_labels": labels}


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
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>Gator Surgical Lab</title>
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
    @media (max-width: 900px) {{ .wrap {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <section class=\"card\">
      <h1>Vitals</h1>
      <h2>Live TPS, PID health, canary graft state, VRAM <span id="tgStatusInline" class="pill">Telegram: 🔴</span></h2>
      <div id=\"pills\" class=\"row\"></div>
      <div class="row" style="margin:8px 0">
        <button id="interruptBtn">Interrupt</button>
        <button id="setupTelegramBtn">⚙️ Setup Telegram</button>
          <button id="mitosisBtn">[M] MITOSIS</button>
          <button id="sessionResetBtn" style="background:#1e4060">↺ Reset Session</button>
      </div>
      <pre id=\"vitals\">Loading...</pre>
    </section>
    <section class=\"card\">
      <h1>Graph Map</h1>
      <h2>Graphify structural map</h2>
      {graph_frame}
    </section>
    <section class=\"card\" style=\"grid-column:1/-1;\">
      <h1>Debug Decisions</h1>
      <h2>High-fidelity logit decisions (when GATOR_DEBUG=true)</h2>
      <pre id=\"debug\">No debug data yet.</pre>
    </section>
    <section class=\"card\" style=\"grid-column:1/-1;\">
      <h1>Ingestion</h1>
      <h2>Shredding and Distillation progress</h2>
      <div class=\"row\" style=\"margin-bottom:8px;\">
        <input id=\"pdfPath\" type=\"text\" placeholder=\"/home/user/Gator/research/sample.pdf\" style=\"flex:1;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef\" />
        <button id=\"ingestBtn\">Start Ingest</button>
      </div>
      <div style=\"height:16px;background:#0f1720;border-radius:999px;overflow:hidden;border:1px solid #24384a;\">
        <div id=\"ingestBar\" style=\"height:100%;width:0%;background:linear-gradient(90deg,#8b1f2a,#d8742f);\"></div>
      </div>
      <div class=\"row\" style=\"margin-top:8px;\">
        <span class=\"pill\">Phase: <span id=\"ingestPhase\">idle</span></span>
        <span class=\"pill\">Progress: <span id=\"ingestPercent\">0</span>%</span>
        <span class=\"pill\">Markers: Shredding / Distillation / Indexing</span>
      </div>
      <pre id=\"ingestStatus\">Idle</pre>
    </section>
      <section class="card" style="grid-column:1/-1;">
        <h1>Hive Status</h1>
        <h2>Active clones and per-node VRAM footprint</h2>
        <div class="row" style="margin-bottom:8px;">
          <select id="cloneSelect" style="flex:1;padding:8px;border-radius:8px;border:1px solid #24384a;background:#0f1720;color:#d6e2ef">
            <option value="">-- Select a clone to decommission --</option>
          </select>
          <button id="decommissionBtn" style="background:#8b4a4a">Decommission</button>
        </div>
        <pre id="hiveStatus">Loading hive...</pre>
      </section>
  </div>
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
        The clone process will be terminated and its environment files cleaned up.
        <br><br>
        <strong>Scholar Sense data will be PRESERVED</strong> — the clone's knowledge contribution remains in the shared memory.
        <br><br>
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
    function showModal() {{
      document.getElementById('tgModal').style.display = 'flex';
    }}
    function hideModal() {{
      document.getElementById('tgModal').style.display = 'none';
    }}
    function showMitosisModal() {{
      document.getElementById('mitosisModal').style.display = 'flex';
    }}
    function hideMitosisModal() {{
      document.getElementById('mitosisModal').style.display = 'none';
    }}
    async function loadTelegramConfig() {{
      const r = await fetch('/api/config/telegram');
      const d = await r.json();
      const token = document.getElementById('tgToken');
      token.value = d.token || '';
      token.placeholder = '';
      const username = document.getElementById('tgUsername');
      username.value = d.username || '';
      username.placeholder = '';
      const chat = document.getElementById('tgChatId');
      chat.value = d.chat_id || '';
      chat.placeholder = '';
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
      if (!r.ok) {{
        document.getElementById('tgModalStatus').textContent = JSON.stringify(d, null, 2);
        return;
      }}
      document.getElementById('tgModalStatus').textContent = JSON.stringify(d, null, 2);
      await refreshVitals();
    }}
    async function refreshVitals() {{
      try {{
        const r = await fetch('/api/vitals');
        if (!r.ok) {{
          throw new Error(`vitals http ${{r.status}}`);
        }}
        const d = await r.json();
        document.getElementById('vitals').textContent = JSON.stringify(d, null, 2);

        const performance = d.performance || {{}};
        const telegram = d.telegram || {{}};
        const canary = d.canary || {{}};

        const pills = [];
        const statusIcon = d.status === 'PASS' ? '🟢' : '🔴';
        const modeLabel = canary.native_mode ? 'NATIVE' : (d.status || 'UNKNOWN');
        const donorTag = canary.donor_addr ? ` [${{canary.donor_addr}}]` : '';
        pills.push(`${{statusIcon}} ${{modeLabel}}${{donorTag}}`);
        pills.push(`TPS~ ${{performance.tps_est ?? 'n/a'}}`);
        pills.push(`VRAM: ${{d.vram || 'n/a'}}`);
        pills.push(`Telegram: ${{telegram.indicator || '🔴'}}`);
        pills.push(`Bias Applied: ${{canary.biases_applied_total ?? 0}}`);
        if (canary.native_mode) {{
          pills.push('native://gator_kern 🟢 ACTIVE');
        }}
        document.getElementById('tgStatusInline').textContent = `Telegram: ${{telegram.indicator || '🔴'}}`;
        document.getElementById('pills').innerHTML = pills.map(p => `<span class=\"pill\">${{p}}</span>`).join('');
      }} catch (err) {{
        document.getElementById('vitals').textContent = `Vitals refresh error: ${{String(err)}}`;
      }}
    }}
    async function refreshDebug() {{
      const r = await fetch('/api/debug_tail');
      const d = await r.json();
      document.getElementById('debug').textContent = JSON.stringify(d, null, 2);
    }}
    let lastGraphRefreshTs = 0;
    function reloadGraphFrame() {{
      const frame = document.getElementById('graphFrame');
      if (!frame) {{
        return;
      }}
      const base = '/graph';
      frame.src = `${{base}}?ts=${{Date.now()}}`;
    }}
    async function refreshIngestStatus() {{
      const r = await fetch('/api/ingest_status');
      const d = await r.json();
      document.getElementById('ingestPhase').textContent = d.state;
      document.getElementById('ingestPercent').textContent = d.percent || 0;
      document.getElementById('ingestBar').style.width = `${{d.percent || 0}}%`;
      document.getElementById('ingestStatus').textContent = JSON.stringify(d, null, 2);
      if (d.state === 'complete' && (d.updated_at || 0) > lastGraphRefreshTs) {{
        lastGraphRefreshTs = d.updated_at || Date.now() / 1000;
        reloadGraphFrame();
      }}
    }}
    async function refreshHiveStatus() {{
      try {{
        const r = await fetch('/api/hive/status');
        if (!r.ok) {{
          throw new Error(`hive http ${{r.status}}`);
        }}
        const d = await r.json();
        if (!d.ok) {{
          document.getElementById('hiveStatus').textContent = JSON.stringify(d, null, 2);
          return;
        }}

        const hive = d.hive || {{}};
        const prime = hive.prime || {{}};
        const clones = Array.isArray(hive.clones) ? hive.clones : Object.values(hive.clones || {{}});
        const lines = [];
        lines.push(`Gator-Prime [${{prime.status || 'UNKNOWN'}}] VRAM=${{prime.vram_mib || 0}} MiB`);

        const select = document.getElementById('cloneSelect');
        select.innerHTML = '<option value="">-- Select a clone to decommission --</option>';
        for (const node of clones) {{
          lines.push(`${{node.name}} [${{node.status}}] VRAM=${{node.vram_mib || 0}} MiB`);
          const opt = document.createElement('option');
          opt.value = node.name;
          opt.textContent = node.name;
          select.appendChild(opt);
        }}
        document.getElementById('hiveStatus').textContent = lines.join(String.fromCharCode(10));
      }} catch (err) {{
        document.getElementById('hiveStatus').textContent = `Hive refresh error: ${{String(err)}}`;
      }}
    }}
    async function spawnClone() {{
      const name = document.getElementById('cloneName').value.trim();
      const r = await fetch('/api/hive/spawn', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name }}),
      }});
      const d = await r.json();
      document.getElementById('mitosisStatus').textContent = JSON.stringify(d, null, 2);
      await refreshHiveStatus();
    }}
    function showDecommissionModal(cloneName) {{
      document.getElementById('decommissionModal').style.display = 'flex';
      document.getElementById('decommissionPrompt').textContent = `Decommission ${{cloneName}}?`;
      document.getElementById('decommissionStatus').textContent = 'Ready to confirm.';
      document.getElementById('decommissionConfirmBtn').dataset.cloneName = cloneName;
    }}
    function hideDecommissionModal() {{
      document.getElementById('decommissionModal').style.display = 'none';
    }}
    async function decommissionClone(cloneName) {{
      if (!cloneName) {{
        alert('Please select a clone to decommission.');
        return;
      }}
      const r = await fetch('/api/hive/decommission', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name: cloneName }}),
      }});
      const d = await r.json();
      document.getElementById('decommissionStatus').textContent = JSON.stringify(d, null, 2);
      await new Promise(r => setTimeout(r, 1000));
      hideDecommissionModal();
      document.getElementById('cloneSelect').value = '';
      await refreshHiveStatus();
    }}
    refreshVitals();
    refreshDebug();
    refreshIngestStatus();
    refreshHiveStatus();
    document.getElementById('interruptBtn').addEventListener('click', async () => {{
      const r = await fetch('/api/interrupt', {{ method: 'POST' }});
      const d = await r.json();
      alert('Interrupt sent: ' + JSON.stringify(d));
      refreshVitals();
    }});
    document.getElementById('setupTelegramBtn').addEventListener('click', async () => {{
      showModal();
      await loadTelegramConfig();
    }});
    document.getElementById('tgCloseBtn').addEventListener('click', () => {{
      hideModal();
    }});
    document.getElementById('tgSaveBtn').addEventListener('click', async () => {{
      await saveTelegramConfig();
    }});
    document.getElementById('mitosisBtn').addEventListener('click', async () => {{
      showMitosisModal();
      document.getElementById('mitosisStatus').textContent = 'Enter name for New Worker Node:';
    }});
    document.getElementById('cloneCloseBtn').addEventListener('click', () => {{
      hideMitosisModal();
    }});
    document.getElementById('sessionResetBtn').addEventListener('click', async () => {{
      const r = await fetch('/api/session_reset', {{ method: 'POST' }});
      const d = await r.json();
      alert('Session Reset: ' + JSON.stringify(d));
    }});
    document.getElementById('cloneSpawnBtn').addEventListener('click', async () => {{
      await spawnClone();
    }});
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
    setInterval(refreshVitals, 7000);
    setInterval(refreshDebug, 9000);
    setInterval(refreshIngestStatus, 1500);
    setInterval(refreshHiveStatus, 2500);
    document.getElementById('decommissionBtn').addEventListener('click', () => {{
      const cloneName = document.getElementById('cloneSelect').value;
      if (!cloneName) {{
        alert('Please select a clone to decommission.');
        return;
      }}
      showDecommissionModal(cloneName);
    }});
    document.getElementById('decommissionCancelBtn').addEventListener('click', () => {{
      hideDecommissionModal();
    }});
    document.getElementById('decommissionConfirmBtn').addEventListener('click', async () => {{
      const cloneName = document.getElementById('decommissionConfirmBtn').dataset.cloneName;
      await decommissionClone(cloneName);
    }});
  </script>
</body>
</html>"""


@app.get("/graph", response_class=HTMLResponse)
def graph_page() -> HTMLResponse:
    if not GRAPH_HTML.exists():
        return HTMLResponse("<h3>Graph map missing</h3>", status_code=404)
    try:
      nodes, edges, labels = _dynamic_graph_assets()
    except ClusterNamerError as exc:
      return HTMLResponse(f"<h3>Graph naming error: {exc}</h3>", status_code=500)
    except Exception as exc:
      return HTMLResponse(f"<h3>Graph render error: {exc}</h3>", status_code=500)

    html = GRAPH_HTML.read_text(encoding="utf-8", errors="replace")
    html = re.sub(r"const RAW_NODES = \[.*?\];", f"const RAW_NODES = {json.dumps(nodes, ensure_ascii=False)};", html, count=1, flags=re.S)
    html = re.sub(r"const RAW_EDGES = \[.*?\];", f"const RAW_EDGES = {json.dumps(edges, ensure_ascii=False)};", html, count=1, flags=re.S)

    legend = []
    counts: dict[int, int] = {}
    colors: dict[int, str] = {}
    for node in nodes:
      cid = -1 if node.get("community") is None or node.get("community") == "" else int(node.get("community"))
      counts[cid] = counts.get(cid, 0) + 1
      color = node.get("color") or {}
      colors[cid] = str((color.get("background") if isinstance(color, dict) else None) or colors.get(cid) or "#4E79A7")
    for cid, count in sorted(counts.items()):
      legend.append({
        "cid": cid,
        "color": colors.get(cid, "#4E79A7"),
        "label": str(labels.get(cid, {}).get("label") or f"Community {cid}"),
        "count": count,
      })
    html = re.sub(r"const LEGEND = \[.*?\];", f"const LEGEND = {json.dumps(legend, ensure_ascii=False)};", html, count=1, flags=re.S)
    html = re.sub(r"<div id=\"stats\">.*?</div>", f"<div id=\"stats\">{len(nodes)} nodes &middot; {len(edges)} edges &middot; {len(legend)} communities</div>", html, count=1, flags=re.S)
    return HTMLResponse(html)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Surgical Lab web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    _main()
