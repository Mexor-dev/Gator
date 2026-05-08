#!/usr/bin/env python3
import json
import urllib.request


def post(url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8", errors="replace")


if __name__ == "__main__":
    payload = {
        "model": "gator",
        "system": "You are concise.",
        "prompt": "Give a two-line plan to reduce API latency.",
        "stream": False,
        "n_predict": 128,
        "temperature": 0.7,
        "top_p": 0.9,
    }
    out = post("http://127.0.0.1:8081/api/generate", payload)
    print("8081 raw:", out)
    data = json.loads(out)
    print("8081 response length:", len(str(data.get("response", ""))))

    out2 = post(
        "http://127.0.0.1:8090/generate",
        {"prompt": "Design a practical 3-step API latency plan with concrete targets"},
    )
    print("8090 raw:", out2)
