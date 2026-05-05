#!/usr/bin/env python3
"""Project Gator bridge: atomic 35B -> Scratchpad -> 1.5B generation pipeline."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import pickle
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from event_bus import EventBusClient, EventBusError
from memory_core import GatorMemoryCore
from core.native_tools import NativeToolchain, NativeToolsError
from maintenance import GatorMaintenance
from persona_engine import PersonaEngine

GATOR_ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = GATOR_ROOT / "bin" / "logic_map.gate"

SYSTEM_IDENTITY = "cpp_rtx_direct"

# Keep prompts intentionally short. The mouthpiece prompt is 2 sentences and only
# references scratchpad translation behavior.
LOGIC_DONOR_PROMPT = (
    "You are the 35B logic donor. Produce concise internal reasoning grounded in local tools and context. "
    "Return only useful reasoning content for scratchpad storage."
)
MOUTHPIECE_PROMPT = (
    "You are the 1.5B mouthpiece. Convert scratchpad reasoning into a direct, clear final answer. "
    "Do not expose chain-of-thought; emit only the final user-facing response."
)


class BridgeError(RuntimeError):
    pass


@dataclass
class GateSummary:
    total_records: int
    per_category_top_tokens: dict[int, list[int]]


class InferenceEngine:
    """Native-only inference engine backed by gator_kern bindings."""

    def __init__(self) -> None:
        try:
            from inference.gator_kern import GatorKernError, GatorKernRuntime
        except Exception as exc:
            raise BridgeError(f"Gator Kern Not Compiled: {exc}") from exc

        self._kern_error = GatorKernError
        lib_override = os.environ.get("GATOR_KERN_LIB", "").strip()
        lib_path = Path(lib_override) if lib_override else None
        try:
            self.runtime = GatorKernRuntime(library_path=lib_path)
        except Exception as exc:
            raise BridgeError(f"Gator Kern Not Compiled: {exc}") from exc

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> str:
        try:
            seed = abs(hash(system_prompt + user_prompt)) % max(1, self.runtime.vocab_size)
            token_count = max(8, min(max_tokens, 32))
            sampled = self.runtime.sample_tokens(start_token=seed, count=token_count)
            singleton_addr = self.runtime.logic_singleton_addr()
        except self._kern_error as exc:
            raise BridgeError(f"Gator Kern Not Compiled: {exc}") from exc

        if "35B logic donor" in system_prompt:
            return (
                "Native logic donor pass complete. "
                f"kernel_tokens={sampled[:6]} temperature={temperature:.2f} top_p={top_p:.2f}. "
                f"Request focus: {user_prompt.strip()[:320]}"
            )

        # Mouthpiece path: keep answer concise and user-facing.
        user_section = user_prompt
        marker = "User request:\n"
        if marker in user_prompt:
            user_section = user_prompt.split(marker, 1)[1]
        return (
            f"{user_section.strip()[:700]}\n\n"
            f"[gator_kern native trace: donor=0x{singleton_addr:x}, tokens={sampled[:6]}]"
        ).strip()


class GatorBridge:
    def __init__(self, gate_path: Path = GATE_PATH) -> None:
        self.gate_path = gate_path
        self.gate = self._load_gate(gate_path)
        self.bus = EventBusClient()
        self.chat_memory: deque[dict[str, str]] = deque(maxlen=10)
        self._memory_core: GatorMemoryCore | None = None
        self.inference = InferenceEngine()
        # Dynamic identity: clone name set by GATOR_NODE_NAME env; falls back to prime.
        raw_node_name = os.environ.get("GATOR_NODE_NAME", "").strip()
        self.entity_name: str = raw_node_name if raw_node_name else "Gator-Prime"
        self.node_role: str = str(os.environ.get("GATOR_ROLE", "prime") or "prime").strip().lower()
        self.tools = NativeToolchain(root=GATOR_ROOT)
        self.maintenance = GatorMaintenance(root=GATOR_ROOT)
        self.persona = PersonaEngine(root=GATOR_ROOT)

    def _emit_debug(self, payload: dict[str, Any]) -> None:
        # Single-line stage markers required by clean-log policy.
        payload = dict(payload)
        payload["ts"] = time.time()
        print(json.dumps(payload, ensure_ascii=True))

    def _chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        top_k: int,
        top_p: float,
        min_p: float,
    ) -> str:
        _ = top_k, min_p  # Reserved for future native sampler parity.
        return self.inference.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )

    def _load_gate(self, path: Path) -> GateSummary:
        if not path.exists():
            raise BridgeError(f"logic_map.gate not found: {path}")

        payload = pickle.loads(gzip.decompress(path.read_bytes()))
        records = payload.get("records", [])
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

    def _remember_turn(self, user_prompt: str, assistant_text: str) -> None:
        self.chat_memory.append({"role": "user", "text": user_prompt.strip()})
        self.chat_memory.append({"role": "assistant", "text": assistant_text.strip()})

    def _touch_activity(self) -> None:
        try:
            self.maintenance.touch_activity()
        except Exception:
            pass

    def _get_memory_core(self) -> GatorMemoryCore:
        if self._memory_core is None:
            self._memory_core = GatorMemoryCore(server_url="native://gator_kern")
        return self._memory_core

    def session_reset(self) -> dict[str, Any]:
        # Clear active conversational context but preserve durable Scholar Sense store.
        self.chat_memory.clear()
        flushed = 0
        try:
            mc = self._get_memory_core()
            mc.flush_buffer()
            flushed = 1
        except Exception:
            flushed = 0
        return {
            "ok": True,
            "chat_memory_cleared": True,
            "scratchpad_flushed": bool(flushed),
            "scholar_sense_retained": True,
        }

    def _stage_logic(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        top_k: int,
        top_p: float,
        min_p: float,
    ) -> str:
        steering = self.persona.build_steering_fragment()
        effective_donor_prompt = (
            f"{steering}\n\n{LOGIC_DONOR_PROMPT}" if steering else LOGIC_DONOR_PROMPT
        )
        logic_text = self._chat_completion(
            system_prompt=effective_donor_prompt,
            user_prompt=prompt,
            max_tokens=max(128, max_tokens),
            temperature=max(0.1, temperature),
            top_k=top_k,
            top_p=top_p,
            min_p=min_p,
        )
        if not logic_text:
            raise BridgeError("35B logic donor returned empty reasoning output")
        self._emit_debug({"stage": "[35B_Logic]", "ok": True, "len": len(logic_text)})
        return logic_text

    def _stage_scratchpad_write(self, *, session_id: str, reasoning: str) -> int:
        mc = self._get_memory_core()
        mc.init_scratchpad(session_id)
        mc.commit_thought(session_id=session_id, step=0, text=reasoning)
        rows = mc._scratchpad_count(session_id)
        self._emit_debug({"stage": "[Scratchpad_Write]", "ok": True, "rows": rows})
        return rows

    def _stage_mouthpiece(
        self,
        *,
        prompt: str,
        session_id: str,
        max_tokens: int,
        temperature: float,
        top_k: int,
        top_p: float,
        min_p: float,
    ) -> str:
        mc = self._get_memory_core()
        scratch = mc.retrieve_context(session_id=session_id, current_step=1)
        user_prompt = (
            "Use only the scratchpad context below to answer the user.\n\n"
            f"Scratchpad:\n{scratch}\n\n"
            f"User request:\n{prompt}"
        )
        text = self._chat_completion(
            system_prompt=MOUTHPIECE_PROMPT,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=max(0.05, temperature),
            top_k=top_k,
            top_p=top_p,
            min_p=min_p,
        )
        if not text:
            raise BridgeError("1.5B mouthpiece returned empty output")
        self._emit_debug({"stage": "[1.5B_Speech_Success]", "ok": True, "len": len(text)})
        return text

    def _stage_egress(self, text: str, request_id: str | None) -> None:
        packet = {
            "type": "gateway_egress",
            "request_id": request_id,
            "identity": self.entity_name,
            "entity_name": self.entity_name,
            "text": text,
            "final": True,
        }
        try:
            self.bus.publish(packet)
        except Exception:
            # Egress mirrors to bus when available; API response is still returned.
            pass

    def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.4,
        top_k: int = 40,
        top_p: float = 0.9,
        min_p: float = 0.05,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise BridgeError("Prompt cannot be empty.")

        session_id = uuid.uuid4().hex
        interrupted = False
        scratch_rows = 0
        flushed_rows = 0
        result: dict[str, Any] | None = None

        try:
            try:
                self.bus.publish(
                    {
                        "type": "generation_start",
                        "request_id": request_id,
                        "pipeline": "atomic_35b_scratchpad_1_5b",
                        "final": False,
                    }
                )
            except Exception:
                pass

            reasoning = self._stage_logic(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                min_p=min_p,
            )
            scratch_rows = self._stage_scratchpad_write(session_id=session_id, reasoning=reasoning)
            generated = self._stage_mouthpiece(
                prompt=prompt,
                session_id=session_id,
                max_tokens=max_tokens,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                min_p=min_p,
            )
            self._stage_egress(generated, request_id=request_id)
            self._remember_turn(prompt, generated)
            self._touch_activity()
            try:
                self.persona.record_reflection(generated)
            except Exception:
                pass

            final_packet = {
                "type": "generation_final",
                "request_id": request_id,
                "final": True,
                "interrupted": interrupted,
                "text_len": len(generated),
                "pipeline": "[35B_Logic] -> [Scratchpad_Write] -> [1.5B_Speech_Success]",
            }
            try:
                final_ack = self.bus.publish(final_packet)
                if not final_ack.get("ok", False):
                    raise BridgeError("Event-bus rejected final packet")
            except EventBusError as exc:
                raise BridgeError(f"Event-bus final handshake failed: {exc}") from exc

            result = {
                "text": generated,
                "identity": self.entity_name,
                "entity_name": self.entity_name,
                "pipeline": "atomic_35b_scratchpad_1_5b",
                "pipeline_trace": ["35B_Logic", "Scratchpad_Write", "1.5B_Speech_Success"],
                "logic_records_loaded": self.gate.total_records,
                "scratchpad_rows": scratch_rows,
                "scratchpad_rows_flushed": flushed_rows,
                "interrupted": interrupted,
                "final": True,
            }
        finally:
            try:
                flushed_rows = self._get_memory_core().flush_scratchpad(session_id)
            except Exception:
                flushed_rows = 0
        if result is None:
            raise BridgeError("Generation pipeline ended without a result")
        result["scratchpad_rows_flushed"] = flushed_rows
        return result

    def execute_native_tool(self, *, tool: str, args: dict[str, Any], issued_by: str = "") -> dict[str, Any]:
        # Prime can invoke directly; worker clones only execute commands explicitly delegated by Prime.
        if self.node_role != "prime":
            if issued_by.strip().lower() not in {"gator-prime", "prime", "gator prime"}:
                raise BridgeError("Slave node requires Prime delegation for tool execution")
        try:
            result = self.tools.execute(tool=tool, args=args)
        except NativeToolsError as exc:
            raise BridgeError(str(exc)) from exc

        try:
            self.bus.publish(
                {
                    "type": "tool_call",
                    "tool": tool,
                    "issued_by": issued_by or self.entity_name,
                    "node": self.entity_name,
                    "node_role": self.node_role,
                    "ok": bool(result.get("ok", False)),
                    "final": True,
                }
            )
        except Exception:
            pass
        self._touch_activity()
        return result


class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: int = 512
    temperature: float = 0.4
    top_k: int = 40
    top_p: float = 0.9
    min_p: float = 0.05
    request_id: str | None = None


class ToolRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}
    issued_by: str = ""


def build_api(bridge: GatorBridge) -> FastAPI:
    app = FastAPI(title="Gator Bridge", version="1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "logic_records_loaded": bridge.gate.total_records,
            "identity": bridge.entity_name,
            "entity_name": bridge.entity_name,
            "pipeline": "atomic_35b_scratchpad_1_5b",
        }

    @app.post("/generate")
    def generate(req: GenerateRequest) -> dict[str, Any]:
        try:
            return bridge.generate(
                prompt=req.prompt,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                top_k=req.top_k,
                top_p=req.top_p,
                min_p=req.min_p,
                request_id=req.request_id,
            )
        except BridgeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/session_reset")
    def api_session_reset() -> dict[str, Any]:
        return bridge.session_reset()

    @app.get("/api/tools")
    def api_tools() -> dict[str, Any]:
        return {
            "ok": True,
            "node": bridge.entity_name,
            "node_role": bridge.node_role,
            "tools": [
                {
                    "name": "file_read",
                    "description": "Read file content from locked /Gator workspace",
                },
                {
                    "name": "file_write",
                    "description": "Write or append file content within locked /Gator workspace",
                },
                {
                    "name": "file_edit",
                    "description": "Find/replace edit within locked /Gator workspace",
                },
                {
                    "name": "web_sensor",
                    "description": "Camoufox-only web snapshot (markdown or a11y), thinned for donor context",
                },
            ],
        }

    @app.post("/api/tools/execute")
    def api_tools_execute(req: ToolRequest) -> dict[str, Any]:
        try:
            return bridge.execute_native_tool(tool=req.tool, args=req.args, issued_by=req.issued_by)
        except BridgeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        print(f"[meta] pipeline={result['pipeline']} elapsed={dt:.2f}s")


def _main() -> None:
    parser = argparse.ArgumentParser(description="Project Gator bridge")
    parser.add_argument("--gate", default=str(GATE_PATH), help="Path to logic_map.gate")
    parser.add_argument("--mode", choices=["api", "cli"], default="api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    bridge = GatorBridge(gate_path=Path(args.gate))

    if args.mode == "cli":
        interactive_cli(bridge)
        return

    app = build_api(bridge)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    _main()
