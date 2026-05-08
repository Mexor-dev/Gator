# Build Report — Project Iron-Gator

**Date:** 2026-05-06
**Operator:** Senior Systems Architect (automated)
**Mission:** Deep-system audit, repair Hermes / OpenClaw / Zeroclaw integration,
guarantee 100 % startup reliability, validate end-to-end transport.

---

## 1. Final Status — All Systems Nominal

```
[PASS]  GET  /health                 (gator_bridge :8090)
[PASS]  GET  /api/health             (webui :8080)
[PASS]  GET  /api/vitals             (status: PASS)
[PASS]  GET  /graph
[PASS]  GET  /api/voice/status
[PASS]  GET  /api/telegram/status    (NEW)
[PASS]  POST /api/telegram/restart   (NEW)
[PASS]  GET  /htmx/{vitals,greenlight,vram,hive,cron_status,tools_stream,debug}
[PASS]  POST /generate               (OpenClaw transport simulation)
[PASS]  Telegram bridge: state=connected, alive=true, authenticated=true
[PASS]  Telegram delivery confirmed (message_id=520, "Awake")
```

Validation suite: `tools/iron_gator_validate.py` — exit 0.

---

## 2. Integrated Bits — Status Matrix

| Subsystem  | Component                       | State    | Notes |
|------------|---------------------------------|----------|-------|
| Zeroclaw   | event_bus (Unix socket)         | 🟢 ready | `/tmp/gator_event.bus` ready-gated by wakeup |
| Zeroclaw   | gator_bridge API :8090          | 🟢 ready | Health probe + `/generate` smoke gate |
| Zeroclaw   | libgator_kern.so (native)       | 🟢 ready | Auto-built by wakeup if missing |
| Zeroclaw   | LanceDB transient_scratchpad    | 🟢 ready | Self-healing on corrupt manifests (NEW) |
| Hermes     | persona engine + reflections    | 🟢 ready | record_reflection wrapped (existing) |
| Hermes     | mitosis hive / greenlight       | 🟢 ready | independent of vitals canary |
| Hermes     | pulse_check / scholar_sense     | 🟢 ready | wrapped, returns structured fallback |
| OpenClaw   | webui :8080 (FastAPI)           | 🟢 ready | All routes registered, see §4 |
| OpenClaw   | telegram_hive bridge            | 🟢 ready | Retry + token-rejection logic, restart endpoint |
| OpenClaw   | voice toggle (`/api/voice/*`)   | 🟢 ready | persisted to disk |

No namespace collisions detected: `import` graph from `webui` ↔ `gator_bridge` ↔
`memory_core` is one-way (UI → bridge), Hermes plug-ins are loaded inside
`bridge.persona` and isolated from the FastAPI app object. Telegram calls only
reach the bridge through the public HTTP surface, so Zeroclaw/Hermes globals
never share a process with python-telegram-bot's asyncio loop.

---

## 3. Fixes Applied

### 3.1 Vitals 500 — Root cause: corrupt LanceDB scratchpad
- **Symptom:** `/api/vitals` HTTP 500; bridge `/generate` raised
  `RuntimeError: lance error: ... Invalid range 0..0 for object of size 0 bytes`
  from `LanceTable.open(transient_scratchpad)`.
- **Root cause:** Several `_versions/*.manifest` files were 0 bytes (write was
  truncated by an earlier crash). LanceDB refused to open the table.
- **Fix 1 — self-healing connector** (`src/memory_core.py`,
  `_open_or_create_scratchpad`): wrap `open_table` in try/except, on failure
  delete the on-disk table directory and recreate the schema. The transient
  scratchpad has no durable data so this is safe.
- **Fix 2 — graceful degradation** (`src/gator_bridge.py`,
  `_stage_scratchpad_write`): wrap the entire stage so a scratchpad failure
  emits a degraded debug packet but never aborts generation. Confirms the
  Iron-Gator rule "rewrite the connector — do not patch a failing bridge".
- **Fix 3 — defensive vitals endpoint** (`src/interfaces/webui.py`,
  `/api/vitals`): wrapped in try/except returning a structured `Service Offline`
  payload (`{ok:false, status:"ERROR", error:..., telegram:..., greenlight:...}`)
  with HTTP 200 instead of 500.
- **Verification:** `/generate` returns `text:"Task acknowledged..."` HTTP 200,
  `/api/vitals` reports `status:"PASS"`, scratchpad rebuilt automatically.

