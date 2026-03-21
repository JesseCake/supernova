"""
core.py — Central LLM processing hub for Supernova.
 
Responsibilities:
  - Owns all active session state (conversation history, response queues, cancel events).
  - Builds prompts and system messages, loads speaker profiles, injects context.
  - Calls the Ollama streaming chat API and forwards token chunks to the response_queue
    so that voice_remote.py can speak them as they arrive (streaming TTS pipeline).
  - Detects and executes tool calls inline, loops back for the model's follow-up.
  - Provides cancel_active_response() so voice_remote can interrupt mid-stream on barge-in.
  - Owns the EventStore and Scheduler so tools can persist and fire future events.
  - Provides schedule_event() and schedule_call() as the clean public API for tools.
 
Threading model:
  core.py runs inside whichever thread calls process_input().
  interfaces should always call process_input() in a daemon Thread so the asyncio
  event loop stays free to stream TTS concurrently.
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

# AppConfig: dataclass / pydantic model parsed from config.yaml — holds ollama host/model,
# voice model path, debug flags, etc.
from core.settings import AppConfig

# ToolLoader: scans the tools/ directory and dynamically imports tool modules.
# Each tool exposes get_definition() (returns the JSON schema shown to the model)
# and execute() (called when the model requests it).
from core.tool_loader import ToolLoader

# PrecontextLoader: reads personality/*.md files so personality can be edited without
# a server restart. VoiceMode controls which system-prompt variant is loaded
# (PLAIN = text API, SPEAKER = voice interface).
from core.precontext import PrecontextLoader, VoiceMode

# Voice ID: load_profiles: reads config/speaker_profiles.json and returns
# {name: {'embedding': ndarray, 'email': str, 'notes': str}}.
# Used here only to inject the identified speaker's name into the system prompt.
from core.speaker_id import load_profiles

# Event scheduling: For managing and spinning off event scheduling as needed/called
from core.event_store import EventStore
from core.scheduler import Scheduler


class CoreProcessor:
    """
    Stateful hub that manages sessions and runs the LLM loop.
 
    One CoreProcessor is created at server startup and shared across all connections.
    Session isolation is achieved via the self.sessions dict, keyed by session_id
    (a UUID assigned per connection by voice_remote.py).
 
    Public API for tools:
        core.schedule_event(...)  — persist + schedule a future event
        core.schedule_call(...)   — convenience wrapper: schedule a voice call
        core.cancel_event(id)     — cancel a scheduled event by id
        core.list_events(type)    — list pending events, optionally by type
        core.voice_remote         — set by main.py; gives tools access to initiate_call()
    """
    def __init__(self, config: AppConfig):
        # Dict of session_id → session dict. Each session holds its own history,
        # queue, and events so concurrent sessions don't interfere.
        self.sessions = {}

        # Resolve absolute paths relative to this file so the server can be
        # launched from any working directory.
        tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../tools')
        config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../config')

        self.config = config
        self.model = config.ollama.model

        # Synchronous ollama.Client — we use it in streaming mode (stream=True)
        # but the streaming itself is blocking-iterator-based, so we run it inside
        # a worker thread (see voice_remote._contact_core).
        self.ollama_client = ollama.Client(host=config.ollama.host)

        # PrecontextLoader caches the parsed .md files; call .get(mode) each turn
        # so file edits are picked up without a restart.
        self.precontext_loader = PrecontextLoader(config_dir)
 
        # ToolLoader discovers tools at construction time, but tool execution is
        # lazy (tools are imported the first time they're called).
        self.tool_loader = ToolLoader(
            tools_dir=tools_dir,
            config_dir=config_dir,
            app_config=config,
        )

        # ── Event system ──────────────────────────────────────────────────────
        # EventStore persists events to config/scheduled_events.json.
        # Scheduler fires them at the right time and calls _on_event_fired().
        # Both are started here so they're available to tools immediately.
        self.event_store = EventStore(config_dir)
        self.scheduler   = Scheduler(self.event_store, self._on_event_fired)
        self.scheduler.start()
 
        # ── voice_remote reference ─────────────────────────────────────────────
        # Set by main.py after VoiceRemoteInterface is created, e.g.:
        #   core_processor.voice_remote = vr
        # Gives _on_event_fired() access to initiate_call() without importing
        # voice_remote here (which would create a circular dependency).
        self.voice_remote = None

        self._event_handlers  = {}
        self._presence_checks = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Event scheduling API  (called by tools)
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
        """
        Schedule a future event that fires after delay_seconds.
 
        Persists to disk so it survives a server reboot. Returns the event id
        which can be passed to cancel_event() to cancel it before it fires.
 
        Args:
            event_type:    Category string, e.g. 'timer', 'reminder', 'alert'.
                           Used for filtering in list_events().
            label:         Human-readable name shown in listings, e.g. 'pasta'.
            delay_seconds: Seconds from now until the event fires.
            endpoint_id:   Which registered satellite to call when it fires.
            announcement:  Text passed as context to the LLM when initiating
                           the voice call, e.g. "The pasta timer is done.
                           Announce this naturally."
            extra:         Optional extra fields stored on the event dict,
                           e.g. {'duration_str': '5m'} for display in listings.
        """
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
        """
        Convenience wrapper: schedule a voice call to an endpoint.
 
        For immediate calls (delay_seconds=0) this still goes through the
        scheduler so it is non-blocking and consistent with deferred calls.
        Returns the event id.
        """
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
        """
        Return pending scheduled events, optionally filtered by type.
        Used by tools to answer "what timers do I have set?"
        """
        if event_type:
            return self.scheduler.list_type(event_type)
        return self.event_store.all()
 
    def register_event_handler(self, callback_type: str, handler):
        """
        Register a handler for a given callback_type.

        handler signature: handler(event: dict) -> None
        Called from the scheduler thread — must be non-blocking.

        Example:
            core.register_event_handler('voice_call', my_voice_handler)
            core.register_event_handler('sms',        my_sms_handler)
        """
        if not hasattr(self, '_event_handlers'):
            self._event_handlers = {}
        self._event_handlers[callback_type] = handler
        print(f"[core] registered event handler: {callback_type!r}")

    def _on_event_fired(self, event: dict):
        callback_type = event.get('callback_type', 'voice_remote')
        print(f"[core] event fired: id={event.get('id')} label={event.get('label')!r} "
              f"callback_type={callback_type!r} handlers={list(self._event_handlers.keys())}")
        handler = self._event_handlers.get(callback_type)

        if handler is None:
            print(f"[core] no handler registered for callback_type={callback_type!r} "
                  f"— event {event.get('id')} dropped")
            return

        try:
            handler(event)
        except Exception as e:
            print(f"[core] event handler error ({callback_type}): {e}")

    def register_presence_check(self, interface: str, checker):
        """
        Register a presence checker for an interface.

        checker signature: checker(endpoint_id: str) -> bool
        Returns True if the endpoint is currently reachable.

        Example:
            core.register_presence_check('telegram', lambda endpoint_id: True)
            core.register_presence_check('voice_remote', lambda eid: vr.get_endpoint(eid) is not None)
        """
        if not hasattr(self, '_presence_checks'):
            self._presence_checks = {}
        self._presence_checks[interface] = checker
        print(f"[core] registered presence check: {interface!r}")

    def is_endpoint_reachable(self, interface: str, endpoint_id: str) -> bool:
        """Check if an endpoint is currently reachable via its interface."""
        if not hasattr(self, '_presence_checks'):
            return False
        checker = self._presence_checks.get(interface)
        if checker is None:
            return False
        try:
            return bool(checker(endpoint_id))
        except Exception as e:
            print(f"[core] presence check error ({interface}): {e}")
            return False
    # ──────────────────────────────────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────────────────────────────────

    def _log(self, label, session=None, extra=None):
        """
        Lightweight structured logger that stamps wall-clock time and, when a
        session is active, the elapsed seconds since process_input() was called.
 
        Keeping timing visible here is important for diagnosing latency — you can
        immediately see where in the pipeline time is being lost (e.g. slow first
        token, slow tool execution, etc.).
        """
        now = datetime.now().isoformat()
        perf = time.perf_counter()
        elapsed = None
        sid = None
        try:
            if session is not None:
                # Reverse-lookup the session_id from the session object so we
                # don't have to pass it everywhere.
                sid = next((k for k, v in self.sessions.items() if v is session), None)
                start = session.get('_ts_start')
                if start:
                    elapsed = perf - start
        except Exception:
            sid = None

        msg = f"[TIMESTAMP] {now} | {label}"
        if sid is not None:
            msg += f" | session={sid}"
        if elapsed is not None:
            msg += f" | elapsed={elapsed:.4f}s"
        if extra is not None:
            msg += f" | {extra}"
        print(msg)

    # ──────────────────────────────────────────────────────────────────────────
    # Session management
    # ──────────────────────────────────────────────────────────────────────────
    def create_session(self, session_id):
        """
        Create a fresh isolated state bucket for one conversation.
 
        Key fields:
          conversation_history  - list of {role, content} dicts that grows each turn.
          response_queue        - Queue[str | None]. Core puts text chunks here;
                                  voice_remote drains it for TTS.
                                  None is the end-of-stream sentinel.
          response_finished     - Event set when process_input() returns. Not currently
                                  used by the TTS path (queue None sentinel is used
                                  instead), but useful for testing / future features.
          close_voice_channel   - Event set by hangup_call tool to signal that the
                                  session should end after this response.
          cancel_event          - Event set by cancel_active_response() to make the
                                  streaming loop break early on barge-in.
          ollama_stream         - Stored reference to the live ollama stream so that
                                  cancel_active_response() can call .close() on it.
          _ts_start             - perf_counter timestamp at session creation, used by
                                  _log() for elapsed timing.
        """
        self._log(f'Creating new session', extra=f"id={session_id}")
        self.sessions[session_id] = {
            'conversation_history':     [],
            'response_queue':           queue.Queue(),
            'response_finished':        threading.Event(),
            'close_voice_channel':      threading.Event(),
            'cancel_event':             threading.Event(),
            'ollama_stream':            None,
            '_ts_start':                time.perf_counter(),
        }

    def _flush_queue(self, q: queue.Queue):
        """
        Atomically drain all items from a Queue without blocking.
 
        Used by cancel_active_response() to discard any TTS chunks that were
        already enqueued but haven't been spoken yet, so the speaker stops
        mid-sentence rather than finishing the old response before hearing the
        new one.
        """
        try:
            with q.mutex:
                q.queue.clear()
        except Exception:
            pass
    
    def get_session(self, session_id):
        """Return the session dict for session_id, or None if not found."""
        return self.sessions.get(session_id)

    def clear_history(self, session_id):
        """Wipe conversation history for a session (used by tools or admin commands)."""
        session = self.get_session(session_id)
        if session is not None:
            session['conversation_history'] = []
           
    # ──────────────────────────────────────────────────────────────────────────
    # Tool result formatting
    # ──────────────────────────────────────────────────────────────────────────
    def _wrap_tool_result(self, name, payload):
        """
        Wrap a tool result in the JSON envelope that send_to_ollama() expects.
 
        The 'tool_result' wrapper lets the generic deserialization in send_to_ollama()
        stay simple regardless of which tool ran.
        ensure_ascii=False allows Unicode (e.g. names, emoji) through without escaping.
        """
        return json.dumps({
            "tool_result": {
                "name": name,
                "content": payload
            }
        }, ensure_ascii=False)  # added so that non ascii characters pass through properly

    # ──────────────────────────────────────────────────────────────────────────
    # Barge-in / cancel
    # ──────────────────────────────────────────────────────────────────────────
    def cancel_active_response(self, session_id: str):
        """
        Hard-abort any in-flight LLM streaming and discard queued TTS text.
 
        Called by voice_remote when it receives an INT0 (barge-in) frame from
        the satellite.  Three things must happen in order:
 
          1. Set cancel_event so the for-chunk loop in send_to_ollama() breaks
             at the next iteration (very fast — within one token's latency).
          2. Flush response_queue so voice_remote's TTS drain loop sees an empty
             queue immediately and stops speaking.
          3. Close the underlying HTTP stream on the ollama client so the server-
             side generation stops and we don't waste GPU cycles.
 
        Note: flush happens twice intentionally — once before stream.close()
        (to stop TTS quickly) and once after (to discard anything that arrived
        in the tiny race window between the two).
        """
        session = self.get_session(session_id)
        if not session:
            return
        
        # 1. Signal the streaming loop to stop at the next token boundary.
        session['cancel_event'].set()

        # 2. Drop any text already queued to speak — first flush (fast path).
        self._flush_queue(session['response_queue'])

        # 3. Hard-abort the ollama HTTP stream if one is active.
        stream = session.get('ollama_stream')
        if stream is not None:
            try:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            except Exception as e:
                print(f"[core] Error closing ollama stream: {e}")
            finally:
                session['ollama_stream'] = None

        # Second flush — catches any tokens that slipped through the race window
        # between cancel_event.set() and stream.close().
        self._flush_queue(session['response_queue'])

        return

    # ──────────────────────────────────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────────────────────────────────
    def process_input(self, input_text, session_id, mode: VoiceMode = VoiceMode.PLAIN, images: list = None):
        """
        Run the full LLM loop for one user utterance and push output to the
        session's response_queue for TTS consumption.
 
        Called in a daemon thread by voice_remote._contact_core() so the asyncio
        event loop stays free to drain and speak the queue concurrently.
 
        Flow:
          1. Reset per-turn events (response_finished, cancel_event, close_voice_channel).
          2. Build the system message (personality + speaker injection + time).
          3. Build the full prompt (system + history + new user message).
          4. Enter the tool loop:
               a. Call send_to_ollama() → streams chunks → response_queue.
               b. If a tool was called, execute it, append result to history, loop.
               c. If no tool, break.
          5. Push None sentinel to response_queue so TTS drain loop terminates.
          6. Set response_finished event (for any waiters).
        """
        session = self.get_session(session_id)
        if session is None:
            # Defensive: create a session on-the-fly if somehow missing.
            self._log("Session not found, creating new session...", extra=f"id={session_id}")
            self.create_session(session_id)
            session = self.get_session(session_id)
            self._log("Created new session", extra=f"id={session_id}")

        # Reset per-turn events — must happen before any await so the queue
        # reader in voice_remote doesn't see stale state.
        session['response_finished'].clear()
        session['cancel_event'].clear()

        # Also clear the channel-close flag for voice turns so a previous
        # hangup doesn't accidentally re-trigger.
        if mode != VoiceMode.PLAIN:
            session['close_voice_channel'].clear()

        conversation_history = session['conversation_history']

        # Defensive init — history should always be a list but guard against
        # accidental None assignment by a tool.
        if conversation_history is None:
            conversation_history = []


        # Build the system message fresh each turn so personality file edits,
        # speaker identification updates, and the current time are always current.
        system_message = self.create_system_message(mode=mode, session=session)

        # Prepend system message to conversation history for this call.
        # history_section is a list of prior {role, content} dicts;
        # create_prompt appends the new user message at the end.
        prompt = [system_message] + self.create_prompt(
            input_text=input_text,
            conversation_history=conversation_history,
        )

        # Get tool schemas for this mode. Voice mode gets a different subset
        # of tools vs plain text mode (e.g. hangup_call is voice-only).
        prompt_tools = self.tool_loader.get_tools(mode=mode)

        # ── Tool loop ─────────────────────────────────────────────────────────
        # We loop because a tool result must be fed back to the model so it can
        # generate a natural-language response incorporating the tool's output.
        # Most turns complete in one iteration (no tool call).
        while True:
            full_response, tool_msg, tool_name, chat_tool_calls = self.send_to_ollama(
                prompt_text=prompt, 
                prompt_tools=prompt_tools, 
                session=session, 
                images=images,
                )
            images = None  # only send images on first turn

            # Append assistant turn to history (text and/or tool_calls).
            if full_response or chat_tool_calls:
                history_entry = {'role': 'assistant', 'content': full_response or ''}
                if chat_tool_calls:
                    history_entry['tool_calls'] = chat_tool_calls
                conversation_history.append(history_entry)

            if tool_msg:
                # Append the tool result and loop to let the model see it.
                conversation_history.append(tool_msg)

                # hangup_call is the one tool whose result ends the session
                # rather than generating a follow-up response — break immediately.
                if tool_name == "hangup_call":
                    break

                # Rebuild prompt with the updated history so the next call
                # to send_to_ollama() includes the tool result.
                prompt = self.update_prompt(conversation_history)
                continue

            else:
                # No tool call → model gave a final response → done.
                break

        # Signal the TTS drain loop in voice_remote that there's nothing more coming.
        self._log("Finished processing input and response", session=session)
        self.response_finished(session)


    def run_headless(core, prompt, tools=None):
        """
        Run a prompt through the LLM with no user present.
        Returns the text response. The LLM can call tools like schedule_call or notify_user, whatever a tool needs assessed.
        """
        session_id = f"headless_{uuid.uuid4().hex[:8]}"
        core.create_session(session_id)
        session = core.get_session(session_id)
        
        # Mark as headless so tools know there's no live user
        session['interface']   = 'headless'
        session['endpoint_id'] = 'jesse_im'   # where to route any notifications
        
        core.process_input(prompt, session_id, mode=VoiceMode.PLAIN)
        
        # Drain the response queue
        result = []
        while True:
            chunk = session['response_queue'].get(timeout=30)
            if chunk is None:
                break
            result.append(chunk)
        
        core.sessions.pop(session_id, None)
        return "".join(result)


    # ──────────────────────────────────────────────────────────────────────────
    # Prompt builders
    # ──────────────────────────────────────────────────────────────────────────

    def create_prompt(self, input_text, conversation_history):
        """
        Assemble the message list for the first call to Ollama in a turn.
 
        Ollama's chat endpoint takes a list of {role, content} dicts.
        We append the new user message after all prior history.
        The system message is prepended by process_input() before this list.
 
        Returns a list of message dicts (NOT including the system message).
        """

        user_input_section = {
            'role': 'user',
            'content': input_text
        }
                # Concatenate prior history + new user message.
        # conversation_history is mutated in-place elsewhere, so we don't copy it —
        # this is intentional; process_input() owns the lifetime.
        prompt = conversation_history + [user_input_section]
        return prompt

    def update_prompt(self, conversation_history):
        """
        Rebuild the prompt after a tool call.
 
        At this point conversation_history already contains:
          … | user msg | assistant msg (with tool_calls) | tool result msg
 
        We return it as-is; send_to_ollama() will prepend the system message
        again via the outer prompt variable in process_input().
 
        Note: the system message is NOT included here — it stays in the `prompt`
        variable in process_input() and gets re-prepended there.
        TODO: delete?
        """
        return conversation_history

    def create_system_message(self, mode: VoiceMode = VoiceMode.PLAIN, session: dict = None):
        """
        Build the system message dict for this turn.
 
        Called fresh every turn so that:
          - Personality file edits take effect immediately (precontext_loader.get()).
          - Context injections from tools (e.g. behaviour tool) are current.
          - The timestamp is accurate.
          - The identified speaker's name is injected if known.
 
        Returns a {role: 'system', content: str} dict ready to prepend to the prompt.
        """
        # Load base personality text. PrecontextLoader handles caching and
        # file-watching internally.
        full_pre_context = self.precontext_loader.get(mode)
 
        # Let registered tools inject additional context into the system prompt.
        # This is the "context injection" pattern: tools that maintain state
        # (e.g. a behaviour/mood tool) can influence the system prompt without
        # core.py knowing about their internals. Each injection is a plain string.
        for injection in self.tool_loader.get_context_injections(self):
            full_pre_context += f"\n\n{injection}"
 
        # Inject current time. Doing this in the system prompt means the model
        # can answer "what time is it?" without a tool call, saving a round-trip.
        day      = datetime.now().strftime("%A")
        date     = datetime.now().strftime("%d %B %Y")   # e.g. 01 January 2024
        time     = datetime.now().strftime("%I:%M%p")    # e.g. 01:00PM
        timezone = "AEST"
 
        full_pre_context += (
            f"\n\nCurrent Time: (use these for user answers as needed)\n"
            f" Time: {time}\nDate: {date}\nDay: {day}\n"
            f"Timezone (if needed): {timezone}\n"
        )

        # Inject the identified speaker's name so the model can address them by name
        # and tools can use the identity without an extra lookup.
        if session and session.get('speaker'):
            speaker     = session['speaker']
            config_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../config')
            profiles    = load_profiles(config_dir)
            block       = f"[SPEAKER IDENTIFIED]\nYou are speaking with {speaker}."
            # Email and notes are loaded but intentionally not injected yet —
            # they'll be passed directly to tools that need them when that feature lands.
            full_pre_context += f"\n\n{block}"

        return {
            'role':    'system',
            'content': full_pre_context,
        }
    

    # ──────────────────────────────────────────────────────────────────────────
    # Ollama streaming
    # ──────────────────────────────────────────────────────────────────────────

    def send_to_ollama(self, prompt_text, prompt_tools, session, images=None):
        """
        Stream a chat completion from Ollama and forward tokens to response_queue.
 
        This is the hot path — everything that adds latency here is felt by the user
        as a pause before they hear speech.  Key design decisions:
 
          - stream=True: we get tokens as they arrive rather than waiting for the
            full response, so TTS can start speaking the first sentence while the
            model is still generating the rest.
          - cancel_event: checked on every token so barge-in stops generation within
            one token's latency (~10-50ms for typical models).
          - tool calls: when the model emits a tool call, we execute it here
            synchronously (in the same worker thread) and return early so that
            process_input()'s tool loop can feed the result back.
 
        Returns:
          (full_response: str,
           tool_message: dict | None,
           tool_name: str | None,
           chat_tool_calls: list | None)
 
        full_response is everything the model said (useful for history even if empty).
        tool_message is the {role:'tool', tool_name:…, content:…} dict to append.
        """
        response_queue = session['response_queue']
        cancel_event = session['cancel_event']

        try:
            response_content = ""
            tool_calls = []

            # attach images to the last user message if provided
            if images:
                for msg in reversed(prompt_text):
                    if msg.get('role') == 'user':
                        msg['images'] = images
                        break

            # Use the chat endpoint (not raw completion) — it handles tool schemas
            # natively and the structured response is easier to parse.
            # keep_alive=-1: keep the model loaded in VRAM indefinitely between calls.
            # think=False: disable chain-of-thought (faster, less verbose for voice, only possibly to disable in chat mode).
            response_stream = self.ollama_client.chat(
                model=self.model,
                messages=prompt_text,
                stream=True,
                keep_alive=-1,
                think=False,
                tools=prompt_tools,
            )

            # Store the live stream reference so cancel_active_response() can
            # call .close() on it to abort the HTTP connection immediately.
            session['ollama_stream'] = response_stream

            self._log("Starting to process chunks", session=session)
            first_chunk_yet = False

            for chunk in response_stream:
                if not first_chunk_yet:
                    self._log("Received first chunk", session=session)
                    first_chunk_yet = True
                    # Print inline token stream header for console debugging.
                    print(f"[STREAM] ", end="", flush=True)

                # ── Barge-in check ────────────────────────────────────────────
                # cancel_event is set by cancel_active_response() when INT0 arrives.
                # We check it on every token so we stop within one token's time.
                if cancel_event and cancel_event.is_set():
                    # add that the user interrupted:
                    print(f"[core] response cancelled by user")
                    response_content += "\n[User interrupted]\n"
                    break

                # ── Text chunk ────────────────────────────────────────────────
                if chunk.message.content:
                    print(chunk.message.content, end="", flush=True)
                    response_content += chunk.message.content
                    # Put the raw token on the queue — voice_remote drains this
                    # and buffers into sentences for TTS.
                    response_queue.put(chunk.message.content)

                # ── Tool call chunk ───────────────────────────────────────────
                # Tool calls arrive as structured objects, not text tokens.
                # We collect them and process after the stream ends.
                if chunk.message.tool_calls:
                    tool_calls.extend(chunk.message.tool_calls)

            print(f"\n[STREAM END] chars={len(response_content)} tools={len(tool_calls)}", flush=True)

            # Clear the stream reference now that iteration is complete.
            session['ollama_stream'] = None

            # ── Tool execution ────────────────────────────────────────────────
            if tool_calls:
                # Only the first tool call is executed per loop iteration.
                # If the model requests multiple, the others are silently dropped.
                # This is a deliberate simplification — most models only emit one.
                tc                  = tool_calls[0]
                tool_name_detected  = tc.function.name
                tool_args           = {
                    'name': tool_name_detected,
                    'parameters': dict(tc.function.arguments) if tc.function.arguments else {},
                }

                self._log(f"Detected tool call: {tool_name_detected}", session=session, extra=f"args={tool_args}")

                try:
                    fn = self.tool_loader.get_executor(tool_name_detected)
                    if fn is None:
                        self._log(f"Tool not found", session=session, extra=tool_name_detected)
                        wrapped = self._wrap_tool_result(tool_name_detected, {"text": "Unknown tool"})
                    else:
                        self._log(f"Executing tool", session=session, extra=tool_name_detected)
                        t_tool = time.perf_counter()
                        wrapped = fn(tool_args=tool_args, session=session, core=self)
                        dt_tool = time.perf_counter() - t_tool
                        self._log(f"Finished tool", session=session, extra=f"{tool_name_detected} dur={dt_tool:.3f}s")

                    # Format the tool result as an Ollama 'tool' role message.
                    if wrapped is None:
                        tool_message = {
                            'role': 'tool',
                            'tool_name': tool_name_detected,
                            'content': json.dumps({"text": "ok"}),
                        }
                    else:
                        tool_message = {
                            'role': 'tool',
                            'tool_name': tool_name_detected,
                            'content': json.dumps(
                                json.loads(wrapped).get('tool_result', {}).get('content', {})
                            ),
                        }

                except Exception as e:
                    tool_message = {
                        'role': 'tool',
                        'tool_name': tool_name_detected,
                        'content': json.dumps({"text": f"Tool error: {e}"}),
                    }
                
                return response_content, tool_message, tool_name_detected, tool_calls

            # No tool call — return the full streamed text for history.
            return response_content, None, None, None

        except Exception as e:
            # Check if it looks like a parsing/tool error vs a connection error
            error_str = str(e)
            self._log(f"Ollama exception", session=session, extra=error_str)
            
            # If we accumulated tool_calls before the exception, try to recover
            # by returning a synthetic error tool result rather than crashing.
            if tool_calls:
                tool_name_detected = tool_calls[0].function.name if tool_calls[0].function else "unknown"
                tool_message = {
                    'role': 'tool',
                    'tool_name': tool_name_detected,
                    'content': json.dumps({"text": f"Tool call failed: {error_str}"}),
                }
                return response_content, tool_message, tool_name_detected, tool_calls

            # Otherwise it's a real connection/parse error — push an error string
            # to the queue so the user hears something.
            response_queue.put(f"\nError: {error_str}")
            return f"Error: {error_str}", None, None, None


    # ──────────────────────────────────────────────────────────────────────────
    # Queue helpers (called by voice_remote at the end of _contact_core)
    # ──────────────────────────────────────────────────────────────────────────

    def send_whole_response(self, response_text, session):
        """
        Push a complete pre-formed string to the response_queue.
        Useful for tool-generated canned responses that bypass LLM streaming.
        """
        session['response_queue'].put(f"{response_text}")

    def response_finished(self, session):
        """
        Push the None sentinel to the response_queue and set response_finished.
 
        None is the end-of-stream signal: voice_remote's drain loop breaks on it.
        response_finished is set for any other waiters (tests, admin endpoints).
        """
        session['response_queue'].put(None)
        session['response_finished'].set()


