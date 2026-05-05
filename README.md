# Gator Sovereign Entity

## Getting Started

Run exactly this:

```bash
chmod +x bootstrap.sh && ./bootstrap.sh
```

### What this does

- Creates/refreshes a Python venv and upgrades `pip`, `setuptools`, and `wheel`.
- Installs the Phase-2 production runtime stack: `llama-cpp-python`, `lancedb`, `fastapi`, `uvicorn`, and vendors HTMX locally for the UI.
- Loads graft definitions from `models/manifest.json` and downloads the donor/chassis models with resume support.
- Verifies SHA-256 integrity of each `.gguf` before continuing.
- Detects acceleration (CUDA, ROCm, Metal, AVX2) and compiles `libgator_kern.so`.
- Runs a forensic scrub that removes setup scaffolding:
  - archives (`.zip`, `.tar`, `.tar.gz`, `.tgz`, `.xz`, `.7z`)
  - temporary C++ objects/static libs (`.o`, `.a`)
  - setup cache/build/log directories
- Protects active assets during purge: `models/*.gguf` and `src/inference/libgator_kern.so`.
- Executes a silent wakeup ignition test and validates HTMX fragments plus VRAM target (`2228 MiB` default).

### Bootstrap Recovery

If the donor file is deleted or corrupted:

1. Remove the broken model file from `models/` (for example `models/donor.gguf`).
2. Re-run bootstrap:

```bash
./bootstrap.sh
```

Bootstrap will re-download from `models/manifest.json`, verify SHA-256, and re-run ignition checks.

### Optional Dry Run

```bash
./bootstrap.sh --dry-run
```
