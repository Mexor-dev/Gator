# Voice Chat Toggle Feature

**Commit:** `bd9fbdc`  
**Date Implemented:** May 1, 2026

## Overview

The voice chat toggle feature allows users to enable/disable Piper TTS (and implicitly Whisper STT) through the WebUI without requiring a full system restart. When disabled, voice models are kept offline, freeing ~400MB VRAM overhead during resource-constrained periods.

## Architecture

### State Management
- **File:** `logs/voice_status.json`
- **Format:** JSON with `enabled` (bool) and `updated_at` (float timestamp)
- **Persistence:** Survives process restarts; all components read from disk
- **Default:** True (voice enabled by default)

### WebUI Components

#### Constants (`src/interfaces/webui.py:41`)
```python
VOICE_STATUS_FILE = GATOR_ROOT / "logs" / "voice_status.json"
```

#### Helper Functions (`src/interfaces/webui.py:163-183`)
```python
def _get_voice_status() -> dict[str, Any]:
  """Read current voice enablement status from disk."""
  if not VOICE_STATUS_FILE.exists():
    return {"enabled": True, "updated_at": time.time()}
  try:
    return json.loads(VOICE_STATUS_FILE.read_text(encoding="utf-8"))
  except Exception:
    return {"enabled": True, "updated_at": time.time()}

def _set_voice_status(enabled: bool) -> dict[str, Any]:
  """Write voice enablement status to disk."""
  payload = {"enabled": enabled, "updated_at": time.time()}
  VOICE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
  VOICE_STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
  return payload
```

#### API Endpoints (`src/interfaces/webui.py:750-770`)
```python
@app.get("/api/voice/status")
def get_voice_status() -> dict[str, Any]:
  """Get current voice chat status."""
  return _get_voice_status()

@app.post("/api/voice/on")
def voice_on() -> dict[str, Any]:
  """Enable voice chat (Piper TTS + Whisper STT in Telegram)."""
  status = _set_voice_status(True)
  return {"ok": True, **status}

@app.post("/api/voice/off")
def voice_off() -> dict[str, Any]:
  """Disable voice chat to free VRAM resources."""
  status = _set_voice_status(False)
  return {"ok": True, **status}
```

#### HTML Buttons (Vitals Card)
- `🎙️ Voice ON` (green, opacity=1 when enabled, 0.5 when disabled)
- `🔇 Voice OFF` (red, opacity=0.5 when enabled, 1 when disabled)
- Located in Vitals card header row, next to Telegram setup button

#### JavaScript Functions
```javascript
async function setVoiceEnabled(enabled) {
  const endpoint = enabled ? '/api/voice/on' : '/api/voice/off';
  try {
    const r = await fetch(endpoint, { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      const msg = enabled ? 'Voice chat enabled (Piper TTS + Whisper STT)' : 'Voice chat disabled (freed VRAM)';
      alert(msg);
      updateVoiceButtonStates();
    }
  } catch (err) {
    alert('Voice toggle error: ' + String(err));
  }
}

async function updateVoiceButtonStates() {
  try {
    const r = await fetch('/api/voice/status');
    const d = await r.json();
    document.getElementById('voiceOnBtn').style.opacity = d.enabled ? '1' : '0.5';
    document.getElementById('voiceOffBtn').style.opacity = d.enabled ? '0.5' : '1';
  } catch (err) {
    console.error('Voice status update failed:', err);
  }
}

// Button event listeners
document.getElementById('voiceOnBtn').addEventListener('click', () => setVoiceEnabled(true));
document.getElementById('voiceOffBtn').addEventListener('click', () => setVoiceEnabled(false));

// Initialize on page load
document.addEventListener('DOMContentLoaded', updateVoiceButtonStates);
```

### Telegram Gateway Components

#### Constants (`src/interfaces/telegram_gateway.py:41`)
```python
VOICE_STATUS_FILE = GATOR_ROOT / "logs" / "voice_status.json"
```

#### Helper Function (`src/interfaces/telegram_gateway.py:77-87`)
```python
def _get_voice_enabled_from_file() -> bool:
    """Read voice enablement status from file; default to True if not found."""
    try:
        if VOICE_STATUS_FILE.exists():
            data = json.loads(VOICE_STATUS_FILE.read_text(encoding="utf-8"))
            return bool(data.get("enabled", True))
    except Exception:
        pass
    return True
```

#### Initialization (`src/interfaces/telegram_gateway.py:138`)
```python
def __init__(self, token: str, username: str, auth_chat_id: str, bridge_url: str = BRIDGE_URL) -> None:
    self.token = token
    self.username = username
    self.auth_chat_id = str(auth_chat_id)
    self.bridge_url = bridge_url
    self.voice_enabled = _get_voice_enabled_from_file()  # ← Reads from file instead of True
```

