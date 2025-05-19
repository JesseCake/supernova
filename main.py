import threading
from core.core import CoreProcessor
from interfaces.web_interface import WebInterface
from interfaces.voice_interface import VoiceInterface
from interfaces.voice_remote import VoiceRemoteInterface


if __name__ == "__main__":
    core_processor = CoreProcessor()

    # Start the voice interface (using defaults)
    voice_interface = VoiceInterface(core_processor=core_processor)
    voice_thread = threading.Thread(target=voice_interface.run)
    voice_thread.start()

    # Start the remote voice interface
    remote_voice_interface =VoiceRemoteInterface(core_processor)
    remote_voice_thread = threading.Thread(target=remote_voice_interface.run, daemon=True)
    remote_voice_thread.start()

    # Join thread (optional, if we want to join for certain functionality)
    #voice_thread.join()
    #remote_voice_thread.join()

    # Start the web interface
    web_interface = WebInterface(core_processor)
    web_interface.run()

