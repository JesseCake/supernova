import gradio as gr
import uuid
import threading

from core.interface_mode import InterfaceMode
from core.session_state import KEY_INTERFACE_MODE, get_response_queue


class WebInterface:
    def __init__(self, core_processor):
        self.core_processor = core_processor
        self.session_id = None  # one session per browser tab/process

    def run(self):
        def process_message(message, history):
            # Initialise session if not already done
            if self.session_id is None:
                self.session_id  = str(uuid.uuid4())
                core_session     = self.core_processor.create_session(self.session_id)
                if core_session is not None:
                    core_session[KEY_INTERFACE_MODE] = InterfaceMode.GENERAL
                    core_session['interface']        = InterfaceMode.GENERAL.value
                    core_session['endpoint_id']      = self.session_id

            # Kick off the core processing in a background thread
            t = threading.Thread(
                target=self.core_processor.process_input,
                kwargs={"input_text": message, "session_id": self.session_id},
                daemon=True,
            )
            t.start()

            session            = self.core_processor.get_session(self.session_id)
            assistant_response = ""

            # Stream chunks from the core processor
            while True:
                chunk = get_response_queue(session).get()  # blocking
                if chunk is None:
                    return  # End of response stream
                assistant_response += chunk
                # IMPORTANT: yield a SINGLE assistant message (dict), not history
                yield {"role": "assistant", "content": assistant_response}

        def clear_chat_history():
            if self.session_id:
                self.core_processor.clear_history(self.session_id)
            return []

        # we need some css to make things sit properly:
        css = """
            html, body, .gradio-container { height: 100% !important; }
            #chatbot { height: calc(100vh - 200px) !important; }
        """
        with gr.Blocks(title="Supernova") as demo:
            gr.Markdown("# Supernova")
            chatbot = gr.Chatbot(height=600, elem_id="chatbot")
            chat    = gr.ChatInterface(
                fn      = process_message,
                chatbot = chatbot,
            )
            clear_btn = gr.Button("Clear History", variant="secondary")
            clear_btn.click(
                fn      = clear_chat_history,
                inputs  = None,
                outputs = chatbot,
            )
        demo.launch(share=False, server_name="0.0.0.0", css=css)