#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/venv"
REQ="$ROOT/requirements.txt"
MODELS_DIR="$ROOT/models"
MANIFEST="$MODELS_DIR/manifest.json"
CACHE_DIR="$ROOT/.bootstrap_cache"
BUILD_DIR="$ROOT/.bootstrap_build"
HEADERS_DIR="$BUILD_DIR/llama_headers"
KERN_OUT="$ROOT/src/inference/libgator_kern.so"
LOGIC_GATE="$ROOT/bin/logic_map.gate"
HTMX_VENDOR_DIR="$ROOT/src/interfaces/static/vendor"
HTMX_VENDOR_FILE="$HTMX_VENDOR_DIR/htmx.min.js"
VRAM_TARGET_MIB="${GATOR_VRAM_TARGET_MIB:-2228}"
LOGIC_MIN_RECORDS="${GATOR_MIN_LOGIC_RECORDS:-100}"
DRY_RUN="false"
SETUP_LOG="$CACHE_DIR/bootstrap_setup.log"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN="true" ;;
    *) echo "[FATAL] Unknown argument: $arg"; exit 1 ;;
  esac
done

log() {
  echo "[bootstrap] $*"
}

fatal() {
  echo "[FATAL] $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fatal "Required command missing: $1"
}

detect_os() {
  local uname_s
  uname_s="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$uname_s" in
    linux*) echo "linux" ;;
    darwin*) echo "macos" ;;
    *) fatal "Unsupported host OS: $uname_s (supported: linux, macos)" ;;
  esac
}

ensure_system_build_prereqs() {
  local os
  os="$(detect_os)"

  local missing=()
  for cmd in python3 curl cmake c++ make; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing+=("$cmd")
    fi
  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    log "System build prerequisites present"
    return 0
  fi

  log "Missing system tools: ${missing[*]}"
  if [[ "$DRY_RUN" == "true" ]]; then
    log "[dry-run] skipping system package install"
    return 0
  fi

  if [[ "$os" == "linux" ]]; then
    if command -v apt-get >/dev/null 2>&1; then
      if command -v sudo >/dev/null 2>&1; then
        sudo apt-get update
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential cmake pkg-config curl python3-venv
      else
        fatal "sudo not found; install build-essential cmake pkg-config curl python3-venv manually"
      fi
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y gcc-c++ make cmake pkgconf-pkg-config curl python3 python3-virtualenv
    elif command -v pacman >/dev/null 2>&1; then
      sudo pacman -Sy --noconfirm base-devel cmake pkgconf curl python
    else
      fatal "Unsupported Linux package manager; install C++ headers/toolchain manually"
    fi
  else
    if command -v brew >/dev/null 2>&1; then
      brew install cmake pkg-config curl
    else
      fatal "Homebrew not found; install Xcode Command Line Tools + cmake manually"
    fi
  fi
}

detect_acceleration() {
  local os
  os="$(detect_os)"
  if command -v nvidia-smi >/dev/null 2>&1; then
    echo "cuda"
    return
  fi
  if command -v rocminfo >/dev/null 2>&1; then
    echo "rocm"
    return
  fi
  if [[ "$os" == "macos" ]]; then
    echo "metal"
    return
  fi
  if grep -qi "avx2" /proc/cpuinfo 2>/dev/null; then
    echo "avx2"
    return
  fi
  echo "cpu"
}

download_file() {
  local url="$1"
  local out="$2"
  if [[ "$DRY_RUN" == "true" ]]; then
    log "[dry-run] download $url -> $out"
    return 0
  fi
  mkdir -p "$(dirname "$out")"
  curl -fL -C - --retry 5 --retry-delay 2 --retry-connrefused "$url" -o "$out"
}

sha_check() {
  local file="$1"
  local expected="$2"
  [[ -z "$expected" ]] && return 0
  local actual
  actual="$(sha256sum "$file" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || fatal "SHA256 mismatch: $file"
}

ensure_python_env() {
  need_cmd python3
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip wheel setuptools

  # Phase-2 production lock stack.
  "$VENV/bin/pip" install llama-cpp-python lancedb fastapi uvicorn
  # Keep project extras from requirements.txt as additive, without re-specifying core stack.
  if [[ -f "$REQ" ]]; then
    "$VENV/bin/pip" install -r "$REQ"
  fi

  # HTMX runtime asset (frontend dependency) vendored locally.
  mkdir -p "$HTMX_VENDOR_DIR"
  download_file "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js" "$HTMX_VENDOR_FILE"
}

