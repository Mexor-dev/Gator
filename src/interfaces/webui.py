#!/home/user/Gator/venv/bin/python3
"""Phase 6 Surgical Lab UI (Hermes-inspired vanilla FastAPI + JS)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from event_bus import EventBusClient
from pulse_check import run_pulse

GATOR_ROOT = Path.home() / "Gator"
GRAPH_HTML = GATOR_ROOT / "research" / "graphify-out" / "graph.html"
DEBUG_FILE = GATOR_ROOT / "logs" / "debug.json"

app = FastAPI(title="Gator Surgical Lab", version="1.0")


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/vitals")
def api_vitals() -> JSONResponse:
    return JSONResponse(run_pulse())

@app.post("/api/interrupt")
def api_interrupt() -> dict[str, Any]:
    try:
        return EventBusClient().interrupt()
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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    graph_url = "/graph" if GRAPH_HTML.exists() else ""
    graph_frame = (
        f'<iframe src="{graph_url}"></iframe>'
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
      <h2>Live TPS, PID health, canary graft state, VRAM</h2>
      <div id=\"pills\" class=\"row\"></div>
      <div class=\"row\" style=\"margin:8px 0\"><button id=\"interruptBtn\">Interrupt</button></div>
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
  </div>
  <script>
    async function refreshVitals() {{
      const r = await fetch('/api/vitals');
      const d = await r.json();
      document.getElementById('vitals').textContent = JSON.stringify(d, null, 2);
      const pills = [];
      pills.push(`Status: ${{d.status}}`);
      pills.push(`TPS~ ${{d.performance.tps_est}}`);
      pills.push(`VRAM: ${{d.vram}}`);
      pills.push(`Bias Applied: ${{d.canary.biases_applied_total}}`);
      document.getElementById('pills').innerHTML = pills.map(p => `<span class=\"pill\">${{p}}</span>`).join('');
    }}
    async function refreshDebug() {{
      const r = await fetch('/api/debug_tail');
      const d = await r.json();
      document.getElementById('debug').textContent = JSON.stringify(d, null, 2);
    }}
    refreshVitals();
    refreshDebug();
    document.getElementById('interruptBtn').addEventListener('click', async () => {{
      const r = await fetch('/api/interrupt', {{ method: 'POST' }});
      const d = await r.json();
      alert('Interrupt sent: ' + JSON.stringify(d));
      refreshVitals();
    }});
    setInterval(refreshVitals, 7000);
    setInterval(refreshDebug, 9000);
  </script>
</body>
</html>"""


@app.get("/graph", response_class=HTMLResponse)
def graph_page() -> HTMLResponse:
    if not GRAPH_HTML.exists():
        return HTMLResponse("<h3>Graph map missing</h3>", status_code=404)
    return HTMLResponse(GRAPH_HTML.read_text(encoding="utf-8", errors="replace"))


def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Surgical Lab web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    _main()
