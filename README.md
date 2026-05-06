<img width="759" height="768" alt="1778013647203" src="https://github.com/user-attachments/assets/22d0425d-7deb-43f0-b3a0-3efec37caeb6" />


**A self-contained, locally-hosted AI substrate.** Gator runs a 35B-class logic
donor through a 1.5B chassis mouthpiece via a native C++ kernel, with a living
persona engine, dual-write hybrid memory, and a HTMX-driven control surface —
all on a single 6 GB consumer GPU.

This repository is the **Sovereign Build v1.0** golden version: zero external
service dependencies, zero cloud calls, zero `llama-server` daemon. The native
kernel `libgator_kern.so` links only to libc / libstdc++ / libm.

---

## 🚀 One-Line Install

```bash
chmod +x bootstrap.sh && ./bootstrap.sh
```

That's it. The bootstrap does everything: OS prereqs, Python venv, model
procurement (with SHA-256 verification), acceleration detection (CUDA / ROCm /
Metal / AVX2), kernel compilation, scaffolding scrub, and ignition validation.

Optional dry-run: `./bootstrap.sh --dry-run`

---

## Engineering Whitepaper

### 1. The 35B Graft Architecture

Gator runs a **35B-parameter logic donor** at production speed on a 6 GB GPU
(target: RTX 3050) via a tiered offload pipeline:

```
┌─────────────────────────────────────────────────────────────┐
│   USER PROMPT                                               │
│        ↓                                                    │
│   ┌─────────────────┐    cold reasoning    ┌──────────────┐ │
│   │  Logic Donor    │ ──────────────────→  │  Scratchpad  │ │
│   │  (35B Q4_K_M)   │      streamed        │  (transient  │ │
│   │  GGUF, partial  │      to Lance        │   Lance ns)  │ │
│   │  GPU offload    │                      └──────┬───────┘ │
│   └─────────────────┘                             │         │
│                                                   ↓         │
│                              ┌────────────────────────────┐ │
│                              │  Chassis (1.5B Q4_K_M)     │ │
│                              │  GPU-resident, hot path    │ │
│                              │  Polishes + speaks         │ │
│                              └─────────────┬──────────────┘ │
│                                            ↓                │
│   USER REPLY  ←─────────────────────────────                │
└─────────────────────────────────────────────────────────────┘
```

- The **donor** does the heavy reasoning offline (cold), writes a structured
  scratchpad into a transient LanceDB namespace, then unloads.
- The **chassis** stays hot in VRAM and produces the final user-facing reply,
  conditioned on the scratchpad.
- Net VRAM at steady state: **2228 MiB** target on RTX 3050 6 GB.

Both models are spec'd in `models/manifest.json` with pinned SHA-256 hashes;
the bootstrap will refuse to proceed on hash mismatch.

### 2. The Native Gator Kernel

`src/inference/gator_kern.cpp` is a hybrid bridge that fuses three execution
paths into one shared object:

- **ZeroClaw** — zero-copy tensor staging and ring-buffer routing.
- **Hermes** — message bus for cross-tier (donor ↔ scratchpad ↔ chassis).
- **OpenClaw** — public ABI for Python bindings (`src/inference/gator_kern.py`).

Build is multi-backend via CMake (`CMakeLists.txt`):

| Backend  | Detection                        | Define                  |
|----------|----------------------------------|-------------------------|
| CUDA     | `check_language(CUDA)`           | `GATOR_BACKEND_CUDA=1`  |
| ROCm     | `find_package(HIP QUIET)`        | `GATOR_BACKEND_ROCM=1`  |
| oneAPI   | `find_package(IntelDPCPP QUIET)` | `GATOR_BACKEND_ONEAPI=1`|
| CPU/AVX2 | fallback                         | (none)                  |

Compiled artifact (`src/inference/libgator_kern.so`, ~31 KB) links **only** to
libc / libstdc++ / libm / libgcc. There is no llama-server daemon, no socket,
no port to forward.

### 3. The Soul System (PersonaEngine)

`src/persona_engine.py` (243 lines) implements a **6-axis living trait
substrate**. Each trait is a continuous float in `[0.0, 1.0]` that drifts based
on reflection feedback:

```json
{
  "curiosity":  0.9,   "directness": 0.1,
  "caution":    0.8,   "creativity": 0.9,
  "empathy":    0.2,   "precision":  0.95
}
```

- `current_traits()` — reads the live state.
- `set_traits()` — explicit override (config + Lance audit).
- `build_steering_fragment()` — injects trait-aware system prompt into bridge.
- `record_reflection()` — writes outcome → drift → Lance reflection store.

Reflections persist in `db/persona_reflections.lance` and feed back into the
trait drift loop on the agentic-cron tick.

### 4. The 6× Worker Density Hive

`src/agentic_cron.py` (328 lines) is a hard-killable background runner that
spawns up to **6 concurrent workers** per tick across:

- **scholar_sense** — PDF/document ingest + chunk + embed.
- **maintenance** — VRAM gates, drift detection, immune snapshots.
- **pulse_check** — heartbeat + state-file rotation.
- **mitosis** — node spawning + decommission (`src/core/mitosis.py`).
- **scout** — JIT tool synthesis (`src/tools/scout.py`).
- **decommission_node** — graceful tier teardown.

State and PIDs in `bin/`; status fragments served live to the UI via HTMX
polling. Hard kill switch: `bin/maintenance_state.json` → `{"halt": true}`.

### 5. Hybrid Memory Substrate

`src/hybrid_memory.py` (249 lines) implements **dual-write SQLite + LanceDB**:

