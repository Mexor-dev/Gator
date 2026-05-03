#!/home/user/Gator/venv/bin/python3
from __future__ import annotations

import json
import time
from statistics import mean
from urllib import request

from event_bus import EventBusClient


def post_json(url: str, payload: dict) -> dict:
    req = request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
    with request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8', errors='replace'))


def main() -> None:
    bus = EventBusClient()
    before = bus.doctor_query()
    vram_samples = []
    trunc = 0
    fails = 0

    for _ in range(50):
        t0 = time.perf_counter()
        out = post_json('http://127.0.0.1:8090/generate', {'prompt': 'hi', 'max_tokens': 16, 'temperature': 0.2})
        dt = time.perf_counter() - t0
        text = str(out.get('text') or '')
        if not out.get('final', False):
            trunc += 1
        if not text.strip() and not out.get('interrupted', False):
            fails += 1

        # sample VRAM each round
        v = request.urlopen('http://127.0.0.1:8080/api/vitals', timeout=30).read().decode('utf-8', errors='replace')
        dv = json.loads(v)
        vram_samples.append(dv.get('vram', ''))

    after = bus.doctor_query()
    final_delta = int(after.get('final_packets', 0)) - int(before.get('final_packets', 0))

    print(json.dumps({
        'runs': 50,
        'truncation_count': trunc,
        'empty_non_interrupt_count': fails,
        'final_packets_delta': final_delta,
        'vram_samples_head': vram_samples[:5],
        'status': 'PASS' if trunc == 0 and final_delta >= 50 and fails == 0 else 'FAIL',
    }, indent=2))


if __name__ == '__main__':
    main()
