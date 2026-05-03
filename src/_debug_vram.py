#!/home/user/Gator/venv/bin/python3
from pathlib import Path
import traceback

from test_gator import parse_pid_file, vram_check, GATOR_ROOT

pid = parse_pid_file(GATOR_ROOT / "bin" / "llama_server.pid")
print("pid:", pid)

try:
    print(vram_check(pid))
except Exception:
    traceback.print_exc()
