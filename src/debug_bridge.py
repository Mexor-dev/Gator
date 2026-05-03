#!/home/user/Gator/venv/bin/python3
from pathlib import Path
from gator_bridge import GatorBridge

bridge = GatorBridge(
    server_url="http://127.0.0.1:8080",
    gate_path=Path("/home/user/Gator/bin/logic_map.gate"),
)

print("records", bridge.gate.total_records)
print("cats", {k: len(v) for k, v in bridge.gate.per_category_top_tokens.items()})
print("class", bridge._classify_prompt("Prove by structured reasoning whether minimum wage can reduce poverty"))

pathway = bridge.gate.per_category_top_tokens.get(bridge._classify_prompt("test"), [])
print("sample pathway len", len(pathway))
