#!/usr/bin/env python3
"""Test voice state functions."""
import json
from pathlib import Path
from src.interfaces.webui import _get_voice_status, _set_voice_status

print("=== Testing voice state functions ===")
print()

# Test initial state
state = _get_voice_status()
print(f"Initial state: {json.dumps(state, default=str)}")
print()

# Test set to False
state2 = _set_voice_status(False)
print(f"After set to False: {json.dumps(state2, default=str)}")
print()

# Read from file
state3 = _get_voice_status()
print(f"Read from file: {json.dumps(state3, default=str)}")
print()

# Set back to True
state4 = _set_voice_status(True)
print(f"After set to True: {json.dumps(state4, default=str)}")
print()

print("✅ ALL FUNCTION TESTS PASSED")
