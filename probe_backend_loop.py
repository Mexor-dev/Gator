#!/usr/bin/env python3
import json
import urllib.request


def post(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


for i in range(1, 4):
    print(f"==== probe {i} ====")
    out = post(
        "http://127.0.0.1:8081/api/generate",
        {
            "model": "gator",
            "system": "You are concise.",
            "prompt": "Give exactly 2 bullet steps to reduce API latency.",
            "stream": False,
            "n_predict": 96,
            "temperature": 0.7,
            "top_p": 0.9,
        },
    )
    text = str(out.get("response", ""))
    print("8081_len=", len(text))
    print(text[:220])

    bridged = post(
        "http://127.0.0.1:8090/generate",
        {
            "prompt": "Design a practical 3-step API latency plan with concrete targets",
            "max_tokens": 220,
            "temperature": 0.65,
        },
    )
    btxt = str(bridged.get("text", ""))
    print("8090_len=", len(btxt))
    print(btxt[:220])
