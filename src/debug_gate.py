#!/usr/bin/env python3
import gzip
import pickle
from pathlib import Path

p = Path('/home/user/Gator/bin/logic_map.gate')
obj = pickle.loads(gzip.decompress(p.read_bytes()))
print('records', len(obj.get('records', [])))
for i, r in enumerate(obj.get('records', [])[:5]):
    print(i, 'cat', r.get('c'), 'len_t', len(r.get('t', [])), 'len_p', len(r.get('p', [])), 'sample_t', r.get('t', [])[:5])
