"""
core.py — Central LLM processing hub for Supernova.

Responsibilities:
  - Owns all active session state (conversation history, response queues, cancel events).
  - Builds prompts and system messages, injects context from tools.
  - Calls the Ollama streaming chat API and forwards token chunks to the response_queue
    so that voice interfaces can speak them as they arrive (streaming TTS pipeline).
  - Detects and executes tool calls inline, loops back for the model's follow-up.
  - Provides cancel_active_response() so voice_remote can interrupt mid-stream on barge-in.
  - Owns the EventStore and Scheduler so tools can persist and fire future events.
  - Provides schedule_event() and schedule_call() as the clean public API for tools.

Threading model:
  core.py runs inside whichever thread calls process_input().
  Interfaces should always call process_input() in a daemon Thread so the asyncio
  event loop stays free to stream TTS concurrently.

Mode model:
  interface_mode — set once at session creation by the interface (SPEAKER/PHONE/GENERAL).
                   Controls which tools are available and is injected into the system prompt.
  agent_mode     — can change mid-session via switch_agent_mode tool.
                   Controls which personality file is loaded and max tool loop iterations.
"""

import json
import threading
import time
import types
import typing
import inspect
try:
    import ollama
except Exception:
    ollama = None
try:
    import openai
except Exception:
    openai = None
import queue
from datetime import datetime, timezone, timedelta
import os
import uuid

from core.settings import AppConfig
from core.tool_loader import ToolLoader
from core.precontext import PrecontextLoader
from core.mode_registry import ModeRegistry
from core.interface_mode import InterfaceMode
from core.session_state import (
    get_interface_mode, get_agent_mode,
    get_history, set_history, clear_history as ss_clear_history,
    get_speaker, get_endpoint_id,
    request_hangup, clear_hangup, hangup_requested,
    get_response_queue, get_cancel_event, get_immediate_send,
    is_immediate_send_only, get_ts_start, get_session_id,
    KEY_HISTORY, KEY_RESPONSE_QUEUE, KEY_RESPONSE_DONE,
    KEY_CLOSE_CHANNEL, KEY_CANCEL, KEY_OLLAMA_STREAM, KEY_TS_START,
    KEY_INTERFACE_MODE, KEY_AGENT_MODE, KEY_SESSION_ID
)
from core.event_store import EventStore
from core.scheduler import Scheduler
from core.presence_registry import PresenceRegistry
from core.logger import get_logger

log = get_logger('core')


