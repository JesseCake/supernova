import gradio as gr
import uuid
import threading
import time
from core.interface_mode import InterfaceMode
from core.session_state import KEY_INTERFACE_MODE, get_response_queue
from core.session_reaper import SessionReaper


class WebInterface:

    SESSION_TTL = 30 * 60  # 30 minutes

    def __init__(self, core_processor):
        self.core_processor = core_processor
        self.session_id     = None
        self._last_active   = {}

    def _ensure_session(self):
        """Create a new session if one doesn't exist."""
        if self.session_id is None:
            self.session_id  = str(uuid.uuid4())
            core_session     = self.core_processor.create_session(self.session_id)
            if core_session is not None:
                core_session[KEY_INTERFACE_MODE] = InterfaceMode.GENERAL
                core_session['interface']        = InterfaceMode.GENERAL.value
                core_session['endpoint_id']      = self.session_id
        self._last_active[self.session_id] = time.monotonic()

    def _close_current_session(self):
        """Close the current session cleanly."""
        if self.session_id:
            self.core_processor.close_session(self.session_id)
            self._last_active.pop(self.session_id, None)
            self.session_id = None

    def _close_stale(self, session_id: str):
        """Called by the reaper when a session has been idle too long."""
        if self.session_id == session_id:
            self._close_current_session()

    def run(self):

        reaper = SessionReaper(
            get_active_sessions = lambda: (
                {self.session_id: self.session_id} if self.session_id else {}
            ),
            get_last_active = lambda: dict(self._last_active),
            close_fn        = self._close_stale,
            ttl_seconds     = self.SESSION_TTL,
            check_interval  = 60,
        )
        reaper.start()

        def process_message(message, history):
            self._ensure_session()

            t = threading.Thread(
                target = self.core_processor.process_input,
                kwargs = {"input_text": message, "session_id": self.session_id},
                daemon = True,
            )
            t.start()

            session            = self.core_processor.get_session(self.session_id)
            assistant_response = ""

            while True:
                chunk = get_response_queue(session).get()
                if chunk is None:
                    return
                assistant_response += chunk
                yield {"role": "assistant", "content": assistant_response}

        def clear_chat_history():
            self._close_current_session()
            self._ensure_session()
            return []

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