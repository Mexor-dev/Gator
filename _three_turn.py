#!/usr/bin/env python3
"""Three-turn validation: name retention + 502 reasoning."""
import json
import sys
import requests

URL = "http://127.0.0.1:8090/generate"
SESSION = "remediation_validation"

PROMPTS = [
    "My name is Maya. Help me solve a 502 error sequence.",
    "What is the priority fix for that error?",
    "Who am I talking to and what is my name?",
]


def ask(prompt: str, idx: int) -> str:
    payload = {
        "prompt": prompt,
        "max_tokens": 260,
        "temperature": 0.3,
        "session_id": SESSION,
    }
    r = requests.post(URL, json=payload, timeout=300)
    r.raise_for_status()
    body = r.json()
    text = body.get("text") or body.get("response") or json.dumps(body)
    print(f"\n=== TURN {idx} ===")
    print(f">>> {prompt}")
    print(f"<<< {text.strip()}")
    return text


def main() -> int:
    for i, p in enumerate(PROMPTS, 1):
        ask(p, i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