fetch_minimal_llama_headers() {
  mkdir -p "$HEADERS_DIR"
  log "Fetching minimal llama bridge headers"
  download_file "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/include/llama.h" "$HEADERS_DIR/llama.h"
  download_file "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/ggml/include/ggml.h" "$HEADERS_DIR/ggml.h"
}

fetch_models_from_manifest() {
  [[ -f "$MANIFEST" ]] || fatal "Missing model manifest: $MANIFEST"
  mkdir -p "$MODELS_DIR" "$CACHE_DIR"

  while IFS=$'\t' read -r mid url target sha; do
    [[ -z "$mid" ]] && continue
    [[ -n "$url" ]] || fatal "Manifest entry '$mid' is missing url"
    [[ -n "$target" ]] || fatal "Manifest entry '$mid' is missing target_file"

    local_target="$MODELS_DIR/$target"
    local_tmp="$CACHE_DIR/$target.part"

    if [[ -s "$local_target" ]]; then
      log "Model present: $target"
      sha_check "$local_target" "$sha"
      continue
    fi

    log "Downloading $mid -> $target"
    download_file "$url" "$local_tmp"
    [[ "$DRY_RUN" == "true" ]] || mv -f "$local_tmp" "$local_target"
    [[ "$DRY_RUN" == "true" ]] || sha_check "$local_target" "$sha"
  done < <("$VENV/bin/python" - <<'PY'
import json
from pathlib import Path
manifest = Path("models/manifest.json")
data = json.loads(manifest.read_text(encoding="utf-8"))
for m in data.get("models", []):
    print(f"{m.get('id','')}\t{m.get('url','')}\t{m.get('target_file','')}\t{m.get('sha256','')}")
PY
)
}

build_native_kernel() {
  need_cmd cmake
  mkdir -p "$BUILD_DIR"
  local accel
  accel="$(detect_acceleration)"
  log "Detected acceleration: $accel"

  local extra_flags=()
  case "$accel" in
    cuda) extra_flags+=("-DGATOR_BACKEND=cuda") ;;
    rocm) extra_flags+=("-DGATOR_BACKEND=rocm") ;;
    metal) extra_flags+=("-DGATOR_BACKEND=metal") ;;
    avx2) extra_flags+=("-DGATOR_BACKEND=cpu" "-DGATOR_CPU_AVX2=ON") ;;
    *) extra_flags+=("-DGATOR_BACKEND=cpu") ;;
  esac

  if [[ "$DRY_RUN" == "true" ]]; then
    log "[dry-run] cmake -S $ROOT -B $BUILD_DIR -DCMAKE_BUILD_TYPE=Release ${extra_flags[*]}"
    log "[dry-run] cmake --build $BUILD_DIR --target gator_kern --parallel"
    return 0
  fi

  cmake -S "$ROOT" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release "${extra_flags[@]}"
  cmake --build "$BUILD_DIR" --target gator_kern --parallel

  if [[ -f "$BUILD_DIR/src/inference/libgator_kern.so" ]]; then
    cp -f "$BUILD_DIR/src/inference/libgator_kern.so" "$KERN_OUT"
  elif [[ -f "$BUILD_DIR/libgator_kern.so" ]]; then
    cp -f "$BUILD_DIR/libgator_kern.so" "$KERN_OUT"
  else
    fatal "Native kernel build completed but libgator_kern.so not found"
  fi

  [[ -f "$KERN_OUT" ]] || fatal "Kernel copy failed: $KERN_OUT"
}

ensure_logic_map() {
  local count=0

  if [[ ! -f "$LOGIC_GATE" ]]; then
    if [[ "$DRY_RUN" == "true" ]]; then
      log "[dry-run] missing logic_map.gate would trigger run_extraction.sh"
      return 0
    fi
    [[ -x "$ROOT/run_extraction.sh" || -f "$ROOT/run_extraction.sh" ]] || fatal "Missing run_extraction.sh"
    log "logic_map.gate missing; running extraction"
    bash "$ROOT/run_extraction.sh"
    [[ -f "$LOGIC_GATE" ]] || fatal "Extraction did not produce $LOGIC_GATE"
  fi

  count="$("$VENV/bin/python" - <<'PY'
import gzip
import pickle
from pathlib import Path

gate = Path("bin/logic_map.gate")
try:
    data = pickle.loads(gzip.decompress(gate.read_bytes()))
except Exception:
    print(-1)
else:
    print(len(data.get("records", [])) if isinstance(data, dict) else -1)
PY
)"

  if [[ "$count" == "-1" ]]; then
    fatal "logic_map.gate exists but is unreadable/corrupt"
  fi

  if (( count < LOGIC_MIN_RECORDS )); then
    log "WARNING: logic_map.gate has ${count} records (< ${LOGIC_MIN_RECORDS}); extraction is recommended for full donor fidelity"
  else
    log "logic_map.gate quality OK: ${count} records"
  fi
}

