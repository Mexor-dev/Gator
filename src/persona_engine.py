#!/usr/bin/env python3
"""Gator Persona Engine – living trait sliders + Lance-backed self-reflection store.

Six scalar traits (0.0–1.0) steer the 35B logic donor's reasoning style at prompt
injection time.  After each generation, a reflection snapshot is written to LanceDB
so Gator can observe and eventually tune its own preferences over time.

Traits
------
curiosity   – 0 = focused/methodical,  1 = wide-ranging/exploratory
directness  – 0 = verbose/elaborate,   1 = terse/surgical
agency      – 0 = passive/reactive,    1 = assertive/execution-first
caution     – 0 = bold/decisive,        1 = careful/hedged
creativity  – 0 = conventional,         1 = inventive/lateral
empathy     – 0 = detached/clinical,    1 = warm/relational
precision   – 0 = approximate/broad,    1 = exact/strict
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
import re
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import pyarrow as pa

GATOR_ROOT = Path(__file__).resolve().parents[1]

TRAIT_DEFAULTS: dict[str, float] = {
    "curiosity": 0.5,
    "directness": 0.5,
    "agency": 0.5,
    "caution": 0.5,
    "creativity": 0.5,
    "empathy": 0.5,
    "precision": 0.5,
}

# Low / high descriptions for each trait used when building the steering fragment.
_TRAIT_DESC: dict[str, tuple[str, str]] = {
    "curiosity":   ("focused and methodical", "wide-ranging and exploratory"),
    "directness":  ("thorough and elaborative", "concise and surgical"),
    "agency":      ("reactive and deferential", "assertive and execution-focused"),
    "caution":     ("bold and decisive", "careful and hedged"),
    "creativity":  ("conventional and reliable", "inventive and lateral"),
    "empathy":     ("detached and clinical", "warm and relational"),
    "precision":   ("approximate and broad", "exact and strictly literal"),
}

_GENERIC_PHRASE_REPLACEMENTS: list[tuple[str, str]] = [
    (r"(?i)\bi\s*[' ]?m\s+here\s+to\s+help\b", "Logic applied. What's the next objective?"),
    (r"(?i)^\s*understood\b[\s,.:;-]*", "Task acknowledged. "),
    (r"(?i)\bright\s+away\b", "moving to execution"),
    (r"(?i)\bi\s+can\s+help\s+you\s+with\s+that\b", "Logic applied. What's the next objective?"),
]
REFLECTION_TABLE = "persona_reflections"
REFLECTION_DIM = 256  # lightweight fallback-only embedding dimension


class PersonaEngine:
    """Manages trait state and the Lance-backed reflection store."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or GATOR_ROOT
        self._trait_file = self._root / "config" / "persona_traits.json"
        self._trait_file.parent.mkdir(parents=True, exist_ok=True)
        db_path = self._root / "db"
        db_path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(db_path))
        self._traits: dict[str, float] = self._load_traits()

    # ------------------------------------------------------------------
    # Trait persistence
    # ------------------------------------------------------------------

    def _load_traits(self) -> dict[str, float]:
        if self._trait_file.exists():
            try:
                raw = json.loads(self._trait_file.read_text(encoding="utf-8"))
                merged = dict(TRAIT_DEFAULTS)
                for k, v in raw.items():
                    if k in TRAIT_DEFAULTS:
                        merged[k] = float(max(0.0, min(1.0, v)))
                return merged
            except Exception:
                pass
        return dict(TRAIT_DEFAULTS)

    def _save_traits(self) -> None:
        self._trait_file.write_text(
            json.dumps(self._traits, indent=2), encoding="utf-8"
        )

    def current_traits(self) -> dict[str, float]:
        return dict(self._traits)

    def set_traits(self, updates: dict[str, float]) -> dict[str, float]:
        """Apply partial trait updates (0.0–1.0 clamped) and persist."""
        for k, v in updates.items():
            if k in TRAIT_DEFAULTS:
                self._traits[k] = float(max(0.0, min(1.0, v)))
        self._save_traits()
        return dict(self._traits)

    # ------------------------------------------------------------------
    # Steering fragment injected into 35B donor system prompt
    # ------------------------------------------------------------------

    def build_steering_fragment(self) -> str:
        """Return a natural-language steering paragraph derived from current traits."""
        lines: list[str] = []
        for trait, (low, high) in _TRAIT_DESC.items():
            v = self._traits.get(trait, 0.5)
            if v <= 0.25:
                lines.append(f"Be {low}.")
            elif v >= 0.75:
                lines.append(f"Be {high}.")
            elif v < 0.5:
                lines.append(f"Lean {low}.")
            elif v > 0.5:
                lines.append(f"Lean {high}.")
            # At exactly 0.5, omit — neutral, no steering pressure.

        if not lines:
            return ""
        return "[Persona steering]\n" + "\n".join(lines)

    def refine_response(self, text: str, *, user_text: str = "", scratchpad: str = "") -> str:
        """Apply sovereign style filtering and contextual tone shaping."""
        out = (text or "").strip()
        if not out:
            return out

        for pattern, replacement in _GENERIC_PHRASE_REPLACEMENTS:
            out = re.sub(pattern, replacement, out)

        if out.lower().startswith("task acknowledged") and "moving to execution" not in out.lower():
            out = out.strip()
            if not out.endswith("."):
                out += "."
            out += " Moving to execution."

        query = (user_text or "").lower()
        asks_runtime = any(k in query for k in ("vram", "worker", "workers", "status", "health", "state"))
        if asks_runtime:
            low = out.lower()
            has_runtime_phrase = any(k in low for k in ("vram", "worker", "workers", "runtime", "telemetry"))
            if not has_runtime_phrase:
                scratch_hint = ""
                if scratchpad and len(scratchpad.strip()) > 0:
                    scratch_hint = " Scratchpad sync is active."
                out = f"{out.rstrip('.')} Runtime telemetry is live for VRAM and worker state.{scratch_hint}".strip()

        return " ".join(out.split())

    # ------------------------------------------------------------------
    # Reflection store
    # ------------------------------------------------------------------

    def _fallback_embedding(self, text: str) -> list[float]:
        vec = np.zeros(REFLECTION_DIM, dtype=np.float32)
        tokens = [t for t in text.lower().split() if len(t) >= 2]
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            idx = int.from_bytes(digest[:4], "little") % REFLECTION_DIM
            sign = 1.0 if (digest[4] & 1) else -1.0
            weight = 1.0 + ((digest[5] % 7) / 10.0)
            vec[idx] += sign * weight
        norm = float(np.linalg.norm(vec))
        if norm > 1e-8:
            vec /= norm
        return vec.tolist()

    def _schema(self) -> pa.Schema:
        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), REFLECTION_DIM)),
                pa.field("traits_snapshot", pa.string()),  # JSON blob
                pa.field("created_at", pa.float64()),
            ]
        )

    def _open_or_create_table(self):
        names = set(self._db.table_names())
        if REFLECTION_TABLE not in names:
            return self._db.create_table(
                REFLECTION_TABLE, schema=self._schema(), mode="create"
            )
        return self._db.open_table(REFLECTION_TABLE)

    def record_reflection(self, text: str) -> str:
        """Store a reflection of generated output alongside the current trait snapshot.

        Returns the new row id.
        """
        row_id = str(uuid.uuid4())
        vector = self._fallback_embedding(text)
        table = self._open_or_create_table()
        table.add(
            pa.table(
                {
                    "id": pa.array([row_id], type=pa.string()),
                    "text": pa.array([text[:2000]], type=pa.string()),
                    "vector": pa.array([vector], type=pa.list_(pa.float32(), REFLECTION_DIM)),
                    "traits_snapshot": pa.array(
                        [json.dumps(self._traits)], type=pa.string()
                    ),
                    "created_at": pa.array([time.time()], type=pa.float64()),
                }
            )
        )
        return row_id

    def get_reflections(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent reflections sorted by creation time descending."""
        try:
            table = self._open_or_create_table()
            rows = (
                table.search()
                .limit(max(1, limit) * 2)  # over-fetch then trim after sort
                .select(["id", "text", "traits_snapshot", "created_at"])
                .to_list()
            )
            rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
            out = []
            for row in rows[:limit]:
                traits = {}
                try:
                    traits = json.loads(row.get("traits_snapshot") or "{}")
                except Exception:
                    pass
                out.append(
                    {
                        "id": row.get("id"),
                        "text": row.get("text", "")[:400],
                        "traits": traits,
                        "created_at": row.get("created_at"),
                    }
                )
            return out
        except Exception:
            return []

    def semantic_search_reflections(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Vector-search the reflection store for text similar to *query*."""
        try:
            vec = self._fallback_embedding(query)
            table = self._open_or_create_table()
            rows = (
                table.search(vec)
                .limit(max(1, limit))
                .select(["id", "text", "traits_snapshot", "created_at"])
                .to_list()
            )
            out = []
            for row in rows:
                traits = {}
                try:
                    traits = json.loads(row.get("traits_snapshot") or "{}")
                except Exception:
                    pass
                out.append(
                    {
                        "id": row.get("id"),
                        "text": row.get("text", "")[:400],
                        "traits": traits,
                        "score": row.get("_distance"),
                        "created_at": row.get("created_at"),
                    }
                )
            return out
        except Exception:
            return []
