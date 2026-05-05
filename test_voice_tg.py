#!/usr/bin/env python3
"""Test telegram_gateway voice functions."""
import json
from pathlib import Path

# Test the helper function
exec(open('src/interfaces/telegram_gateway.py').read())

print("=== Testing telegram_gateway voice functions ===")
print()

# Test helper function with True state
print("Setting voice to True...")
VOICE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
VOICE_STATUS_FILE.write_text(json.dumps({"enabled": True}))
result = _get_voice_enabled_from_file()
print(f"Voice enabled from file (True case): {result}")
assert result == True, "Expected True"
print()

# Test with False state
print("Setting voice to False...")
VOICE_STATUS_FILE.write_text(json.dumps({"enabled": False}))
result = _get_voice_enabled_from_file()
print(f"Voice enabled from file (False case): {result}")
assert result == False, "Expected False"
print()

# Test default when file missing
print("Testing default when file missing...")
VOICE_STATUS_FILE.unlink(missing_ok=True)
result = _get_voice_enabled_from_file()
print(f"Voice enabled when file missing: {result}")
assert result == True, "Expected True (default)"
print()

print("✅ ALL TELEGRAM GATEWAY TESTS PASSED")
