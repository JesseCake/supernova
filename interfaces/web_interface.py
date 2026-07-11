import gradio as gr
import uuid
import threading
import time
from core.interface_mode import InterfaceMode
from core.session_state import KEY_INTERFACE_MODE, get_response_queue
from core.session_reaper import SessionReaper


# Gradio disables the textbox while a response streams and focus is lost
# when it re-enables. autofocus=True handles this on recent Gradio; this
# observer is a version-tolerant fallback.
REFOCUS_JS = """
() => {
    const find = () => document.querySelector('.gradio-container textarea');
    const obs = new MutationObserver(() => {
        const ta = find();
        if (ta && !ta.disabled && document.activeElement !== ta) ta.focus();
    });
    obs.observe(document.body, {
        subtree: true, attributes: true, attributeFilter: ['disabled']
    });
    const ta = find();
    if (ta) ta.focus();
}
"""

ANON = "(anonymous)"


class WebInterface:
    """
    Multi-user web chat.

    Each browser tab gets its own token (issued on page load) and therefore
    its own core session — two laptops no longer share history, identity,
    or a response queue. The chosen identity is stored per-browser in
    localStorage (gr.BrowserState, Gradio 5+) so it survives reloads;
    on older Gradio it falls back to per-tab state that resets on reload.
    """

    SESSION_TTL = 30 * 60  # 30 minutes

    def __init__(self, core_processor):
        self.core_processor = core_processor
        self._sessions      = {}   # tab token -> session_id
        self._speakers      = {}   # tab token -> friendly name or None
        self._last_active   = {}   # session_id -> monotonic ts

    # ── Users ─────────────────────────────────────────────────────────────────

    def _user_choices(self) -> list:
        registry = getattr(self.core_processor, 'presence_registry', None)
        if registry is None:
            return [ANON]
        return [ANON] + [registry.get_friendly_name(u)
                         for u in registry.all_users()]

    # ── Session management (per tab token) ────────────────────────────────────

    def _ensure_session(self, token: str) -> str:
        session_id = self._sessions.get(token)
        if session_id is None or self.core_processor.get_session(session_id) is None:
            session_id   = str(uuid.uuid4())
            core_session = self.core_processor.create_session(session_id)
            if core_session is not None:
                core_session[KEY_INTERFACE_MODE] = InterfaceMode.GENERAL
                core_session['interface']        = InterfaceMode.GENERAL.value
                core_session['endpoint_id']      = session_id
                speaker = self._speakers.get(token)
                if speaker:
                    # Anchors identity: memory injection, turn logging and
                    # end-of-session fact extraction all resolve from this.
                    core_session['speaker'] = speaker
            self._sessions[token] = session_id
        self._last_active[session_id] = time.monotonic()
        return session_id

    def _close_tab_session(self, token: str):
        session_id = self._sessions.pop(token, None)
        if session_id:
            self.core_processor.close_session(session_id)
            self._last_active.pop(session_id, None)

    def _close_stale(self, session_id: str):
        """Called by the reaper when a session has been idle too long."""
        for token, sid in list(self._sessions.items()):
            if sid == session_id:
                self._sessions.pop(token, None)
        self._last_active.pop(session_id, None)
        self.core_processor.close_session(session_id)

    # ── UI ────────────────────────────────────────────────────────────────────

    def run(self):

        reaper = SessionReaper(
            get_active_sessions = lambda: {
                sid: sid for sid in self._sessions.values()
            },
            get_last_active = lambda: dict(self._last_active),
            close_fn        = self._close_stale,
            ttl_seconds     = self.SESSION_TTL,
            check_interval  = 60,
        )
        reaper.start()

        choices = self._user_choices()

        def process_message(message, history, token):
            if not token:
                token = str(uuid.uuid4())   # defensive: load event missed
            session_id = self._ensure_session(token)

            t = threading.Thread(
                target = self.core_processor.process_input,
                kwargs = {"input_text": message, "session_id": session_id},
                daemon = True,
            )
            t.start()

            session            = self.core_processor.get_session(session_id)
            assistant_response = ""

            while True:
                chunk = get_response_queue(session).get()
                if chunk is None:
                    return
                assistant_response += chunk
                yield {"role": "assistant", "content": assistant_response}

        css = """
            html, body, .gradio-container { height: 100% !important; }
            #chatbot { height: calc(100vh - 330px) !important; }
        """

        with gr.Blocks(title="Supernova") as demo:
            gr.Markdown("# Supernova")

            # Per-tab token: issued fresh on every page load.
            tab_token = gr.State(None)

            # Identity store: localStorage-backed on Gradio 5+ so the
            # browser remembers who you are across reloads.
            if hasattr(gr, 'BrowserState'):
                speaker_store = gr.BrowserState(ANON)
            else:
                speaker_store = gr.State(ANON)

            user_dd = gr.Dropdown(
                choices = choices,
                value   = ANON,
                label   = "Who are you?",
            )

            chatbot = gr.Chatbot(elem_id="chatbot")
            chat    = gr.ChatInterface(
                fn                = process_message,
                chatbot           = chatbot,
                additional_inputs = [tab_token],
                textbox           = gr.Textbox(
                    placeholder = "Message Supernova…",
                    autofocus   = True,
                ),
            )
            clear_btn = gr.Button("Clear History", variant="secondary")

            # ChatInterface keeps its own copy of the history in an internal
            # gr.State — a custom clear must wipe BOTH or old messages
            # resurrect on the next submit.
            clear_targets = [chatbot]
            inner_state = getattr(chat, 'chatbot_state', None)
            if isinstance(inner_state, gr.State):
                clear_targets.append(inner_state)

            def _wiped():
                return [] if len(clear_targets) == 1 \
                    else [[] for _ in clear_targets]

            def init_tab(saved):
                """Page load: issue a tab token, restore saved identity."""
                token  = str(uuid.uuid4())
                choice = saved if saved in choices else ANON
                self._speakers[token] = None if choice == ANON else choice
                return token, gr.update(value=choice)

            def clear_chat_history(token):
                self._close_tab_session(token)
                return _wiped()

            def select_user(choice, token):
                # New identity = new session: keeps attribution clean for
                # memory logging and extraction.
                self._speakers[token] = None if choice == ANON else choice
                self._close_tab_session(token)
                wiped = _wiped()
                if len(clear_targets) == 1:
                    return wiped, choice
                return (*wiped, choice)

            demo.load(
                fn      = init_tab,
                inputs  = [speaker_store],
                outputs = [tab_token, user_dd],
            )
            demo.load(fn=None, inputs=None, outputs=None, js=REFOCUS_JS)

            clear_btn.click(
                fn      = clear_chat_history,
                inputs  = [tab_token],
                outputs = clear_targets,
            )
            user_dd.change(
                fn      = select_user,
                inputs  = [user_dd, tab_token],
                outputs = clear_targets + [speaker_store],
            )

        demo.launch(share=False, server_name="0.0.0.0", css=css)