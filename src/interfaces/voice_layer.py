#!/home/user/Gator/venv/bin/python3
"""Phase 4 voice layer: CPU Whisper STT + Piper TTS.

Voice is a PRIME-ONLY privilege.  Any clone or non-Prime entity that
calls transcribe_wav() or synthesize_to_wav() receives a VoiceHardBlock
and is redirected to the text logger.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel

GATOR_ROOT = Path.home() / "Gator"
VOICE_DIR = GATOR_ROOT / "models" / "voice"
PIPER_MODEL = VOICE_DIR / "en_US-lessac-medium.onnx"
PIPER_CONFIG = VOICE_DIR / "en_US-lessac-medium.onnx.json"
VOICE_LOG = GATOR_ROOT / "logs" / "voice_text_log.jsonl"

# Entity names that are allowed to use the voice layer.
_PRIME_ENTITY_NAMES: frozenset[str] = frozenset({"", "gator-prime", "prime", "gator"})


def _is_prime() -> bool:
    """Return True only when running as Gator-Prime."""
    node_name = os.environ.get("GATOR_NODE_NAME", "").strip().lower()
    return node_name in _PRIME_ENTITY_NAMES


class VoiceLayerError(RuntimeError):
    pass


class VoiceHardBlock(VoiceLayerError):
    """Raised when a non-Prime entity attempts to use the voice layer.

    Callers should catch this and route to the text logger instead.
    """

    def __init__(self, entity: str) -> None:
        self.entity = entity
        super().__init__(
            f"HardBlock: Voice layer is restricted to Gator-Prime. "
            f"Entity '{entity}' is TEXT-ONLY. Request diverted to text logger."
        )


def _text_log_fallback(text: str, entity: str | None = None) -> dict[str, Any]:
    """Write text to the voice text-log file and return a status dict."""
    VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"entity": entity or os.environ.get("GATOR_NODE_NAME", "unknown"), "text": text}
    with VOICE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return {"hard_block": True, "logged_text": text, "entity": entry["entity"]}


class VoiceLayer:
    def __init__(self, whisper_model_size: str = "tiny", whisper_compute_type: str = "int8") -> None:
        self.whisper_model_size = whisper_model_size
        self.whisper_compute_type = whisper_compute_type
        self._whisper: WhisperModel | None = None
        # Eagerly check identity so callers can inspect before making calls.
        self.voice_disabled: bool = not _is_prime()
        self._entity: str = os.environ.get("GATOR_NODE_NAME", "Gator-Prime")

    def ensure_piper_model(self) -> dict[str, str]:
        VOICE_DIR.mkdir(parents=True, exist_ok=True)

        model_url = (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "en/en_US/lessac/medium/en_US-lessac-medium.onnx"
        )
        config_url = model_url + ".json"

        if not PIPER_MODEL.exists():
            subprocess.run(["curl", "-L", "-o", str(PIPER_MODEL), model_url], check=True)
        if not PIPER_CONFIG.exists():
            subprocess.run(["curl", "-L", "-o", str(PIPER_CONFIG), config_url], check=True)

        return {"model": str(PIPER_MODEL), "config": str(PIPER_CONFIG)}

    def _whisper_model(self) -> WhisperModel:
        if self._whisper is None:
            self._whisper = WhisperModel(
                self.whisper_model_size,
                device="cpu",
                compute_type=self.whisper_compute_type,
            )
        return self._whisper

    def transcribe_wav(self, wav_path: Path, language: str = "en") -> dict[str, Any]:
        if self.voice_disabled:
            raise VoiceHardBlock(self._entity)
        if not wav_path.exists():
            raise VoiceLayerError(f"Audio file not found: {wav_path}")

        model = self._whisper_model()
        segments, info = model.transcribe(str(wav_path), language=language, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()

        return {
            "text": text,
            "language": getattr(info, "language", language),
            "duration": float(getattr(info, "duration", 0.0) or 0.0),
        }

    def synthesize_to_wav(self, text: str, out_path: Path) -> dict[str, Any]:
        if self.voice_disabled:
            raise VoiceHardBlock(self._entity)
        self.ensure_piper_model()
        if not text.strip():
            raise VoiceLayerError("Cannot synthesize empty text")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(GATOR_ROOT / "venv" / "bin" / "piper"),
            "--model",
            str(PIPER_MODEL),
            "--config",
            str(PIPER_CONFIG),
            "--output-file",
            str(out_path),
            "--sentence-silence",
            "0.08",
        ]
        proc = subprocess.run(cmd, input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise VoiceLayerError(f"Piper failed: {(proc.stderr or proc.stdout).decode('utf-8', errors='replace')}")

        return {"output_wav": str(out_path), "bytes": out_path.stat().st_size}

    def text_log_fallback(self, text: str) -> dict[str, Any]:
        """Divert text to the voice log when voice is hard-blocked."""
        return _text_log_fallback(text, entity=self._entity)


def _main() -> None:
    parser = argparse.ArgumentParser(description="VoiceLayer utility")
    parser.add_argument("--speak", type=str)
    parser.add_argument("--speak-out", type=str, default=str(GATOR_ROOT / "logs" / "phase4_tts.wav"))
    parser.add_argument("--transcribe", type=str)
    args = parser.parse_args()

    vl = VoiceLayer()
    out: dict[str, Any] = {}

    if args.speak:
        out["speak"] = vl.synthesize_to_wav(args.speak, Path(args.speak_out))
    if args.transcribe:
        out["transcribe"] = vl.transcribe_wav(Path(args.transcribe))

    if not out:
        parser.error("Provide --speak and/or --transcribe")

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
