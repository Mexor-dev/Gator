#!/usr/bin/env python3
"""Telegram gateway process with dynamic .env configuration."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import request

import dns.resolver
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from event_bus import EventBusClient  # noqa: E402

GATOR_ROOT = Path.home() / "Gator"
BRIDGE_URL = "http://127.0.0.1:8090/generate"
STATUS_FILE = GATOR_ROOT / "logs" / "telegram_gateway_status.json"
PIPER_BIN = GATOR_ROOT / "bin" / "piper"
PIPER_RUNTIME_DIR = GATOR_ROOT / "bin" / "piper_runtime"
PIPER_ESPEAK_DATA = PIPER_RUNTIME_DIR / "espeak-ng-data"
PIPER_MODEL = GATOR_ROOT / "models" / "tts" / "en_GB-alba-medium.onnx"
PIPER_MODEL_CONFIG = GATOR_ROOT / "models" / "tts" / "en_GB-alba-medium.onnx.json"
TTS_TMP_DIR = GATOR_ROOT / "tmp" / "tts"
VOICE_STATUS_FILE = GATOR_ROOT / "logs" / "voice_status.json"
STOP = False
NATIVE_LOG = GATOR_ROOT / "logs" / "native.log"
KERNEL_LOG = GATOR_ROOT / "logs" / "kernel.log"
_TRACE_PAT = re.compile(r"\[gator_kern native trace:[^\]]*\]")
DNS_FALLBACK_HOST = "api.telegram.org"
DNS_FALLBACK_SERVERS = ["1.1.1.1", "8.8.8.8"]


def _post_json(url: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _write_status(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def _bus_ok() -> bool:
    try:
        snap = EventBusClient().doctor_query()
        return bool(snap.get("ok"))
    except Exception:
        return False


def _handle_stop(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP
    STOP = True


def _sleep_with_stop(seconds: float) -> None:
    end = time.time() + max(0.0, seconds)
    while not STOP and time.time() < end:
        time.sleep(0.2)


def _get_voice_enabled_from_file() -> bool:
    """Read voice enablement status from file; default to True if not found."""
    try:
        if VOICE_STATUS_FILE.exists():
            data = json.loads(VOICE_STATUS_FILE.read_text(encoding="utf-8"))
            return bool(data.get("enabled", True))
    except Exception:
        pass
    return True


def _resolve_with_public_dns(host: str) -> list[str]:
    if host != DNS_FALLBACK_HOST:
        return []
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = DNS_FALLBACK_SERVERS
    resolver.timeout = 2.0
    resolver.lifetime = 4.0
    out: list[str] = []
    for record_type in ("A", "AAAA"):
        try:
            answers = resolver.resolve(host, record_type)
            out.extend([str(rdata).strip() for rdata in answers])
        except Exception:
            continue
    seen: set[str] = set()
    uniq: list[str] = []
    for ip in out:
        if ip and ip not in seen:
            uniq.append(ip)
            seen.add(ip)
    return uniq


@contextlib.contextmanager
def _telegram_dns_fallback_session() -> Any:
    original_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return original_getaddrinfo(host, port, *args, **kwargs)
        except socket.gaierror as exc:
            if host != DNS_FALLBACK_HOST:
                raise
            fallback_ips = _resolve_with_public_dns(host)
            if not fallback_ips:
                raise exc
            results: list[Any] = []
            for ip in fallback_ips:
                results.extend(original_getaddrinfo(ip, port, *args, **kwargs))
            if not results:
                raise exc
            print(
                f"[INFO] DNS fallback used for {host}: {', '.join(fallback_ips)}",
                flush=True,
            )
            return results

    socket.getaddrinfo = patched_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


class TelegramGateway:
    def __init__(self, token: str, username: str, auth_chat_id: str, bridge_url: str = BRIDGE_URL) -> None:
        self.token = token
        self.username = username
        self.auth_chat_id = str(auth_chat_id)
        self.bridge_url = bridge_url
        self.voice_enabled = _get_voice_enabled_from_file()

    async def _ask_gator(self, text: str) -> tuple[str, str, bool]:
        request_id = uuid.uuid4().hex
        payload = {
            "prompt": text,
            "max_tokens": 900,
            "temperature": 0.82,
            "top_k": 40,
            "request_id": request_id,
        }
        bridge_task = asyncio.create_task(asyncio.to_thread(_post_json, self.bridge_url, payload, 180.0))
        bus = EventBusClient(timeout=2.0)
        buffer = ""
        after_seq = 0
        saw_final = False
        deadline = time.time() + 180.0

        while True:
            try:
                packet_batch = await asyncio.to_thread(bus.consume_packets, after_seq, request_id, 256)
                for packet in packet_batch.get("packets", []):
                    seq = int(packet.get("seq", after_seq) or after_seq)
                    after_seq = max(after_seq, seq)
                    piece = str(packet.get("piece") or "")
                    if piece:
                        buffer += piece
                    if bool(packet.get("final", False)):
                        saw_final = True
                        break
            except Exception:
                pass

            if saw_final and bridge_task.done():
                break

            if bridge_task.done() and (saw_final or time.time() >= deadline):
                break

            if time.time() >= deadline:
                break

            await asyncio.sleep(0.05)

        data = await bridge_task
        fallback = str(data.get("text") or data.get("output") or data.get("response") or "")
        entity_name = str(data.get("entity_name") or data.get("identity") or "Gator-Prime")
        if not saw_final:
            return "", entity_name, False

        if buffer.strip():
            return buffer.strip(), entity_name, True
        return fallback.strip(), entity_name, True

    def _authorized(self, update: Update) -> bool:
        msg = update.message
        if not msg or not msg.chat:
            return False
        return str(msg.chat.id) == self.auth_chat_id

    async def _send_text_and_voice(self, bot: Any, chat_id: int, text: str) -> None:
        message_text = self._sanitize_outbound_text(text or "", source="send_text")
        if not message_text:
            return
        await bot.send_message(chat_id=chat_id, text=message_text)
        if self.voice_enabled:
            asyncio.create_task(self._send_voice_only(bot, chat_id, message_text))

    def _sanitize_outbound_text(self, text: str, *, source: str) -> str:
        """Emergency guardrail: never allow native traces into user-visible channels.

        If a native trace marker appears, strip all text from marker onward and
        redirect stripped content to logs/kernel.log for developer monitoring.
        """
        raw = (text or "").strip()
        if not raw:
            return ""

        marker = "[gator_kern"
        lower_raw = raw.lower()
        marker_idx = lower_raw.find(marker)
        if marker_idx >= 0:
            cleaned = raw[:marker_idx].strip()
            stripped = raw[marker_idx:].strip()
            try:
                KERNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
                with KERNEL_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(f"{time.time():.3f} [{source}] stripped={stripped}\n")
            except Exception:
                pass
            return cleaned

        return self._strip_native_trace(raw)

    def _strip_native_trace(self, text: str) -> str:
        """Second-line defence: strip any kern trace that escaped the bridge filter.

        Redirects matched fragments to logs/native.log for diagnostics.
        """
        traces = _TRACE_PAT.findall(text or "")
        if traces:
            try:
                NATIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
                with NATIVE_LOG.open("a", encoding="utf-8") as fh:
                    for fragment in traces:
                        fh.write(f"{time.time():.3f} [tg-filter] {fragment}\n")
            except Exception:
                pass
        return _TRACE_PAT.sub("", text or "").strip()

    def _clean_reasoning_output(self, text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return raw
        has_steps = bool(re.search(r"STEP_1|STEP_2", raw))

        def _strip_step_headers(s: str) -> str:
            s = re.sub(r"(?im)^\s*STEP_\d+[_A-Z]*\s*:?.*$", "", s)
            s = re.sub(r"(?im)^\s*STEP_\d+\s*$", "", s)
            s = re.sub(r"(?im)^\s*FINAL_ANSWER\s*:?\s*", "", s)
            return s.strip()

        if has_steps:
            match_final = re.search(r"(?is)FINAL_ANSWER\s*:\s*", raw)
            if match_final:
                candidate = raw[match_final.end() :].strip()
                cleaned = _strip_step_headers(candidate)
                return cleaned or _strip_step_headers(raw)

            tail_start = max(0, int(len(raw) * 0.8))
            tail = raw[tail_start:]
            colon_newline_matches = list(re.finditer(r":\s*\n+", tail))
            if colon_newline_matches:
                cut = tail_start + colon_newline_matches[-1].end()
                candidate = raw[cut:].strip()
            else:
                candidate = tail.strip()

            cleaned = _strip_step_headers(candidate)
            if cleaned:
                return cleaned
            print("[WARN] Reasoning Failure: STEP scaffold present and no clean final segment found", flush=True)
            return _strip_step_headers(tail)

        match = re.search(r"FINAL_ANSWER\s*:\s*", raw)
        if match:
            cleaned = _strip_step_headers(raw[match.end() :])
            return cleaned or _strip_step_headers(raw)

        print("[WARN] Reasoning Failure: FINAL_ANSWER not found; sending raw output", flush=True)
        return raw

    def _strip_user_echo(self, user_text: str, answer: str) -> str:
        """Remove leading user-echo segments from model output."""
        original = (answer or "").strip()
        if not original:
            return original

        user = (user_text or "").strip()
        if not user:
            return original

        candidate = re.sub(r"^\[[^\]]+\]\s*", "", original).strip()
        user_norm = re.sub(r"\s+", " ", user).lower()
        cand_norm = re.sub(r"\s+", " ", candidate).lower()

        trimmed = candidate
        changed = False

        if cand_norm.startswith(user_norm):
            trimmed = candidate[len(user) :].lstrip(" \t\r\n:,-.!?")
            changed = True
        else:
            idx = cand_norm.find(user_norm)
            if 0 <= idx <= 40:
                trimmed = candidate[idx + len(user) :].lstrip(" \t\r\n:,-.!?")
                changed = True

        if changed:
            if not trimmed:
                trimmed = "I'm running at peak efficiency, ready for the next task."
            try:
                KERNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
                with KERNEL_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"{time.time():.3f} [echo-strip] user={user[:180]} | before={original[:260]} | after={trimmed[:260]}\n"
                    )
            except Exception:
                pass
            return trimmed

        return original

    def _mark_truncated_thought(self, text: str) -> str:
        cleaned = (text or "").rstrip()
        if not cleaned:
            return cleaned
        if cleaned.endswith("...") or cleaned.endswith("…"):
            return cleaned
        if not cleaned.endswith("."):
            print("[INFO] VRAM Ceiling Hit - Thought Truncated", flush=True)
            return f"{cleaned}..."
        return cleaned

    async def _send_voice_only(self, bot: Any, chat_id: int, text: str) -> None:
        voice_path = await asyncio.to_thread(self.synthesize_speech, text)
        if voice_path is not None and voice_path.exists():
            try:
                with voice_path.open("rb") as voice_fp:
                    await bot.send_voice(chat_id=chat_id, voice=voice_fp)
            finally:
                voice_path.unlink(missing_ok=True)

    async def mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update)
            return
        self.voice_enabled = False
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Voice output muted. Text replies remain active.")

    async def unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update)
            return
        self.voice_enabled = True
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Voice output unmuted. Text + voice replies are active.")

    def synthesize_speech(self, text: str) -> Path | None:
        text = self._sanitize_outbound_text(text, source="tts")
        if not text.strip():
            return None
        if not PIPER_BIN.exists() or not PIPER_MODEL.exists() or not PIPER_MODEL_CONFIG.exists():
            print("[WARN] Piper TTS assets missing; skipping voice generation", flush=True)
            return None

        TTS_TMP_DIR.mkdir(parents=True, exist_ok=True)
        stem = uuid.uuid4().hex
        wav_path = TTS_TMP_DIR / f"{stem}.wav"
        ogg_path = TTS_TMP_DIR / f"{stem}.ogg"
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = f"{PIPER_RUNTIME_DIR}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")

        try:
            subprocess.run(
                [
                    str(PIPER_BIN),
                    "--model",
                    str(PIPER_MODEL),
                    "--config",
                    str(PIPER_MODEL_CONFIG),
                    "--output_file",
                    str(wav_path),
                    "--espeak_data",
                    str(PIPER_ESPEAK_DATA),
                    "--quiet",
                ],
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                env=env,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(wav_path),
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "48k",
                    str(ogg_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                env=env,
            )
            return ogg_path if ogg_path.exists() else None
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else str(exc)
            print(f"[WARN] Piper TTS failed: {detail[:400]}", flush=True)
            return None
        finally:
            if wav_path.exists():
                wav_path.unlink(missing_ok=True)

    async def _reject(self, update: Update) -> None:
        if update.message:
            await self._send_text_and_voice(update.get_bot(), update.effective_chat.id, "Unauthorized chat id.")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update)
            return
        await self._send_text_and_voice(context.bot, update.effective_chat.id, "Gator Telegram Gateway online.")

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update)
            return
        user_text = (update.message.text or "").strip()
        if not user_text:
            return
        answer, entity_name, saw_final = await self._ask_gator(user_text)
        if not saw_final:
            print("[WARN] Skipping Telegram send: final packet not observed", flush=True)
            return
        if not answer:
            answer = "No response from Gator bridge."
        # Strip any native kern debug traces before the answer reaches the user.
        answer = self._strip_native_trace(answer)
        answer = self._clean_reasoning_output(answer)
        answer = self._strip_user_echo(user_text, answer)
        answer = answer[:3500]
        answer = self._mark_truncated_thought(answer)
        # Sovereign Polish: prefix every outbound message with the node identity.
        label = f"[{entity_name}]"
        answer = f"{label} {answer}" if answer and not answer.startswith(label) else answer
        await self._send_text_and_voice(context.bot, update.effective_chat.id, answer)
        answer = ""

    async def post_init(self, application: Application) -> None:
        bus_ok = _bus_ok()
        ready_msg = "Telegram Gateway: AUTHENTICATED & READY"
        print(ready_msg, flush=True)

        _write_status(
            {
                "ok": True,
                "authenticated": True,
                "startup_message_delivered": False,
                "connected_event_bus": bus_ok,
                "username": self.username,
                "chat_id": self.auth_chat_id,
                "ts": int(time.time()),
                "message": ready_msg,
            }
        )

        try:
            await self._send_text_and_voice(application.bot, int(self.auth_chat_id), "Connection Established")
            _write_status(
                {
                    "ok": True,
                    "authenticated": True,
                    "startup_message_delivered": True,
                    "connected_event_bus": bus_ok,
                    "username": self.username,
                    "chat_id": self.auth_chat_id,
                    "ts": int(time.time()),
                    "message": ready_msg,
                }
            )
        except Exception as exc:
            _write_status(
                {
                    "ok": True,
                    "authenticated": True,
                    "startup_message_delivered": False,
                    "connected_event_bus": bus_ok,
                    "username": self.username,
                    "chat_id": self.auth_chat_id,
                    "ts": int(time.time()),
                    "error": f"startup message failed: {exc}",
                }
            )
            print(f"[WARN] startup message failed: {exc}", flush=True)

    def run(self) -> None:
        app = Application.builder().token(self.token).post_init(self.post_init).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("mute", self.mute))
        app.add_handler(CommandHandler("unmute", self.unmute))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))
        with _telegram_dns_fallback_session():
            app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


def _load_config() -> dict[str, str]:
    env_path = GATOR_ROOT / ".env"
    load_dotenv(dotenv_path=env_path, override=True)
    return {
        "token": os.environ.get("GATOR_TG_BOT_TOKEN", "").strip(),
        "username": os.environ.get("GATOR_TG_BOT_USERNAME", "").strip(),
        "chat_id": os.environ.get("GATOR_TG_AUTH_CHAT_ID", "").strip(),
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Telegram gateway")
    parser.add_argument("--bridge", type=str, default=BRIDGE_URL)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    while not STOP:
        cfg = _load_config()
        missing = [k for k, v in cfg.items() if not v]
        if missing:
            _write_status(
                {
                    "ok": False,
                    "authenticated": False,
                    "connected_event_bus": _bus_ok(),
                    "ts": int(time.time()),
                    "error": f"Missing Telegram config: {', '.join(missing)}",
                }
            )
            print(f"[INFO] Telegram gateway idle: missing {', '.join(missing)}", flush=True)
            _sleep_with_stop(3.0)
            continue

        gw = TelegramGateway(
            token=cfg["token"],
            username=cfg["username"],
            auth_chat_id=cfg["chat_id"],
            bridge_url=args.bridge,
        )
        try:
            gw.run()
        except Exception as exc:
            _write_status(
                {
                    "ok": False,
                    "authenticated": False,
                    "connected_event_bus": _bus_ok(),
                    "username": cfg["username"],
                    "chat_id": cfg["chat_id"],
                    "ts": int(time.time()),
                    "error": str(exc),
                }
            )
            print(f"[WARN] Telegram gateway restart loop: {exc}", flush=True)
            _sleep_with_stop(5.0)

    _write_status(
        {
            "ok": False,
            "authenticated": False,
            "connected_event_bus": _bus_ok(),
            "ts": int(time.time()),
            "message": "Telegram gateway stopped",
        }
    )


if __name__ == "__main__":
    _main()
