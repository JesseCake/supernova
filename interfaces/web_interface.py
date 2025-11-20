import gradio as gr
from gradio import ChatMessage
import uuid
import time
import threading
import os
import base64

class WebInterface:
    def __init__(self, core_processor):
        self.core_processor = core_processor
        self.session_id = None  # store the session ID here

    def run(self):
        def process_message(message, history):
            # print("web: starting to process message")

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
            #while not session['response_finished'].is_set() or not session['response_queue'].empty():
            while True:
                if not session['response_queue'].empty():
                    response_chunk = session['response_queue'].get()
                    if response_chunk is None:
                        print('web: response finished')
                        break
                    else:
                        print(f'{response_chunk}', end='')

                        assistant_response += response_chunk

                        # Yield the history as ChatMessage objects (Gradio will convert them properly)
                        yield ChatMessage(role="assistant", content=assistant_response)

                time.sleep(0.01)

            # print(f"web: broke out of queue")

            # Final yield of the complete history
            return history
        
        here = os.path.dirname(os.path.abspath(__file__))
        logo_path = os.path.join(here, "operator.png")

        # Encode the image as base64 so we can use it directly in <img src="...">
        with open(logo_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        img_data = f"data:image/png;base64,{encoded}"



        custom_css = """
        footer { visibility: hidden; }

        /* Header */
        .header {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 12px;
            margin: 12px 0;
            flex: 0 0 auto;
        }

        .header-logo {
            height: 60px;
        }

        .header-title {
            font-size: 1.6rem;
            font-weight: 600;
        }

        /* Column that holds the chat should fill the viewport
        minus ~100px for header + outer padding. Adjust if needed. */
        #chat-col {
            height: calc(100vh - 120px) !important;
        }

        /* Make the chatbot itself grow to fill that column */
        #chatbot {
            flex-grow: 1 !important;
            height: 100% !important;
            overflow: auto !important;
        }
        """

        with gr.Blocks(title="NCM - The Operator", css=custom_css) as interface:
            # Single, clean, centered header
            gr.HTML(
                f"""
                <div class="header">
                    <img class="header-logo" src="{img_data}" alt="The Operator Logo" />
                    <span class="header-title">The Operator</span>
                </div>
                """,
            )
            with gr.Column(elem_id="chat-col"):
                chat = gr.ChatInterface(
                    fn=process_message,
                    type="messages",
                    #provide our own chatbot so we can give it an elem_id:
                    chatbot=gr.Chatbot(
                        elem_id="chatbot", 
                        render=False,
                        type="messages",
                        ),
                )
        
        interface.launch(
            share=False, 
            server_name="0.0.0.0", 
            show_api=False,
            )
