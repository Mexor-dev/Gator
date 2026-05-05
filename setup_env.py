#!/usr/bin/env python3
"""
Project Gator - Phase 1 Environment Setup
Initializes directory structure, relocates models, installs dependencies.
Run once from ~/Gator/ or anywhere with: python3 ~/Gator/setup_env.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
HOME         = Path.home()
GATOR_ROOT   = HOME / "Gator"
DIRS = [
    GATOR_ROOT,
    GATOR_ROOT / "models",
    GATOR_ROOT / "bin",
    GATOR_ROOT / "db",
    GATOR_ROOT / "src",
    GATOR_ROOT / "logs",
]

# Source GGUF locations (both currently sit in HOME)
CHASSIS_SRC = HOME / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
DONOR_SRC   = HOME / "Qwen2.5-32B-Instruct-IQ3_M.gguf"
CHASSIS_DST = GATOR_ROOT / "models" / "chassis.gguf"
DONOR_DST   = GATOR_ROOT / "models" / "donor.gguf"

# ── Packages ───────────────────────────────────────────────────────────────────
# Native kernel path: no llama-cpp-python dependency.
VENV_DIR = GATOR_ROOT / "venv"

STANDARD_PKGS = [
    "lancedb>=0.6.0",
    "tantivy",
    "numpy>=1.26",
    "fastapi>=0.111",
    "uvicorn[standard]",
    "pyarrow",          # required by lancedb
]


def banner(msg: str) -> None:
    width = len(msg) + 4
    print("\n" + "─" * width)
    print(f"  {msg}")
    print("─" * width)


def step_dirs() -> None:
    banner("STEP 1/3 — Directory tree")
    for d in DIRS:
        d.mkdir(parents=True, exist_ok=True)
        status = "ok" if d.exists() else "FAILED"
        print(f"  [{status}]  {d}")


def step_models() -> None:
    banner("STEP 2/3 — Model files")
    for src, dst, label in [
        (CHASSIS_SRC, CHASSIS_DST, "chassis (1.5B)"),
        (DONOR_SRC,   DONOR_DST,   "donor   (32B) "),
    ]:
        if dst.exists():
            print(f"  [skip]  {label} already at {dst}")
            continue
        if not src.exists():
            print(f"  [WARN]  {label} source not found: {src}")
            print(f"          Place the GGUF manually at: {dst}")
            continue
        print(f"  [move]  {label}\n         {src}\n      -> {dst}")
        # Move is instant on same filesystem; use rename for atomicity
        src.rename(dst)
        print(f"  [done]  {label}")


def _venv_python() -> str:
    """Return path to the venv Python executable."""
    return str(VENV_DIR / "bin" / "python")


def _venv_pip() -> str:
    return str(VENV_DIR / "bin" / "pip")


def _run_pip(*args: str, env_extra: dict | None = None) -> bool:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [_venv_pip(), "install", *args],
        env=env,
        capture_output=False,
    )
    return result.returncode == 0


def step_deps() -> None:
    banner("STEP 3/3 — Python dependencies")

    # ── Create virtualenv if not already present ───────────────────────────────
    if not (VENV_DIR / "bin" / "python").exists():
        print(f"\n  [venv] Creating virtualenv at {VENV_DIR}")
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            capture_output=False,
        )
        if result.returncode != 0:
            print("  [FAIL] venv creation failed.  Install python3-venv: sudo apt install python3-venv")
            return
        print("  [ok]  venv created")
    else:
        print(f"  [skip] venv already exists at {VENV_DIR}")

    # ── enforce purge of legacy llama-cpp-python wheel ───────────────────────
    print("\n  [pip]  purge legacy llama-cpp-python")
    subprocess.run([_venv_pip(), "uninstall", "-y", "llama-cpp-python"], capture_output=False)

    # ── standard packages ──────────────────────────────────────────────────────
    print("\n  [pip]  standard packages")
    if not _run_pip(*STANDARD_PKGS):
        print("  [FAIL]  one or more standard packages failed")
    else:
        print("  [ok]   all standard packages")


def verify() -> None:
    banner("Verification")
    venv_py = _venv_python()
    checks = ["lancedb", "tantivy", "numpy", "fastapi"]
    labels = {
        "lancedb":   "lancedb",
        "tantivy":   "tantivy",
        "numpy":     "numpy",
        "fastapi":   "fastapi",
    }
    all_ok = True
    for mod in checks:
        result = subprocess.run(
            [venv_py, "-c", f"import {mod}; print({mod}.__version__ if hasattr({mod}, '__version__') else 'ok')"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  [ok]   {labels[mod]}  {result.stdout.strip()}")
        else:
            print(f"  [FAIL] {labels[mod]} — {result.stderr.strip()[:80]}")
            all_ok = False
    print()
    if all_ok:
        print(f"  Environment ready.")
        print(f"  Activate venv : source {VENV_DIR}/bin/activate")
        print(f"  Next step     : python3 ~/Gator/src/extract_logic.py")
    else:
        print("  Some packages missing.  Resolve errors above then re-run.")


if __name__ == "__main__":
    print("=" * 60)
    print("  Project Gator — Phase 1 Setup")
    print(f"  Python {sys.version.split()[0]} | Root: {GATOR_ROOT}")
    print("=" * 60)
    step_dirs()
    step_models()
    step_deps()
    verify()