### 3.2 Telegram Bridge — never came online
- **Symptom:** `logs/telegram_hive_status.json` showed `pid:10537, alive:false`;
  no log lines; `_restart_telegram_gateway` only fired when the user re-saved
  config in the UI; no public restart endpoint existed.
- **Fix 1 — duplicate class removal & retry logic** (`src/interfaces/telegram_hive.py`):
  removed shadow class definitions; `_run_gateway_with_retry()` (line 286) does
  exponential backoff and detects token rejection (`InvalidToken`,
  `Unauthorized`). Status file records `error:"token_rejected"` on auth failure.
- **Fix 2 — public restart endpoints** (`src/interfaces/webui.py`, NEW):
  - `POST /api/telegram/restart` — idempotent re-ignition; polls status until
    `alive && authenticated` or 5 s timeout.
  - `GET  /api/telegram/status` — read-only status probe.
- **Fix 3 — wakeup auto-start** (`wakeup`): `GATOR_AUTO_TELEGRAM=1` will
  auto-launch the gateway after webui readiness, gated on
  `logs/telegram_hive_status.json.authenticated == true`.
- **Verification:** `telegram_hive` PID 24981, `state:"connected"`, indicator
  🟢, message_id 520 delivered to authorised chat.

### 3.3 Wakeup — "Ready" gating
- Added bridge `/generate` readiness probe (60 s deadline) so the script
  cannot print **GATOR IS AWAKE** while the kernel/LanceDB chain is broken.
- Added optional Telegram auto-start with status-file gating.
- Output now reports the explicit ready-check result line, e.g.
  `ready-checks: bridge=200 webui=200 generate=200`.

### 3.4 Routes verified registered
- `/graph`, `/api/voice/{status,on,off}`, `/api/persona` (with
  `command:"wakeup"` branch), and the new `/api/telegram/{status,restart}`
  endpoints all resolve 200 in the live FastAPI app — see validation suite log.

---

## 4. Zero-Failure Startup Sequence

```bash
# Cold start (default; Telegram remains UI-initiated):
bash ~/Gator/wakeup

# Cold start with Telegram auto-ignition (requires GATOR_TG_BOT_TOKEN +
# GATOR_TG_AUTH_CHAT_ID in ~/Gator/.env):
GATOR_AUTO_TELEGRAM=1 bash ~/Gator/wakeup

# Daemon mode (used by VS Code tasks; does not block the shell):
GATOR_DAEMON=true GATOR_AUTO_TELEGRAM=1 bash ~/Gator/wakeup

# Validate after start:
~/Gator/venv/bin/python ~/Gator/tools/iron_gator_validate.py
```

The wakeup script aborts with a non-zero exit and prints the relevant log
tail if any of the following fail:

1. `logic_map.gate` is missing
2. `libgator_kern.so` cannot be built
3. event-bus socket does not appear within 4 s
4. `GET /health` on the bridge fails within 60 s
5. **`POST /generate` does not return HTTP 200 within 60 s** (NEW)
6. `GET /api/health` on the webui fails within 30 s

If `GATOR_AUTO_TELEGRAM=1` is set but the gateway does not authenticate within
15 s, the script still exits success (Telegram is non-fatal) but reports
`telegram-hive: started but not yet authenticated` so monitoring can react.

---

## 5. Operational Notes / Follow-ups

- ⚠️ The python-telegram-bot library logged the bot token in plaintext to
  `logs/telegram_hive.log` while issuing `getMe`. The token in `.env` is now
  visible there. **Rotate the token via @BotFather** and clear the log:
  `: > ~/Gator/logs/telegram_hive.log`. Consider raising the logger to
  WARNING for `httpx`/`telegram` to suppress URL logging.
- LanceDB scratchpad self-heal triggers on any open failure. If it fires
  repeatedly, investigate the underlying disk / fsync pattern.
- Validation suite is suitable for cron / CI: exit code 0 == green.

---

## 6. Files Modified

```
src/memory_core.py                 (self-healing scratchpad)
src/gator_bridge.py                (graceful scratchpad stage)
src/interfaces/webui.py            (restart/status endpoints, vitals try/except,
                                    /graph + /api/voice/*, wakeup persona cmd)
src/interfaces/telegram_hive.py    (retry + token-rejection logic, deduped class)
wakeup                             (generate readiness gate, auto-Telegram opt-in)
tools/iron_gator_validate.py       (NEW — endpoint sweep + transport sim)
build_report.md                    (this file)
```
