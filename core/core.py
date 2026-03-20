import importlib
import json
import threading
import time
try:
    import ollama
except Exception:
    ollama = None
import queue
from datetime import datetime
import os
from urllib.parse import urlparse

import tempfile

# interface and core config loader
from core.settings import AppConfig

# dynamic tool loader:
from core.tool_loader import ToolLoader

# dynamic precontext loader:
from core.precontext import PrecontextLoader, VoiceMode

# voice printing:
from core.speaker_id import load_profiles


class CoreProcessor:
    def __init__(self, config: AppConfig):
        self.sessions = {}

        tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../tools')
        config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../config')

        self.config = config
        self.model = config.ollama.model
        self.ollama_client = ollama.Client(host=config.ollama.host)

        self.precontext_loader = PrecontextLoader(config_dir)
 
        self.tool_loader = ToolLoader(
            tools_dir=tools_dir,
            config_dir=config_dir,
            app_config=config,
        )

    def _log(self, label, session=None, extra=None):
        """Lightweight timing/log helper. Prints wallclock time and elapsed since session start when available."""
        now = datetime.now().isoformat()
        perf = time.perf_counter()
        elapsed = None
        sid = None
        try:
            if session is not None:
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

    def create_session(self, session_id):
        self._log(f'Creating new session', extra=f"id={session_id}")
        # create a new session for each connection inbound to keep histories etc separate:
        self.sessions[session_id] = {
            'conversation_history': [],
            'response_queue': queue.Queue(),
            'response_finished': threading.Event(),
            'close_voice_channel': threading.Event(),  # for flagging channel close from functions in voice mode
            'cancel_event': threading.Event(),  # for cancelling current processing - tells LLM to stop!
            'ollama_stream': None,  # to hold the current ollama stream object so we can kill it if interrupting
            '_ts_start': time.perf_counter(),
        }

    def _flush_queue(self, q: queue.Queue):
        try:
            with q.mutex:
                q.queue.clear()
        except Exception:
            pass
    
    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def clear_history(self, session_id):
        session = self.get_session(session_id)
        if session is not None:
            session['conversation_history'] = []
           
    def _wrap_tool_result(self, name, payload):
        return json.dumps({
            "tool_result": {
                "name": name,
                "content": payload
            }
        }, ensure_ascii=False)  # added so that non ascii characters pass through properly

    def cancel_active_response(self, session_id: str):
        session = self.get_session(session_id)
        if not session:
            return
        # Signal the streaming loop to stop
        session['cancel_event'].set()
        # Drop any text already queued to speak
        self._flush_queue(session['response_queue'])

        # hard abort the current ollama stream if present:
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

        # drop any already-buffered text so the TTS side stops immediately:
        self._flush_queue(session['response_queue'])

        return

    def process_input(self, input_text, session_id, mode: VoiceMode = VoiceMode.PLAIN):
        # Retrieve the session-specific data - NOT SURE IF NEEDED NOW:
        session = self.get_session(session_id)
        if session is None:
            self._log("Session not found, creating new session...", extra=f"id={session_id}")
            self.create_session(session_id)
            session = self.get_session(session_id)
            self._log("Created new session", extra=f"id={session_id}")

        # clear the response_finished event at the start:
        session['response_finished'].clear()
        session['cancel_event'].clear()

        # as well as close_voice_channel event flag if relevant
        if mode != VoiceMode.PLAIN:
            session['close_voice_channel'].clear()

        conversation_history = session['conversation_history']

        # if we're starting a new conversation, create the pre-context and instructions:
        if conversation_history is None:
            conversation_history = []

        # now we create the system message:
        system_message = self.create_system_message(mode=mode, session=session)

        prompt = [system_message] + self.create_prompt(
            input_text=input_text,
            conversation_history=conversation_history,
        )

        # now the tools:
        prompt_tools = self.tool_loader.get_tools(mode=mode)

        # Debugging - print the full prompt then tools to console:
        #print(f"DEBUG PROMPT:\n==================\n{prompt}\n\n")
        #print(f"DEBUG TOOLS:\n==================\n{prompt_tools}\n\n")

        while True:
            full_response, tool_msg, tool_name, chat_tool_calls = self.send_to_ollama(
                prompt_text=prompt, 
                prompt_tools=prompt_tools, 
                session=session, 
                )

            # Build assistant history entry
            if full_response or chat_tool_calls:
                history_entry = {'role': 'assistant', 'content': full_response or ''}
                if chat_tool_calls:
                    history_entry['tool_calls'] = chat_tool_calls
                conversation_history.append(history_entry)

            if tool_msg:
                # add tool output to history and loop again
                conversation_history.append(tool_msg)

                if tool_name == "hangup_call":
                    break

                # rebuild prompt to let the model see the tool result
                prompt = self.update_prompt(conversation_history)
                continue

            else:
                break

        # if we break out of loop, set that we've finished to calling thread:
        self._log("Finished processing input and response", session=session)
        self.response_finished(session)

        # print('\ncore: Finishing processing input and response')

    def create_prompt(self, input_text, conversation_history):
        """
        Creates a formatted prompt for sending to Ollama, using separate sections for system, tools, and messages.

        Parameters:
        - input_text (str): The latest input from the user.
        - conversation_history (list): List of previous conversation messages with 'role' and 'content'.
        - voice (bool): Flag indicating whether this is a voice interaction, affecting tool selection.

        Returns:
        - str: The formatted prompt text.
        """

        history_section = conversation_history

        user_input_section = {
            'role': 'user',
            'content': input_text
        }

        prompt = history_section + [user_input_section]
        return prompt

    def update_prompt(self, conversation_history):
        history_section = conversation_history

        prompt = history_section


        return prompt

    def create_system_message(self, mode: VoiceMode = VoiceMode.PLAIN, session: dict = None):
        # Dynamically load precontext from personality/ files — picks up edits without restart
        full_pre_context = self.precontext_loader.get(mode)
 
        # Ask all registered context providers to inject their content in priority order.
        # Each provider returns a string (or empty string if nothing to add).
        # This is how tools like behaviour inject into the system prompt without
        # core.py needing to know anything about them specifically.
        for injection in self.tool_loader.get_context_injections(self):
            full_pre_context += f"\n\n{injection}"
 
        # Inject current time so the model can answer time questions without a tool call
        day      = datetime.now().strftime("%A")
        date     = datetime.now().strftime("%d %B %Y")   # e.g. 01 January 2024
        time     = datetime.now().strftime("%I:%M%p")    # e.g. 01:00PM
        timezone = "AEST"
 
        full_pre_context += f"\n\nCurrent Time: (use these for user answers as needed)\n Time: {time}\nDate: {date}\nDay: {day}\nTimezone (if needed): {timezone}\n"
 
        # Inject identified speaker if known:
        if session and session.get('speaker'):
            speaker = session['speaker']
            config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../config')
            profiles = load_profiles(config_dir)
            profile  = profiles.get(speaker, {})
            block    = f"[SPEAKER IDENTIFIED]\nYou are speaking with {speaker}."
            # let's not use this yet, we might just use it on tools directly
            #if profile.get('email'):
            #    block += f"\nTheir email address is {profile['email']}."
            #if profile.get('notes'):
            #    block += f"\n{profile['notes']}"
            full_pre_context += f"\n\n{block}"

        return {
            'role':    'system',
            'content': full_pre_context,
        }

    def send_to_ollama(self, prompt_text, prompt_tools, session):
        """
        Streams model output to response_queue. If a tool call JSON is detected,
        immediately:
        - push a short pre-feedback line to the queue,
        - execute the tool here,
        - return early with the tool message for conversation history.

        Returns:
        (full_response: str, tool_message: dict|None, tool_name: str|None)
        """
        response_queue = session['response_queue']
        cancel_event = session['cancel_event']

        try:
            response_content = ""
            tool_calls = []

            # this uses the simpler structured chat endpoint which is easier to drive, and seems to definitely support the think flag (raw was misbehaving)
            response_stream = self.ollama_client.chat(
                model=self.model,
                messages=prompt_text,
                stream=True,
                keep_alive=-1,
                think=False,
                tools=prompt_tools,
            )

            self._log("Starting to process chunks", session=session)
            first_chunk_yet = False

            for chunk in response_stream:
                if not first_chunk_yet:
                    self._log("Received first chunk", session=session)
                    first_chunk_yet = True
                    # Print header for the inline token stream
                    print(f"[STREAM] ", end="", flush=True)

                # new cancel logic if interrupted:
                if cancel_event and cancel_event.is_set():
                    # add that the user interrupted:
                    print(f"[core] response cancelled by user")
                    response_content += "\n[User interrupted]\n"
                    # stop streaming further tokens
                    break

                # messages:
                if chunk.message.content:
                    # stream chunks to log:
                    print(chunk.message.content, end="", flush=True)

                    response_content += chunk.message.content
                    response_queue.put(chunk.message.content)

                # tools:
                if chunk.message.tool_calls:
                    tool_calls.extend(chunk.message.tool_calls)

            print(f"\n[STREAM END] chars={len(response_content)} tools={len(tool_calls)}", flush=True)

            if tool_calls:
                tc = tool_calls[0]  # we only support one tool call per response for now, so just take the first if multiple come through
                tool_name_detected = tc.function.name
                tool_args = {
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

                    # Chat mode tool result format
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

            # no tools called, just return the full response for history and let the caller know no tool message:
            return response_content, None, None, None

        except Exception as e:
            # Check if it looks like a parsing/tool error vs a connection error
            error_str = str(e)
            self._log(f"Ollama exception", session=session, extra=error_str)
            
            # If we got some tool_calls before the error, try to recover
            if tool_calls:
                tool_name_detected = tool_calls[0].function.name if tool_calls[0].function else "unknown"
                tool_message = {
                    'role': 'tool',
                    'tool_name': tool_name_detected,
                    'content': json.dumps({"text": f"Tool call failed: {error_str}"}),
                }
                return response_content, tool_message, tool_name_detected, tool_calls

            # Otherwise it's a real error, put it in the queue for the user
            response_queue.put(f"\nError: {error_str}")
            return f"Error: {error_str}", None, None, None

    def send_whole_response(self, response_text, session):
        session['response_queue'].put(f"{response_text}")

    def response_finished(self, session):
        session['response_queue'].put(None)


