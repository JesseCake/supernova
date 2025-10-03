import threading
import asyncio
from core.core import CoreProcessor
from interfaces.web_interface import WebInterface
#from interfaces.voice_interface import VoiceInterface
from interfaces.voice_remote import VoiceRemoteInterface


if __name__ == "__main__":
    # Initialize core processor
    core_processor = CoreProcessor()

    # Start the voice interface (using defaults)
    #voice_interface = VoiceInterface(core_processor=core_processor)
    #voice_thread = threading.Thread(target=voice_interface.run)
    #voice_thread.start()

    # Start the remote voice interface
    #def handler_factory(reader, writer):
    #    return VoiceRemoteInterface(reader, writer, core_processor)

    def run_remote_voice_interface():
        asyncio.run(VoiceRemoteInterface(core_processor).run(host="0.0.0.0", port=10400))

    #remote_voice_thread = threading.Thread(target=run_remote_voice_interface)
    #remote_voice_thread.start()
    
    #Run directly in main thread for debugging:
    run_remote_voice_interface()

    # Join thread (optional, if we want to join for certain functionality)
    #voice_thread.join()
    #remote_voice_thread.join()

    # Start the web interface
    #web_interface = WebInterface(core_processor)
    #web_interface.run()