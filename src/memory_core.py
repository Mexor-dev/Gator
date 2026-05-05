#!/usr/bin/env python3
"""Project Gator - Step 3: Direct-link LanceDB memory substrate.

This module stores memories in LanceDB and generates embeddings by calling
local llama-server endpoints backed by the 1.5B chassis model.

No secondary embedding model is used.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
import hashlib
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from urllib import error, request

import lancedb
import numpy as np
import pandas as pd
import pyarrow as pa

GATOR_ROOT = Path(__file__).resolve().parents[1]
DB_ROOT = GATOR_ROOT / "db"
TABLE_NAME = "gator_memory"
SCRATCHPAD_TABLE = "transient_scratchpad"
SCRATCHPAD_DEFAULT_DIM = 1536  # Qwen2.5-1.5B embedding dimension
DEFAULT_SERVER = "http://127.0.0.1:8081"


class MemoryCoreError(RuntimeError):
    """Raised for memory substrate runtime errors."""


@dataclass
class IngestResult:
    id: str
    dimension: int
    endpoint_used: str
    table: str


class GatorMemoryCore:
    def __init__(self, db_path: Path | None = None, server_url: str = DEFAULT_SERVER) -> None:
        shared_db = os.environ.get("GATOR_SHARED_DB", "").strip()
        if db_path is None:
            db_path = Path(shared_db) if shared_db else DB_ROOT
        self.db_path = db_path
        self.server_url = server_url.rstrip("/")
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.db_path))

    def _post_json(self, url: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise MemoryCoreError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except error.URLError as exc:
            raise MemoryCoreError(f"Cannot reach llama-server at {url}: {exc}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryCoreError(f"Non-JSON response from {url}: {raw[:300]}") from exc

    def _parse_embedding(self, payload: dict[str, Any]) -> list[float]:
        if isinstance(payload.get("embedding"), list):
            return [float(x) for x in payload["embedding"]]

        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            emb = data[0].get("embedding")
            if isinstance(emb, list):
                return [float(x) for x in emb]

        raise MemoryCoreError("Embedding payload shape not recognized.")

    def _fallback_embedding(self, text: str, dim: int = SCRATCHPAD_DEFAULT_DIM) -> list[float]:
        vec = np.zeros(dim, dtype=np.float32)
        tokens = re.findall(r"[A-Za-z0-9_]{2,}", text.lower())
        if not tokens:
            return vec.tolist()

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % dim
            sign = 1.0 if (digest[4] & 1) else -1.0
            weight = 1.0 + ((digest[5] % 7) / 10.0)
            vec[idx] += sign * weight

        norm = float(np.linalg.norm(vec))
        if norm > 1e-8:
            vec /= norm
        return vec.tolist()

    def _embed_text(self, text: str) -> tuple[list[float], str]:
        """Call llama-server embedding endpoints and return (vector, endpoint)."""
        if not text.strip():
            raise MemoryCoreError("Cannot embed empty text.")

        attempts = [
            (f"{self.server_url}/embedding", {"content": text}),
            (f"{self.server_url}/embedding", {"input": text}),
            (f"{self.server_url}/v1/embeddings", {"input": text}),
        ]

        last_exc: Exception | None = None
        for url, payload in attempts:
            try:
                data = self._post_json(url, payload)
                vec = self._parse_embedding(data)
                if not vec:
                    raise MemoryCoreError(f"Empty embedding from {url}")
                return vec, url
            except Exception as exc:
                last_exc = exc
                continue

        fallback = self._fallback_embedding(text)
        if fallback:
            return fallback, "local://lexical-hash-fallback"
        raise MemoryCoreError(f"All embedding endpoint attempts failed: {last_exc}")

    def _schema_for_dim(self, dim: int) -> pa.Schema:
        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
                pa.field("created_at", pa.float64()),
            ]
        )

    def _open_or_create_table(self, dim: int):
        names = set(self.db.table_names())
        if TABLE_NAME not in names:
            schema = self._schema_for_dim(dim)
            return self.db.create_table(TABLE_NAME, schema=schema, mode="create")
        return self.db.open_table(TABLE_NAME)

    def ingest_document(self, text: str) -> IngestResult:
        """Ingest text into LanceDB using chassis-generated embeddings."""
        vector, endpoint = self._embed_text(text)
        dim = len(vector)
        table = self._open_or_create_table(dim)

        row_id = str(uuid.uuid4())
        row = {
            "id": row_id,
            "text": text,
            "vector": np.asarray(vector, dtype=np.float32).tolist(),
            "created_at": time.time(),
        }

        try:
            table.add([row])
        except Exception as exc:
            raise MemoryCoreError(f"Failed writing to LanceDB table '{TABLE_NAME}': {exc}") from exc

        return IngestResult(
            id=row_id,
            dimension=dim,
            endpoint_used=endpoint,
            table=TABLE_NAME,
        )

    def count(self) -> int:
        names = set(self.db.table_names())
        if TABLE_NAME not in names:
            return 0
        table = self.db.open_table(TABLE_NAME)
        return int(table.count_rows())

    def flush_buffer(self, transient_tables: list[str] | None = None) -> dict[str, int]:
        """Flush transient tables to guarantee zero-bloat next-cycle startup."""
        table_names = set(self.db.table_names())
        targets = transient_tables or [SCRATCHPAD_TABLE, "transient_buffer", "transient_session"]
        deleted: dict[str, int] = {}
        for name in targets:
            if name not in table_names:
                deleted[name] = 0
                continue
            table = self.db.open_table(name)
            try:
                row_count = int(table.count_rows())
            except Exception:
                row_count = 0
            if row_count > 0:
                try:
                    table.delete("1 = 1")
                except Exception:
                    # Some backends are strict about predicates; overwrite as fallback.
                    self.db.drop_table(name)
                    table = self.db.create_table(name, data=[], mode="create")
            deleted[name] = row_count
        return deleted

    def compact_and_vacuum(self, tables: list[str] | None = None) -> dict[str, str]:
        """Attempt native LanceDB optimization for latency stability."""
        table_names = set(self.db.table_names())
        targets = tables or sorted(table_names)
        results: dict[str, str] = {}
        for name in targets:
            if name not in table_names:
                results[name] = "missing"
                continue
            table = self.db.open_table(name)
            status = "noop"
            try:
                if hasattr(table, "optimize"):
                    table.optimize()
                    status = "optimized"
                elif hasattr(table, "compact_files"):
                    table.compact_files()
                    status = "compacted"
            except Exception as exc:
                status = f"error:{exc}"
            results[name] = status
        return results

    def chunk_research_text(
        self,
        text: str,
        max_chars: int = 700,
        overlap_chars: int = 80,
        max_chunks: int = 6,
    ) -> list[str]:
        cleaned = " ".join(text.split())
        if not cleaned:
            return []

        units = [
            unit.strip()
            for unit in re.split(r"(?<=[.!?])\s+|\n+", cleaned)
            if unit.strip()
        ]
        if not units:
            return [cleaned[:max_chars]]

        chunks: list[str] = []
        current = ""
        for unit in units:
            candidate = f"{current} {unit}".strip() if current else unit
            if current and len(candidate) > max_chars:
                chunks.append(current)
                if len(chunks) >= max_chunks:
                    break
                overlap = current[-overlap_chars:].strip()
                current = f"{overlap} {unit}".strip() if overlap else unit
            else:
                current = candidate
        if current and len(chunks) < max_chunks:
            chunks.append(current)
        return chunks[:max_chunks]

    def prepare_vector_snippets(
        self,
        text: str,
        label: str = "",
        focus_terms: list[str] | None = None,
        max_snippets: int = 3,
    ) -> list[str]:
        chunks = self.chunk_research_text(text)
        if not chunks:
            return []

        focus_terms = [term.lower() for term in (focus_terms or [])]

        def score(chunk: str) -> tuple[int, int, int]:
            lower = chunk.lower()
            focus_hits = sum(lower.count(term) for term in focus_terms)
            numeric_hits = len(re.findall(r"\b\d+(?:\.\d+)?\b", chunk))
            return (focus_hits, numeric_hits, len(chunk))

        ranked = sorted(chunks, key=score, reverse=True)[:max_snippets]
        snippets: list[str] = []
        for idx, chunk in enumerate(ranked, start=1):
            prefix = f"{label} :: segment {idx}" if label else f"segment {idx}"
            snippets.append(f"{prefix}\n{chunk}")
        return snippets

    def synthesize_research_notes(
        self,
        text: str,
        label: str = "Research digest",
        focus_terms: list[str] | None = None,
    ) -> str:
        snippets = self.prepare_vector_snippets(text, label=label, focus_terms=focus_terms)
        if not snippets:
            return label
        bullets = [f"- {snippet.splitlines()[-1]}" for snippet in snippets]
        return f"{label}\n" + "\n".join(bullets)

    # ------------------------------------------------------------------
    # Lance of Larger Thinking — Transient Scratchpad API
    # ------------------------------------------------------------------

    def _scratchpad_schema(self, dim: int) -> pa.Schema:
        return pa.schema([
            pa.field("session_id", pa.string()),
            pa.field("step_number", pa.int32()),
            pa.field("thought_chunk", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ])

    def _open_or_create_scratchpad(self, dim: int = SCRATCHPAD_DEFAULT_DIM):
        names = set(self.db.table_names())
        if SCRATCHPAD_TABLE not in names:
            schema = self._scratchpad_schema(dim)
            return self.db.create_table(SCRATCHPAD_TABLE, schema=schema, mode="create")
        return self.db.open_table(SCRATCHPAD_TABLE)

    def init_scratchpad(self, session_id: str) -> None:
        """Create or clear the transient buffer for a new generation session."""
        table = self._open_or_create_scratchpad()
        try:
            table.delete(f"session_id = '{session_id}'")
        except Exception:
            pass  # Empty table on first use — no rows to delete

    def commit_thought(self, session_id: str, step: int, text: str) -> None:
        """
        Embed and persist one intermediate reasoning chunk to the scratchpad.
        Falls back to a zero-vector if the embedding server is unreachable so
        the structural write always succeeds.
        """
        if not text.strip():
            return
        try:
            vector, _ = self._embed_text(text)
            dim = len(vector)
        except MemoryCoreError:
            dim = SCRATCHPAD_DEFAULT_DIM
            vector = [0.0] * dim
        table = self._open_or_create_scratchpad(dim)
        row = {
            "session_id": session_id,
            "step_number": step,
            "thought_chunk": text.strip(),
            "vector": np.asarray(vector, dtype=np.float32).tolist(),
        }
        try:
            table.add([row])
        except Exception as exc:
            raise MemoryCoreError(f"Failed writing thought to scratchpad: {exc}") from exc

    def retrieve_context(self, session_id: str, current_step: int) -> str:
        """
        Return a formatted string of all committed reasoning steps whose
        step_number < current_step for the given session.
        Returns an empty string when no prior steps exist.
        """
        names = set(self.db.table_names())
        if SCRATCHPAD_TABLE not in names:
            return ""
        table = self.db.open_table(SCRATCHPAD_TABLE)
        try:
            df: pd.DataFrame = table.to_pandas()
        except Exception:
            return ""
        if df.empty:
            return ""
        mask = (df["session_id"] == session_id) & (df["step_number"] < current_step)
        prior = df[mask].sort_values("step_number")
        if prior.empty:
            return ""
        parts = [
            f"[Step {int(row['step_number'])} Analysis]:\n{row['thought_chunk']}"
            for _, row in prior.iterrows()
        ]
        return "\n\n".join(parts)

    def flush_scratchpad(self, session_id: str) -> int:
        """
        Delete all scratchpad rows for this session and return the row count
        that was deleted.  Must be called after successful response delivery
        to prevent disk bloat.
        """
        names = set(self.db.table_names())
        if SCRATCHPAD_TABLE not in names:
            return 0
        table = self.db.open_table(SCRATCHPAD_TABLE)
        try:
            df: pd.DataFrame = table.to_pandas()
            count = int((df["session_id"] == session_id).sum()) if not df.empty else 0
            if count > 0:
                table.delete(f"session_id = '{session_id}'")
            return count
        except Exception as exc:
            raise MemoryCoreError(
                f"Failed flushing scratchpad for session {session_id!r}: {exc}"
            ) from exc

    def _scratchpad_count(self, session_id: str) -> int:
        """Return the number of scratchpad rows for a given session. Test helper."""
        names = set(self.db.table_names())
        if SCRATCHPAD_TABLE not in names:
            return 0
        table = self.db.open_table(SCRATCHPAD_TABLE)
        try:
            df: pd.DataFrame = table.to_pandas()
            if df.empty:
                return 0
            return int((df["session_id"] == session_id).sum())
        except Exception:
            return 0


def _main() -> None:
    parser = argparse.ArgumentParser(description="Project Gator memory substrate")
    parser.add_argument("--ingest", type=str, help="Text to ingest")
    parser.add_argument("--count", action="store_true", help="Print row count")
    parser.add_argument("--server", type=str, default=DEFAULT_SERVER, help="llama-server URL")
    args = parser.parse_args()

    core = GatorMemoryCore(server_url=args.server)

    if args.count:
        print(core.count())
        return

    if args.ingest is None:
        parser.error("Provide --ingest TEXT or --count")

    result = core.ingest_document(args.ingest)
    print(json.dumps(result.__dict__, indent=2))


if __name__ == "__main__":
    _main()
