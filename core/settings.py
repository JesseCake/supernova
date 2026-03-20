from dataclasses import dataclass, field
import yaml, os
from typing import Optional

# This file is a thin loader to load the settings.yaml to validate and expose a typed dataclass 

@dataclass
class OllamaConfig:
    host: str
    model: str
    model_type: str  # used to select precontext + message formatting

@dataclass
class ServerConfig:
    remote_voice_host: str
    remote_voice_port: int

@dataclass
class InterfacesConfig:
    voice_remote: bool = True
    voice_local: bool = False
    web: bool = True
    asterisk: bool = False

@dataclass
class AsteriskConfig:
    ari_host: str           = "127.0.0.1"
    ari_port: int           = 8088
    ari_user: str           = "supernova"
    ari_password: str       = "changeme"
    rtp_local_ip: str       = "127.0.0.1"

@dataclass
class VoiceConfig:
    model_path: str = "./libs/voices/voice.onnx"
    use_cuda: bool = False

@dataclass
class AppConfig:
    ollama: OllamaConfig
    server: ServerConfig
    interfaces: InterfacesConfig
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    asterisk: AsteriskConfig = field(default_factory=AsteriskConfig)

def load_config(path: str = None) -> AppConfig:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "../config/core_config.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f)

    # Asterisk config loading
    asterisk_raw = raw.get("asterisk") or {}
    asterisk = AsteriskConfig(**{
        k: v for k, v in asterisk_raw.items()
        if k in AsteriskConfig.__dataclass_fields__
    })

    # Voice config
    voice_raw = raw.get("voice") or {}
    voice = VoiceConfig(**{
        k: v for k, v in voice_raw.items()
        if k in VoiceConfig.__dataclass_fields__
    })

    return AppConfig(
        ollama=OllamaConfig(**raw["ollama"]),
        server=ServerConfig(**raw["server"]),
        interfaces=InterfacesConfig(**(raw.get("interfaces") or {})),
        voice=voice,
        asterisk=asterisk,
    )