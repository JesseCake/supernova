from dataclasses import dataclass
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

@dataclass
class PtvConfig:
    api_key: str
    stop_id: str           = "14312"   # Anstey Station platform 1
    stop_name: str         = "Anstey Station"
    direction: str         = "citybound"
    gtfs_zip_folder: str   = "2"       # folder inside GTFS zip for this line
    cache_file: str        = ""        # auto-set to config dir if empty
    walk_minutes: int         = 7         # used in prompt formatting to give user an idea of how long to get to station before next train

@dataclass
class AppConfig:
    ollama: OllamaConfig
    server: ServerConfig
    interfaces: InterfacesConfig
    ha_url: str
    ptv: Optional[PtvConfig] = None
    searxng_url: str = "http://localhost:8888"  # default so it works without config entry

def load_config(path: str = None) -> AppConfig:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "settings.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f)
    
    # Conditionally include ptv config if present, but tools will check for presence of config.ptv before enabling related tools
    ptv = None
    if raw.get("ptv") and raw["ptv"].get("api_key"):
        ptv_raw = raw["ptv"]
        #auto-set cache_file path next to settings.yaml if not specified
        if not ptv_raw.get("cache_file"):
            ptv_raw["cache_file"] = os.path.join(os.path.dirname(path), "ptv_cache.json")
        ptv = PtvConfig(**{k: v for k, v in ptv_raw.items() if k in PtvConfig.__dataclass_fields__})

    return AppConfig(
        ollama=OllamaConfig(**raw["ollama"]),
        server=ServerConfig(**raw["server"]),
        interfaces=InterfacesConfig(**(raw.get("interfaces") or {})),
        ha_url=raw["home_assistant"]["url"],
        ptv=ptv,
        searxng_url=raw.get("searxng", {}).get("url", "http://localhost:8888"),
    )