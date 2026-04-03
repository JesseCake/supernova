from dataclasses import dataclass, field
import yaml
import os
from typing import Optional


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class OllamaConfig:
    host:       str
    model:      str
    model_type: str   # drives message formatting: "gemma", "qwen3" etc.


@dataclass
class ServerConfig:
    remote_voice_host: str
    remote_voice_port: int


@dataclass
class InterfacesConfig:
    """Interface enable flags."""
    speaker:     bool = True
    voice_local: bool = False
    web:         bool = True
    asterisk:    bool = False


@dataclass
class AsteriskEndpointConfig:
    """A single static phone endpoint for the Asterisk interface."""
    number:        str = ""
    friendly_name: str = ""

@dataclass
class TelegramEndpointConfig:
    chat_id:       str = ""
    friendly_name: str = ""

@dataclass
class TelegramConfig:
    enabled:   bool = False
    token:     str  = ""
    endpoints: dict = field(default_factory=dict)

@dataclass
class AsteriskConfig:
    enabled:            bool = False
    ari_host:           str  = "127.0.0.1"
    ari_port:           int  = 8088
    ari_user:           str  = "supernova"
    ari_password:       str  = "changeme"
    rtp_local_ip:       str  = "127.0.0.1"
    outbound_caller_id: str  = ""
    endpoints: dict = field(default_factory=dict)


@dataclass
class VoiceConfig:
    model_path: str  = "./libs/voices/voice.onnx"
    use_cuda:   bool = False


@dataclass
class SpeakerConfig:
    threshold: float = 0.75


@dataclass
class DebugConfig:
    record_audio: bool = False
    record_dir:   str  = "./debug_audio"
    verbose:      bool = False


@dataclass
class AppConfig:
    ollama:     OllamaConfig
    server:     ServerConfig
    interfaces: InterfacesConfig
    voice:      VoiceConfig    = field(default_factory=VoiceConfig)
    asterisk:   AsteriskConfig = field(default_factory=AsteriskConfig)
    telegram:   TelegramConfig = field(default_factory=TelegramConfig)
    debug:      DebugConfig    = field(default_factory=DebugConfig)
    speaker_id: SpeakerConfig  = field(default_factory=SpeakerConfig)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_yaml(path: str) -> dict:
    """Load a YAML file, returning empty dict if the file doesn't exist."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _dataclass_from_dict(cls, raw: dict):
    """Construct a dataclass from a dict, ignoring unknown keys."""
    return cls(**{
        k: v for k, v in raw.items()
        if k in cls.__dataclass_fields__
    })


# ── Main loader ───────────────────────────────────────────────────────────────

def load_config(path: str = None) -> AppConfig:
    """
    Load configuration from:
      core_config.yaml          — ollama, server, interfaces, voice, debug, speaker_id
      asterisk_interface.yaml   — asterisk settings + endpoints
      telegram_interface.yaml   — telegram settings + endpoints

    The interfaces block in core_config.yaml controls which interfaces are
    enabled. Each interface's own yaml file takes precedence for its enabled flag.
    """
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "../config/core_config.yaml")

    config_dir = os.path.dirname(path)

    with open(path) as f:
        raw = yaml.safe_load(f)

    # ── Asterisk ──────────────────────────────────────────────────────────────
    asterisk_raw = _load_yaml(os.path.join(config_dir, "asterisk_interface.yaml"))
    if not asterisk_raw:
        asterisk_raw = dict(raw.get("asterisk") or {})

    asterisk_enabled = asterisk_raw.pop("enabled", None)
    if asterisk_enabled is None:
        asterisk_enabled = bool((raw.get("interfaces") or {}).get("asterisk", False))

    endpoints_raw = asterisk_raw.pop("endpoints", {}) or {}
    asterisk = _dataclass_from_dict(AsteriskConfig, asterisk_raw)
    asterisk.enabled = asterisk_enabled
    asterisk.endpoints = {
        name: AsteriskEndpointConfig(**{
            k: v for k, v in ep.items()
            if k in AsteriskEndpointConfig.__dataclass_fields__
        })
        for name, ep in endpoints_raw.items()
    }

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_raw = _load_yaml(os.path.join(config_dir, "telegram_interface.yaml"))
    if not telegram_raw:
        telegram_raw = dict(raw.get("telegram") or {})

    telegram_enabled = telegram_raw.pop("enabled", None)
    if telegram_enabled is None:
        telegram_enabled = bool((raw.get("interfaces") or {}).get("telegram", False))

    endpoints_raw = telegram_raw.pop("endpoints", {}) or {}
    telegram = _dataclass_from_dict(TelegramConfig, telegram_raw)
    telegram.enabled = telegram_enabled
    telegram.endpoints = {
        name: TelegramEndpointConfig(**{
            k: v for k, v in ep.items()
            if k in TelegramEndpointConfig.__dataclass_fields__
        })
        for name, ep in endpoints_raw.items()
    }

    # ── Voice ─────────────────────────────────────────────────────────────────
    voice = _dataclass_from_dict(VoiceConfig, raw.get("voice") or {})

    # ── Debug ─────────────────────────────────────────────────────────────────
    debug = _dataclass_from_dict(DebugConfig, raw.get("debug") or {})

    # ── Speaker ID ────────────────────────────────────────────────────────────
    speaker_id = _dataclass_from_dict(SpeakerConfig, raw.get("speaker_id") or {})

    # ── Interfaces ────────────────────────────────────────────────────────────
    interfaces_raw = dict(raw.get("interfaces") or {})
    # Rename voice_remote → speaker in yaml for backwards compat
    if "voice_remote" in interfaces_raw and "speaker" not in interfaces_raw:
        interfaces_raw["speaker"] = interfaces_raw.pop("voice_remote")
    # Sync asterisk and telegram enabled flags
    interfaces_raw["asterisk"] = asterisk_enabled
    interfaces_raw["telegram"] = telegram_enabled

    interfaces = InterfacesConfig(**{
        k: v for k, v in interfaces_raw.items()
        if k in InterfacesConfig.__dataclass_fields__
    })

    return AppConfig(
        ollama     = OllamaConfig(**raw["ollama"]),
        server     = ServerConfig(**raw["server"]),
        interfaces = interfaces,
        voice      = voice,
        asterisk   = asterisk,
        telegram   = telegram,
        debug      = debug,
        speaker_id = speaker_id,
    )