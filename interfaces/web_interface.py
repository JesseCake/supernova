import gradio as gr
from gradio import ChatMessage
import uuid
import time
import threading

class WebInterface:
    def __init__(self, core_processor):
        self.core_processor = core_processor
        self.session_id = None  # store the session ID here

    def run(self):
        def process_message(message, history):
            if history is None:
                history = []

            # initialise session if not already done:
            if self.session_id is None:
                self.session_id = str(uuid.uuid4())
                self.core_processor.create_session(self.session_id)

            # Add user message using ChatMessage dataclass
            history.append(ChatMessage(role="user", content=message))

            # Run the input processing in a separate thread
            process_thread = threading.Thread(
                target=self.core_processor.process_input,
                kwargs={"input_text": message, "session_id": self.session_id, "is_voice":False}
            )
            process_thread.start()


            session = self.core_processor.get_session(self.session_id)

            assistant_response = ""

            # Stream response chunks incrementally
            while not session['response_finished'].is_set() or not session['response_queue'].empty():
                response_chunk = session['response_queue'].get()
                # print(f'{response_chunk}', end='')

                assistant_response += response_chunk

                # Yield the history as ChatMessage objects (Gradio will convert them properly)
                yield ChatMessage(role="assistant", content=assistant_response)

                time.sleep(0.05)

            # Final yield of the complete history
            return history

        gr.ChatInterface(
            fn=process_message,
            type="messages",
            title="Supernova",
        ).launch(share=False, server_name="0.0.0.0")
