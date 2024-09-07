import threading
from core.core import CoreProcessor
from interfaces.web_interface import WebInterface
from interfaces.voice_interface import VoiceInterface


if __name__ == "__main__":
    core_processor = CoreProcessor()

    # Start the web interface
    web_interface = WebInterface(core_processor)
    web_interface.start()

    # Start the voice interface
    #voice_interface = VoiceInterface(core_processor)
    #voice_thread = threading.Thread(target=voice_interface.start_listening)
    #voice_thread.start()

    # Join thread (optional, depending on your use case)
    #voice_thread.join()

