#!/usr/bin/env python3
"""Hybrid memory substrate for SQL metadata + LanceDB vectors.

Task 1 scaffold:
- SQLite stores relational metadata and traceability.
- LanceDB stores semantic vectors for search.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import pyarrow as pa

from memory_core import GatorMemoryCore

GATOR_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT = GATOR_ROOT / "research" / "scholar_sense"
SQLITE_DB_FILE = RESEARCH_ROOT / "hybrid_registry.db"
CHUNK_TABLE = "hybrid_chunk_registry"
COMMUNITY_TABLE = "hybrid_community_registry"

LANCE_CHUNK_TABLE = "scholar_memory"
LANCE_COMMUNITY_TABLE = "community_memory"


class HybridMemoryError(RuntimeError):
    pass


class HybridMemoryStore:
    def __init__(self, server_url: str = "http://127.0.0.1:8081") -> None:
        shared_db = os.environ.get("GATOR_SHARED_DB", "").strip()
        db_path = Path(shared_db) if shared_db else (GATOR_ROOT / "db")
        self.db = lancedb.connect(str(db_path))
        self.mem = GatorMemoryCore(db_path=db_path, server_url=server_url)

        RESEARCH_ROOT.mkdir(parents=True, exist_ok=True)
        self.sqlite_path = SQLITE_DB_FILE
        self._init_sqlite()

    def _init_sqlite(self) -> None:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CHUNK_TABLE} (
                    id TEXT PRIMARY KEY,
                    lance_id TEXT NOT NULL UNIQUE,
                    source_path TEXT NOT NULL,
                    node_ids_json TEXT NOT NULL,
                    text_preview TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {COMMUNITY_TABLE} (
                    community_id INTEGER PRIMARY KEY,
                    label TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    node_count INTEGER NOT NULL,
                    update_reason TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    vector_row_id TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _table_names(self) -> set[str]:
        raw = self.db.list_tables()
        if hasattr(raw, "tables"):
            raw = getattr(raw, "tables")
        names: set[str] = set()
        for item in raw:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, (list, tuple)) and item:
                names.add(str(item[0]))
            else:
                names.add(str(item))
        return names

    def _community_schema_for_dim(self, dim: int) -> pa.Schema:
        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("community_id", pa.int32()),
                pa.field("label", pa.string()),
                pa.field("signature", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
                pa.field("created_at", pa.float64()),
            ]
        )

    def _open_or_create_community_table(self, dim: int):
        names = self._table_names()
        if LANCE_COMMUNITY_TABLE not in names:
            return self.db.create_table(
                LANCE_COMMUNITY_TABLE,
                schema=self._community_schema_for_dim(dim),
                mode="create",
            )
        return self.db.open_table(LANCE_COMMUNITY_TABLE)

    def register_chunk_metadata(
        self,
        *,
        lance_id: str,
        source_path: str,
        node_ids: list[str],
        text: str,
        created_at: float,
    ) -> str:
        names = self._table_names()
        if LANCE_CHUNK_TABLE not in names:
            raise HybridMemoryError(f"Missing Lance table: {LANCE_CHUNK_TABLE}")

        table = self.db.open_table(LANCE_CHUNK_TABLE)
        try:
            rows = table.to_arrow().to_pylist()
            exists = any(str(row.get("id") or "") == str(lance_id) for row in rows)
        except Exception as exc:
            raise HybridMemoryError(f"Failed validating Lance row existence: {exc}") from exc

        if not exists:
            raise HybridMemoryError(f"No matching Lance vector row for lance_id={lance_id}")

        row_id = str(uuid.uuid4())
        text_preview = " ".join((text or "").split())[:280]
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {CHUNK_TABLE}
                (id, lance_id, source_path, node_ids_json, text_preview, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    lance_id,
                    source_path,
                    json.dumps(node_ids, ensure_ascii=True),
                    text_preview,
                    float(created_at),
                ),
            )
            conn.commit()
        return row_id

    def index_community_label(
        self,
        *,
        community_id: int,
        label: str,
        signature: str,
        node_count: int,
        update_reason: str,
        snippets: list[str],
    ) -> str:
        payload_text = (
            f"community_id={community_id}\n"
            f"label={label}\n"
            f"signature={signature}\n"
            + "\n".join(snippets[:12])
        )
        vector, _ = self.mem._embed_text(payload_text)
        dim = len(vector)
        table = self._open_or_create_community_table(dim)

        row_id = str(uuid.uuid4())
        try:
            table.delete(f"community_id = {int(community_id)}")
        except Exception:
            pass
        table.add(
            [
                {
                    "id": row_id,
                    "community_id": int(community_id),
                    "label": label,
                    "signature": signature,
                    "text": payload_text,
                    "vector": np.asarray(vector, dtype=np.float32).tolist(),
                    "created_at": time.time(),
                }
            ]
        )

        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {COMMUNITY_TABLE}
                (community_id, label, signature, node_count, update_reason, updated_at, vector_row_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(community_id),
                    label,
                    signature,
                    int(node_count),
                    update_reason,
                    time.time(),
                    row_id,
                ),
            )
            conn.commit()

        return row_id

    def semantic_search_communities(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        names = self._table_names()
        if LANCE_COMMUNITY_TABLE not in names:
            return []

        table = self.db.open_table(LANCE_COMMUNITY_TABLE)
        qvec, _ = self.mem._embed_text(query)
        rows = table.search(np.asarray(qvec, dtype=np.float32).tolist()).limit(max(1, top_k)).to_list()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "community_id": int(row.get("community_id", -1)),
                    "label": str(row.get("label") or ""),
                    "signature": str(row.get("signature") or ""),
                    "distance": float(row.get("_distance", 0.0) or 0.0),
                }
            )
        return out

    def audit_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.sqlite_path) as conn:
            chunk_count = int(conn.execute(f"SELECT COUNT(*) FROM {CHUNK_TABLE}").fetchone()[0])
            community_count = int(conn.execute(f"SELECT COUNT(*) FROM {COMMUNITY_TABLE}").fetchone()[0])
        return {
            "sql_chunks": chunk_count,
            "sql_communities": community_count,
        }
