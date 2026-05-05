#!/usr/bin/env python3
"""Dynamic community naming for Graphify clusters.

Community labels are persisted so the graph legend can survive reloads, but are
re-evaluated whenever the node set or source snippets for a cluster change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hybrid_memory import HybridMemoryStore

GATOR_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = GATOR_ROOT / "research"
GRAPH_JSON = RESEARCH_ROOT / "graphify-out" / "graph.json"
REGISTRY_FILE = RESEARCH_ROOT / "scholar_sense" / "node_registry.json"
BRIDGE_URL = "http://127.0.0.1:8090/generate"


class ClusterNamerError(RuntimeError):
    pass


@dataclass
class CommunityRecord:
    community_id: int
    label: str
    signature: str
    node_count: int
    updated_at: float
    update_reason: str


class ClusterNamer:
    def __init__(
        self,
        graph_json: Path = GRAPH_JSON,
        registry_file: Path = REGISTRY_FILE,
        bridge_url: str = BRIDGE_URL,
    ) -> None:
        self.graph_json = graph_json
        self.registry_file = registry_file
        self.bridge_url = bridge_url
        self.hybrid = HybridMemoryStore(server_url="native://gator_kern")

    def _community_id(self, value: Any) -> int:
        if value is None or value == "":
            return -1
        return int(value)

    def _load_graph(self) -> dict[str, Any]:
        if not self.graph_json.exists():
            raise ClusterNamerError(f"graph.json missing at {self.graph_json}")
        return json.loads(self.graph_json.read_text(encoding="utf-8", errors="replace"))

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_file.exists():
            return {"communities": {}}
        try:
            return json.loads(self.registry_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return {"communities": {}}

    def _save_registry(self, payload: dict[str, Any]) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        self.registry_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _source_path_for_node(self, node: dict[str, Any]) -> Path | None:
        source_file = str(node.get("source_file") or "").strip()
        if not source_file:
            return None
        direct = RESEARCH_ROOT / source_file
        if direct.exists():
            return direct
        stemmed = RESEARCH_ROOT / Path(source_file).name
        if stemmed.exists():
            return stemmed
        return None

    def _snippet_for_node(self, node: dict[str, Any], char_cap: int = 320) -> str:
        source = self._source_path_for_node(node)
        if source is None:
            return str(node.get("label") or "")
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return str(node.get("label") or "")
        compact = " ".join(text.split())
        return compact[:char_cap]

    def _community_nodes(self, graph: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for node in graph.get("nodes", []):
            cid = self._community_id(node.get("community", -1))
            grouped.setdefault(cid, []).append(node)
        return grouped

    def _signature_for_nodes(self, nodes: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for node in sorted(nodes, key=lambda item: str(item.get("id") or "")):
            snippet = self._snippet_for_node(node)
            parts.append(
                json.dumps(
                    {
                        "id": node.get("id"),
                        "label": node.get("label"),
                        "source_file": node.get("source_file"),
                        "snippet": snippet,
                    },
                    sort_keys=True,
                )
            )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _community_prompt(self, community_id: int, nodes: list[dict[str, Any]]) -> str:
        lines = [
            f"Identify the common theme of these research nodes. Provide a 2-3 word technical category name.",
            f"Community ID: {community_id}",
            "Node Data:",
        ]
        for node in nodes[:12]:
            title = str(node.get("label") or node.get("id") or "untitled")
            snippet = self._snippet_for_node(node)
            lines.append(f"- Title: {title}")
            lines.append(f"  Snippet: {snippet}")
        lines.append("Return only the category name.")
        return "\n".join(lines)

    def _bridge_generate(self, prompt: str) -> str:
        req = urllib.request.Request(
            self.bridge_url,
            data=json.dumps(
                {
                    "prompt": prompt,
                    "max_tokens": 48,
                    "temperature": 0.1,
                    "top_k": 20,
                    "top_p": 0.8,
                    "min_p": 0.05,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        return str(payload.get("text") or payload.get("response") or "").strip()

    def _sanitize_label(self, raw: str, nodes: list[dict[str, Any]], community_id: int) -> str:
        candidate = (raw or "").strip().splitlines()[0].strip().strip('"\'`[]')
        candidate = candidate.replace("[gator_kern native trace:", "").strip()
        candidate = " ".join(candidate.split())
        words = [word for word in candidate.split() if word]
        generic_words = {"community", "phase", "phase2", "test", "node", "snippet", "title", "return", "identify", "theme", "name"}
        normalized_words = {re.sub(r"[^a-z0-9]+", "", word.lower()) for word in words}
        if (
            1 <= len(words) <= 5
            and len(candidate) <= 48
            and not candidate.lower().startswith("identify the common theme")
            and not normalized_words.intersection(generic_words)
        ):
            return candidate

        stopwords = {
            "the", "and", "for", "with", "that", "this", "from", "into", "while", "builds", "stores",
            "used", "using", "note", "typical", "adult", "limits", "depending", "guidance", "code",
            "test", "phase", "phase2", "pdf", "py", "md", "corpus", "file", "graph", "nodes",
        }
        token_counts: dict[str, int] = {}
        for node in nodes:
            source_text = f"{node.get('label') or ''} {self._snippet_for_node(node)}"
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]+", source_text.lower()):
                token = token.strip("_-")
                if len(token) < 4 or token in stopwords:
                    continue
                token_counts[token] = token_counts.get(token, 0) + 1

        preferred = {
            "vector", "pivot", "graphify", "lancedb", "embedding", "retrieval", "medication",
            "dosing", "ibuprofen", "contraindications", "safety", "semantic", "research",
        }
        ranked = sorted(
            token_counts.items(),
            key=lambda item: (
                -(item[0] in preferred),
                -item[1],
                -len(item[0]),
                item[0],
            ),
        )
        if ranked:
            chosen = [token.replace("_", " ") for token, _ in ranked[:3]]
            return " ".join(chosen)
        return f"Community {community_id}"

    def refresh_community_labels(self) -> dict[int, dict[str, Any]]:
        graph = self._load_graph()
        registry = self._load_registry()
        communities = registry.setdefault("communities", {})
        grouped = self._community_nodes(graph)
        out: dict[int, dict[str, Any]] = {}

        for community_id, nodes in grouped.items():
            signature = self._signature_for_nodes(nodes)
            key = str(community_id)
            existing = communities.get(key, {})
            label = str(existing.get("label") or "").strip()
            update_reason = "cache_hit"

            if existing.get("signature") != signature or not label:
                raw = self._bridge_generate(self._community_prompt(community_id, nodes))
                label = self._sanitize_label(raw, nodes, community_id)
                update_reason = "initial_label" if not existing else "surgical_update"
                communities[key] = {
                    "community_id": community_id,
                    "label": label,
                    "signature": signature,
                    "node_count": len(nodes),
                    "updated_at": time.time(),
                    "update_reason": update_reason,
                    "nodes": [
                        {
                            "id": node.get("id"),
                            "label": node.get("label"),
                            "source_file": node.get("source_file"),
                            "snippet": self._snippet_for_node(node),
                        }
                        for node in nodes
                    ],
                }

            snippets = [self._snippet_for_node(node) for node in nodes[:12]]
            vector_row_id = self.hybrid.index_community_label(
                community_id=community_id,
                label=communities[key]["label"],
                signature=signature,
                node_count=len(nodes),
                update_reason=str(communities[key].get("update_reason") or update_reason),
                snippets=snippets,
            )
            communities[key]["vector_row_id"] = vector_row_id

            out[community_id] = {
                "label": communities[key]["label"],
                "signature": signature,
                "node_count": len(nodes),
                "update_reason": communities[key].get("update_reason", update_reason),
                "updated_at": communities[key].get("updated_at", time.time()),
                "vector_row_id": communities[key].get("vector_row_id"),
            }

        stale_keys = [key for key in communities if int(key) not in grouped]
        for key in stale_keys:
            del communities[key]

        registry["communities"] = communities
        registry["graph_json"] = str(self.graph_json)
        registry["updated_at"] = time.time()
        self._save_registry(registry)
        return out

    def semantic_lookup(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        return self.hybrid.semantic_search_communities(query=query, top_k=top_k)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Name graph communities via the Gator bridge")
    parser.add_argument("--graph-json", default=str(GRAPH_JSON))
    parser.add_argument("--registry", default=str(REGISTRY_FILE))
    parser.add_argument("--bridge-url", default=BRIDGE_URL)
    args = parser.parse_args()

    namer = ClusterNamer(
        graph_json=Path(args.graph_json),
        registry_file=Path(args.registry),
        bridge_url=args.bridge_url,
    )
    print(json.dumps(namer.refresh_community_labels(), indent=2))


if __name__ == "__main__":
    _main()
