#!/usr/bin/env python3
"""
Gator native model registry manager.

Pulls model blobs from two registries with no third-party SDK dependencies:

  * Ollama registry  (e.g. ``llama3``, ``llama3:8b``)
  * HuggingFace Hub  (e.g. ``hf:Qwen/Qwen2.5-7B-Instruct-GGUF`` or
    ``hf:repo/model:filename.gguf`` to pin a single file)

CLI:

    gator pull llama3
    gator pull llama3:8b
    gator pull hf:Qwen/Qwen2.5-7B-Instruct-GGUF
    gator pull hf:Qwen/Qwen2.5-7B-Instruct-GGUF:qwen2.5-7b-instruct-q4_k_m.gguf

Designed to be invoked from ``main.py`` (``python main.py pull <ref>``).

This module is a from-scratch reimplementation of the relevant blob-download
semantics from Ollama and huggingface_hub; it embeds no upstream source code
and requires only the standard library + ``requests`` (already a dep).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover - requests is in requirements.txt
    sys.stderr.write("registry_manager requires `requests`. pip install requests\n")
    raise

GATOR_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = GATOR_ROOT / "models"
BLOBS_DIR = MODELS_DIR / "blobs"
MANIFESTS_DIR = MODELS_DIR / "manifests"
REGISTRY_INDEX = MODELS_DIR / "registry.json"

OLLAMA_REGISTRY = "https://registry.ollama.ai"
HF_API = "https://huggingface.co"

CHUNK_SIZE = 1 << 20  # 1 MiB

USER_AGENT = "gator-registry/1.0"


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    BLOBS_DIR.mkdir(parents=True, exist_ok=True)
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _print_progress(prefix: str, downloaded: int, total: int | None) -> None:
    if total and total > 0:
        pct = 100.0 * downloaded / total
        sys.stderr.write(
            f"\r{prefix}: {_human(downloaded)} / {_human(total)} ({pct:5.1f}%)"
        )
    else:
        sys.stderr.write(f"\r{prefix}: {_human(downloaded)}")
    sys.stderr.flush()


def _stream_to_file(
    resp: requests.Response,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    label: str = "blob",
) -> Path:
    """Stream ``resp`` to ``dest``; verify sha256 if provided. Atomic write."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    total_hdr = resp.headers.get("Content-Length")
    total = int(total_hdr) if total_hdr and total_hdr.isdigit() else None
    h = hashlib.sha256() if expected_sha256 else None
    downloaded = 0
    last_print = 0.0
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            if h is not None:
                h.update(chunk)
            now = time.time()
            if now - last_print > 0.25:
                _print_progress(label, downloaded, total)
                last_print = now
    _print_progress(label, downloaded, total)
    sys.stderr.write("\n")
    if h is not None and expected_sha256:
        got = h.hexdigest()
        if got.lower() != expected_sha256.lower().removeprefix("sha256:"):
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"sha256 mismatch for {label}: expected {expected_sha256}, got {got}"
            )
    os.replace(tmp, dest)
    return dest


def _registry_index_load() -> dict[str, Any]:
    if REGISTRY_INDEX.exists():
        try:
            return json.loads(REGISTRY_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "entries": {}}


def _registry_index_save(idx: dict[str, Any]) -> None:
    _ensure_dirs()
    REGISTRY_INDEX.write_text(json.dumps(idx, indent=2), encoding="utf-8")


def _record_entry(ref: str, payload: dict[str, Any]) -> None:
    idx = _registry_index_load()
    idx.setdefault("entries", {})[ref] = {
        **payload,
        "pulled_at": time.time(),
    }
    _registry_index_save(idx)


# ---------------------------------------------------------------------------
# Ollama puller
# ---------------------------------------------------------------------------
# Ollama registry uses an OCI-flavored manifest API:
#   GET /v2/library/<model>/manifests/<tag>
#   GET /v2/library/<model>/blobs/<digest>
# A tag manifest lists "layers", each with a digest and mediaType. The model
# weights are typically a single layer with mediaType
# "application/vnd.ollama.image.model".

OLLAMA_MODEL_MEDIATYPES = {
    "application/vnd.ollama.image.model",
    "application/vnd.ollama.image.weights",
}


def _parse_ollama_ref(ref: str) -> tuple[str, str, str]:
    """Return (namespace, model, tag). ``llama3:8b`` → ('library', 'llama3', '8b')."""
    if "/" in ref:
        ns, rest = ref.split("/", 1)
    else:
        ns, rest = "library", ref
    if ":" in rest:
        model, tag = rest.split(":", 1)
    else:
        model, tag = rest, "latest"
    return ns, model, tag


