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
try:
    import ollama
except Exception:
    ollama = None
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
            full_response, tool_msg, chat_tool_calls = self.send_to_ollama(
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
                prompt = get_history(session)
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

    def run_headless(self, prompt: str, endpoint_id: str = '') -> str:
        """
        Run a prompt through the LLM with no live user present.
        Returns the text response. Tools can still fire callbacks/notifications.
        """
        session_id = f"headless_{uuid.uuid4().hex[:8]}"
        session    = self.create_session(session_id)

        session[KEY_INTERFACE_MODE] = InterfaceMode.GENERAL
        session['interface']        = 'headless'
        session['endpoint_id']      = endpoint_id
        session['_headless']        = True

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

        messages = conversation_history + [{'role': 'system', 'content': context}]

        turn_injections = []
        for injection in self.tool_loader.get_turn_context_injections(self, session, input_text):
            msg = {'role': 'system', 'content': injection}
            messages.append(msg)
            turn_injections.append(msg)

        return messages, turn_injections

    def create_system_message(self, session: dict) -> dict:
        """
        Build the system message dict for this turn.

        Assembly order:
          1. Agent personality (.md file selected by agent_mode)
          2. Tool context injections (behaviour rules, home automation entities, etc.)
          3. Interface declaration (LLM knows which interface it's on)
          4. Current time
          5. Speaker identification (if known)
        """
        interface_mode = get_interface_mode(session)
        agent_mode     = get_agent_mode(session)

        # 1. Agent personality
        full_context = self.precontext_loader.get(agent_mode)

        # 2. Tool context injections — session-aware
        for injection in self.tool_loader.get_context_injections(self, session):
            full_context += f"\n\n{injection}"

        # 3. Interface declaration
        full_context += f"\n\n[INTERFACE]\nThe user is interacting via: {interface_mode}"

        # 4. Speaker identification
        #speaker = get_speaker(session)
        #if speaker:
        #    full_context += f"\n\n[SPEAKER IDENTIFIED]\nYou are speaking with {speaker}."

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

            response_stream = self.ollama_client.chat(
                model      = self.model,
                messages   = prompt_text,
                stream     = True,
                keep_alive = -1,
                think      = False,
                tools      = prompt_tools,
                options    = {
                    'num_ctx': 16384,
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
                # Fire pre-tool LLM text immediately before tools run
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
                            tool_message = {
                                'role':      'tool',
                                'tool_name': tool_name_detected,
                                'content':   json.dumps({"text": "ok"}),
                            }
                        else:
                            tool_message = {
                                'role':      'tool',
                                'tool_name': tool_name_detected,
                                'content':   json.dumps(
                                    json.loads(wrapped).get('tool_result', {}).get('content', {})
                                ),
                            }
                    except Exception as e:
                        log.error("Tool execution error", extra={'data': f"{tool_name_detected}: {e}"})
                        tool_message = {
                            'role':      'tool',
                            'tool_name': tool_name_detected,
                            'content':   json.dumps({"text": f"Tool error: {e}"}),
                        }

                    tool_messages.append(tool_message)

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
    # Queue helpers
    # ──────────────────────────────────────────────────────────────────────────

    def send_whole_response(self, response_text: str, session: dict):
        """Push a complete pre-formed string to the response_queue."""
        get_response_queue(session).put(str(response_text))

    def response_finished(self, session: dict):
        """Push the None sentinel and set response_finished event."""
        get_response_queue(session).put(None)
        session[KEY_RESPONSE_DONE].set()