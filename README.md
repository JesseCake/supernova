# Supernova

A locally-run home AI assistant built for NVIDIA Jetson hardware. Supernova runs entirely on-device — no cloud required — and connects to your home through voice, phone, and web interfaces.

---

## Overview

Supernova is built around a modular plugin architecture that makes it easy to extend without touching core code. Tools are self-contained Python files that the system picks up automatically. Personality and behaviour rules are hot-reloading markdown files. Voice interfaces connect via a satellite device or a phone line through Asterisk.

Key properties:
- Fully local — LLM inference via Ollama, TTS via Piper, ASR via Whisper
- Dynamic tool loading — add or edit tools without restarting
- Multi-interface — voice satellite, phone (Asterisk/ARI), web chat
- Speaker identification — identifies who is speaking and personalises responses
- Behaviour rules — persistent rules the assistant follows across all sessions

---

## Hardware

Designed for and tested on:
- **NVIDIA Jetson** (JetPack 6, R36.x) — aarch64
- **Grandstream HT802** ATA for phone connectivity
- A satellite device running the voice client (Raspberry Pi or similar)

---

## Architecture

```
supernova/
├── main.py                     — entry point, starts all interfaces
├── core/
│   ├── core.py                 — CoreProcessor: session management, Ollama, tool dispatch
│   ├── settings.py             — AppConfig dataclasses + load_config()
│   ├── tool_loader.py          — dynamic tool loader, watches for file changes
│   ├── precontext.py           — PrecontextLoader, VoiceMode enum
│   └── speaker_id.py           — speaker identification (resemblyzer)
├── tools/                      — tool plugins (one file = one or more tools)
│   ├── check_weather.py
│   ├── perform_search.py
│   ├── home_automation.py
│   ├── send_email.py
│   ├── ptv_departures.py
│   ├── behaviour.py
│   ├── open_website.py
│   ├── hangup_call.py
│   └── perform_math_operation.py
├── config/
│   ├── core_config.yaml        — main config (interfaces, ollama, asterisk, debug)
│   ├── *.yaml                  — per-tool config sidecars
│   └── speaker_profiles.json  — enrolled speaker embeddings (auto-generated)
├── personality/
│   ├── personality.md          — core personality (hot-reloads)
│   ├── voice_instructions.md   — voice mode instructions + hangup rules
│   └── phone_instructions.md  — phone-specific instructions
├── cache/
│   └── ptv_cache.json         — PTV stops/routes cache
├── interfaces/
│   ├── voice_remote.py         — TCP voice satellite interface
│   ├── asterisk_interface.py   — Asterisk ARI phone interface
│   └── web_interface.py        — web chat interface
└── scripts/
    ├── enroll_speaker.py       — speaker enrollment CLI
    ├── record_sample.py        — microphone recording utility
    ├── test_email.py           — email config test utility
    └── ptv_cache_update.py     — PTV cache updater (run via cron)
```

---

## Interfaces

### Voice Remote (`voice_remote.py`)
A TCP-based voice interface for a satellite device. The satellite streams raw 16kHz PCM audio and receives 16kHz PCM TTS in return. The server handles VAD, transcription, speaker identification, and TTS synthesis.

Protocol frames:
| Frame | Direction | Description |
|-------|-----------|-------------|
| `OPEN`/`WAKE` | Client → Server | Open channel, trigger greeting |
| `AUD0` | Client → Server | Raw int16 mono 16kHz PCM chunk |
| `INT0` | Client → Server | Barge-in — interrupt current TTS |
| `STOP` | Client → Server | End of utterance, flush buffer |
| `TTS0` | Server → Client | TTS audio response |
| `RDY0` | Server → Client | Ready for next utterance |
| `THNK` | Server → Client | Processing indicator |
| `CLOS` | Server → Client | Channel closing |

### Asterisk Phone (`asterisk_interface.py`)
Handles inbound calls via Asterisk ARI. Audio is exchanged over RTP as G.711 ulaw at 8kHz, converted internally to 16kHz for VAD/Whisper/Piper. One call at a time.

Requires Asterisk with:
- `http.conf` — ARI HTTP on 127.0.0.1:8088
- `ari.conf` — ARI user matching config
- `pjsip.conf` — SIP endpoint for your ATA
- `extensions.conf` — `Stasis(supernova)` routing

### Web Interface (`web_interface.py`)
Simple text chat interface. Plain mode — no TTS, no VAD.

---

## Tool Plugin System

Tools live in `tools/` with matching yaml config sidecars in `config/`. The loader watches file modification times and reloads changed tools on the next request — no restart needed.

### Minimal tool structure

```python
# tools/my_tool.py
from typing import Annotated
from pydantic import Field

def my_tool(
    param: Annotated[str, Field(description="What this does. Required.")],
) -> str:
    """One-line description — shown to the LLM to decide when to call it."""
    ...

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = tool_args.get('parameters', {})
    value  = params.get('param', '')
    return core._wrap_tool_result("my_tool", {"text": f"Done: {value}"})
```

```yaml
# config/my_tool.yaml
enabled: true
```

### Tool yaml options

| Field | Description |
|-------|-------------|
| `enabled` | Set `false` to disable without deleting |
| `voice_only` | Only include in SPEAKER/PHONE modes |
| `context_priority` | System prompt injection order (lower = earlier) |

### Context providers

Tools can inject into the system prompt every turn by defining `provide_context(core, tool_config) -> str`. Used by `behaviour.py` to inject active rules and `send_email.py` to inject known contact names.

### Multi-tool files

Export a `TOOLS` list for files that define more than one tool:
```python
TOOLS = [
    {'schema': tool_one, 'name': 'tool_one', 'execute': execute_one},
    {'schema': tool_two, 'name': 'tool_two', 'execute': execute_two},
]
```