def pull_ollama(ref: str) -> dict[str, Any]:
    """Pull an Ollama model. Returns metadata dict."""
    _ensure_dirs()
    ns, model, tag = _parse_ollama_ref(ref)
    canonical = f"{ns}/{model}:{tag}"
    sys.stderr.write(f"[ollama] resolving {canonical}\n")

    manifest_url = f"{OLLAMA_REGISTRY}/v2/{ns}/{model}/manifests/{tag}"
    resp = requests.get(
        manifest_url,
        headers={
            "Accept": "application/vnd.docker.distribution.manifest.v2+json",
            "User-Agent": USER_AGENT,
        },
        timeout=30,
    )
    if resp.status_code == 404:
        raise RuntimeError(f"ollama manifest not found: {canonical}")
    resp.raise_for_status()
    manifest = resp.json()

    # Persist manifest.
    manifest_path = MANIFESTS_DIR / f"ollama__{ns}__{model}__{tag}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    layers = manifest.get("layers") or []
    weight_layers = [l for l in layers if l.get("mediaType") in OLLAMA_MODEL_MEDIATYPES]
    if not weight_layers:
        # Fallback: take the largest layer.
        weight_layers = sorted(layers, key=lambda l: int(l.get("size", 0)), reverse=True)[:1]
    if not weight_layers:
        raise RuntimeError(f"no model weight layer found in manifest for {canonical}")

    pulled: list[dict[str, Any]] = []
    for layer in weight_layers:
        digest = layer["digest"]  # e.g. "sha256:abcd..."
        size = int(layer.get("size", 0))
        blob_url = f"{OLLAMA_REGISTRY}/v2/{ns}/{model}/blobs/{digest}"
        local = BLOBS_DIR / digest.replace(":", "_")
        if local.exists() and local.stat().st_size == size:
            sys.stderr.write(f"[ollama] cached {digest} ({_human(size)})\n")
        else:
            sys.stderr.write(f"[ollama] fetching {digest} ({_human(size)})\n")
            with requests.get(
                blob_url,
                headers={"User-Agent": USER_AGENT},
                stream=True,
                timeout=60,
            ) as r:
                r.raise_for_status()
                _stream_to_file(r, local, expected_sha256=digest, label=f"blob {digest[:19]}…")
        # Friendly symlink: models/<model>-<tag>.gguf
        friendly = MODELS_DIR / f"{model}-{tag}.gguf"
        try:
            if friendly.is_symlink() or friendly.exists():
                friendly.unlink()
            friendly.symlink_to(local)
        except OSError:
            # Symlinks may fail on filesystems that disallow them; fall back to
            # leaving only the canonical blob path.
            pass
        pulled.append({"digest": digest, "size": size, "path": str(local), "alias": str(friendly)})

    meta = {
        "source": "ollama",
        "ref": canonical,
        "manifest": str(manifest_path),
        "blobs": pulled,
    }
    _record_entry(f"ollama:{canonical}", meta)
    sys.stderr.write(f"[ollama] OK {canonical}\n")
    return meta


# ---------------------------------------------------------------------------
# HuggingFace puller
# ---------------------------------------------------------------------------
# Hub API (no auth required for public repos):
#   GET /api/models/<repo>          → tree info incl. siblings[]
#   GET /<repo>/resolve/main/<file> → blob (CDN-redirected)

def _parse_hf_ref(ref: str) -> tuple[str, str | None, str]:
    """Return (repo, filename_or_None, revision='main'). Forms:

    * ``hf:org/model``
    * ``hf:org/model:filename.gguf``
    * ``hf:org/model@revision``
    * ``hf:org/model@revision:filename.gguf``
    """
    body = ref[3:] if ref.startswith("hf:") else ref
    revision = "main"
    if "@" in body:
        body, revision = body.split("@", 1)
        if ":" in revision:
            revision, fname_tail = revision.split(":", 1)
            body = f"{body}:{fname_tail}"
    if ":" in body:
        repo, fname = body.split(":", 1)
    else:
        repo, fname = body, None
    return repo, fname, revision


def _hf_pick_default_file(siblings: list[dict[str, Any]]) -> str:
    """Pick the most useful single-file weight when caller did not specify one."""
    names = [s.get("rfilename", "") for s in siblings]
    # Prefer GGUF, then safetensors, then bin. Within GGUF, prefer Q4_K_M.
    ggufs = [n for n in names if n.lower().endswith(".gguf")]
    if ggufs:
        prefer = [n for n in ggufs if "q4_k_m" in n.lower()]
        return (prefer or ggufs)[0]
    safe = [n for n in names if n.lower().endswith(".safetensors")]
    if safe:
        return safe[0]
    bins = [n for n in names if n.lower().endswith(".bin")]
    if bins:
        return bins[0]
    raise RuntimeError("could not auto-pick a weight file; pass hf:repo:filename")