- **SQLite** (`research/scholar_sense/hybrid_registry.db`) — relational chunk
  metadata, source provenance, FK integrity.
- **LanceDB** (`db/*.lance/`) — vector embeddings for semantic retrieval.

Every chunk lands in both stores in one transaction. `audit_counts()` on each
agentic-cron tick reconciles drift and reports any orphans.

Active LanceDB namespaces:

| Namespace                        | Purpose                                |
|----------------------------------|----------------------------------------|
| `gator_memory.lance`             | Long-term episodic memory              |
| `community_memory.lance`         | Shared/multi-user pool                 |
| `scholar_memory.lance`           | Document-derived knowledge             |
| `skill_nodes.lance`              | JIT tool definitions                   |
| `transient_scratchpad.lance`     | Donor → chassis hand-off               |
| `persona_reflections.lance`      | Trait drift audit log                  |

### 6. Native Toolchain (Prime-Locked)

`src/core/native_tools.py` (116 lines) — file scalpel + web sensor with a
**prime-only authorization gate**. Operations:

- `file_read(path)` / `file_write(path, content)` / `file_edit(path, old, new)`
- `execute(cmd)` — subprocess with cwd lock to repo root

Locked to the prime user (`uid=$(id -u)` at boot). Any subprocess that escapes
the cwd lock is killed and audited.

### 7. The HTMX Control Surface

`src/interfaces/webui.py` (1132 lines) — FastAPI dashboard at
`http://127.0.0.1:8080`. Zero JavaScript polling: every panel is an HTMX
fragment with server-driven refresh.

| Endpoint               | Purpose                | Refresh |
|------------------------|------------------------|---------|
| `GET /htmx/vitals`     | CPU/RAM/load           | 7s      |
| `GET /htmx/vram`       | GPU memory             | 5s      |
| `GET /htmx/cron_status`| Agentic-cron state     | 3s      |
| `GET /htmx/tools_stream`| Live tool invocations | 3s      |
| `GET /htmx/hive`       | Worker densities       | 4s      |
| `GET /htmx/debug`      | Last-error stream      | 9s      |

HTMX is vendored locally at `src/interfaces/static/vendor/htmx.min.js` — no
CDN, no external dependency at runtime.

---

## Repository Layout

```
Gator/
├── bootstrap.sh              # one-line install — does everything
├── CMakeLists.txt            # multi-backend kernel build
├── README.md                 # this file
├── requirements.txt          # 12 production pkgs
├── .gitignore                # protects 18GB models + .env + runtime state
├── models/
│   └── manifest.json         # graft procurement spec (URLs + SHA-256)
├── config/
│   └── (persona_traits.json gitignored — runtime state)
├── src/
│   ├── gator_bridge.py       # 35B donor → scratchpad → 1.5B chassis
│   ├── persona_engine.py     # 6-axis living trait soul
│   ├── agentic_cron.py       # 6× worker hive
│   ├── hybrid_memory.py      # SQLite + LanceDB dual-write
│   ├── memory_core.py        # episodic memory primitives
│   ├── scholar_sense.py      # PDF/document ingest
│   ├── maintenance.py        # VRAM gates + immune snapshots
│   ├── pulse_check.py        # heartbeat
│   ├── event_bus.py          # in-process pub/sub
│   ├── skills.py             # capability registry
│   ├── decommission_node.py  # graceful tier teardown
│   ├── extract_logic.py      # graft logic extraction
│   ├── core/
│   │   ├── native_tools.py   # prime-locked file scalpel
│   │   ├── mitosis.py        # node spawning
│   │   ├── gator_map.py      # snapshot/blueprint exporter
│   │   └── validation.py     # gauntlet runner
│   ├── inference/
│   │   ├── gator_kern.cpp    # native kernel (ZeroClaw/Hermes/OpenClaw)
│   │   ├── gator_kern.py     # Python bindings
│   │   └── CMakeLists.txt
│   ├── interfaces/
│   │   ├── webui.py          # HTMX dashboard (FastAPI)
│   │   ├── static/vendor/    # locally-vendored HTMX
│   │   ├── telegram_gateway.py
│   │   ├── telegram_hive.py
│   │   ├── tg_bot.py
│   │   └── voice_layer.py    # piper TTS
│   ├── discovery/
│   │   └── cluster_namer.py
│   └── tools/
│       └── scout.py          # JIT tool synthesis
└── (db/, logs/, venv/, build/, *.gguf — all gitignored runtime artifacts)
```

---

## Bootstrap Recovery

If a model file is deleted or corrupted:

1. Remove the broken file from `models/` (e.g. `rm models/donor.gguf`).
2. Re-run: `./bootstrap.sh`

Bootstrap will resume the download from `models/manifest.json`, verify SHA-256,
and re-run ignition checks.

If the kernel is broken: `rm src/inference/libgator_kern.so && ./bootstrap.sh`
will rebuild from `src/inference/gator_kern.cpp`.

---

## Operational Targets

| Metric                          | Target                |
|---------------------------------|-----------------------|
| Steady-state VRAM (RTX 3050 6GB)| 2228 MiB              |
| Cold ignition → first reply     | < 12 s                |
| Donor → scratchpad latency      | streamed, async       |
| Chassis chat-turn latency       | < 1.5 s for 256 tok   |
| Agentic-cron tick interval      | 30 s                  |
| External services required      | **0**                 |

---

## License & Provenance

This is a sovereign personal build. Model weights are downloaded from public
HuggingFace mirrors at install time and are governed by their respective
upstream licenses (Qwen2.5: Apache 2.0 with Tongyi addendum). The Gator
substrate code in this repository is the user's own work.

**No telemetry. No external services. No cloud. By design.**
