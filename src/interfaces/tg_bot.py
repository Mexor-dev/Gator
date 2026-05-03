#!/home/user/Gator/venv/bin/python3
"""Phase 4 Pocket Remote: Telegram bot bridge for text and voice."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import request

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from voice_layer import VoiceLayer

GATOR_ROOT = Path.home() / "Gator"
BRIDGE_URL = "http://127.0.0.1:8090/generate"


class TgBotError(RuntimeError):
    pass


def _post_json(url: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


class GatorTelegramBot:
    def __init__(self, token: str, bridge_url: str = BRIDGE_URL) -> None:
        self.token = token
        self.bridge_url = bridge_url
        self.voice = VoiceLayer()

    async def _ask_gator(self, text: str) -> str:
        payload = {"prompt": text, "max_tokens": 180, "temperature": 0.2}
        data = _post_json(self.bridge_url, payload)
        return str(data.get("text") or data.get("output") or data.get("response") or "")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Gator Pocket Remote online. Commands: /scan /wakeup")

    async def wakeup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        proc = subprocess.run(["bash", str(GATOR_ROOT / "wakeup")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = "wakeup completed" if proc.returncode == 0 else "wakeup failed"
        await update.message.reply_text(msg)

    async def scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pulse = GATOR_ROOT / "src" / "pulse_check.py"
        if not pulse.exists():
            await update.message.reply_text("pulse_check.py not deployed yet")
            return
        proc = subprocess.run(
            [str(GATOR_ROOT / "venv" / "bin" / "python"), str(pulse)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        await update.message.reply_text(out[:3500] if out else "scan complete")

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_text = (update.message.text or "").strip()
        if not user_text:
            return
        answer = await self._ask_gator(user_text)
        if not answer:
            answer = "No response from Gator bridge."
        await update.message.reply_text(answer[:3500])

    async def voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message.voice:
            return

        voice_file = await update.message.voice.get_file()
        with tempfile.TemporaryDirectory(prefix="gator_tg_") as td:
            td_path = Path(td)
            in_ogg = td_path / "in.ogg"
            in_wav = td_path / "in.wav"
            out_wav = td_path / "reply.wav"

            await voice_file.download_to_drive(str(in_ogg))
            ff = subprocess.run(
                ["ffmpeg", "-y", "-i", str(in_ogg), str(in_wav)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if ff.returncode != 0:
                await update.message.reply_text("Voice decode failed (ffmpeg).")
                return

            stt = self.voice.transcribe_wav(in_wav)
            query = stt.get("text") or ""
            if not query:
                await update.message.reply_text("Could not transcribe voice note.")
                return

            answer = await self._ask_gator(query)
            if not answer or not answer.strip():
                answer = "No response from Gator bridge."

            self.voice.synthesize_to_wav(answer, out_wav)
            await update.message.reply_voice(voice=open(out_wav, "rb"), caption=f"Heard: {query[:120]}")

    def run(self) -> None:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("scan", self.scan))
        app.add_handler(CommandHandler("wakeup", self.wakeup))
        app.add_handler(MessageHandler(filters.VOICE, self.voice_message))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def simulate_voice_roundtrip(input_text: str, bridge_url: str = BRIDGE_URL) -> dict[str, Any]:
    voice = VoiceLayer()
    logs = GATOR_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    ts = int(time.time())
    in_wav = logs / f"phase4_in_{ts}.wav"
    out_wav = logs / f"phase4_out_{ts}.wav"

    voice.synthesize_to_wav(input_text, in_wav)
    stt = voice.transcribe_wav(in_wav)
    heard = stt.get("text", "")

    payload = {"prompt": heard or input_text, "max_tokens": 120, "temperature": 0.2}
    reply = _post_json(bridge_url, payload)
    answer = str(reply.get("text") or reply.get("output") or "")
    if not answer.strip():
        answer = "No response from Gator bridge."

    voice.synthesize_to_wav(answer, out_wav)

    return {
        "input_text": input_text,
        "transcribed": heard,
        "bridge_answer_preview": (answer or "")[:160],
        "reply_audio": str(out_wav),
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Telegram voice interface")
    parser.add_argument("--token", type=str, default=os.environ.get("GATOR_TG_BOT_TOKEN", ""))
    parser.add_argument("--bridge", type=str, default=BRIDGE_URL)
    parser.add_argument("--simulate-voice", type=str, help="Run local voice-note simulation without Telegram")
    args = parser.parse_args()

    if args.simulate_voice:
        print(json.dumps(simulate_voice_roundtrip(args.simulate_voice, bridge_url=args.bridge), indent=2))
        return

    if not args.token:
        raise SystemExit("[ERROR] Missing Telegram token. Set GATOR_TG_BOT_TOKEN or pass --token")

    bot = GatorTelegramBot(token=args.token, bridge_url=args.bridge)
    bot.run()


if __name__ == "__main__":
    _main()
