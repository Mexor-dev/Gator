#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib import request

from dotenv import dotenv_values
from telegram import Update
from telegram.error import InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from event_bus import EventBusClient
from core.mitosis import HIVE_STATE_FILE, MitosisEngine
from core.validation import HardwareValidator

GATOR_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = GATOR_ROOT / ".env"
STATUS_FILE = GATOR_ROOT / "logs" / "telegram_hive_status.json"
PRIME_BRIDGE_URL = "http://127.0.0.1:8090/generate"
VOICE_WAV = GATOR_ROOT / "logs" / "tg_voice_summary.wav"

# Seconds before a running task emits a progress ping.
PROGRESS_INTERVAL_S: float = 15.0
MAX_STARTUP_RETRIES: int = 5
LOGGER = logging.getLogger("telegram_hive")


def _post_json(url: str, payload: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _load_hive_state() -> dict[str, Any]:
    if not HIVE_STATE_FILE.exists():
        return {"prime": {"name": "Gator-Prime"}, "clones": {}}
    try:
        return json.loads(HIVE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"prime": {"name": "Gator-Prime"}, "clones": {}}


def _write_status(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class TelegramHiveGateway:
    def __init__(self, token: str, auth_chat_id: str) -> None:
        self.token = token
        self.auth_chat_id = str(auth_chat_id)
        self.bus = EventBusClient()
        self._last_seq = 0
        self.validator = HardwareValidator()
        self._mitosis = MitosisEngine(root=GATOR_ROOT)

    def _authorized(self, update: Update) -> bool:
        return bool(update.effective_chat and str(update.effective_chat.id) == self.auth_chat_id)

    def _resolve_target(self, user_text: str) -> tuple[str, str]:
        # Route form: @Gator-Scout: task...  or  Scout, task...
        m = re.match(r"\s*@?([A-Za-z0-9_-]+)\s*:\s*(.+)", user_text, re.S)
        if not m:
            return "Gator-Prime", user_text.strip()
        return m.group(1).strip(), m.group(2).strip()

    def _route_bridge(self, target: str) -> str:
        if target.lower() in {"gator-prime", "prime", "gator"}:
            return PRIME_BRIDGE_URL
        state = _load_hive_state()
        for node in state.get("clones", {}).values():
            name = str(node.get("name") or "")
            if name.lower() == target.lower() or str(node.get("slug") or "").lower() == target.lower():
                port = int(node.get("bridge_port") or 0)
                if port > 0:
                    return f"http://127.0.0.1:{port}/generate"
        return PRIME_BRIDGE_URL

    def _is_clone(self, target: str) -> bool:
        return target.lower() not in {"gator-prime", "prime", "gator"}

    def _canonical_name(self, target: str) -> str:
        """Return the full canonical node name (e.g. 'Gator-Scout')."""
        state = _load_hive_state()
        for node in state.get("clones", {}).values():
            name = str(node.get("name") or "")
            if name.lower() == target.lower() or str(node.get("slug") or "").lower() == target.lower():
                return name
        prefix = "Gator-" if not target.lower().startswith("gator") else ""
        return prefix + target.strip().capitalize()

    async def _send(self, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        await context.bot.send_message(chat_id=int(self.auth_chat_id), text=text)

    async def _progress_watchdog(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        worker_name: str,
        task: str,
        done_event: asyncio.Event,
    ) -> None:
        """Fires a progress ping after PROGRESS_INTERVAL_S if task isn't done."""
        try:
            await asyncio.wait_for(asyncio.shield(done_event.wait()), timeout=PROGRESS_INTERVAL_S)
        except asyncio.TimeoutError:
            if not done_event.is_set():
                header = self._mitosis.worker_header(worker_name)
                await self._send(
                    context,
                    f"{header}: Still working... Scholar Sense syncing. Task: {task[:60]}{'...' if len(task) > 60 else ''}",
                )

    async def _try_prime_voice(self, summary: str) -> bool:
        """Attempt to synthesize Prime's voice summary. Returns True on success."""
        try:
            import sys as _sys
            _sys.path.insert(0, str(GATOR_ROOT / "src" / "interfaces"))
            from voice_layer import VoiceLayer, VoiceHardBlock
            vl = VoiceLayer()
            vl.synthesize_to_wav(summary, VOICE_WAV)
            return True
        except Exception:
            return False

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await context.bot.send_message(chat_id=update.effective_chat.id, text="Unauthorized chat id.")
            return
        await context.bot.send_message(chat_id=update.effective_chat.id, text="[Gator-Prime] Hive gateway online.")

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await context.bot.send_message(chat_id=update.effective_chat.id, text="Unauthorized chat id.")
            return
        text = str(update.message.text or "").strip()
        if not text:
            return

        target_raw, task = self._resolve_target(text)
        is_clone = self._is_clone(target_raw)
        worker_name = self._canonical_name(target_raw) if is_clone else "Gator-Prime"
        url = self._route_bridge(target_raw)

        # ── Step 1: Prime publicly delegates to worker ──
        if is_clone:
            await self._send(
                context,
                f"[Gator-Prime]: {worker_name}, initialize task: {task[:80]}{'...' if len(task) > 80 else ''}",
            )

        # ── Step 2: Worker acknowledges before executing ──
        if is_clone:
            header = self._mitosis.worker_header(worker_name)
            await self._send(
                context,
                f"{header}: Acknowledged. Accessing web tools now... Task: {task[:80]}{'...' if len(task) > 80 else ''}",
            )

        # ── Step 3: Execute with 15s progress watchdog ──
        done_event = asyncio.Event()
        if is_clone:
            asyncio.create_task(self._progress_watchdog(context, worker_name, task, done_event))

        answer = ""
        validation_ok = True
        try:
            payload = {"prompt": task, "max_tokens": 350, "temperature": 0.7, "top_k": 40}
            out = await asyncio.to_thread(_post_json, url, payload, 120.0)
            done_event.set()

            answer = str(out.get("text") or out.get("output") or out.get("response") or "").strip()
            if not answer:
                answer = "No response text returned."

            validation = self.validator.validate_text(answer)
            if not validation.get("ok", False):
                retry_msg = str(validation.get("retry_prompt") or "").strip()
                if retry_msg:
                    retry_out = await asyncio.to_thread(
                        _post_json,
                        url,
                        {"prompt": f"{task}\n\n{retry_msg}", "max_tokens": 350, "temperature": 0.4, "top_k": 40},
                        120.0,
                    )
                    retried = str(retry_out.get("text") or retry_out.get("output") or retry_out.get("response") or "").strip()
                    if retried:
                        answer = retried
                        validation = self.validator.validate_text(answer)

            if not validation.get("ok", False):
                validation_ok = False
        except Exception as exc:
            done_event.set()
            display = worker_name if is_clone else "Gator-Prime"
            header = self._mitosis.worker_header(display) if is_clone else "[Gator-Prime]"
            await self._send(context, f"{header}: error: {exc}")
            return

        if not validation_ok:
            display = worker_name if is_clone else "Gator-Prime"
            header = self._mitosis.worker_header(display) if is_clone else "[Gator-Prime]"
            await self._send(
                context,
                f"{header}: Gator-Guard blocked an invalid claim; response withheld until verified context is available.",
            )
            return

        # ── Step 4: Worker posts result with full header (two messages: sync announce + answer) ──
        if is_clone:
            header = self._mitosis.worker_header(worker_name)
            await self._send(context, f"{header}: Found result. Syncing to Scholar Sense...")
            await self._send(context, f"{header}: {answer}")
        else:
            await self._send(context, f"[Gator-Prime] {answer}")

        # ── Step 5: Prime speaks the final summary via voice (Prime only) ──
        if is_clone:
            summary = f"Task completed by {worker_name}. " + answer[:200]
            voiced = await self._try_prime_voice(summary)
            if voiced:
                await self._send(
                    context,
                    f"[Gator-Prime]: {worker_name} task complete. Summary synthesized via Voice Chat.",
                )
            else:
                await self._send(
                    context,
                    f"[Gator-Prime]: {worker_name} task complete. Voice synthesis unavailable; text summary above.",
                )

    async def _pump_ignition_messages(self, app: Application) -> None:
        while True:
            try:
                batch = self.bus.consume_packets(after_seq=self._last_seq, limit=256)
                for packet in batch.get("packets", []):
                    self._last_seq = max(self._last_seq, int(packet.get("seq", 0)))
                    if str(packet.get("type")) != "hive_ignition":
                        continue
                    speaker = str(packet.get("speaker") or "Gator-Prime")
                    msg = str(packet.get("text") or "").strip()
                    if msg:
                        await app.bot.send_message(chat_id=int(self.auth_chat_id), text=msg)
            except Exception:
                pass
            await asyncio.sleep(0.8)

    async def run(self) -> None:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))

        _write_status({"started_at": time.time(), "authenticated": True, "mode": "hive"})

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        asyncio.create_task(self._pump_ignition_messages(app))

        while True:
            await asyncio.sleep(1.0)


def _status_base(token: str, chat_id: str) -> dict[str, Any]:
    return {
        "ts": time.time(),
        "mode": "hive",
        "token_present": bool(token),
        "chat_id_present": bool(chat_id),
    }


async def _run_gateway_with_retry(gateway: TelegramHiveGateway) -> None:
    for attempt in range(1, MAX_STARTUP_RETRIES + 1):
        try:
            _write_status(
                {
                    **_status_base(gateway.token, gateway.auth_chat_id),
                    "authenticated": False,
                    "state": "starting",
                    "attempt": attempt,
                }
            )
            await gateway.run()
            return
        except InvalidToken as exc:
            LOGGER.error("Telegram token rejected by API: %s", exc)
            _write_status(
                {
                    **_status_base(gateway.token, gateway.auth_chat_id),
                    "authenticated": False,
                    "state": "error",
                    "error": "token_rejected",
                    "detail": str(exc),
                    "attempt": attempt,
                }
            )
            raise SystemExit("Telegram token rejected by API. Check GATOR_TG_BOT_TOKEN.") from exc
        except (NetworkError, TimedOut, RetryAfter, OSError, asyncio.TimeoutError) as exc:
            delay = min(30, 2**attempt)
            LOGGER.warning(
                "Telegram startup/connect failed (attempt %s/%s): %s. Retrying in %ss",
                attempt,
                MAX_STARTUP_RETRIES,
                exc,
                delay,
            )
            _write_status(
                {
                    **_status_base(gateway.token, gateway.auth_chat_id),
                    "authenticated": False,
                    "state": "retrying",
                    "error": "startup_connect_failed",
                    "detail": str(exc),
                    "attempt": attempt,
                    "retry_in_s": delay,
                }
            )
            await asyncio.sleep(delay)
        except TelegramError as exc:
            lower_msg = str(exc).lower()
            if "unauthorized" in lower_msg or "invalid token" in lower_msg:
                LOGGER.error("Telegram token rejected by API: %s", exc)
                _write_status(
                    {
                        **_status_base(gateway.token, gateway.auth_chat_id),
                        "authenticated": False,
                        "state": "error",
                        "error": "token_rejected",
                        "detail": str(exc),
                        "attempt": attempt,
                    }
                )
                raise SystemExit("Telegram token rejected by API. Check GATOR_TG_BOT_TOKEN.") from exc

            delay = min(30, 2**attempt)
            LOGGER.warning(
                "Telegram API error on startup (attempt %s/%s): %s. Retrying in %ss",
                attempt,
                MAX_STARTUP_RETRIES,
                exc,
                delay,
            )
            _write_status(
                {
                    **_status_base(gateway.token, gateway.auth_chat_id),
                    "authenticated": False,
                    "state": "retrying",
                    "error": "telegram_api_error",
                    "detail": str(exc),
                    "attempt": attempt,
                    "retry_in_s": delay,
                }
            )
            await asyncio.sleep(delay)

    _write_status(
        {
            **_status_base(gateway.token, gateway.auth_chat_id),
            "authenticated": False,
            "state": "error",
            "error": "startup_retry_exhausted",
            "attempts": MAX_STARTUP_RETRIES,
        }
    )
    raise SystemExit("Telegram startup failed after retries; see logs/telegram_hive.log and logs/telegram_hive_status.json")


def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [telegram_hive] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Telegram hive gateway")
    parser.add_argument("--token", default="")
    parser.add_argument("--chat-id", default="")
    args = parser.parse_args()

    env = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    token = args.token.strip() or str(env.get("GATOR_TG_BOT_TOKEN") or "").strip()
    chat_id = args.chat_id.strip() or str(env.get("GATOR_TG_AUTH_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise SystemExit("Missing telegram token/chat id")

    gateway = TelegramHiveGateway(token=token, auth_chat_id=chat_id)
    asyncio.run(_run_gateway_with_retry(gateway))


if __name__ == "__main__":
    _main()
