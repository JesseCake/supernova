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
import math

# wikipedia search
import wikipedia

# web search
from ddgs import DDGS

# for the web request/search sections:
import requests
from bs4 import BeautifulSoup
import requests
from urllib.parse import urlparse
import re

# for weather:
from collections import defaultdict

# home assistant API link
try:
    from homeassistant_api import Client as HAClient
except Exception:
    HAClient = None

# our precontext and tools info:
from config import precontext, tools
from config.tools import get_tools
import tempfile

# config
from config.settings import AppConfig



class CoreProcessor:
    def __init__(self, config: AppConfig):
        self.sessions = {}

        self.config = config
        self.model = config.ollama.model
        self.ollama_client = ollama.Client(host=config.ollama.host)
        self.ha_url = config.ha_url
        self.model_type = config.ollama.model_type  # currently supports "gemma3" or "qwen3" NOT NEEDED ANYMORE - TO REMOVE

        self.pre_context = precontext.llama3_context
        self.voice_pre_context = precontext.voice_context
        self.current_conversation = None

        # for self edited behaviour rules:
        self._behaviour_lock = threading.Lock()
        self._behaviour_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "../config/behaviour_overrides.json")
        self._behaviour_mtime = 0.0
        self.behaviour_overrides = {"global": []}
        self._load_behaviour_overrides(force=True)

        # for weather forecasts:
        self.weather_api_key = self.get_weather_key()

        # Home assistant integration
        self.ha_key = self.get_ha_key()
        #self.ha_url = 'http://192.168.20.3:8123/api'
        self.home_assistant = HAClient(self.ha_url, self.ha_key)
        self._ha_cache = {"stamp": 0.0, "text": ""}

        self.available_functions = {
            'hangup_call': self.hangup_call,
            #'get_current_time': self.get_current_time,
            'perform_search': self.perform_search,
            'open_website': self.open_website,
            'home_automation_action': self.home_automation_action,
            'check_weather': self.check_weather,
            'perform_math_operation': self.perform_math_operation,
            'update_behaviour': self.update_behaviour,
            'remove_behaviour': self.remove_behaviour,
            'list_behaviour': self.list_behaviour,
        }

        # conditional inclusion of ptv tool based on config presence:
        if config.ptv:
            from tools.ptv_trains import get_departures, format_departures
            self._ptv_get_departures = get_departures
            self._ptv_format_departures = format_departures
            self.available_functions['get_train_departures'] = self.get_train_departures

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
    
    def _load_behaviour_overrides(self, force=False):
        """Load overrides from disk; if not force, only reload when mtime changed."""
        try:
            st = os.stat(self._behaviour_path)
            mtime = st.st_mtime
        except FileNotFoundError:
            if force:
                self.behaviour_overrides = {"global": []}
                self._behaviour_mtime = 0.0
            return

        if not force and mtime == self._behaviour_mtime:
            return  # up-to-date

        try:
            with open(self._behaviour_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rules = data.get("global", [])
            # sanitize: strings only, de-dupe, cap lengths/count
            seen, out = set(), []
            for r in rules:
                if isinstance(r, str):
                    r = r.strip()[:200]
                    if r and r not in seen:
                        seen.add(r); out.append(r)
            self.behaviour_overrides = {"global": out[:20]}
            self._behaviour_mtime = mtime
            print(f"[behaviour] reloaded {len(out)} rule(s)")
        except Exception as e:
            print(f"[behaviour] load error: {e}")

    def _save_behaviour_overrides(self):
        os.makedirs(os.path.dirname(self._behaviour_path), exist_ok=True)
        payload = {"global": self.behaviour_overrides.get("global", [])[:20]}

        # atomic write
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._behaviour_path), prefix=".beh.tmp.")

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._behaviour_path)
            # update cached mtime to avoid immediate re-read
            try:
                self._behaviour_mtime = os.stat(self._behaviour_path).st_mtime
            except Exception:
                pass
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def update_behaviour(self, tool_args, session):
        rule = ((tool_args.get("parameters") or {}).get("rule") or "").strip()
        if not rule:
            return self._wrap_tool_result("update_behaviour", {"text":"No rule provided"})
        rule = rule[:200]
        with self._behaviour_lock:
            lst = self.behaviour_overrides.setdefault("global", [])
            if rule not in lst:
                lst.append(rule)
                self._save_behaviour_overrides()
        self.send_whole_response("Added Behaviour Rule.", session)
        return self._wrap_tool_result("update_behaviour", {"text": "Rule added"})

    def remove_behaviour(self, tool_args, session):
        rule = ((tool_args.get("parameters") or {}).get("rule") or "").strip()
        with self._behaviour_lock:
            lst = self.behaviour_overrides.setdefault("global", [])
            if rule in lst:
                lst.remove(rule)
                self._save_behaviour_overrides()
                msg = "Rule removed"
            else:
                msg = "Rule not found"
        self.send_whole_response("Removed Behaviour Rule.", session)
        return self._wrap_tool_result("remove_behaviour", {"text": msg})

    def list_behaviour(self, tool_args, session):
        """Return all active behavior rules that will be appended to the system message."""
        with self._behaviour_lock:
            rules = self.behaviour_overrides.get("global", [])

        if not rules:
            message = "No behaviour rules are currently active."
        else:
            # Build a friendly summary for voice/text
            message = "Current behaviour rules:\n" + "\n".join(f"- {r}" for r in rules)

        # Send it as a streaming/queued response so the voice interface can read it
        self.send_whole_response("Listing behaviour rules", session)
        return self._wrap_tool_result("list_behaviour", {"rules": rules})

    def get_ha_key(self):
        """Pulls the Home Assistant API key from file"""
        # Get the directory of the current script (core.py)
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Construct the full path to the home_assistant_api file
        file_path = os.path.join(script_dir, '../config/home_assistant_api')

        with open(file_path, "r") as file:
            for line in file:
                if line.startswith("HA_API_KEY"):
                    return line.split('=')[1].strip().strip('"')
                
    def _wrap_tool_result(self, name, payload):
        return json.dumps({
            "tool_result": {
                "name": name,
                "content": payload
            }
        })

    def get_weather_key(self):
        """Pulls the Weather API key from file"""
        # Get the directory of the current script (core.py)
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Construct the full path to the home_assistant_api file
        file_path = os.path.join(script_dir, '../config/weather_api')

        with open(file_path, "r") as file:
            for line in file:
                if line.startswith("WEATHER_API_KEY"):
                    return line.split('=')[1].strip().strip('"')

    def add_ha_to_pre_context(self, pre_context):
        # we only do this once every 30 seconds so we're not chewing time with each response:
        now = time.time()
        self._log("add_ha_to_pre_context start")
        if now - self._ha_cache["stamp"] > 30:  # refresh every 30s
            self._ha_cache["text"] = self.ha_get_available_switches_and_scenes()
            self._ha_cache["stamp"] = now
        self._log("add_ha_to_pre_context end", extra=f"len={len(self._ha_cache['text'])}")
        return pre_context + f"\n{self._ha_cache['text']}"

    def add_voice_to_pre_context(self, pre_context):
        """Adds the voice commands to pre-context"""
        pre_context += self.voice_pre_context()
        return pre_context

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

    def process_input(self, input_text, session_id, is_voice=False):
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
        if is_voice:
            session['close_voice_channel'].clear()

        conversation_history = session['conversation_history']

        # if we're starting a new conversation, create the pre-context and instructions:
        if conversation_history is None:
            conversation_history = []

        # now we create the system message:
        system_message = self.create_system_message(is_voice=is_voice)

        prompt = [system_message] + self.create_prompt(
            input_text=input_text,
            conversation_history=conversation_history,
        )

        # now we construct the tools:
        base_tools = get_tools(self.config)
        if is_voice:
            # it seems order is important, putting voice close channel tool first:
            #prompt_tools = tools.voice_tools + tools.general_tools
            prompt_tools = tools.voice_tools + base_tools
        else:
            prompt_tools = base_tools
            #prompt_tools = tools.general_tools

        while True:
            full_response, tool_msg, tool_name, chat_tool_calls = self.send_to_ollama(
                prompt_text=prompt, 
                prompt_tools=prompt_tools, 
                session=session, 
                available_functions=self.available_functions
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
        # right now we reload this each time so we can tweak it live, may be unnecessary in future:
        #loadprecontext = importlib.import_module('config.precontext')
        #importlib.reload(loadprecontext)

        '''full_pre_context = self.pre_context

        if voice:
            full_pre_context += self.voice_pre_context

        # we add this each time so we have up to date info from Home Assistant:
        full_pre_context += self.add_ha_to_pre_context(full_pre_context)

        system_section = {
            'role': 'system',
            'content': full_pre_context,
        }'''

        history_section = conversation_history

        #prompt = [system_section] + history_section
        prompt = history_section

        # print(f"DEBUGGING HISTORY: \n{prompt}")

        return prompt

    def create_system_message(self, is_voice=False):
        # pick up external/tool edits before building system text
        with self._behaviour_lock:
            self._load_behaviour_overrides(force=False)

        full_pre_context = self.pre_context

        # hard suppress thinking (test):
        #full_pre_context = "/no_think\n" + full_pre_context

        if is_voice:
            full_pre_context += self.voice_pre_context

        # we add this each time so we have up to date info from Home Assistant:
        #full_pre_context = self.add_ha_to_pre_context(full_pre_context)

        # append behaviour block if present
        rules = self.behaviour_overrides.get("global", [])
        if rules:
            full_pre_context += "\n\n[BEHAVIOUR_OVERRIDES]\n" + "\n".join(f"- {r}" for r in rules)

        # we'll add the current time to the system message so the model can use it if needed without calling the tool:
        day = datetime.now().strftime("%A")
        date = datetime.now().strftime("%d %B %Y")  # format: 01 January 2024
        time = datetime.now().strftime("%I:%M%p")  # format: 01:00PM
        timezone = "AEST"

        full_pre_context += f"\n\nCurrent Time: (use these for user answers as needed)\n Time: {time}\nDate: {date}\nDay: {day}\nTimezone: {timezone}\n"

        system_section = {
            'role': 'system',
            'content': full_pre_context,
        }

        #print(f"DEBUGGING SYSTEM: \n{system_section['content']}")

        return system_section

    def send_to_ollama(self, prompt_text, prompt_tools, session, available_functions=None):
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
                    fn = (available_functions or {}).get(tool_name_detected)
                    if fn is None:
                        self._log(f"Tool not found", session=session, extra=tool_name_detected)
                        wrapped = self._wrap_tool_result(tool_name_detected, {"text": "Unknown tool"})
                    else:
                        self._log(f"Executing tool", session=session, extra=tool_name_detected)
                        t_tool = time.perf_counter()
                        wrapped = fn(tool_args=tool_args, session=session)
                        dt_tool = time.perf_counter() - t_tool
                        self._log(f"Finished tool", session=session, extra=f"{tool_name_detected} dur={dt_tool:.3f}s")

                    # Chat mode tool result format
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

    def hangup_call(self, tool_args, session):
        # print("TOOL: CLOSE COMMS CHANNEL")
        # self.send_whole_response("Agent closed channel", session)
        session['close_voice_channel'].set()

    def get_current_time(self, tool_args, session):
        self.send_whole_response(f"Checking Time.\n\r", session)
        now = datetime.now()
        now_time = now.strftime('%I:%M%p')
        #return json.dumps({'response': f'current time: {now_time}'})
        return self._wrap_tool_result("get_current_time", {"text": f"System Message: Current time: {now_time} - tell the user this time"})

    def perform_math_operation(self, tool_args, session):
        self.send_whole_response("Calculating.\n\r", session)

        operation = tool_args.get('parameters').get('operation')
        number1 = float(tool_args.get('parameters').get('number1'))
        number2 = float(tool_args.get('parameters').get('number2'))

        try:
            if operation == "addition":
                result = number1 + number2
            elif operation == "subtraction":
                result = number1 - number2
            elif operation == "multiplication":
                result = number1 * number2
            elif operation == "division":
                if number2 == 0:
                    #return json.dumps({"response": "Division by zero is undefined."})
                    return self._wrap_tool_result("perform_math_operation", {"text": "Division by zero is undefined."})
                result = number1 / number2
            elif operation == "power":
                result = math.pow(number1, number2)
            elif operation == "square_root":
                if number1 < 0:
                    #return json.dumps({"response": "Square root of a negative number is undefined in real numbers."})
                    return self._wrap_tool_result("perform_math_operation", {"text": "Square root of a negative number is undefined in real numbers."})
                result = math.sqrt(number1)
            else:
                #return json.dumps({"response": f"Operation '{operation}' is not supported."})
                return self._wrap_tool_result("perform_math_operation", {"text": f"Operation '{operation}' is not supported."})

            print(f"core: calculated result = {result}")
            #return json.dumps({"response": f"Result of {operation}: {result}"})
            return self._wrap_tool_result("perform_math_operation", {"text": f"Result of {operation}: {result} - tell the user this result"})

        except Exception as e:
            #return json.dumps({"response": f"An error occurred: {str(e)}"})
            return self._wrap_tool_result("perform_math_operation", {"text": f"An error occurred: {str(e)}"})

    def perform_search(self, tool_args, session):
        query = tool_args.get('parameters').get('query')
        source = tool_args.get('parameters').get('source')
        num_responses = int(tool_args.get('parameters').get('number', 5))  # default reduced from 10, but tool can still be called with different number via the parameters if needed

        if source == 'web':
            self.send_whole_response(f"Performing Web Search on '{query}'.\n\r", session)

            return self._wrap_tool_result("perform_search", {
                "instruction": "Use these results to answer the user's question directly if possible, or choose the most relevant URL to open with the open_website tool for further research. Respond in English only. Do not reproduce these results verbatim.",
                "results": self._perform_web_search(query, num_responses),
            })

        elif source == 'wikipedia':
            self.send_whole_response(f"Performing Wikipedia search on subject {query}.\n\r", session)

            return self._wrap_tool_result("perform_search", {
                "instruction": "Use these results to answer the user's question. Respond in English only.",
                "results": self._perform_wikipedia_search(query),
            })

        else:
            return self._wrap_tool_result("perform_search", {
                "error": "Invalid source. Choose web or wikipedia."
            })

    def _perform_web_search(self, query, num_responses):
        self._log("_perform_web_search start", extra=f"q={query} n={num_responses}")
        try:
            results = []
            with DDGS() as ddgs:
                # region="au-en" for Australian English results
                for r in ddgs.text(
                    query=query,
                    region="au-en",
                    safesearch="moderate",
                    max_results=num_responses,
                ):
                    results.append({
                        "title": r.get("title")[:100],  # cap title length
                        "snippet": r.get("body")[:200], # cap snippet length
                        "link": r.get("href"),
                    })

            # Handle case where no results come back
            if not results:
                results.append({
                    "error": "no results found, possibly due to web search tool failure"
                })

            self._log("_perform_web_search end", extra=f"found={len(results)}")
            return results

        except Exception as e:
            # Catch any network or parsing issues
            self._log("_perform_web_search error", extra=str(e))
            return [{"error": f"Search failed: {e}"}]

    def _perform_wikipedia_search(self, query):
        self._log("_perform_wikipedia_search start", extra=f"q={query}")
        search_results = wikipedia.search(query)
        results = []

        if search_results:
            for title in search_results:
                try:
                    summary = wikipedia.summary(title, sentences=2)
                    page = wikipedia.page(title)

                    result = {
                        "title": title,
                        "summary": summary,
                        "url": page.url
                    }
                    results.append(result)
                except wikipedia.DisambiguationError:
                    results.append({
                        "title": title,
                        "summary": "Disambiguation page, multiple meanings exist",
                        "url": None
                    })
                except wikipedia.PageError:
                    results.append({
                        "title": title,
                        "summary": "Page does not exist.",
                        "url": None
                    })
        else:
            results.append({'error': 'No results, try another search term'})

        self._log("_perform_wikipedia_search end", extra=f"found={len(results)}")
        return results

    def open_website(self, tool_args, session, max_retries=3):
        url = (tool_args.get('parameters') or {}).get('url', '')
        # 1) normalize scheme
        if url and not urlparse(url).scheme:
            url = 'https://' + url  # prefer https

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        }

        self._log("open_website start", session=session, extra=url)
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
                resp.raise_for_status()

                # Parse static HTML (no JS rendering) for reliability
                soup = BeautifulSoup(resp.text, 'html.parser')
                
                # Remove boilerplate noise before truncating
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                    tag.decompose()
                
                text = soup.get_text(separator="\n", strip=True)

                # Collapse excessive blank lines
                text = re.sub(r'\n{3,}', '\n\n', text)

                # trim very large pages to keep the LLM responsive
                max_chars = 3000
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n...[truncated]"

                # UX ping only on success
                self._log("open_website success", session=session, extra=f"chars={len(text)}")
                self.send_whole_response(f"Opened website\n\r", session)  # speaks immediately
                #return self._wrap_tool_result("open_website", {"text": f"System Message: Text: {text} \n\n - Use the text scraped from this website to help answer the user's question, or to decide to try other websites/results. Do not simply read this out, you must interpret and summarise these reults to answer the user's question. If any of it is in another language, do not switch to that language for the user, stick to English unless asked or you have a cute phrase you just learned from the research."})
                return self._wrap_tool_result("open_website", {"text": (
                    "SYSTEM INSTRUCTIONS (follow these before reading the content below):\n"
                    "1. YOUR RESPONSE MUST BE IN ENGLISH ONLY. The page below may contain non-English text — do NOT reproduce, translate inline, or switch language. Summarise only in English.\n"
                    "2. Stay focused on the user's original question. Extract only what is relevant.\n"
                    "3. Do not read this content aloud verbatim — interpret and summarise it.\n"
                    "4. If the page content is irrelevant or unhelpful, say so briefly and suggest trying another source.\n"
                    f"\nPAGE CONTENT:\n{text}"
                    "REMEMBER: ENGLISH ONLY RESPONSE, INTERPRET AND SUMMARISE, STAY FOCUSED ON USER'S ORIGINAL QUESTION"
                )})

            except requests.exceptions.RequestException as e:
                last_err = e
                self._log("open_website attempt failed", session=session, extra=f"attempt={attempt} err={e}")
                # simple backoff
                time.sleep(min(1 + attempt, 3))
            except Exception as e:
                # unexpected parser errors
                return self._wrap_tool_result("open_website", {"text": f"Unexpected error for {url}: {e}"})

        # after retries, return the reason we failed (don’t claim success)
        self._log("open_website failed", session=session, extra=str(last_err))
        return self._wrap_tool_result("open_website", {
            "text": f"Failed to open {url} after {max_retries} attempts: {last_err}"
        })

    def home_automation_action(self, tool_args, session):
        action_type = tool_args.get('parameters').get('action_type')
        entity_id = tool_args.get('parameters').get('entity_id')
        #self.send_whole_response(f"Performing '{action_type}' action in Home Assistant", session)

        try:
            if action_type == "set_switch":
                state = tool_args.get('parameters').get('state')
                switch = self.home_assistant.get_domain("switch")
                self.send_whole_response(f"{entity_id} {state}.", session)

                if state == "on":
                    switch.turn_on(entity_id=f"switch.{entity_id}")
                else:
                    switch.turn_off(entity_id=f"switch.{entity_id}")

                #return json.dumps({'response': f'Successfully switched {entity_id} {state}'})
                return self._wrap_tool_result("home_automation_action", {"text": f'Successfully switched {entity_id} {state}'})

            elif action_type == "activate_scene":
                self.send_whole_response(f"Activating Scene '{entity_id}'.", session)
                scene_id = f"scene.{entity_id}"
                scene = self.home_assistant.get_domain("scene")
                scene.turn_on(entity_id=scene_id)

                #return json.dumps({'response': f'Successfully activated scene {scene_id}'})
                return self._wrap_tool_result("home_automation_action", {"text": f'Successfully activated scene {scene_id}'})

            else:
                #return json.dumps({'response': 'Error: Invalid action type specified. Use "set_switch" or "activate_scene" with this tool.'})
                return self._wrap_tool_result("home_automation_action", {"text": 'Error: Invalid action type specified. Use "set_switch" or "activate_scene" with this tool.'})

        except Exception as e:
            # self.send_whole_response(f"Error in tool! {e}", session)
            #return json.dumps({'response': f'Error performing {action_type} on {entity_id}: {str(e)}. Consider the names of the entities you are trying to control.'})
            return self._wrap_tool_result("home_automation_action", {"text": f'Error performing {action_type} on {entity_id}: {str(e)}. Consider the names of the entities you are trying to control.'})

    def ha_get_available_switches_and_scenes(self):
        """For adding to the end of your pre-context"""
        # Retrieve all states from Home Assistant
        all_states = self.home_assistant.get_states()

        # Filter for switches and scenes
        #available_switches = [entity.entity_id for entity in all_states if entity.entity_id.startswith("switch.")]
        available_switches = [entity.entity_id.split("switch.")[1] for entity in all_states if
                              entity.entity_id.startswith("switch.")]
        available_scenes = [entity.entity_id.split('.')[1] for entity in all_states if
                            entity.entity_id.startswith("scene.")]

        # Format the available switches and scenes into a string
        pre_context_info = (
                "Available Home Automation Entities for use with tools:\n"
                "Available Switch entity_id:\n" +
                "\n".join([f" - {switch}" for switch in available_switches]) +
                "\n\nAvailable Scene entity_id:\n" +
                "\n".join([f" - {scene}" for scene in available_scenes])
        )

        return pre_context_info

    def ha_list_entities_with_states(self):
        # Retrieve all states from Home Assistant
        all_states = self.home_assistant.get_states()

        # Print each entity's ID and current state
        #print("All Entities and Their States:")
        #for entity in all_states:
        #    print(f" - {entity.entity_id}: {entity.state}")

        # Domains of interest (so we don't give the LLM everything
        domains_of_interest = {
            "scene": "Available Lighting Scenes:",
            "switch": "Available Switches with current states:",
            #"light": "Available Lights with current states:",
            "media_player": "Available Media Players with current states:"
        }

        # Organize entities by domain
        categorized_entities = {domain: [] for domain in domains_of_interest}

        for entity in all_states:
            domain = entity.entity_id.split('.')[0]
            if domain in categorized_entities:
                categorized_entities[domain].append(entity)

            # Prepare the output dictionary
            response = {"Available Home Automation objects": {}}

            for domain, header in domains_of_interest.items():
                if categorized_entities[domain]:
                    if domain == "scene":
                        # Include only scene names
                        response["Available Home Automation objects"][header] = [
                            entity.entity_id.split('.')[1] for entity in categorized_entities[domain]
                        ]
                    else:
                        # Include entity IDs and their states for other domains
                        response["Available Home Automation objects"][header] = [
                            {entity.entity_id: entity.state} for entity in categorized_entities[domain]
                        ]
                else:
                    response["Available Home Automation objects"][header] = []

        # Return the formatted JSON to the LLM
        return json.dumps(response)
    

    def check_weather(self, tool_args, session):
        location = tool_args.get('parameters').get('location', 'Brunswick, VIC, Australia')
        #location = "Brunswick, VIC, Australia"
        forecast = tool_args.get('parameters').get('forecast', False)
        self.send_whole_response(f"\n\rFetching weather for {location}. ", session)

        try:
            if forecast:
                self.send_whole_response("(5 day forecast). \n\r", session)
                # Get the 5-day forecast
                if location == "Brunswick, VIC, Australia":
                    # Forecast weather by coordinates (problems with weather pulled from wrong location):
                    url = f"http://api.openweathermap.org/data/2.5/forecast?lat=-37.7746&lon=144.9631&appid={self.weather_api_key}&units=metric"
                else:
                    url = f"http://api.openweathermap.org/data/2.5/forecast?q={location}&appid={self.weather_api_key}&units=metric"

                response = requests.get(url)
                weather_data = response.json()

                if response.status_code == 200:
                    # Group by day and take the midday reading for each day as the forecast (or the closest to it)
                    days = defaultdict(list)
                    for entry in weather_data['list']:
                        day = entry['dt_txt'].split(' ')[0]
                        days[day].append(entry)

                    forecast_data = []
                    for day, entries in sorted(days.items())[:5]:  # 5 days
                        # prefer midday reading - TODO: find a better way to do this
                        midday = next((e for e in entries if '12:00' in e['dt_txt']), entries[0])
                        forecast_data.append({
                            'date': datetime.strptime(day, '%Y-%m-%d').strftime('%A, %d %B'),
                            'min_temp': f"{round(min(e['main']['temp_min'] for e in entries), 1)}°C",
                            'max_temp': f"{round(max(e['main']['temp_max'] for e in entries), 1)}°C",
                            'description': midday['weather'][0]['description'],
                        })
                    
                    result = {
                        "location": location,
                        "days": forecast_data,
                    }

                    #debug return:
                    #print(f"[check_weather] forecast result: \n{json.dumps(result, indent=2)}")

                    return self._wrap_tool_result("check_weather", {"forecast": result})

                else:
                    #return json.dumps({'check_weather': f"Failed to fetch forecast data: {weather_data.get('message', 'Unknown error')}"})
                    return self._wrap_tool_result("check_weather", {"text": f"Failed to fetch forecast data: {weather_data.get('message', 'Unknown error')}"})

            else:
                # Get the current weather
                self.send_whole_response("(current)\n\r", session)
                if location == "Brunswick, VIC, Australia":
                    # Current weather by coordinates (problems with weather pulled from wrong location):
                    url = f"http://api.openweathermap.org/data/2.5/weather?lat=-37.7746&lon=144.9631&appid={self.weather_api_key}&units=metric"
                else:
                    url = f"http://api.openweathermap.org/data/2.5/weather?q={location}&appid={self.weather_api_key}&units=metric"
                response = requests.get(url)
                weather_data = response.json()

                if response.status_code == 200:
                    main = weather_data['main']
                    weather_desc = weather_data['weather'][0]['description']
                    temp = main['temp']
                    feels_like = main['feels_like']
                    humidity = main['humidity']

                    result = {
                        'location': location,
                        'temperature': f"{temp}°C",
                        'feels_like': f"{feels_like}°C",
                        'humidity': f"{humidity}%",
                        'description': weather_desc
                    }
                    # Debugging:
                    #self.send_whole_response(f"Raw tool result: {result}", session)

                    return self._wrap_tool_result("check_weather", {"current_weather": result})

                else:
                    return self._wrap_tool_result("check_weather", {"text": f"Failed to fetch weather data: {weather_data.get('message', 'Unknown error')}"})

        except Exception as e:
            return self._wrap_tool_result("check_weather", {"text": f"Error fetching weather data: {str(e)}"})
        
    def get_train_departures(self, tool_args, session):
        count = tool_args.get('parameters', {}).get('count', 2)
        self.send_whole_response("Checking train times. ", session)
        try:
            cfg = self.config.ptv
            print(f"[ptv] fetching departures | stop={cfg.stop_id} name={cfg.stop_name} count={count} cache={cfg.cache_file}")
            deps = self._ptv_get_departures(
                cfg.api_key, cfg.stop_id, cfg.stop_name, cfg.cache_file, n=count
            )
            print(f"[ptv] got {len(deps)} departures: {deps}")
            result = self._ptv_format_departures(deps, cfg.stop_name, cfg.walk_minutes)
            print(f"[ptv] formatted result: {result}")
            return self._wrap_tool_result("get_train_departures", {"text": result})
        except FileNotFoundError:
            print(f"[ptv] ERROR: cache not found at {self.config.ptv.cache_file}")
            return self._wrap_tool_result("get_train_departures", {"text": "Train timetable cache not found. Run the cache update script first."})
        except Exception as e:
            print(f"[ptv] ERROR: {e}")
            return self._wrap_tool_result("get_train_departures", {"text": f"Error fetching train times: {e}"})