def pull_huggingface(ref: str) -> dict[str, Any]:
    """Pull from HuggingFace Hub. Returns metadata dict."""
    _ensure_dirs()
    repo, filename, revision = _parse_hf_ref(ref)
    sys.stderr.write(f"[hf] resolving {repo}@{revision}\n")

    info_url = f"{HF_API}/api/models/{repo}/revision/{revision}"
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(info_url, headers=headers, timeout=30)
    if resp.status_code == 404:
        raise RuntimeError(f"hf repo not found or gated: {repo}@{revision}")
    resp.raise_for_status()
    info = resp.json()
    siblings = info.get("siblings") or []
    sha = info.get("sha", revision)

    manifest_path = MANIFESTS_DIR / f"hf__{repo.replace('/', '__')}__{revision}.json"
    manifest_path.write_text(json.dumps(info, indent=2), encoding="utf-8")

    if not filename:
        filename = _hf_pick_default_file(siblings)

    sib = next((s for s in siblings if s.get("rfilename") == filename), None)
    if sib is None:
        raise RuntimeError(
            f"file '{filename}' not present in {repo}@{revision}; available: "
            f"{[s.get('rfilename') for s in siblings][:8]}"
        )
    expected_size = int(sib.get("size") or 0)
    expected_oid = sib.get("lfs", {}).get("sha256") or sib.get("blobId")

    blob_url = f"{HF_API}/{repo}/resolve/{quote(revision, safe='')}/{quote(filename, safe='/')}"
    safe_repo = repo.replace("/", "__")
    local = BLOBS_DIR / f"hf__{safe_repo}__{revision}__{filename.replace('/', '__')}"

    if local.exists() and (expected_size == 0 or local.stat().st_size == expected_size):
        sys.stderr.write(f"[hf] cached {filename} ({_human(local.stat().st_size)})\n")
    else:
        sys.stderr.write(f"[hf] fetching {filename}\n")
        with requests.get(blob_url, headers=headers, stream=True, timeout=60, allow_redirects=True) as r:
            r.raise_for_status()
            # HF blob ids are git-style SHA-1, not sha256 of file content, so we
            # only verify sha256 when the LFS pointer exposes it.
            verify_sha = expected_oid if expected_oid and len(expected_oid) == 64 else None
            _stream_to_file(r, local, expected_sha256=verify_sha, label=filename)

    # Friendly symlink for *.gguf pulls so gator-server can find them by name.
    if filename.lower().endswith(".gguf"):
        friendly = MODELS_DIR / Path(filename).name
        try:
            if friendly.is_symlink() or friendly.exists():
                friendly.unlink()
            friendly.symlink_to(local)
        except OSError:
            pass

    meta = {
        "source": "huggingface",
        "repo": repo,
        "revision": sha,
        "filename": filename,
        "manifest": str(manifest_path),
        "path": str(local),
    }
    _record_entry(f"hf:{repo}:{filename}", meta)
    sys.stderr.write(f"[hf] OK {repo}:{filename}\n")
    return meta


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

def pull(ref: str) -> dict[str, Any]:
    """Resolve a model reference and download it. ``ref`` may be:

    * ``hf:org/model[:filename][@revision]`` → HuggingFace
    * ``ollama:llama3[:tag]``                → explicit Ollama
    * ``llama3[:tag]``                       → defaults to Ollama library
    """
    if not ref or not ref.strip():
        raise ValueError("empty model reference")
    r = ref.strip()
    if r.startswith("hf:") or r.startswith("huggingface:"):
        return pull_huggingface(r.removeprefix("huggingface:") if r.startswith("huggingface:") else r)
    if r.startswith("ollama:"):
        return pull_ollama(r[len("ollama:") :])
    return pull_ollama(r)


def list_local() -> dict[str, Any]:
    return _registry_index_load()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gator pull", description="Gator model registry")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pull = sub.add_parser("pull", help="Download a model")
    p_pull.add_argument("ref", help="Model reference (e.g. llama3, hf:org/model)")
    sub.add_parser("list", help="List local models")
    args = parser.parse_args(argv)

    if args.cmd == "pull":
        meta = pull(args.ref)
        print(json.dumps(meta, indent=2))
        return 0
    if args.cmd == "list":
        print(json.dumps(list_local(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