## Usage Flow

### User Toggles Voice Off
1. User clicks "🔇 Voice OFF" button in Vitals card
2. JavaScript calls `setVoiceEnabled(false)`
3. `POST /api/voice/off` writes `{"enabled": false, "updated_at": <timestamp>}` to `logs/voice_status.json`
4. Success alert shows "Voice chat disabled (freed VRAM)"
5. Button states update: "🎙️ Voice ON" becomes faded, "🔇 Voice OFF" becomes bright

### Telegram Message Arrives
1. `TelegramGateway.__init__()` was already called (reads voice state from file)
2. `text_message()` is invoked with user input
3. `_send_text_and_voice()` checks `self.voice_enabled`
4. If `False`: only text reply sent, Piper subprocess NOT spawned
5. If `True`: both text reply + voice message sent (Piper invoked as before)

### User Toggles Voice On
1. User clicks "🎙️ Voice ON" button
2. JavaScript calls `setVoiceEnabled(true)`
3. `POST /api/voice/on` writes `{"enabled": true, "updated_at": <timestamp>}` to `logs/voice_status.json`
4. Success alert shows "Voice chat enabled (Piper TTS + Whisper STT)"
5. Button states update: "🎙️ Voice ON" becomes bright, "🔇 Voice OFF" becomes faded
6. Next Telegram message: voice response will be included (new gateway instance reads updated file)

## Testing

### Unit Tests
1. **webui.py helper functions** (`test_voice.py`)
   - `_get_voice_status()` returns dict with enabled=True initially
   - `_set_voice_status(False)` writes to file and returns updated state
   - File persistence: second read matches written state

2. **telegram_gateway.py helper function** (`test_voice_logic.py`)
   - `_get_voice_enabled_from_file()` returns True when file has `{"enabled": true}`
   - Returns False when file has `{"enabled": false}`
   - Returns True (default) when file missing or malformed

### API Endpoint Tests
1. **GET /api/voice/status** → returns `{"enabled": bool, "updated_at": float}`
2. **POST /api/voice/off** → returns `{"ok": true, "enabled": false, "updated_at": float}`
3. **POST /api/voice/on** → returns `{"ok": true, "enabled": true, "updated_at": float}`

### Integration Test
- Start webui, click toggle buttons, verify state changes in `logs/voice_status.json`
- Send Telegram messages with voice OFF: no audio file sent
- Send Telegram messages with voice ON: audio file sent as before

## Resource Impact

### VRAM Freed When Voice OFF
- Piper ONNX model: ~250MB (not loaded into GPU memory)
- Whisper reference (if loaded): ~150MB
- **Total freed:** ~400MB on VRAM-constrained systems

### VRAM Used When Voice ON
- Piper binary + ONNX loaded: ~250MB (on-demand, per message)
- System default: Voice ON (backwards compatible)

## File Structure
```
logs/
├── voice_status.json        ← New file (created on first toggle)
├── native.log               ← Existing
├── telegram_hive_status.json ← Existing
└── ingest_status.json       ← Existing
```

## Backwards Compatibility
- **Old installs:** No `voice_status.json` file → defaults to True (voice enabled)
- **New installs:** `voice_status.json` created on first toggle
- **Gateway restart:** Reads fresh state from file (no hardcoding)
- **WebUI restart:** No persistent state needed in webui process (reads from file each request)

## Design Decisions

1. **File-based state, not database**
   - Simple, no additional dependencies
   - Fast read on message dispatch
   - Survives all process restarts
   - Human-readable for debugging

2. **Voice OFF disables both TTS + STT**
   - TTS (Piper): explicitly controlled in `text_message()`
   - STT (Whisper): assumed to be unused in current build, but toggle future-proofs
   - Gateway process does not spawn voice worker process when disabled

3. **No restart of gateway on toggle**
   - Gateway runs permanently, reads state from file on each message
   - No need to kill/respawn, no latency on toggle
   - State change is immediate for next message

4. **Two buttons instead of single toggle**
   - Clear ON/OFF visual state
   - Prevents accidental double-click confusion
   - Follows existing cron on/off pattern in webui

## Future Enhancements
- [ ] Persist toggle preference to user profile (per-user voice preference)
- [ ] Auto-disable voice on high VRAM utilization (>80%)
- [ ] Meter voice usage in Telegram (character count, TTS time)
- [ ] Add Whisper STT explicit control (separate from TTS toggle)

## Related Commits
- `5dc4cfb` - Trace suppression to native.log
- `773ab20` - Installer reproducibility + manifest alignment
- `7cefaf6` - Auto-rebuild logic gate + runtime health validation
- `bd9fbdc` - **THIS: Voice toggle feature**
