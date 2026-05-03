#!/home/user/Gator/venv/bin/python3
"""Project Gator - Step 4: Logit-Processor bridge (the graft).

Loads logic_map.gate into RAM, receives prompts, and performs token-by-token
completion against local llama-server while applying donor-derived logit biases.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import pickle
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from event_bus import EventBusClient, EventBusError

GATOR_ROOT = Path.home() / "Gator"
GATE_PATH = GATOR_ROOT / "bin" / "logic_map.gate"
DEFAULT_SERVER = "http://127.0.0.1:8081"
BIAS_WEIGHT = 0.4
DEBUG_ENABLED = os.environ.get("GATOR_DEBUG", "false").lower() == "true"
DEBUG_FILE = GATOR_ROOT / "logs" / "debug.json"

CATEGORY_TAGS = {
    "chain_of_thought": 0,
    "analysis": 1,
    "fact_checking": 2,
    "mathematical": 3,
    "causal": 4,
    "counterfactual": 5,
    "ethical": 6,
    "analogical": 7,
    "deductive": 8,
    "inductive": 9,
}
INV_TAGS = {v: k for k, v in CATEGORY_TAGS.items()}


class BridgeError(RuntimeError):
    pass


@dataclass
class GateSummary:
    total_records: int
    per_category_top_tokens: dict[int, list[int]]


class GatorBridge:
    def __init__(self, server_url: str = DEFAULT_SERVER, gate_path: Path = GATE_PATH) -> None:
        self.server_url = server_url.rstrip("/")
        self.gate_path = gate_path
        self.gate = self._load_gate(gate_path)
        self.bus = EventBusClient()

    def _emit_debug(self, payload: dict[str, Any]) -> None:
        if not DEBUG_ENABLED:
            return
        DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=True) + "\n")

    def _post_json(self, url: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise BridgeError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except error.URLError as exc:
            raise BridgeError(f"Cannot reach llama-server at {url}: {exc}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"Non-JSON from {url}: {raw[:300]}") from exc

    def _load_gate(self, path: Path) -> GateSummary:
        if not path.exists():
            raise BridgeError(f"logic_map.gate not found: {path}")

        payload = pickle.loads(gzip.decompress(path.read_bytes()))
        records = payload.get("records", [])

        # Aggregate donor token preferences per category.
        agg: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for record in records:
            cat = int(record["c"])
            token_ids = record["t"]
            probs = record["p"]
            for tok, prob in zip(token_ids, probs):
                agg[cat][int(tok)] += float(prob)

        per_category_top_tokens: dict[int, list[int]] = {}
        for cat, token_map in agg.items():
            ranked = sorted(token_map.items(), key=lambda kv: kv[1], reverse=True)
            per_category_top_tokens[cat] = [tok for tok, _ in ranked[:256]]

        return GateSummary(total_records=len(records), per_category_top_tokens=per_category_top_tokens)

    def _classify_prompt(self, prompt: str) -> int:
        p = prompt.lower()
        rules = [
            (r"\b(prove|deriv|equation|integral|theorem|matrix)\b", CATEGORY_TAGS["mathematical"]),
            (r"\b(fact-check|verify|source|citation|true or false)\b", CATEGORY_TAGS["fact_checking"]),
            (r"\b(cause|causal|mechanism|confound|correlation)\b", CATEGORY_TAGS["causal"]),
            (r"\b(ethic|moral|dilemma|obligation)\b", CATEGORY_TAGS["ethical"]),
            (r"\b(if .* then|necessarily follows|deductive|syllogism)\b", CATEGORY_TAGS["deductive"]),
            (r"\b(inductive|generaliz|sample bias|falsify)\b", CATEGORY_TAGS["inductive"]),
            (r"\b(counterfactual|what if|alternate history)\b", CATEGORY_TAGS["counterfactual"]),
            (r"\b(analogy|analogical|mental model)\b", CATEGORY_TAGS["analogical"]),
            (r"\b(analy[sz]e|framework|first principles|trade-off)\b", CATEGORY_TAGS["analysis"]),
        ]
        for pattern, cat in rules:
            if re.search(pattern, p):
                return cat
        return CATEGORY_TAGS["chain_of_thought"]

    def _tokenize(self, text: str) -> list[int]:
        payload = {"content": text, "add_special": True, "with_pieces": False}
        data = self._post_json(f"{self.server_url}/tokenize", payload, timeout=30)
        tokens = data.get("tokens")
        if isinstance(tokens, list):
            return [int(t) for t in tokens]
        return []

    def _trajectory_needs_bias(self, trajectory_tokens: list[int], pathway: list[int]) -> bool:
        if not pathway:
            return False
        if not trajectory_tokens:
            return True

        path_set = set(pathway[:128])
        overlap = sum(1 for t in trajectory_tokens[-64:] if t in path_set)
        score = overlap / max(1, min(64, len(trajectory_tokens)))
        return score < 0.10

    def generate(
        self,
        prompt: str,
        max_tokens: int = 128,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise BridgeError("Prompt cannot be empty.")

        cat = self._classify_prompt(prompt)
        pathway = self.gate.per_category_top_tokens.get(cat, [])
        if not pathway:
            pathway = self.gate.per_category_top_tokens.get(CATEGORY_TAGS["chain_of_thought"], [])
        if not pathway:
            for toks in self.gate.per_category_top_tokens.values():
                if toks:
                    pathway = toks
                    break

        generated = ""
        biases_applied_total = 0
        step_meta: list[dict[str, Any]] = []
        interrupted = False

        try:
            self.bus.publish({"type": "generation_start", "prompt_preview": prompt[:120], "final": False})
        except Exception:
            pass

        for step_idx in range(max_tokens):
            try:
                if self.bus.consume_interrupt().get("interrupt", False):
                    interrupted = True
                    break
            except Exception:
                pass

            running_prompt = f"{prompt}{generated}"
            traj_tokens = self._tokenize(running_prompt)
            needs_bias = self._trajectory_needs_bias(traj_tokens, pathway)

            # Prime the trajectory with donor guidance on the first step.
            if step_idx == 0 and pathway:
                needs_bias = True

            logit_bias = None
            if needs_bias:
                # Static donor force: +0.4 on top pathway tokens.
                selected = pathway[:64]
                logit_bias = {str(tok): BIAS_WEIGHT for tok in selected}
                biases_applied_total += len(selected)
            else:
                selected = []

            payload = {
                "prompt": running_prompt,
                "n_predict": 1,
                "temperature": temperature,
                "top_p": top_p,
                "cache_prompt": True,
                "stop": ["<|im_end|>", "</s>"],
            }
            if logit_bias is not None:
                payload["logit_bias"] = logit_bias

            data = self._post_json(f"{self.server_url}/completion", payload, timeout=120)
            piece = data.get("content", "")
            if not piece:
                break

            generated += piece
            step_meta.append(
                {
                    "step": step_idx,
                    "needs_bias": needs_bias,
                    "bias_count": len(selected),
                    "traj_tokens": len(traj_tokens),
                    "piece": piece,
                }
            )
            try:
                self.bus.publish(
                    {
                        "type": "token_step",
                        "step": step_idx,
                        "piece": piece,
                        "bias_count": len(selected),
                        "final": False,
                    }
                )
            except Exception:
                pass
            if data.get("stop", False):
                break

        final_packet = {
            "type": "generation_final",
            "final": True,
            "interrupted": interrupted,
            "text_len": len(generated),
            "biases_applied_total": biases_applied_total,
            "category": INV_TAGS.get(cat, str(cat)),
        }
        try:
            final_ack = self.bus.publish(final_packet)
            if not final_ack.get("ok", False):
                raise BridgeError("Event-bus rejected final packet")
        except EventBusError as exc:
            raise BridgeError(f"Event-bus final handshake failed: {exc}") from exc

        self._emit_debug(
            {
                "ts": time.time(),
                "prompt_preview": prompt[:240],
                "category": INV_TAGS.get(cat, str(cat)),
                "bias_weight": BIAS_WEIGHT,
                "biases_applied_total": biases_applied_total,
                "selected_pathway_preview": pathway[:12],
                "steps": step_meta,
            }
        )

        return {
            "text": generated,
            "category": INV_TAGS.get(cat, str(cat)),
            "bias_weight": BIAS_WEIGHT,
            "biases_applied_total": biases_applied_total,
            "logic_records_loaded": self.gate.total_records,
            "interrupted": interrupted,
            "final": True,
        }


class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: int = 128
    temperature: float = 0.7
    top_p: float = 0.9


def build_api(bridge: GatorBridge) -> FastAPI:
    app = FastAPI(title="Gator Bridge", version="1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "logic_records_loaded": bridge.gate.total_records,
            "bias_weight": BIAS_WEIGHT,
        }

    @app.post("/generate")
    def generate(req: GenerateRequest) -> dict[str, Any]:
        try:
            return bridge.generate(
                prompt=req.prompt,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                top_p=req.top_p,
            )
        except BridgeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app



def interactive_cli(bridge: GatorBridge) -> None:
    print("Gator Bridge CLI ready. Type 'exit' to quit.")
    while True:
        try:
            prompt = input("gator> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if prompt.lower() in {"exit", "quit"}:
            break
        if not prompt:
            continue
        t0 = time.perf_counter()
        result = bridge.generate(prompt)
        dt = time.perf_counter() - t0
        print(result["text"].strip())
        print(
            f"[meta] category={result['category']} bias_weight={result['bias_weight']} "
            f"biases_applied_total={result['biases_applied_total']} elapsed={dt:.2f}s"
        )



def _main() -> None:
    parser = argparse.ArgumentParser(description="Project Gator bridge")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="llama-server base URL")
    parser.add_argument("--gate", default=str(GATE_PATH), help="Path to logic_map.gate")
    parser.add_argument("--mode", choices=["api", "cli"], default="api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    bridge = GatorBridge(server_url=args.server, gate_path=Path(args.gate))

    if args.mode == "cli":
        interactive_cli(bridge)
        return

    app = build_api(bridge)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    _main()
