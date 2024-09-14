import threading
from core.core import CoreProcessor
from interfaces.web_interface import WebInterface
from interfaces.voice_interface import VoiceInterface


if __name__ == "__main__":
    core_processor = CoreProcessor()

    # Start the voice interface (using defaults)
    voice_interface = VoiceInterface(core_processor=core_processor)
    voice_thread = threading.Thread(target=voice_interface.run)
    voice_thread.start()

    # Join thread (optional, if we want to join for certain functionality)
    # voice_thread.join()

    # Start the web interface
    web_interface = WebInterface(core_processor)
    web_interface.run()