snapshot_logic_map() {
  if [[ "$DRY_RUN" == "true" ]]; then
    log "[dry-run] gator_map snapshot skipped"
    return 0
  fi
  "$VENV/bin/python" "$ROOT/src/core/gator_map.py" --snapshot --reason bootstrap
}

scrub_install_waste() {
  if [[ "$DRY_RUN" == "true" ]]; then
    log "[dry-run] scrub archives, object/static libs, and temp caches"
    return 0
  fi

  # Remove model-download archives/caches without touching active gguf files.
  find "$MODELS_DIR" -type f \( -name '*.zip' -o -name '*.tar' -o -name '*.tar.gz' -o -name '*.tgz' -o -name '*.xz' -o -name '*.7z' \) -delete || true
  rm -rf "$CACHE_DIR"

  # Remove C++ build waste (.o/.a) and temporary build tree.
  find "$BUILD_DIR" -type f \( -name '*.o' -o -name '*.a' \) -delete || true
  rm -rf "$BUILD_DIR"

  # Remove temporary logs/caches produced by bootstrap.
  rm -f "$SETUP_LOG" "$ROOT/bootstrap.log" 2>/dev/null || true

  # Keep production artifacts only.
  [[ -f "$MODELS_DIR/donor.gguf" ]] || fatal "Missing donor.gguf after scrub"
  [[ -f "$MODELS_DIR/chassis.gguf" ]] || fatal "Missing chassis.gguf after scrub"
  [[ -f "$LOGIC_GATE" ]] || fatal "Missing logic_map.gate after scrub"
  [[ -f "$KERN_OUT" ]] || fatal "Missing production kernel after scrub"
}

validate_runtime() {
  if [[ "$DRY_RUN" == "true" ]]; then
    log "[dry-run] wakeup + HTMX + VRAM validation skipped"
    return 0
  fi

  log "Running silent wakeup check"
  mkdir -p "$CACHE_DIR"
  GATOR_DAEMON=true "$ROOT/wakeup" >/dev/null 2>"$CACHE_DIR/wakeup.stderr.log"

  for _ in $(seq 1 60); do
    if curl -sSf "http://127.0.0.1:8080/api/health" | "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
import json
import sys
payload = json.load(sys.stdin)
ok = payload.get("ok")
status = str(payload.get("status", "")).lower()
if ok is True or status == "ok":
    sys.exit(0)
sys.exit(1)
PY
    then
      break
    fi
    sleep 1
  done

  curl -sSf "http://127.0.0.1:8080/htmx/vitals" >/dev/null
  curl -sSf "http://127.0.0.1:8080/htmx/vram" >/dev/null
  curl -sSf "http://127.0.0.1:8080/htmx/cron_status" >/dev/null

  if command -v nvidia-smi >/dev/null 2>&1; then
    used="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d '[:space:]')"
    if [[ -n "$used" ]]; then
      if (( used > VRAM_TARGET_MIB )); then
        fatal "VRAM check failed: ${used}MiB > target ${VRAM_TARGET_MIB}MiB"
      fi
      log "VRAM check OK: ${used}MiB <= ${VRAM_TARGET_MIB}MiB"
    fi
  else
    log "nvidia-smi not found; VRAM validation skipped"
  fi

  log "HTMX endpoint validation OK"

  # Purge bootstrap temporary logs/caches after successful ignition.
  rm -rf "$CACHE_DIR"
}

main() {
  cd "$ROOT"
  need_cmd curl
  ensure_system_build_prereqs
  ensure_python_env
  fetch_models_from_manifest
  fetch_minimal_llama_headers
  build_native_kernel
  ensure_logic_map
  snapshot_logic_map
  scrub_install_waste
  validate_runtime

  log "Bootstrap complete"
  log "One-click setup done: env + models + kernel + scrub + wakeup validation"
}

main "$@"
