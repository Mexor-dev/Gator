#!/home/user/Gator/venv/bin/python3
"""Project Gator - Phase 2 Scholar Sense (Hybrid RAG).

Builds a CPU/SSD graph layer via graphify and a semantic vector layer via
LanceDB embeddings served by local llama-server.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import pyarrow as pa
from pypdf import PdfReader

from memory_core import GatorMemoryCore, MemoryCoreError

GATOR_ROOT = Path.home() / "Gator"
RESEARCH_ROOT = GATOR_ROOT / "research"
GRAPH_OUT = RESEARCH_ROOT / "graphify-out"
TABLE_NAME = "scholar_memory"
DEFAULT_SERVER = "http://127.0.0.1:8081"
GRAPHIFY_BIN = Path.home() / ".local" / "bin" / "graphify"
SIMILARITY_FLOOR = 0.25


class ScholarSenseError(RuntimeError):
    """Raised on scholar failures."""


@dataclass
class ScholarChunk:
    text: str
    node_ids: list[str]
    source_path: str


class ScholarSense:
    def __init__(self, server_url: str = DEFAULT_SERVER) -> None:
        self.server_url = server_url.rstrip("/")
        RESEARCH_ROOT.mkdir(parents=True, exist_ok=True)
        GRAPH_OUT.mkdir(parents=True, exist_ok=True)

        self.mem = GatorMemoryCore(server_url=self.server_url)
        self.db = lancedb.connect(str(GATOR_ROOT / "db"))

    def _schema_for_dim(self, dim: int) -> pa.Schema:
        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
                pa.field("node_ids", pa.list_(pa.string())),
                pa.field("source_path", pa.string()),
                pa.field("created_at", pa.float64()),
            ]
        )

    def _table_names(self) -> set[str]:
        raw = self.db.list_tables()
        if hasattr(raw, "tables"):
            raw = getattr(raw, "tables")
        names: set[str] = set()
        for item in raw:
            if isinstance(item, str):
                names.add(item)
                continue
            if isinstance(item, (list, tuple)) and item:
                names.add(str(item[0]))
                continue
            names.add(str(item))
        return names

    def _open_or_create_table(self, dim: int):
        names = self._table_names()
        if TABLE_NAME not in names:
            return self.db.create_table(TABLE_NAME, schema=self._schema_for_dim(dim), mode="create")
        return self.db.open_table(TABLE_NAME)

    def _run_graphify_update(self) -> dict[str, Any]:
        if not GRAPHIFY_BIN.exists():
            raise ScholarSenseError(f"graphify not found at {GRAPHIFY_BIN}")

        cmd = [str(GRAPHIFY_BIN), "update", str(RESEARCH_ROOT)]
        started = time.time()
        proc = subprocess.run(
            cmd,
            cwd=str(GATOR_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        duration = time.time() - started
        if proc.returncode != 0:
            raise ScholarSenseError(
                "graphify update failed: "
                f"rc={proc.returncode}, stderr={proc.stderr[-400:]}"
            )

        return {
            "returncode": proc.returncode,
            "duration_sec": round(duration, 3),
            "stdout_tail": proc.stdout[-400:],
        }

    def _load_graph(self) -> dict[str, Any]:
        graph_path = GRAPH_OUT / "graph.json"
        if not graph_path.exists():
            raise ScholarSenseError(f"graph.json missing at {graph_path}")
        return json.loads(graph_path.read_text(encoding="utf-8"))

    def _extract_text_from_pdf(self, pdf_path: Path) -> str:
        if not pdf_path.exists():
            raise ScholarSenseError(f"PDF not found: {pdf_path}")
        reader = PdfReader(str(pdf_path))
        texts: list[str] = []
        for page in reader.pages:
            texts.append((page.extract_text() or "").strip())
        text = "\n".join(x for x in texts if x)
        if not text.strip():
            raise ScholarSenseError(f"No extractable text found in {pdf_path}")
        return text

    def _chunk_text(self, text: str, chunk_words: int = 180) -> list[str]:
        words = text.split()
        if not words:
            return []
        chunks = []
        for i in range(0, len(words), chunk_words):
            chunk = " ".join(words[i : i + chunk_words])
            if chunk.strip():
                chunks.append(chunk)
        return chunks

    def _pick_god_nodes(self, question: str, max_nodes: int = 12) -> list[str]:
        graph = self._load_graph()
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        edges = graph.get("edges", []) if isinstance(graph, dict) else []

        if not isinstance(nodes, list) or not nodes:
            return []

        qterms = {w.lower() for w in question.split() if len(w) > 2}
        deg: dict[str, int] = {}
        for edge in edges if isinstance(edges, list) else []:
            if not isinstance(edge, dict):
                continue
            s = str(edge.get("source", ""))
            t = str(edge.get("target", ""))
            if s:
                deg[s] = deg.get(s, 0) + 1
            if t:
                deg[t] = deg.get(t, 0) + 1

        scored: list[tuple[float, str]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id", "")).strip()
            if not node_id:
                continue
            label = str(node.get("label") or node.get("name") or "").lower()
            overlap = 0
            if label and qterms:
                overlap = sum(1 for q in qterms if q in label)
            centrality = math.log1p(deg.get(node_id, 0))
            score = overlap * 2.0 + centrality
            scored.append((score, node_id))

        scored.sort(reverse=True)
        return [node_id for _, node_id in scored[:max_nodes]]

    def _embed(self, text: str) -> list[float]:
        vec, _ = self.mem._embed_text(text)
        return vec

    def ingest_pdf(self, pdf_path: Path) -> dict[str, Any]:
        text = self._extract_text_from_pdf(pdf_path)
        sidecar = RESEARCH_ROOT / f"{pdf_path.stem}.md"
        sidecar.write_text(text, encoding="utf-8")

        graph_info = self._run_graphify_update()

        chunks = self._chunk_text(text)
        if not chunks:
            raise ScholarSenseError("No chunks extracted from PDF text")

        default_nodes = self._pick_god_nodes(text, max_nodes=10)

        sample_vec = self._embed(chunks[0])
        dim = len(sample_vec)
        table = self._open_or_create_table(dim)

        rows = []
        for idx, chunk in enumerate(chunks):
            vec = sample_vec if idx == 0 else self._embed(chunk)
            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "text": chunk,
                    "vector": np.asarray(vec, dtype=np.float32).tolist(),
                    "node_ids": default_nodes,
                    "source_path": str(pdf_path),
                    "created_at": time.time(),
                }
            )

        table.add(rows)

        return {
            "pdf": str(pdf_path),
            "graph_source_sidecar": str(sidecar),
            "chunks": len(chunks),
            "vector_dim": dim,
            "graphify": graph_info,
            "assigned_node_ids": len(default_nodes),
            "table": TABLE_NAME,
        }

    def query(self, question: str, top_k: int = 8, token_cap: int = 768) -> dict[str, Any]:
        if token_cap > 768:
            token_cap = 768

        names = self._table_names()
        if TABLE_NAME not in names:
            raise ScholarSenseError("Scholar memory table missing, ingest first")

        table = self.db.open_table(TABLE_NAME)
        qvec = self._embed(question)
        god_nodes = self._pick_god_nodes(question, max_nodes=12)

        # Pull a broader candidate pool, then apply Vector Pivot filtering in Python.
        candidates = table.search(np.asarray(qvec, dtype=np.float32).tolist()).limit(max(top_k * 5, 20)).to_list()

        if not candidates:
            return {
                "question": question,
                "god_nodes": god_nodes,
                "selected_chunks": [],
                "token_cap": token_cap,
                "estimated_tokens_used": 0,
                "context": "",
                "strategy": "graphify_god_nodes -> lancedb_vector_pivot",
                "zero_context": True,
                "floor": SIMILARITY_FLOOR,
                "best_similarity": 0.0,
                "reason": "NO_CANDIDATES",
            }

        # LanceDB returns distance; map to a bounded similarity score.
        best_dist = float(candidates[0].get("_distance", 1e9) or 1e9)
        best_similarity = 1.0 / (1.0 + max(0.0, best_dist))

        # Enforce a conservative hallucination floor by combining vector similarity
        # with lexical grounding in the top candidate.
        q_terms = {w.lower() for w in question.split() if len(w) > 3}
        top_text = str(candidates[0].get("text", "")).lower()
        overlap = 0.0
        if q_terms:
            overlap = sum(1 for t in q_terms if t in top_text) / max(1, len(q_terms))
        effective_similarity = best_similarity * max(0.05, overlap)

        if effective_similarity < SIMILARITY_FLOOR:
            return {
                "question": question,
                "god_nodes": god_nodes,
                "selected_chunks": [],
                "token_cap": token_cap,
                "estimated_tokens_used": 0,
                "context": "",
                "strategy": "graphify_god_nodes -> lancedb_vector_pivot",
                "zero_context": True,
                "floor": SIMILARITY_FLOOR,
                "best_similarity": round(best_similarity, 4),
                "effective_similarity": round(effective_similarity, 4),
                "reason": "SIMILARITY_FLOOR",
            }

        filtered: list[dict[str, Any]] = []
        god_set = set(god_nodes)
        for row in candidates:
            row_nodes = set(row.get("node_ids") or [])
            if not god_set or row_nodes.intersection(god_set):
                filtered.append(row)
            if len(filtered) >= top_k:
                break

        if not filtered:
            filtered = candidates[:top_k]

        used_tokens = 0
        context_parts: list[str] = []
        selected = []
        for row in filtered:
            text = str(row.get("text", ""))
            est_tokens = max(1, len(text.split()))
            if used_tokens + est_tokens > token_cap:
                break
            context_parts.append(text)
            used_tokens += est_tokens
            selected.append(
                {
                    "id": row.get("id"),
                    "source_path": row.get("source_path"),
                    "node_ids": row.get("node_ids"),
                }
            )

        return {
            "question": question,
            "god_nodes": god_nodes,
            "selected_chunks": selected,
            "token_cap": token_cap,
            "estimated_tokens_used": used_tokens,
            "context": "\n\n".join(context_parts),
            "strategy": "graphify_god_nodes -> lancedb_vector_pivot",
            "zero_context": False,
            "floor": SIMILARITY_FLOOR,
            "best_similarity": round(best_similarity, 4),
            "effective_similarity": round(effective_similarity, 4),
        }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Scholar Sense hybrid retrieval")
    parser.add_argument("--server", type=str, default=DEFAULT_SERVER, help="llama-server URL")
    parser.add_argument("--ingest-pdf", type=str, help="Path to PDF to ingest")
    parser.add_argument("--query", type=str, help="Question to query")
    parser.add_argument("--top-k", type=int, default=8, help="Top chunks to retrieve")
    parser.add_argument("--token-cap", type=int, default=768, help="Retrieval context cap")
    args = parser.parse_args()

    ss = ScholarSense(server_url=args.server)

    out: dict[str, Any] = {}
    if args.ingest_pdf:
        out["ingest"] = ss.ingest_pdf(Path(args.ingest_pdf).expanduser())
    if args.query:
        out["query"] = ss.query(args.query, top_k=args.top_k, token_cap=args.token_cap)

    if not out:
        parser.error("Provide --ingest-pdf and/or --query")

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    try:
        _main()
    except (ScholarSenseError, MemoryCoreError) as exc:
        raise SystemExit(f"[ERROR] {exc}")