class CoreProcessor:
    """
    Stateful hub that manages sessions and runs the LLM loop.

    One CoreProcessor is created at server startup and shared across all connections.
    Session isolation is achieved via the self.sessions dict, keyed by session_id
    (a UUID assigned per connection by voice_remote.py).

    Public API for tools (via ToolBase — do not call directly from tools):
        core.schedule_event(...)  — persist + schedule a future event
        core.schedule_call(...)   — convenience wrapper: schedule a voice call
        core.cancel_event(id)     — cancel a scheduled event by id
        core.list_events(type)    — list pending events, optionally by type
    """

    def __init__(self, config: AppConfig):
        self.sessions = {}

        tools_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../tools')
        config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../config')

        self.config = config
        self.model  = config.ollama.model

        self.ollama_client = ollama.Client(host=config.ollama.host)

        self.llamaserver_client = None
        if getattr(config, 'llama_server', None) and openai is not None:
            self.llamaserver_client = openai.OpenAI(
                base_url = f"{config.llama_server.host.rstrip('/')}/v1",
                api_key = "not-needed",
            )

        backend = getattr(config, 'backend', 'ollama')
        self._send_to_llm = (
            self.send_to_llamaserver if backend == 'llama_server' else self.send_to_ollama
        )

        # Agent mode registry — loads config/modes.yaml, hot-reloads on change
        self.mode_registry = ModeRegistry(config_dir)

        # Personality loader — loads agent mode .md files, hot-reloads on change
        self.precontext_loader = PrecontextLoader(config_dir)

        # Tool loader — scans tools/, hot-reloads on change
        self.tool_loader = ToolLoader(
            tools_dir  = tools_dir,
            config_dir = config_dir,
            app_config = config,
        )

        # ── Event system ──────────────────────────────────────────────────────
        self.event_store = EventStore(config_dir)
        self.scheduler   = Scheduler(self.event_store, self._on_event_fired)
        self.scheduler.start()

        # ── Presence registry ─────────────────────────────────────────────────
        # Tracks known users, active sessions, and contact resolution.
        self.presence_registry = PresenceRegistry(config_dir)

        # ── Interface references ───────────────────────────────────────────────
        # Interfaces register themselves by name via register_interface().
        # Tools use get_interface() to reach them generically.
        self._interfaces: dict = {}

        self._event_handlers  = {}
        self._presence_checks = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Event scheduling API  (called by tools via ToolBase)
    # ──────────────────────────────────────────────────────────────────────────

    def schedule_event(
        self,
        event_type:    str,
        label:         str,
        delay_seconds: float,
        endpoint_id:   str,
        announcement:  str,
        extra:         dict = None,
    ) -> str:
        """Schedule a future event. Returns event_id."""
        due_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        ).isoformat()
        return self.scheduler.schedule(
            event_type   = event_type,
            label        = label,
            due_at_iso   = due_at,
            endpoint_id  = endpoint_id,
            announcement = announcement,
            extra        = extra or {},
        )

    def schedule_call(
        self,
        endpoint_id:   str,
        announcement:  str,
        delay_seconds: float = 0,
        label:         str   = "call",
    ) -> str:
        """Convenience wrapper: schedule a voice call to an endpoint."""
        return self.schedule_event(
            event_type    = 'call',
            label         = label,
            delay_seconds = delay_seconds,
            endpoint_id   = endpoint_id,
            announcement  = announcement,
        )

    def cancel_event(self, event_id: str) -> bool:
        """Cancel a scheduled event by id. Returns True if it existed."""
        return self.scheduler.cancel(event_id)

    def list_events(self, event_type: str = None) -> list:
        """Return pending scheduled events, optionally filtered by type."""
        if event_type:
            return self.scheduler.list_type(event_type)
        return self.event_store.all()

    def register_interface(self, name: str, interface):
        """
        Register an interface instance by name so tools can reach it generically.
        Called by main.py after each interface is created.

        Usage (main.py):
            core_processor.register_interface('speaker', vr)
            core_processor.register_interface('telegram', telegram)

        Usage (tools):
            iface = core.get_interface('telegram')
        """
        self._interfaces[name] = interface
        log.info("Interface registered", extra={'data': f"name={name!r}"})

    def get_interface(self, name: str):
        """
        Return a registered interface by name, or None if not registered.

        Usage:
            iface = core.get_interface('telegram')
            if iface:
                iface.send_relay_message(endpoint_id, message)
        """
        return self._interfaces.get(name)

    def register_event_handler(self, callback_type: str, handler):
        """
        Register a handler for a given callback_type.
        handler signature: handler(event: dict) -> None
        Called from the scheduler thread — must be non-blocking.
        """
        self._event_handlers[callback_type] = handler
        log.info("Event handler registered", extra={'data': f"type={callback_type!r}"})

    def _on_event_fired(self, event: dict):
        callback_type = event.get('callback_type', 'voice_remote')
        log.info("Event fired", extra={'data': f"id={event.get('id')} label={event.get('label')!r} callback_type={callback_type!r}"})
        handler = self._event_handlers.get(callback_type)
        if handler is None:
            log.warning("No handler for event", extra={'data': f"callback_type={callback_type!r} id={event.get('id')}"})
            return
        try:
            handler(event)
        except Exception as e:
            log.error("Event handler error", extra={'data': f"{callback_type}: {e}"})

    def register_presence_check(self, interface: str, checker):
        """Register a presence checker. checker(endpoint_id: str) -> bool"""
        self._presence_checks[interface] = checker
        log.info("Presence check registered", extra={'data': f"interface={interface!r}"})

    def is_endpoint_reachable(self, interface: str, endpoint_id: str) -> bool:
        """Check if an endpoint is currently reachable via its interface."""
        checker = self._presence_checks.get(interface)
        if checker is None:
            return False
        try:
            return bool(checker(endpoint_id))
        except Exception as e:
            log.warning("Presence check error", extra={'data': f"{interface}: {e}"})
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────────────────────────────────

    def _elapsed(self, session: dict, extra: str = None) -> dict:
        """Build logging kwargs with elapsed time since session start."""
        start = get_ts_start(session)
        parts = []
        if start:
            parts.append(f"elapsed={time.perf_counter() - start:.3f}s")
        if extra:
            parts.append(extra)
        return {'extra': {'data': " | ".join(parts)}} if parts else {}
    
    def _log_prompt(self, prompt_text: list, prompt_tools: list, session: dict) -> None:
        """Write the exact prompt sent to Ollama to a per-session text file."""
        if not self.config.debug.log_prompts:
            return
        import os
        from core.session_state import get_session_id
        os.makedirs(self.config.debug.log_prompts_dir, exist_ok=True)
        session_id = get_session_id(session) or 'unknown'
        filepath   = os.path.join(self.config.debug.log_prompts_dir, f"{session_id}.txt")
        turn       = sum(1 for m in prompt_text if m.get('role') == 'user')
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"TURN {turn} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*80}\n\n")
            for msg in prompt_text:
                role      = msg.get('role', 'unknown')
                content   = msg.get('content', '')
                tool_name = msg.get('tool_name', '')
                tool_calls = msg.get('tool_calls', [])
                if tool_name:
                    f.write(f"[{role}: {tool_name}]\n")
                else:
                    f.write(f"[{role}]\n")
                if content:
                    f.write(f"{content}\n")
                if tool_calls:
                    f.write(f"{json.dumps(tool_calls, indent=2, default=str)}\n")
                f.write("\n")
            f.write(f"[tools]\n")
            f.write(f"{json.dumps(prompt_tools, indent=2, default=str)}\n")
            f.write("\n")

    def _log_wire_payload(self, model: str, oai_messages: list, oai_tools: list, extra_body: dict, session: dict) -> None:
        """
        Dump the exact JSON body about to be sent to llama-server, one file per
        turn, so turns can be diffed directly to spot anything that's silently
        shifting the prompt prefix and breaking the KV cache.
        """
        import os
        from core.session_state import get_session_id

        dump_dir = os.path.join(self.config.debug.log_prompts_dir, 'wire_dumps')
        os.makedirs(dump_dir, exist_ok=True)

        session_id = get_session_id(session) or 'unknown'
        turn = sum(1 for m in oai_messages if m.get('role') == 'user')

        payload = {
            'model': model,
            'messages': oai_messages,
            'tools': oai_tools if oai_tools else None,
            'stream': True,
            'extra_body': extra_body,
        }

        filepath = os.path.join(dump_dir, f"{session_id}_turn{turn:03d}.json")
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, default=str, ensure_ascii=False)

    # ──────────────────────────────────────────────────────────────────────────
    # Session management
    # ──────────────────────────────────────────────────────────────────────────

    def create_session(self, session_id: str) -> dict:
        """
        Create a fresh isolated state bucket for one conversation.

        Returns the session dict. Interfaces must set interface_mode immediately
        after creation — it defaults to GENERAL as a safe fallback:

            session = core.create_session(session_id)
            session[KEY_INTERFACE_MODE] = InterfaceMode.SPEAKER
        """
        log.info("Session created", extra={'data': f"id={session_id}"})
        session = {
            KEY_HISTORY:        [],
            KEY_RESPONSE_QUEUE: queue.Queue(),
            KEY_RESPONSE_DONE:  threading.Event(),
            KEY_CLOSE_CHANNEL:  threading.Event(),
            KEY_CANCEL:         threading.Event(),
            KEY_OLLAMA_STREAM:  None,
            KEY_TS_START:       time.perf_counter(),
            # Modes — interfaces must override interface_mode after creation
            KEY_INTERFACE_MODE: InterfaceMode.GENERAL,
            KEY_AGENT_MODE:     self.mode_registry.default(),
            'session_id':       session_id,
        }
        self.sessions[session_id] = session
        return session

    def _flush_queue(self, q: queue.Queue):
        """Atomically drain all items from a Queue without blocking."""
        try:
            with q.mutex:
                q.queue.clear()
        except Exception:
            pass

    def get_session(self, session_id: str) -> dict | None:
        """Return the session dict for session_id, or None if not found."""
        return self.sessions.get(session_id)

    def clear_history(self, session_id: str):
        """Wipe conversation history for a session."""
        session = self.get_session(session_id)
        if session is not None:
            ss_clear_history(session)

    def close_session(self, session_id: str):
        """
        Called by interfaces when a session ends cleanly.
        Fires all registered on_session_end plugin hooks then removes
        the session. Runs in a daemon thread so it never blocks the interface.
        """
        session = self.get_session(session_id)
        if session is None:
            return

        def _run():
            try:
                self.tool_loader.call_session_end_handlers(self, session)
            except Exception as e:
                log.error("Session end handler error", extra={'data': str(e)})
            finally:
                self.sessions.pop(session_id, None)
                log.info("Session closed", extra={'data': f"id={session_id}"})

        threading.Thread(target=_run, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────────
    # Tool result formatting
    # ──────────────────────────────────────────────────────────────────────────

    def _wrap_tool_result(self, name: str, payload) -> str:
        """Wrap a tool result in the standard JSON envelope."""
        return json.dumps({
            "tool_result": {
                "name":    name,
                "content": payload,
            }
        }, ensure_ascii=False)

    # ──────────────────────────────────────────────────────────────────────────
    # Barge-in / cancel
    # ──────────────────────────────────────────────────────────────────────────

    def cancel_active_response(self, session_id: str):
        """
        Hard-abort any in-flight LLM streaming and discard queued TTS text.
        Called by voice_remote when it receives an INT0 (barge-in) frame.

        Note: flush happens twice — once before stream.close() for fast TTS
        stop, and once after to catch tokens in the race window.
        """
        session = self.get_session(session_id)
        if not session:
            return

        cancel = get_cancel_event(session)
        if cancel:
            cancel.set()

        self._flush_queue(get_response_queue(session))

        stream = session.get(KEY_OLLAMA_STREAM)
        if stream is not None:
            try:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            except Exception as e:
                log.warning("Error closing ollama stream", extra={'data': str(e)})
            finally:
                session[KEY_OLLAMA_STREAM] = None

        self._flush_queue(get_response_queue(session))

    # ──────────────────────────────────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────────────────────────────────

    def process_input(self, input_text: str, session_id: str, images: list = None):
        """
        Run the full LLM loop for one user utterance and push output to the
        session's response_queue for consumption by the interface.

        interface_mode and agent_mode are read from the session — interfaces
        set them at session creation. No mode parameter needed here.
        """
        session = self.get_session(session_id)
        if session is None:
            log.warning("Session not found, creating on the fly", extra={'data': f"id={session_id}"})
            session = self.create_session(session_id)

        # Reset per-turn events
        session[KEY_RESPONSE_DONE].clear()
        session[KEY_CANCEL].clear()

        # Clear hangup flag at start of each turn for voice interfaces
        interface_mode = get_interface_mode(session)
        if interface_mode.is_voice():
            clear_hangup(session)

        conversation_history = get_history(session)

        # Build system message fresh each turn
        system_message = self.create_system_message(session=session)

        turn_prompt, turn_injections = self.create_prompt(
            input_text           = input_text,
            conversation_history = conversation_history,
            session              = session,
        )

        user_message = {'role': 'user', 'content': input_text}
        prompt = [system_message] + turn_prompt + [user_message]

        # Persist turn injections and user message to history
        for msg in turn_injections:
            get_history(session).append(msg)
        get_history(session).append(user_message)

        # Get tools filtered by both interface_mode and agent_mode
        agent_mode   = get_agent_mode(session)

        if '_headless_tools' in session:
            prompt_tools = session['_headless_tools']
        else:
            prompt_tools = self.tool_loader.get_tools(
                interface_mode = interface_mode,
                agent_mode     = agent_mode,
            )


        # max_tool_loops from agent mode config
        max_loops  = agent_mode.max_tool_loops if agent_mode else 5
        loop_count = 0
        had_images = bool(images)

        # ── Tool loop ─────────────────────────────────────────────────────────
        while loop_count < max_loops:
            full_response, tool_msg, chat_tool_calls = self._send_to_llm(
                prompt_text  = prompt,
                prompt_tools = prompt_tools,
                session      = session,
                images       = images,
            )
            images     = None
            loop_count += 1

            # Annotate last user message in history if images were attached
            if had_images:
                for msg in reversed(get_history(session)):
                    if msg.get('role') == 'user':
                        msg['content'] = f"[image was attached] {msg['content']}"
                        break
                had_images = False

            # Append assistant turn to history
            if full_response or chat_tool_calls:
                history_entry = {'role': 'assistant', 'content': full_response or ''}
                if chat_tool_calls:
                    history_entry['tool_calls'] = chat_tool_calls
                get_history(session).append(history_entry)

            # Flush pre-tool LLM text immediately for text interfaces
            # (already handled inside send_to_ollama before tool execution,
            # but kept here as belt-and-braces for any edge cases)
            #if tool_msg and full_response and full_response.strip():
            #    send_fn = get_immediate_send(session)
            #    if send_fn:
            #        send_fn(full_response.strip())
            #        self._flush_queue(get_response_queue(session))

            if tool_msg:
                for msg in tool_msg:
                    get_history(session).append(msg)
                if any(m.get('tool_name') == 'hangup_call' for m in tool_msg):
                    break
                prompt = [system_message] + get_history(session)
                continue
            else:
                break

        # Fire final response via immediate_send for text interfaces
        send_fn = get_immediate_send(session)
        if send_fn and is_immediate_send_only(session):
            final_chunks = []
            q = get_response_queue(session)
            while not q.empty():
                try:
                    chunk = q.get_nowait()
                    if chunk is not None:
                        final_chunks.append(chunk)
                except Exception:
                    break
            final_text = "".join(final_chunks).strip()
            if final_text:
                send_fn(final_text)

        log.info("Response finished", **self._elapsed(session))
        self.response_finished(session)

    # ──────────────────────────────────────────────────────────────────────────
    # Headless (background tasks)
    # ──────────────────────────────────────────────────────────────────────────

    def run_headless(
        self,
        prompt:           str,
        endpoint_id:      str  = '',
        tools:            list = None,
        session_overrides: dict = None,
        model:            str  = None,
        num_ctx:          int  = None,
    ) -> str:
        """
        Run a prompt through the LLM with no live user present.
        Returns the text response.

        Args:
            prompt:             The user-turn prompt text to process.
            endpoint_id:        Optional endpoint to associate with this session
                                (used for event callbacks from tools).
            tools:              Optional explicit list of schema functions to expose
                                to the LLM. Pass [] for no tools, or a list of
                                specific schema functions to restrict the tool set.
                                Defaults to None, which uses the standard tool loader
                                filtered by interface/agent mode (existing behaviour).
            session_overrides:  Optional dict merged into the session after creation.
                                Use to inject speaker, user identity, or any other
                                session keys needed by tools (e.g. _get_user_id).
                                Example: {'speaker': 'jesse', 'endpoint_id': '123'}
        """
        session_id = f"headless_{uuid.uuid4().hex[:8]}"
        session    = self.create_session(session_id)

        session[KEY_INTERFACE_MODE] = InterfaceMode.GENERAL
        session['interface']        = 'headless'
        session['endpoint_id']      = endpoint_id
        session['_headless']        = True

        # Merge any caller-supplied identity / context overrides
        if session_overrides:
            session.update(session_overrides)

        # model override:
        if model:
            session['_model_override'] = model
        if num_ctx:
            session['_num_ctx_override'] = num_ctx

        # If an explicit tool list is provided, stash it on the session so
        # process_input can pick it up instead of querying the tool loader.
        if tools is not None:
            session['_headless_tools'] = tools

        self.process_input(prompt, session_id)

        result = []
        q = get_response_queue(session)
        while True:
            try:
                chunk = q.get(timeout=30)
                if chunk is None:
                    break
                result.append(chunk)
            except Exception:
                break

        self.sessions.pop(session_id, None)
        return "".join(result)

    # ──────────────────────────────────────────────────────────────────────────
    # Prompt builders
    # ──────────────────────────────────────────────────────────────────────────

    def create_prompt(self, input_text: str, conversation_history: list, session: dict) -> tuple[list, list]:
        """
        Assemble the message list for the first Ollama call in a turn. 
        We add the time here because it was slowing down startup when included in the system message (wasn't caching).
        Returns:
            (full_message_list, turn_injections)
            turn_injections are the dynamic system messages added this turn,
            returned separately so process_input() can persist them to history.
        """

        now      = datetime.now()
        day      = now.strftime("%A")
        date     = now.strftime("%d %B %Y")
        time_str = now.strftime("%I:%M%p")
        speaker  = get_speaker(session)

        context = f"Current Time:\nTime: {time_str}\nDate: {date}\nDay: {day}\nTimezone: AEST"
        if speaker:
            context += f"\n\n[USER IDENTIFIED]\nYou are speaking with {speaker}."

        if session.get('_headless'):
            messages = conversation_history[:]
        else:
            messages = conversation_history + [{'role': 'system', 'content': context}]

        turn_injections = []
        # skip tool injections for headless sessions
        if not session.get('_headless'):
            for text, persist in self.tool_loader.get_turn_context_injections(self, session, input_text):
                msg = {'role': 'system', 'content': text}
                messages.append(msg)
                if persist:
                    turn_injections.append(msg)

        return messages, turn_injections

    def create_system_message(self, session: dict) -> dict:
        """
        Build the system message dict for this turn.

        Assembly order:
          1. Agent personality (.md file selected by agent_mode)
          2. Tool context injections (behaviour rules, home automation entities, etc.)
          3. Interface declaration (LLM knows which interface it's on)
        """
        if session.get('_model_override'):
            return {'role': 'system', 'content': 'You are a helpful assistant. Follow instructions precisely and concisely.'}
    
        interface_mode = get_interface_mode(session)
        agent_mode     = get_agent_mode(session)

        # 1. Agent personality
        full_context = self.precontext_loader.get(agent_mode)

        # 2. Tool context injections — session-aware
        for injection in self.tool_loader.get_context_injections(self, session):
            full_context += f"\n\n{injection}"

        # 3. Interface declaration
        full_context += f"\n\n[INTERFACE]\nThe user is interacting via: {interface_mode}"

        return {'role': 'system', 'content': full_context}

    # ──────────────────────────────────────────────────────────────────────────
    # Ollama streaming
    # ──────────────────────────────────────────────────────────────────────────

    def send_to_ollama(
        self,
        prompt_text:  list,
        prompt_tools: list,
        session:      dict,
        images:       list = None,
    ) -> tuple:
        """
        Stream a chat completion from Ollama and forward tokens to response_queue.

        Returns:
            (full_response: str, tool_messages: list | None, chat_tool_calls: list | None)
        """
        response_queue = get_response_queue(session)
        cancel_event   = get_cancel_event(session)

        try:
            response_content = ""
            tool_calls       = []

            # Attach images to last user message if provided
            if images:
                for msg in reversed(prompt_text):
                    if msg.get('role') == 'user':
                        msg['images'] = images
                        break

            # debug logging for full prompt and tool list send to Ollama so we can diagnose KV cache breaking changes between sessions/turns:
            self._log_prompt(prompt_text, prompt_tools, session)

            # if we have any model overrides (for headless mode) use them, otherwise default to self.model:
            model = session.get('_model_override', self.model)
            num_ctx = session.get('_num_ctx_override', self.config.ollama.num_ctx)  # allow override for headless calls etc, otherwise default to config value

            response_stream = self.ollama_client.chat(
                model      = model,
                messages   = prompt_text,
                stream     = True,
                keep_alive = -1,
                think      = False,
                tools      = prompt_tools,
                options    = {
                    'num_ctx': num_ctx,
                }

            )

            session[KEY_OLLAMA_STREAM] = response_stream
            log.debug("Stream started", **self._elapsed(session))
            first_chunk_yet = False

            for chunk in response_stream:
                if not first_chunk_yet:
                    log.debug("First chunk received", **self._elapsed(session))
                    first_chunk_yet = True
                    print(f"[STREAM] ", end="", flush=True)

                # Barge-in check
                if cancel_event and cancel_event.is_set():
                    log.debug("Response cancelled by user", **self._elapsed(session))
                    response_content += "\n[User interrupted]\n"
                    break

                if chunk.message.content:
                    print(chunk.message.content, end="", flush=True)
                    response_content += chunk.message.content
                    response_queue.put(chunk.message.content)

                if chunk.message.tool_calls:
                    tool_calls.extend(chunk.message.tool_calls)

            log.debug("Stream ended", extra={'data': f"chars={len(response_content)} tools={len(tool_calls)}"})
            session[KEY_OLLAMA_STREAM] = None

            # ── Tool execution ────────────────────────────────────────────────
            if tool_calls:
                tool_messages = self._execute_tool_calls(tool_calls, session, response_content, response_queue)
                return response_content, tool_messages, tool_calls
            
            return response_content, None, None

        except Exception as e:
            session[KEY_OLLAMA_STREAM] = None
            return self._handle_ollama_error(
                error            = e,
                session          = session,
                response_content = response_content,
                tool_calls       = tool_calls,
                response_queue   = response_queue,
            )

    def _handle_ollama_error(self, error, session, response_content, tool_calls, response_queue) -> tuple:
        """Classify an Ollama exception and return the appropriate response tuple."""
        if isinstance(error, ConnectionError):
            msg = "Cannot connect to Ollama — is it running? Try: ollama serve"
            log.error("Ollama connection error", extra={'data': str(error)})
            response_queue.put(f"\nError: {msg}")
            return f"Error: {msg}", None, None

        if ollama and isinstance(error, ollama.RequestError):
            msg = f"Bad request to Ollama: {error.error}"
            log.error("Ollama request error", extra={'data': msg})
            response_queue.put(f"\nError: {msg}")
            return f"Error: {msg}", None, None

        if ollama and isinstance(error, ollama.ResponseError):
            log.error("Ollama response error", extra={'data': f"status={error.status_code} {error.error}"})

            if error.status_code == 404:
                log.info("Model not found — attempting pull", extra={'data': self.model})
                try:
                    self.ollama_client.pull(self.model)
                    msg = "Model was missing and has been pulled. Please try again."
                    log.info("Model pulled successfully", extra={'data': self.model})
                except Exception as pull_err:
                    msg = f"Model '{self.model}' not found and pull failed: {pull_err}"
                    log.error("Model pull failed", extra={'data': str(pull_err)})
                response_queue.put(f"\nError: {msg}")
                return f"Error: {msg}", None, None

            if error.status_code == 500:
                self._send_retry_notice(session)
                return self._loop_back_bad_tool(
                    response_content, tool_calls, session,
                    "Your previous response contained malformed JSON or an invalid tool call. Please try again."
                )

            msg = f"Ollama server error ({error.status_code})"
            response_queue.put(f"\nError: {msg}")
            return f"Error: {msg}", None, None

        if tool_calls:
            return self._loop_back_bad_tool(
                response_content, tool_calls, session,
                f"Tool call failed: {error}. Please try again."
            )

        msg = str(error)
        log.error("Unexpected Ollama exception", extra={'data': msg})
        response_queue.put(f"\nError: {msg}")
        return f"Error: {msg}", None, None

    def _loop_back_bad_tool(self, response_content, tool_calls, session, message) -> tuple:
        """Return a tool error that loops back to the LLM so it can retry."""
        tool_name = "unknown"
        try:
            if tool_calls and tool_calls[0].function:
                tool_name = tool_calls[0].function.name
        except Exception:
            pass
        log.warning("Looping back bad tool call", extra={'data': f"tool={tool_name}"})
        tool_message = {
            'role':      'tool',
            'tool_name': tool_name,
            'content':   json.dumps({"text": message}),
        }
        return response_content, [tool_message], tool_calls

    def _send_retry_notice(self, session: dict):
        """Send a friendly retry message to the user before looping back."""
        msg     = "Oops, I made a mistake — let me try that again."
        send_fn = get_immediate_send(session)
        if send_fn:
            send_fn(msg)
        else:
            get_response_queue(session).put(msg)

    # ──────────────────────────────────────────────────────────────────────────
    # Shared tool execution (used by both send_to_ollama and send_to_llamaserver)
    # ──────────────────────────────────────────────────────────────────────────

    def _execute_tool_calls(self, tool_calls: list, session: dict, response_content: str, response_queue: queue.Queue) -> list:
        """
        Run each tool call and return the list of resulting tool-role messages
        to append to history.

        tool_calls entries just need to expose:
            .function.name        — str
            .function.arguments   — dict
            .id                   — str, optional (only used by the llama-server
                                     wire format; Ollama's path doesn't need it)
        """
        if response_content.strip():
            send_fn = get_immediate_send(session)
            if send_fn:
                send_fn(response_content.strip())
                self._flush_queue(response_queue)

        tool_messages = []
        for tc in tool_calls:
            tool_name_detected = tc.function.name
            tool_args          = {
                'name':       tool_name_detected,
                'parameters': dict(tc.function.arguments) if tc.function.arguments else {},
            }
            tc_id = getattr(tc, 'id', None)
            log.info("Tool call detected", **self._elapsed(session, tool_name_detected))

            try:
                fn = self.tool_loader.get_executor(tool_name_detected)
                if fn is None:
                    log.warning("Tool not found", extra={'data': tool_name_detected})
                    wrapped = self._wrap_tool_result(tool_name_detected, {"text": "Unknown tool"})
                else:
                    log.info("Executing tool", **self._elapsed(session, tool_name_detected))
                    t_tool  = time.perf_counter()
                    wrapped = fn(tool_args=tool_args, session=session, core=self)
                    dt_tool = time.perf_counter() - t_tool
                    log.info("Tool finished", **self._elapsed(session, f"{tool_name_detected} dur={dt_tool:.3f}s"))

                if wrapped is None:
                    content = json.dumps({"text": "ok"})
                else:
                    content = json.dumps(
                        json.loads(wrapped).get('tool_result', {}).get('content', {})
                    )
            except Exception as e:
                log.error("Tool execution error", extra={'data': f"{tool_name_detected}: {e}"})
                content = json.dumps({"text": f"Tool error: {e}"})

            tool_message = {
                'role':      'tool',
                'tool_name': tool_name_detected,
                'content':   content,
            }
            if tc_id:
                tool_message['tool_call_id'] = tc_id

            tool_messages.append(tool_message)

        return tool_messages

    # ──────────────────────────────────────────────────────────────────────────
    # llama-server (OpenAI-compatible) streaming
    # ──────────────────────────────────────────────────────────────────────────

    def send_to_llamaserver(
        self,
        prompt_text:  list,
        prompt_tools: list,
        session:      dict,
        images:       list = None,
    ) -> tuple:
        """
        Stream a chat completion from llama-server's OpenAI-compatible
        /v1/chat/completions endpoint and forward tokens to response_queue.

        Mirrors send_to_ollama's contract exactly:
            Returns (full_response: str, tool_messages: list | None, chat_tool_calls: list | None)
        """
        response_queue = get_response_queue(session)
        cancel_event   = get_cancel_event(session)

        response_content = ""
        tool_calls        = []

        try:
            oai_messages = self._messages_to_openai(prompt_text, images)
            oai_tools    = [self._tool_to_openai_schema(fn) for fn in (prompt_tools or [])]

            self._log_prompt(prompt_text, prompt_tools, session)

            model = session.get('_model_override', self.config.llama_server.model)

            extra_body = {}
            if self.config.llama_server.use_slots:
                if session.get('_headless'):
                    extra_body['id_slot'] = self.config.llama_server.headless_slot
                else:
                    interface = session.get('interface', 'general')
                    extra_body['id_slot'] = self.config.llama_server.slot_map.get(
                        interface, self.config.llama_server.default_slot
                    )

            # Dump exact wire payload for cache-diffing between turns
            if self.config.debug.log_prompts:
                self._log_wire_payload(model, oai_messages, oai_tools, extra_body, session)

            stream = self.llamaserver_client.chat.completions.create(
                model    = model,
                messages = oai_messages,
                tools    = oai_tools or openai.NOT_GIVEN,
                stream   = True,
                extra_body = extra_body,
            )

            session[KEY_OLLAMA_STREAM] = stream
            log.debug("Stream started", **self._elapsed(session))
            first_chunk_yet = False

            pending_tool_calls = {}

            for chunk in stream:
                if not first_chunk_yet:
                    log.debug("First chunk received", **self._elapsed(session))
                    first_chunk_yet = True
                    print(f"[STREAM] ", end="", flush=True)

                if cancel_event and cancel_event.is_set():
                    log.debug("Response cancelled by user", **self._elapsed(session))
                    response_content += "\n[User interrupted]\n"
                    break

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                if delta.content:
                    print(delta.content, end="", flush=True)
                    response_content += delta.content
                    response_queue.put(delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        entry = pending_tool_calls.setdefault(
                            tc_delta.index, {'id': None, 'name': None, 'arguments': ''}
                        )
                        if tc_delta.id:
                            entry['id'] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                entry['name'] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                entry['arguments'] += tc_delta.function.arguments

            log.debug("Stream ended", extra={'data': f"chars={len(response_content)} tools={len(pending_tool_calls)}"})
            session[KEY_OLLAMA_STREAM] = None

            tool_calls = self._finalize_tool_calls(pending_tool_calls)

            if tool_calls:
                tool_messages = self._execute_tool_calls(tool_calls, session, response_content, response_queue)
                return response_content, tool_messages, tool_calls

            return response_content, None, None

        except Exception as e:
            session[KEY_OLLAMA_STREAM] = None
            return self._handle_llamaserver_error(
                error            = e,
                session          = session,
                response_content = response_content,
                tool_calls       = tool_calls,
                response_queue   = response_queue,
            )

    def _finalize_tool_calls(self, pending_tool_calls: dict) -> list:
        """
        Turn the index-keyed, fragment-accumulated tool call dicts built up
        during streaming into the normalized objects _execute_tool_calls expects
        (.id, .function.name, .function.arguments-as-dict).
        """
        tool_calls = []
        for idx in sorted(pending_tool_calls):
            entry = pending_tool_calls[idx]
            if not entry['name']:
                continue
            try:
                args = json.loads(entry['arguments']) if entry['arguments'] else {}
            except json.JSONDecodeError:
                log.warning(
                    "Malformed tool-call arguments from llama-server",
                    extra={'data': f"name={entry['name']} raw={entry['arguments']!r}"}
                )
                args = {}
            tool_calls.append(types.SimpleNamespace(
                id       = entry['id'] or f"call_{idx}",
                function = types.SimpleNamespace(name=entry['name'], arguments=args),
            ))
        return tool_calls

    def _messages_to_openai(self, messages: list, images: list = None) -> list:
        """
        Translate our internal, backend-agnostic message list into OpenAI
        wire format, respecting Qwen3.5's template constraint that only one
        leading system message is honored.

        Only the very first system message in the list is kept as a true
        system-role entry. Any later system-role messages (the per-turn
        "current time" message, turn-context injections) are folded into
        the content of the next user message that follows them -- this
        preserves the live, per-turn injection timing your history is
        actually built with, instead of statically front-loading everything
        into one block at the top.
        """
        oai = []
        pending_system = []
        leading_system_seen = False
        last_user_idx = None

        for i, msg in enumerate(messages):
            role = msg.get('role')

            if role == 'system':
                content = msg.get('content', '')
                if not leading_system_seen:
                    leading_system_seen = True
                    oai.append({'role': 'system', 'content': content})
                elif content:
                    pending_system.append(content)
                continue

            if role == 'tool':
                oai.append({
                    'role':         'tool',
                    'tool_call_id': msg.get('tool_call_id') or msg.get('tool_name', 'unknown'),
                    'content':      msg.get('content', ''),
                })
                continue

            if role == 'assistant' and msg.get('tool_calls'):
                oai.append({
                    'role':       'assistant',
                    'content':    msg.get('content') or None,
                    'tool_calls': [
                        {
                            'id':   getattr(tc, 'id', None) or f"call_{i}_{j}",
                            'type': 'function',
                            'function': {
                                'name':      tc.function.name,
                                'arguments': json.dumps(tc.function.arguments),
                            },
                        }
                        for j, tc in enumerate(msg['tool_calls'])
                    ],
                })
                continue

            content = msg.get('content', '')
            if role == 'user' and pending_system:
                injected = '\n\n'.join(f"[context]\n{c}\n[/context]" for c in pending_system)
                content = f"{injected}\n\n{content}" if content else injected
                pending_system = []

            oai.append({'role': role, 'content': content})
            if role == 'user':
                last_user_idx = len(oai) - 1

        # Shouldn't normally happen given how process_input builds history
        # (injections are always immediately followed by a new user message),
        # but don't silently drop anything if it does.
        if pending_system:
            if oai and oai[0]['role'] == 'system':
                oai[0]['content'] += '\n\n' + '\n\n'.join(pending_system)
            else:
                oai.insert(0, {'role': 'system', 'content': '\n\n'.join(pending_system)})
                if last_user_idx is not None:
                    last_user_idx += 1

        if images and last_user_idx is not None:
            text = oai[last_user_idx]['content']
            content_blocks = [{'type': 'text', 'text': text}]
            for img_b64 in images:
                content_blocks.append({
                    'type':      'image_url',
                    'image_url': {'url': f"data:image/jpeg;base64,{img_b64}"},
                })
            oai[last_user_idx]['content'] = content_blocks

        return oai

    def _annotation_to_json_schema(self, annotation) -> dict:
        """
        Convert a single Python type annotation to a JSON-schema property dict.

        Handles:
        - Annotated[X, Field(description=...)]  -- unwraps to X, keeps the
            Pydantic Field's description (this is the pattern every schema
            function in this codebase actually uses)
        - Optional[X] / X | None                -- unwraps to X
        - list[X] / set[X] / tuple[X, ...]      -- {"type": "array", "items": ...}
        - dict[...]                              -- {"type": "object"}
        - flat types (str/int/float/bool/list/dict)

        A plain origin->name dict lookup misses all of the above except the
        flat case -- that was the root cause of the shopping-list bug (a
        list[str] parameter silently advertised to the model as "string",
        so it passed a bare string, which then iterated character-by-character
        against code expecting a list).
        """
        import typing

        origin = typing.get_origin(annotation)

        if origin is typing.Annotated:
            args = typing.get_args(annotation)
            underlying, *metadata = args
            schema = self._annotation_to_json_schema(underlying)
            for m in metadata:
                desc = getattr(m, 'description', None)
                if desc:
                    schema['description'] = desc
            return schema

        args = typing.get_args(annotation)

        if origin is typing.Union:
            non_none = [a for a in args if a is not type(None)]
            if non_none:
                return self._annotation_to_json_schema(non_none[0])
            return {'type': 'string'}

        if origin in (list, set, tuple):
            item_type = self._annotation_to_json_schema(args[0]) if args else {'type': 'string'}
            return {'type': 'array', 'items': item_type}

        if origin is dict:
            return {'type': 'object'}

        flat = {str: 'string', int: 'integer', float: 'number', bool: 'boolean',
                list: 'array', dict: 'object'}
        return {'type': flat.get(annotation, 'string')}

    def _tool_to_openai_schema(self, fn) -> dict:
        """
        Convert one of our tool schema functions into an OpenAI-style tool
        schema dict for llama-server, properly handling Annotated/Pydantic-Field
        parameter typing (see _annotation_to_json_schema for why this matters).
        """
        sig = inspect.signature(fn)
        doc = inspect.getdoc(fn) or ""
        description = doc.split("\n\n")[0].strip()

        properties = {}
        required   = []
        for name, param in sig.parameters.items():
            if name == 'self':
                continue
            annotation = param.annotation if param.annotation is not inspect.Parameter.empty else str
            properties[name] = self._annotation_to_json_schema(annotation)
            if param.default is inspect.Parameter.empty:
                required.append(name)

        return {
            'type': 'function',
            'function': {
                'name':        fn.__name__,
                'description': description,
                'parameters': {
                    'type':       'object',
                    'properties': properties,
                    'required':   required,
                },
            },
        }

    def _handle_llamaserver_error(self, error, session, response_content, tool_calls, response_queue) -> tuple:
        """Classify a llama-server (openai client) exception and return the response tuple."""
        if openai and isinstance(error, openai.APIConnectionError):
            msg = "Cannot connect to llama-server — is it running?"
            log.error("llama-server connection error", extra={'data': str(error)})
            response_queue.put(f"\nError: {msg}")
            return f"Error: {msg}", None, None

        if openai and isinstance(error, openai.APIStatusError):
            status = error.status_code
            log.error("llama-server response error", extra={'data': f"status={status} {error.message}"})

            if status == 500:
                self._send_retry_notice(session)
                return self._loop_back_bad_tool(
                    response_content, tool_calls, session,
                    "Your previous response contained malformed JSON or an invalid tool call. Please try again."
                )

            msg = f"llama-server error ({status})"
            response_queue.put(f"\nError: {msg}")
            return f"Error: {msg}", None, None

        if tool_calls:
            return self._loop_back_bad_tool(
                response_content, tool_calls, session,
                f"Tool call failed: {error}. Please try again."
            )

        msg = str(error)
        log.error("Unexpected llama-server exception", extra={'data': msg})
        response_queue.put(f"\nError: {msg}")
        return f"Error: {msg}", None, None

    # ──────────────────────────────────────────────────────────────────────────
    # Queue helpers
    # ──────────────────────────────────────────────────────────────────────────

    def send_whole_response(self, response_text: str, session: dict):
        """
        Push a complete pre-formed string to the response_queue.

        Wrapped in newlines: the voice-side sentence splitter treats newlines
        as hard boundaries, so the leading one flushes any partial sentence
        already buffered, and the trailing one guarantees this text is spoken
        immediately rather than held as an incomplete sentence until the next
        LLM round happens to produce more tokens.
        """
        get_response_queue(session).put("\n" + str(response_text) + "\n")

    def response_finished(self, session: dict):
        """Push the None sentinel and set response_finished event."""
        get_response_queue(session).put(None)
        session[KEY_RESPONSE_DONE].set()