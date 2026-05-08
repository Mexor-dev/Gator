#!/usr/bin/env python3
"""Project Gator — Deep Logit Extraction Pipeline.

Strategy
--------
1. Spin up the decommissioned 32B donor on its own llama-server port (8181).
2. Stream every calibration prompt through donor /completion with `n_probs=64`.
3. For each generated token, capture the top-64 (token_id, prob) pairs and
   record them keyed by a hash of the preceding context window.
4. Emit `bin/logic_map.gate` (gzip+pickle) in the format the bridge expects:
        {
            "version": "deep-1.0",
            "top_k": 64,
            "vocab_size": V,
            "meta": { ...extraction stats... },
            "records": [ { "h": <ctx hash>, "c": <category>, "t": [...], "p": [...] }, ... ],
        }

Run: GATOR_ROOT/venv/bin/python src/extract_logic.py
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pickle
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

GATOR_ROOT = Path(__file__).resolve().parents[1]
DONOR_PATH = GATOR_ROOT / "models" / "_decommissioned" / "donor.gguf.bak"
SERVER_BIN = GATOR_ROOT / "bin" / "gator-server"
GATE_OUT = GATOR_ROOT / "bin" / "logic_map.gate"
META_OUT = GATOR_ROOT / "bin" / "logic_map.meta.json"

DONOR_PORT = int(os.environ.get("EXTRACT_DONOR_PORT", "8181"))
DONOR_URL = f"http://127.0.0.1:{DONOR_PORT}"
TOP_K = int(os.environ.get("EXTRACT_TOP_K", "64"))
TOKENS_PER_PROMPT = int(os.environ.get("EXTRACT_TOKENS_PER_PROMPT", "22"))
GPU_LAYERS = int(os.environ.get("EXTRACT_GPU_LAYERS", "14"))
CTX = int(os.environ.get("EXTRACT_CTX", "2048"))
HEALTH_TIMEOUT_S = int(os.environ.get("EXTRACT_HEALTH_TIMEOUT", "240"))
CONTEXT_HASH_WINDOW = 256  # last N chars of running context define the hash key

sys.path.insert(0, str(GATOR_ROOT / "src"))
from extraction_corpus import ALL_PROMPTS  # noqa: E402


# ---------------------------------------------------------------------------
# Donor server lifecycle
# ---------------------------------------------------------------------------
def spawn_donor() -> subprocess.Popen[bytes]:
    if not DONOR_PATH.exists():
        raise SystemExit(f"[FATAL] donor missing: {DONOR_PATH}")
    if not SERVER_BIN.exists():
        raise SystemExit(f"[FATAL] gator-server missing: {SERVER_BIN}")

    log_path = GATOR_ROOT / "logs" / "donor_extract.log"
    log_path.parent.mkdir(exist_ok=True)
    log_fp = open(log_path, "wb")

    cmd = [
        str(SERVER_BIN),
        "--host", "127.0.0.1",
        "--port", str(DONOR_PORT),
        "--model", str(DONOR_PATH),
        "--alias", "gator-donor",
        "--ctx-size", str(CTX),
        "--batch-size", "256",
        "--n-gpu-layers", str(GPU_LAYERS),
        "--threads", "8",
    ]
    print(f"[extract] spawning donor: {' '.join(cmd)}")
    print(f"[extract] log: {log_path}")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env={**os.environ, "LD_LIBRARY_PATH": str(GATOR_ROOT / "lib")},
        preexec_fn=os.setsid,
    )
    return proc


def wait_donor_ready() -> None:
    print(f"[extract] waiting up to {HEALTH_TIMEOUT_S}s for donor on {DONOR_URL}/health …")
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    last_err = ""
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{DONOR_URL}/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") in ("ok", "no slot available"):
                print(f"[extract] donor healthy after {int(HEALTH_TIMEOUT_S - (deadline - time.monotonic()))}s")
                return
            last_err = f"HTTP {r.status_code}: {r.text[:120]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:120]
        time.sleep(2)
    raise SystemExit(f"[FATAL] donor never became healthy. Last: {last_err}")


def kill_donor(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    print("[extract] sending SIGTERM to donor pgid")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=15)
        print("[extract] donor exited cleanly")
    except subprocess.TimeoutExpired:
        print("[extract] SIGKILL'ing donor")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def hash_ctx(ctx: str) -> str:
    tail = ctx[-CONTEXT_HASH_WINDOW:].encode("utf-8", "ignore")
    return hashlib.blake2b(tail, digest_size=8).hexdigest()


def extract_one(prompt: str, category: int) -> tuple[list[dict[str, Any]], int, str]:
    """Send one prompt to the donor and harvest n_probs records per token.

    Returns (records, n_tokens, generated_text).
    """
    payload = {
        "prompt": prompt,
        "n_predict": TOKENS_PER_PROMPT,
        "temperature": 0.7,
        "top_k": 0,
        "top_p": 1.0,
        "n_probs": TOP_K,
        "stream": False,
        "cache_prompt": True,
    }
    # llama-server's native /completion endpoint preserves `completion_probabilities`.
    r = requests.post(f"{DONOR_URL}/completion", json=payload, timeout=600)
    r.raise_for_status()
    body = r.json()
    completion_probs = body.get("completion_probabilities") or []
    generated = body.get("content", "")

    records: list[dict[str, Any]] = []
    running_ctx = prompt
    for step in completion_probs:
        # Modern llama.cpp returns either:
        #   { "content": "...", "probs": [ {"tok_str": "..", "prob": 0.x, "id": N}, ...] }
        # or { "content": "...", "top_logprobs": [...] }.
        probs = step.get("probs")
        if probs is None:
            probs = step.get("top_probs") or step.get("top_logprobs") or []
        if not probs:
            continue
        token_ids: list[int] = []
        token_ps: list[float] = []
        for entry in probs[:TOP_K]:
            tid = entry.get("id")
            if tid is None:
                # Some llama.cpp builds omit the id; skip rather than guess.
                continue
            p = entry.get("prob")
            if p is None:
                # logprob → prob fallback
                lp = entry.get("logprob")
                if lp is None:
                    continue
                p = pow(2.718281828, float(lp))
            token_ids.append(int(tid))
            token_ps.append(float(p))
        if not token_ids:
            continue
        records.append({
            "h": hash_ctx(running_ctx),
            "c": int(category),
            "t": token_ids,
            "p": token_ps,
        })
        running_ctx += step.get("content", "")
    return records, len(records), generated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=int(os.environ.get("EXTRACT_TARGET", "5000")),
                        help="Minimum total record count to emit")
    parser.add_argument("--max-prompts", type=int, default=int(os.environ.get("EXTRACT_MAX_PROMPTS", "0")),
                        help="Cap prompts processed (0 = all)")
    parser.add_argument("--no-server", action="store_true",
                        help="Assume donor is already running on DONOR_PORT")
    args = parser.parse_args()

    proc: subprocess.Popen[bytes] | None = None
    if not args.no_server:
        proc = spawn_donor()
        wait_donor_ready()

    all_records: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    started = time.monotonic()
    last_status = started
    n_prompts = 0
    failures = 0

    prompts = list(ALL_PROMPTS)
    if args.max_prompts > 0:
        prompts = prompts[: args.max_prompts]

    print(f"[extract] {len(prompts)} prompts, top_k={TOP_K}, n_predict={TOKENS_PER_PROMPT}")
    print(f"[extract] target records={args.target}")

    try:
        # Loop the corpus until the record target is met or the corpus is
        # exhausted twice (in which case we accept what we have).
        passes = 0
        while len(all_records) < args.target and passes < 3:
            passes += 1
            print(f"[extract] === pass {passes} === records so far: {len(all_records)}")
            for cat, prompt in prompts:
                if len(all_records) >= args.target:
                    break
                n_prompts += 1
                try:
                    recs, n, _ = extract_one(prompt, cat)
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    print(f"[extract] FAIL cat={cat} prompt#{n_prompts}: {e}")
                    continue
                # Dedupe by record hash so re-runs on same prompts add value
                # only when stochastic decoding produced a new context.
                fresh = [r for r in recs if r["h"] not in seen_hashes]
                for r in fresh:
                    seen_hashes.add(r["h"])
                all_records.extend(fresh)
                now = time.monotonic()
                if now - last_status > 5 or n_prompts <= 3:
                    elapsed = now - started
                    rate = len(all_records) / elapsed if elapsed > 0 else 0
                    eta = max(0, (args.target - len(all_records)) / rate) if rate > 0 else -1
                    print(f"[extract] cat={cat} p#{n_prompts} added={len(fresh)}/{n} "
                          f"total={len(all_records)} rate={rate:.1f}/s eta={eta:.0f}s")
                    last_status = now
    finally:
        if proc is not None:
            kill_donor(proc)

    elapsed = time.monotonic() - started
    print(f"[extract] DONE: {len(all_records)} records from {n_prompts} prompts "
          f"in {elapsed:.0f}s (failures={failures})")

    if len(all_records) < 100:
        raise SystemExit(f"[FATAL] only {len(all_records)} records — refusing to overwrite gate")

    # ----- determine vocab size -----
    vocab_size = 152064  # Qwen2.5 default
    try:
        tk = requests.post(f"{DONOR_URL}/tokenize", json={"content": "x"}, timeout=10)
        if tk.ok:
            tokens = tk.json().get("tokens") or []
            # /props gives the real vocab if available
            pr = requests.get(f"{DONOR_URL}/props", timeout=10)
            if pr.ok:
                vocab_size = int(pr.json().get("default_generation_settings", {}).get("n_vocab", vocab_size))
            _ = tokens
    except Exception:
        pass

    payload = {
        "version": "deep-1.0",
        "top_k": TOP_K,
        "vocab_size": vocab_size,
        "meta": {
            "extracted_at": int(time.time()),
            "donor": str(DONOR_PATH),
            "n_prompts": n_prompts,
            "n_records": len(all_records),
            "n_failures": failures,
            "elapsed_s": int(elapsed),
            "tokens_per_prompt": TOKENS_PER_PROMPT,
            "ctx_window_hash_chars": CONTEXT_HASH_WINDOW,
            "categories": {"1": "technical_triage", "2": "project_context", "3": "agentic_reasoning"},
        },
        "records": all_records,
    }

    # Backup the existing gate before clobbering.
    if GATE_OUT.exists():
        backup = GATE_OUT.with_suffix(".gate.prev")
        backup.write_bytes(GATE_OUT.read_bytes())
        print(f"[extract] backed up previous gate -> {backup}")

    blob = gzip.compress(pickle.dumps(payload, protocol=4))
    GATE_OUT.write_bytes(blob)
    META_OUT.write_text(json.dumps(payload["meta"], indent=2))

    print(f"[extract] wrote {GATE_OUT} ({len(blob):,} bytes, {len(all_records)} records)")
    print(f"[extract] wrote {META_OUT}")


if __name__ == "__main__":
    main()
