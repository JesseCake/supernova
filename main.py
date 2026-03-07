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

    # Start the local voice interface (using defaults)
    #voice_interface = VoiceInterface(core_processor=core_processor)
    #voice_thread = threading.Thread(target=voice_interface.run)
    #voice_thread.start()

    # Start the remote voice interface - wait is this used anymore?
    #def handler_factory(reader, writer):
    #    return VoiceRemoteInterface(reader, writer, core_processor)

    def run_remote_voice_interface():
        asyncio.run(VoiceRemoteInterface(core_processor).run(host="0.0.0.0", port=10400))

    remote_voice_thread = threading.Thread(target=run_remote_voice_interface)
    remote_voice_thread.start()
    
    #Run directly in main thread for debugging (don't use this for production):
    #run_remote_voice_interface()

    # Join thread (optional, if we want to join for certain functionality)
    #voice_thread.join()
    #remote_voice_thread.join()

    # Start the web interface
    web_interface = WebInterface(core_processor)
    web_interface.run()