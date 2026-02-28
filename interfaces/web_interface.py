import gradio as gr
import uuid
import threading


class WebInterface:
    def __init__(self, core_processor):
        self.core_processor = core_processor
        self.session_id = None  # one session per browser tab/process

    def run(self):
        def process_message(message, history):
            """
            For gr.ChatInterface(type="messages"):
            - Gradio manages `history`.
            - This function should yield/return ONLY the assistant message
              (as a dict {'role','content'} or ChatMessage), NOT the full history list.
            """

            # Initialise session if not already done
            if self.session_id is None:
                self.session_id = str(uuid.uuid4())
                self.core_processor.create_session(self.session_id)

            # Kick off the core processing in a background thread
            t = threading.Thread(
                target=self.core_processor.process_input,
                kwargs={"input_text": message, "session_id": self.session_id, "is_voice": False},
                daemon=True,
            )
            t.start()

            session = self.core_processor.get_session(self.session_id)

            assistant_response = ""

            # Stream chunks from the core processor
            while True:
                chunk = session["response_queue"].get()  # blocking
                if chunk is None:
                    break

                assistant_response += chunk

                # IMPORTANT: yield a SINGLE assistant message (dict), not history
                yield {"role": "assistant", "content": assistant_response}

            # Just exit (no `return history` in a generator)

        gr.ChatInterface(
            fn=process_message,
            type="messages",
            title="Supernova",
        ).launch(share=False, server_name="0.0.0.0")