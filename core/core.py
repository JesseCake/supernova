import importlib
import json
import threading
import time
import ollama
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

# home assistant API link
from homeassistant_api import Client as HAClient

# our precontext and tools info:
from config import precontext, tools
import tempfile


class CoreProcessor:
    def __init__(self):
        self.sessions = {}

        self.model = "gemma3:4b"
        self.ollama_client = ollama.Client(host='http://localhost:11434')
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
        self.ha_url = 'http://192.168.20.3:8123/api'
        self.home_assistant = HAClient(self.ha_url, self.ha_key)
        self._ha_cache = {"stamp": 0.0, "text": ""}

        self.available_functions = {
            'close_voice_channel': self.close_voice_channel,
            'get_current_time': self.get_current_time,
            'perform_search': self.perform_search,
            'open_website': self.open_website,
            'home_automation_action': self.home_automation_action,
            'check_weather': self.check_weather,
            'perform_math_operation': self.perform_math_operation,
            'update_behaviour': self.update_behaviour,
            'remove_behaviour': self.remove_behaviour,
            'list_behaviour': self.list_behaviour,
        }

    def create_session(self, session_id):
        print(f'Creating new session with ID: {session_id}')
        # create a new session for each connection inbound to keep histories etc separate:
        self.sessions[session_id] = {
            'conversation_history': [],
            'response_queue': queue.Queue(),
            'response_finished': threading.Event(),
            'close_voice_channel': threading.Event(),  # for flagging channel close from functions in voice mode
        }
    
    def get_session(self, session_id):
        return self.sessions.get(session_id)
    
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
        if now - self._ha_cache["stamp"] > 30:  # refresh every 30s
            self._ha_cache["text"] = self.ha_get_available_switches_and_scenes()
            self._ha_cache["stamp"] = now
        return pre_context + f"\n{self._ha_cache['text']}"

    def add_voice_to_pre_context(self, pre_context):
        """Adds the voice commands to pre-context"""
        pre_context += self.voice_pre_context()
        return pre_context

    def process_input(self, input_text, session_id, is_voice=False):
        print(f"[DEBUG] process_input called: session_id={session_id}, input_text={input_text!r}, is_voice={is_voice}")

        # Retrieve the session-specific data - NOT SURE IF NEEDED NOW:
        session = self.get_session(session_id)
        if session is None:
            self.create_session(session_id)
            session = self.get_session(session_id)

        # clear the response_finished event at the start:
        session['response_finished'].clear()

        # as well as close_voice_channel event flag if relevant
        if is_voice:
            session['close_voice_channel'].clear()

        conversation_history = session['conversation_history']

        # if we're starting a new conversation, create the pre-context and instructions:
        if conversation_history is None:
            conversation_history = []

        prompt = self.create_prompt(
            input_text=input_text,
            conversation_history=conversation_history,
        )

        conversation_history.append({
            'role': 'user',
            'content': input_text,
        })

        # Debugging: Print the formatted conversation context:
        # print(f"Generated prompt: {prompt}")

        # now we create the system message:
        system_message = self.create_system_message(voice=is_voice)

        # now we construct the tools:
        if is_voice:
            # it seems order is important, putting voice close channel tool first:
            prompt_tools = tools.voice_tools + tools.general_tools
        else:
            prompt_tools = tools.general_tools

        # debugging:
        # print(f"PROMPT TOOLS=\n\n {prompt_tools}")

        while True:
            #print(f"[DEBUG] Sending prompt to LLM: prompt={prompt}, system_message={system_message}, tools={prompt_tools}")
            full_response, tool_msg, tool_name = self.send_to_ollama(
                prompt_text=prompt, 
                prompt_system=system_message, 
                prompt_tools=prompt_tools, 
                session=session, 
                raw_mode=True,
                available_functions=self.available_functions
                )

            # We then record the outputs and actions in the conversation history:
            if full_response:
                conversation_history.append({
                    'role': 'assistant',
                    'content': full_response
                })

            if tool_msg:
                # add tool output to history and loop again
                conversation_history.append(tool_msg)

                if tool_name == "close_voice_channel":
                    break

                # rebuild prompt to let the model see the tool result
                prompt = self.update_prompt(conversation_history)
                continue

            else:
                break

        # if we break out of loop, set that we've finished to calling thread:
        print("core: Finished processing input and response")
        self.response_finished(session)

        # print('\ncore: Finishing processing input and response')

    def create_prompt(self, input_text, conversation_history):  # functions_json=None):
        """
        Creates a formatted prompt for sending to Ollama, using separate sections for system, tools, and messages.

        Parameters:
        - input_text (str): The latest input from the user.
        - conversation_history (list): List of previous conversation messages with 'role' and 'content'.
        - voice (bool): Flag indicating whether this is a voice interaction, affecting tool selection.

        Returns:
        - str: The formatted prompt text.
        """

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

        user_input_section = {
            'role': 'user',
            'content': input_text
        }

        #prompt = [system_section] + history_section + [user_input_section]
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

    def create_system_message(self, voice=False):
        # pick up external/tool edits before building system text
        with self._behaviour_lock:
            self._load_behaviour_overrides(force=False)

        full_pre_context = self.pre_context

        if voice:
            full_pre_context += self.voice_pre_context

        # we add this each time so we have up to date info from Home Assistant:
        full_pre_context = self.add_ha_to_pre_context(full_pre_context)

        # append behaviour block if present
        rules = self.behaviour_overrides.get("global", [])
        if rules:
            full_pre_context += "\n\n[BEHAVIOUR_OVERRIDES]\n" + "\n".join(f"- {r}" for r in rules)

        system_section = {
            'role': 'system',
            'content': full_pre_context,
        }

        #print(f"DEBUGGING SYSTEM: \n{system_section['content']}")

        return system_section
    
    def format_raw_prompt_gemma(self, system, messages, tools=None):
        """
        Build a raw prompt that matches Ollama's Gemma 3 template:

        user  -> <start_of_turn>user\n{content}<end_of_turn>\n
        model -> <start_of_turn>model\n{content}[<end_of_turn>\n if not last]
        system -> treated as a user turn at the top
        """
        parts = []

        # 1) System (Gemma has no dedicated system header; treat as a user turn)
        sys_text = ""
        if isinstance(system, dict):
            sys_text = (system.get("content") or "").strip()
        elif isinstance(system, str):
            sys_text = system.strip()

        # Append tools to system text (Gemma has no dedicated header)
        if tools:
            tools_json = json.dumps(tools, indent=2)
            sys_text += (
                "\n\n---\nTOOLS\n"
                "You may use tools when helpful. Available tools (JSON schema):\n"
                f"{tools_json}\n\n"
                "Tool-calling protocol:\n"
                "1) To call a tool, reply with EXACTLY one JSON object on a single line:\n"
                '   {"name":"<function_name>","parameters":{...}}\n'
                "   - No backticks, no surrounding text, no extra keys.\n"
                "2) After a tool runs, its result will arrive as a USER turn wrapped like:\n"
                "   <TOOL_RESULT>\n"
                '   {"tool_result":{"name":"<function_name>","content":{...}}}\n'
                "   </TOOL_RESULT>\n"
                "   - Read and use the JSON at tool_result.content.\n"
                "3) If another tool is needed, repeat step (1). Otherwise, answer the user.\n"
                "4) Only call ONE tool per message.\n"
            )

        if sys_text:
            parts.append("<start_of_turn>user\n")
            parts.append(sys_text)
            parts.append("<end_of_turn>\n")

        # 2) Conversation
        n = len(messages)
        for i, msg in enumerate(messages):
            last = (i == n - 1)
            role = msg.get("role")
            content = (msg.get("content") or "").rstrip()

            if role in ("user", "system"):
                parts.append("<start_of_turn>user\n")
                parts.append(content)
                parts.append("<end_of_turn>\n")
                if last:
                    # After the last user/system turn, open the model header to begin generation
                    parts.append("<start_of_turn>model\n")

            elif role == "assistant":
                parts.append("<start_of_turn>model\n")
                parts.append(content)
                if not last:
                    parts.append("<end_of_turn>\n")

            elif role == "tool":
                # Gemma template doesn't define a tool role. If you must include tool output,
                # feed it back as a user turn.
                parts.append("<start_of_turn>user\n")
                parts.append("<TOOL_RESULT>\n")
                parts.append(content.strip())  # this is the JSON from _wrap_tool_result(...)
                parts.append("\n</TOOL_RESULT>")
                parts.append("\n<end_of_turn>\n")
                if last:
                    parts.append("<start_of_turn>model\n")

            else:
                raise ValueError(f"Unsupported role for Gemma template: {role!r}")

        return "".join(parts)

    def format_raw_prompt_llama(self, system, messages, tools=None):
        """
        Creates a minimal prompt text for the LLM, focusing on essential content and omitting internal metadata.

        Parameters:
        - system (str): The system message content.
        - messages (list of dict): Conversation history, each dict containing 'role' and 'content'.
        - tools (list of str): List of tool descriptions.

        Returns:
        - str: Cleaned and formatted prompt text.
        """
        prompt_text = ""

        # Add system content
        if system and 'content' in system:
            #prompt_text += f"System: {system['content']}\n\n"
            prompt_text += f"<|start_header_id|>system<|end_header_id|>\n{system['content']}\n<|eot_id|>\n\n"

        # Conditionally add tools section if needed
        if tools:
            #prompt_text += f"When you receive a tool call response, use the output to format an answer to the original use question, or to further call other tools to do so."
            #prompt_text += "Available functions:\n" + json.dumps(tools, indent=2) + "\n"
            #prompt_text += 'Given the previous functions, if required to assist the user, please respond with a JSON for a function call with its proper arguments that best answers the given prompt. Respond in the format {"name": function name, "parameters": dictionary of argument name and its value}. Do not use variables.\n\n'
            tools_json = json.dumps(tools, indent=2)
            prompt_text += f"""
            <|start_header_id|>tools<|end_header_id|>
            When required to answer user queries, use the following tools. You do not have to use them every time.

            Available tools:
            {tools_json}

            When you receive a message from the 'tool' role, it will be a JSON object like {{"response": "..."}}.
            Always extract the value from the 'response' key and use it directly to answer the user's question, unless further tool action is required.
            Important: Do not reuse values from the example.
            
            Instructions:
            1. Do not use these functions unnecessarily for things that can be done in text yourself (eg simple maths or conversions).
            2. Do not discuss the tools; just use them or not as required
            3. Do not refer to or tell the user about using tools (unless one has failed).
            4. Do not offer tools that do not relate to the users request
            
            If you need to use a tool, respond in the format {{"name": function name, "parameters": dictionary of argument names and value}}. Do not use variables.
            <|eot_id|>
            """

        # Add conversation history without metadata
        for message in messages:
            role = message['role']
            content = message['content']
            #prompt_text += f"{role.capitalize()}: {content}\n\n"
            prompt_text += f"<|start_header_id|>{role}<|end_header_id|>\n{content}\n<|eot_id|>\n\n"

        # finally a kick to make the LLM respond:
        prompt_text += "<|start_header_id|>assistant<|end_header_id|>"

        return prompt_text.strip()

    def send_to_ollama(self, prompt_text, prompt_system, prompt_tools, session, raw_mode=False, available_functions=None):
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

        try:
            full_response = ""
            tool_calls = []
            tool_message = None
            tool_name_detected = None

            json_accumulator = ""
            json_collecting = False
            json_brackets = 0
            inside_code_block = False  # for when we receive code
            backtick_buffer = ""

            if raw_mode:
                #print("starting to process RAW mode...")
                combined_prompt = self.format_raw_prompt_gemma(system=prompt_system, tools=prompt_tools, messages=prompt_text)
                #print("Created full prompt from scratch...")
                #print(f"Full prompt:\n\n{combined_prompt}")

                # Send raw text input to Ollama
                response_stream = self.ollama_client.generate(
                    model=self.model,
                    prompt=combined_prompt,
                    stream=True,
                    raw=True,
                    keep_alive="48h",  # no timeout so we keep alive
                    options={
                        "stop": ["<end_of_turn>"],
                    }
                )

            else:
                response_stream = self.ollama_client.chat(
                    model=self.model,
                    messages=prompt_text,
                    stream=True,
                    keep_alive="2h",
                    tools=prompt_tools
                )

            for chunk in response_stream:
                #debugging responses:
                #print(f"{chunk}")

                if raw_mode is True:
                    response_content = chunk.get('response', '')
                else:
                    response_content = chunk.get('message', {}).get('content', '')

                #debugging responses:
                #print(f"{response_content}", end="")

                full_response += response_content

                if response_content:
                    for char in response_content:
                        # disable code blocks for now:
                        # receiving code/not receiving code:
                        #if char == '`':
                        #    backtick_buffer += '`'
                        #    if backtick_buffer == "```":
                        #        inside_code_block = not inside_code_block
                        #        backtick_buffer = "" # reset buffer
                        #else:
                        #    backtick_buffer = ""  # this way we only accumulate on consecutive backticks

                        # receiving json:
                        def _normalize_possible_json(s: str) -> str:
                            # Replace Unicode curly quotes / apostrophes with ASCII - sometimes a problem
                            return (s.replace('\u201c', '"').replace('\u201d', '"')   # “ ”
                                    .replace('\u2018', "'").replace('\u2019', "'")) # ‘ ’

                        if char == '{':
                            if not json_collecting:
                                json_collecting = True
                                json_accumulator = ""
                            json_brackets += 1

                        if json_collecting:
                            json_accumulator += char

                        if char == '}':
                            json_brackets -= 1
                            if json_brackets == 0:
                                tool_call = None
                                try:
                                    # try to parse the accumulated JSON (after normalizing quotes which is a problem sometimes):
                                    tool_call = json.loads(_normalize_possible_json(json_accumulator.strip()))
                                    print(f"\n[DEBUG] Detected JSON: {tool_call}\n")

                                except json.JSONDecodeError:
                                    tool_call = None
                                    print(f"\n[DEBUG] JSON decode error for: {json_accumulator}\n")

                                # reset collectors in case of other tools:
                                json_collecting = False
                                json_accumulator = ""
                                
                                if tool_call and isinstance(tool_call, dict) and "name" in tool_call:
                                    tool_name_detected = tool_call["name"]
                                    params = tool_call.get("parameters", {}) or {}
                                    print(f"[DEBUG] Tool detected: {tool_name_detected} with params {params}")

                                    # Execute the tool call immediately:
                                    try:
                                        fn = (available_functions or {}).get(tool_name_detected)
                                        if fn is None:
                                            print(f"core: tool {tool_name_detected} not found")
                                            wrapped = self._wrap_tool_result(tool_name_detected, {"text": "Unknown tool"})
                                        else:
                                            print(f"core: executing tool {tool_name_detected} with params {params}")
                                            wrapped = fn(tool_args=tool_call, session=session)  # your tools already return wrapped JSON

                                        # 3) prepare a tool message for history
                                        tool_message = {'role': 'tool', 'content': wrapped}

                                    except Exception as e:
                                        wrapped = self._wrap_tool_result(tool_name_detected, {"text": f"Tool error: {e}"})
                                        tool_message = {'role': 'tool', 'content': wrapped}

                                    # return early with the tool message for conversation history:
                                    return full_response, tool_message, tool_name_detected


                    if not json_collecting and not inside_code_block:
                        # send the non-tool call response chunk back to the response thread live:
                        # print(f'{response_content}', end='')
                        response_queue.put(response_content)

            # return the full response when finished for chat history, along with tool calls to process:
            return full_response, None, None

        except Exception as e:
            response_queue.put(f"\nError in processing Ollama response: {e}")
            return f"Error in processing Ollama response: {e}", None, None

    def send_whole_response(self, response_text, session):
        session['response_queue'].put(f"{response_text}")

    def response_finished(self, session):
        session['response_queue'].put(None)

    def close_voice_channel(self, tool_args, session):
        # print("TOOL: CLOSE COMMS CHANNEL")
        # self.send_whole_response("Agent closed channel", session)
        session['close_voice_channel'].set()

    def get_current_time(self, tool_args, session):
        self.send_whole_response("Checking Time.", session)
        now = datetime.now()
        now_time = now.strftime('%I:%M%p')
        #return json.dumps({'response': f'current time: {now_time}'})
        return self._wrap_tool_result("get_current_time", {"text": f"current time: {now_time}"})

    def perform_math_operation(self, tool_args, session):
        self.send_whole_response("Calculating!", session)

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
            return self._wrap_tool_result("perform_math_operation", {"text": f"Result of {operation}: {result}"})

        except Exception as e:
            #return json.dumps({"response": f"An error occurred: {str(e)}"})
            return self._wrap_tool_result("perform_math_operation", {"text": f"An error occurred: {str(e)}"})

    def perform_search(self, tool_args, session):
        query = tool_args.get('parameters').get('query')
        source = tool_args.get('parameters').get('source')
        num_responses = int(tool_args.get('parameters').get('number', 10))

        if source == 'web':
            self.send_whole_response(f"Performing Web Search on '{query}'.", session)
            #return self._perform_web_search(query, num_responses)
            return self._wrap_tool_result("perform_search", {"results": self._perform_web_search(query, num_responses)})

        elif source == 'wikipedia':
            self.send_whole_response(f"Performing Wikipedia search on subject {query}.", session)
            #return self._perform_wikipedia_search(query)
            return self._wrap_tool_result("perform_search", {"results": self._perform_wikipedia_search(query)})

        else:
            #return json.dumps({'error': 'Invalid source specified. Choose "web" or "wikipedia".'})
            return self._wrap_tool_result("perform_search", {"text": 'Invalid source specified. Choose "web" or "wikipedia".'})

    def _perform_web_search(self, query, num_responses):
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
                        "title": r.get("title"),
                        "snippet": r.get("body"),
                        "link": r.get("href"),
                    })

            # Handle case where no results come back
            if not results:
                results.append({
                    "error": "no results found, possibly due to web search tool failure"
                })

            return json.dumps({"response": results})

        except Exception as e:
            # Catch any network or parsing issues
            return json.dumps({"response": f"Error in web search: {e}"})

    def _perform_wikipedia_search(self, query):
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

        return json.dumps({'response': results})

    def open_website(self, tool_args, session, max_retries=3):
        url = (tool_args.get('parameters') or {}).get('url', '')
        # 1) normalize scheme
        if url and not urlparse(url).scheme:
            url = 'https://' + url  # prefer https

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        }

        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
                resp.raise_for_status()

                # Parse static HTML (no JS rendering) for reliability
                soup = BeautifulSoup(resp.text, 'html.parser')
                text = soup.get_text(separator="\n", strip=True)

                # trim very large pages to keep the LLM responsive
                max_chars = 8000
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n...[truncated]"

                # UX ping only on success
                self.send_whole_response(f"Opened website", session)  # speaks immediately
                return self._wrap_tool_result("open_website", {"text": text})

            except requests.exceptions.RequestException as e:
                last_err = e
                # simple backoff
                time.sleep(min(1 + attempt, 3))
            except Exception as e:
                # unexpected parser errors
                return self._wrap_tool_result("open_website", {"text": f"Unexpected error for {url}: {e}"})

        # after retries, return the reason we failed (don’t claim success)
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
        self.send_whole_response(f"Fetching weather for {location}.", session)

        try:
            if forecast:
                self.send_whole_response("Fetching 5 day forecast.", session)
                # Get the 5-day forecast
                url = f"http://api.openweathermap.org/data/2.5/forecast?q={location}&appid={self.weather_api_key}&units=metric"
                response = requests.get(url)
                weather_data = response.json()

                if response.status_code == 200:
                    forecast_list = weather_data['list']
                    forecast_data = []
                    for entry in forecast_list[:5]:  # Limit to the first 5 entries (next 15 hours)
                        forecast_data.append({
                            'datetime': entry['dt_txt'],
                            'temperature': entry['main']['temp'],
                            'description': entry['weather'][0]['description'],
                        })

                    result = {
                        'location': location,
                        'forecast': forecast_data
                    }
                    # self.send_whole_response(f"Forecast Result: {result}", session)
                    #return json.dumps({'check_weather': result})
                    return self._wrap_tool_result("check_weather", {"forecast": result})

                else:
                    #return json.dumps({'check_weather': f"Failed to fetch forecast data: {weather_data.get('message', 'Unknown error')}"})
                    return self._wrap_tool_result("check_weather", {"text": f"Failed to fetch forecast data: {weather_data.get('message', 'Unknown error')}"})

            else:
                # Get the current weather
                self.send_whole_response("Fetching current weather.", session)
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
                        'temperature': temp,
                        'feels_like': feels_like,
                        'humidity': humidity,
                        'description': weather_desc
                    }
                    # self.send_whole_response(f"Current Result: {result}", session)
                    #return json.dumps({'check_weather': result})
                    return self._wrap_tool_result("check_weather", {"current_weather": result})

                else:
                    #return json.dumps({'check_weather': f"Failed to fetch weather data: {weather_data.get('message', 'Unknown error')}"})
                    return self._wrap_tool_result("check_weather", {"text": f"Failed to fetch weather data: {weather_data.get('message', 'Unknown error')}"})

        except Exception as e:
            #return json.dumps({'check_weather': f"Error fetching weather data: {str(e)}"})
            return self._wrap_tool_result("check_weather", {"text": f"Error fetching weather data: {str(e)}"})



