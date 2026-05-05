#!/usr/bin/env python3
from pathlib import Path
from gator_bridge import GatorBridge

bridge = GatorBridge(
    gate_path=Path("/home/user/Gator/bin/logic_map.gate"),
)

print("records", bridge.gate.total_records)
print("cats", {k: len(v) for k, v in bridge.gate.per_category_top_tokens.items()})
sample_category = next(iter(bridge.gate.per_category_top_tokens.keys()), None)
pathway = bridge.gate.per_category_top_tokens.get(sample_category, []) if sample_category is not None else []
print("sample pathway len", len(pathway))
