#!/home/user/Gator/venv/bin/python3
"""Project Gator - Step 3: Direct-link LanceDB memory substrate.

This module stores memories in LanceDB and generates embeddings by calling
local llama-server endpoints backed by the 1.5B chassis model.

No secondary embedding model is used.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

import lancedb
import numpy as np
import pyarrow as pa

GATOR_ROOT = Path.home() / "Gator"
DB_ROOT = GATOR_ROOT / "db"
TABLE_NAME = "gator_memory"
DEFAULT_SERVER = "http://127.0.0.1:8080"


class MemoryCoreError(RuntimeError):
    """Raised for memory substrate runtime errors."""


@dataclass
class IngestResult:
    id: str
    dimension: int
    endpoint_used: str
    table: str


class GatorMemoryCore:
    def __init__(self, db_path: Path = DB_ROOT, server_url: str = DEFAULT_SERVER) -> None:
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
