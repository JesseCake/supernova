import threading
import asyncio
from core.core import CoreProcessor

# Config
from core.settings import load_config

# Interfaces
from interfaces.web_interface import WebInterface
#from interfaces.voice_interface import VoiceInterface  # DEAD NOW TO REMOVE
from interfaces.voice_remote import VoiceRemoteInterface
from interfaces.asterisk_interface import AsteriskInterface

# Shared instances — initialised once before any interfaces start - otherwise we clash
#from whisper_live.transcriber import WhisperModel  # WE NOW USE THE DIRECT FASTER WHISPER
from faster_whisper import WhisperModel
from whisper_live.vad import VoiceActivityDetector
whisper_model = WhisperModel(model_size_or_path="base.en")
vad = VoiceActivityDetector(threshold=0.5, frame_rate=16000)


if __name__ == "__main__":  
    # Config
    config = load_config()

    # Initialize core processor with config
    core_processor = CoreProcessor(config)

    # Logic to set which interfaces from config to start:

    if config.interfaces.voice_local:
        # Start the local voice interface (using defaults) - CURRENTLY NOT IN USE
        #voice_interface = VoiceInterface(core_processor=core_processor)
        #voice_thread = threading.Thread(target=voice_interface.run)
        #voice_thread.start()
        pass

    if config.interfaces.asterisk:
        def run_asterisk_interface():
            asyncio.run(
                AsteriskInterface(core_processor, config, transcriber=whisper_model, vad=vad).run()
            )
        asterisk_thread = threading.Thread(
            target=run_asterisk_interface, daemon=True
        )
        asterisk_thread.start()
        print(f"[main] asterisk interface started, connecting to ARI at {config.asterisk.ari_host}:{config.asterisk.ari_port}")

    if config.interfaces.voice_remote:
        # Start the remote voice interface (runs in background thread)
        def run_remote_voice_interface():
            asyncio.run(
                VoiceRemoteInterface(core_processor).run(
                    host=config.server.remote_voice_host, 
                    port=config.server.remote_voice_port,
                )
            )
        remote_voice_thread = threading.Thread(target=run_remote_voice_interface)
        remote_voice_thread.start()
        print(f"[main] voice_remote started on {config.server.remote_voice_host}:{config.server.remote_voice_port}")

    if config.interfaces.web:
        # Web interface runs in main thread (blocks)
        web_interface = WebInterface(core_processor)
        web_interface.run()