See `TOOL_PLUGIN_SYSTEM.md` for full documentation.

---

## Speaker Identification

Speaker identification runs in a background thread as soon as VAD fires, so identification is usually complete before the utterance ends — zero added latency.

Uses **resemblyzer** (GE2E model) — lightweight, no torchaudio dependency, runs on CUDA automatically.

### Enrollment

```bash
# Record a sample
python3 scripts/record_sample.py --out samples/jesse.wav

# Enroll from audio file
python3 scripts/enroll_speaker.py --name Jesse --audio samples/jesse.wav \
    --email jesse@example.com --notes "Primary developer."

# Enroll from multiple files (averaged for better accuracy)
python3 scripts/enroll_speaker.py --name Jesse \
    --audio samples/jesse_mic.wav samples/jesse_phone.wav

# Merge new audio into existing profile
python3 scripts/enroll_speaker.py --merge Jesse --audio samples/jesse_new.wav

# Update email/notes without re-recording
python3 scripts/enroll_speaker.py --update Jesse --email new@example.com

# List enrolled speakers
python3 scripts/enroll_speaker.py --list

# Test identification
python3 scripts/enroll_speaker.py --test --audio samples/test.wav
```

Profiles are stored in `config/speaker_profiles.json`. When a speaker is identified, their name, email, and notes are injected into the system prompt so the LLM knows who it's talking to.

### Threshold tuning

Adjust in `core_config.yaml`:
```yaml
speaker:
  threshold: 0.5   # lower for noisy/phone conditions, higher for cleaner audio
```

---

## Configuration

### `config/core_config.yaml`

```yaml
interfaces:
  voice_remote: true
  voice_local: false
  web: true
  asterisk: true

ollama:
  host: "http://localhost:11434"
  model: "qwen3.5:9b"
  model_type: "qwen3"

server:
  remote_voice_host: "0.0.0.0"
  remote_voice_port: 10400

voice:
  model_path: "./libs/voices/glados_piper_medium.onnx"
  use_cuda: false

asterisk:
  ari_host: "127.0.0.1"
  ari_port: 8088
  ari_user: "supernova"
  ari_password: "yourpassword"
  rtp_local_ip: "127.0.0.1"

speaker:
  threshold: 0.5

debug:
  record_audio: false
  record_dir: "./debug_audio"
```

### Debug audio recording

Set `debug.record_audio: true` to save every recognised utterance as a WAV file in `debug_audio/`. Useful for diagnosing ASR accuracy and building speaker enrollment samples from real usage.

---

## Built-in Tools

| Tool | Description |
|------|-------------|
| `check_weather` | Current weather and 5-day forecast via OpenWeatherMap |
| `perform_search` | Web search via SearXNG |
| `home_automation` | Home Assistant entity control |
| `send_email` | Send email via SMTP with contact book |
| `ptv_departures` | Melbourne PTV train/tram/bus departures |
| `behaviour` | Persistent behaviour rules (add/remove/list) |
| `open_website` | Open URLs in browser |
| `hangup_call` | End voice/phone call (voice-only) |
| `perform_math_operation` | Safe arithmetic evaluation |

---

## Personality & Behaviour

### Personality files

Files in `personality/` hot-reload on every request — edit and save, changes take effect immediately.

| File | Used when |
|------|-----------|
| `personality.md` | Always |
| `voice_instructions.md` | SPEAKER and PHONE modes |
| `phone_instructions.md` | PHONE mode only |

### Behaviour rules

Persistent rules stored in `config/behaviour_overrides.json`. The LLM can add, remove, and list rules via the behaviour tool. Rules are injected into the system prompt on every request.

Example:
> "Always address Jesse by name when responding."
> "When asked about the weather, also mention if it's a good day for cycling."

---

## Dependencies

```bash
# Core
pip install ollama faster-whisper piper-tts

# Voice
pip install webrtcvad-wheels numpy resampy

# Speaker ID
pip install resemblyzer soundfile librosa

# Interfaces
pip install aiohttp flask

# Tools
pip install requests pydantic pyyaml
```

PyTorch for Jetson — install from the Jetson AI Lab mirror:
```bash
pip install torch --index-url https://jetson.webredirect.org/jp6/cu126
```

---

## PTV Cache

The PTV departures tool uses a local cache of stops and routes. Update weekly via cron:

```bash
# Update manually
python3 scripts/ptv_cache_update.py --update-cache

# Add to crontab (Monday 3am)
0 3 * * 1 cd /home/jesse/supernova && python3 scripts/ptv_cache_update.py --update-cache
```

---

## Email Setup

Configure in `config/send_email.yaml`:

```yaml
smtp_host: "smtp.gmail.com"
smtp_port: 587
username: "you@gmail.com"
password: "your-16-char-app-password"
from_address: "you@gmail.com"

contacts:
  Jesse: "jesse@example.com"
  Dean:  "dean@example.com"
```

For Gmail, generate an app password at: Google Account → Security → 2-Step Verification → App passwords.

Test your config:
```bash
python3 scripts/test_email.py --to Jesse
```

---

## Voice Mode Behaviour

| Mode | Interface | Hangup tool | Speaker ID |
|------|-----------|-------------|------------|
| `PLAIN` | Web | No | No |
| `SPEAKER` | Voice remote | Yes | Yes |
| `PHONE` | Asterisk | Yes | Yes |

In SPEAKER and PHONE modes the assistant is instructed to be brief, avoid markdown, and invoke the `hangup_call` tool when a conversation is complete.

---

## License - open source

MIT License. See [LICENSE](LICENSE) for details.

The VAD file in the whisper_live folder is from (also MIT license):
https://github.com/AIWintermuteAI/WhisperLive