#!/usr/bin/env python3
import json
from urllib import request

payload = {
    "prompt": "Prove by structured reasoning whether a policy that increases minimum wage can reduce poverty",
    "max_tokens": 24,
    "temperature": 0.6,
    "top_p": 0.9,
}
req = request.Request(
    "http://127.0.0.1:8090/generate",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with request.urlopen(req, timeout=120) as r:
    print(r.read().decode("utf-8", errors="replace"))
