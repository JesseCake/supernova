import threading
import asyncio
from core.core import CoreProcessor

# Config
from config.settings import load_config

# Interfaces
from interfaces.web_interface import WebInterface
#from interfaces.voice_interface import VoiceInterface
from interfaces.voice_remote import VoiceRemoteInterface


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