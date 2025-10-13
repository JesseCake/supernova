import threading
import asyncio
from core.core import CoreProcessor
from interfaces.web_interface import WebInterface
#from interfaces.voice_interface import VoiceInterface
#from interfaces.voice_remote import VoiceRemoteInterface
from interfaces.context_editor import create_app as context_create_app, create_server as context_create_server


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

    #def run_remote_voice_interface():
    #    asyncio.run(VoiceRemoteInterface(core_processor).run(host="0.0.0.0", port=10400))

    #remote_voice_thread = threading.Thread(target=run_remote_voice_interface)
    #remote_voice_thread.start()
    
    #Run directly in main thread for debugging:
    #run_remote_voice_interface()

    # Join thread (optional, if we want to join for certain functionality)
    #voice_thread.join()
    #remote_voice_thread.join()

    # Start the context editor interface
    context_editor_app = context_create_app(
        system_message_path="config/knowledgebase.txt",
        admin_token="furby",
        app_title="The Operator Precontext Editor"
    )
    
    context_editor_thread = context_create_server(context_editor_app, host="0.0.0.0", port=5000)
    context_editor_thread.start()

    # Start the general LLM web interface
    web_interface = WebInterface(core_processor)
    web_interface.run()