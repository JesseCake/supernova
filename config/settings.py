from dataclasses import dataclass
import yaml, os

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

@dataclass
class AppConfig:
    ollama: OllamaConfig
    server: ServerConfig
    interfaces: InterfacesConfig
    ha_url: str

def load_config(path: str = None) -> AppConfig:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "settings.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f)
    return AppConfig(
        ollama=OllamaConfig(**raw["ollama"]),
        server=ServerConfig(**raw["server"]),
        interfaces=InterfacesConfig(**(raw.get("interfaces") or {})),
        ha_url=raw["home_assistant"]["url"],
    )