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

# google search
from googlesearch import search

# for the web request/search sections:
import requests
from bs4 import BeautifulSoup
from requests_html import HTMLSession
import urllib.parse

# home assistant API link
from homeassistant_api import Client as HAClient

# our precontext and tools info:
from config import precontext, tools


class CoreProcessor:
    def __init__(self):
        self.sessions = {}

        self.model = "gemma3:12b"
        self.ollama_client = ollama.Client(host='http://localhost:11434')
        self.pre_context = precontext.llama3_context
        self.voice_pre_context = precontext.voice_context
        self.current_conversation = None

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
                
    def _wrap_tool_result(self, name, payload):
        return json.dumps({
            "tool_result": {
                "name": name,
                "content": payload
            }
        })

    def _kb_path(self):
        """Absolute path to ./config/knowledgebase.txt (relative to this file)."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(script_dir, '../config/knowledgebase.txt')

    def read_knowledgebase_text(self) -> str:
        """
        Returns the current contents of knowledgebase.txt as a string.
        Loads from disk on every call so edits are picked up dynamically.
        """
        try:
            with open(self._kb_path(), 'r', encoding='utf-8') as f:
                kb = f.read().strip()
                return kb
        except FileNotFoundError:
            # Silently ignore if the file doesn't exist
            return ""
        except Exception as e:
            # Fail soft: don't break the run if the kb can't be read
            return f"[knowledgebase read error: {e}]"

    def add_kb_to_pre_context(self, pre_context: str) -> str:
        """Append the latest knowledgebase contents to pre_context, if any."""
        kb = self.read_knowledgebase_text()
        if not kb:
            return pre_context
        return pre_context + "\n\n---\nKnowledgebase (live):\n" + kb

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
        #print(f"[DEBUG] process_input called: session_id={session_id}, input_text={input_text!r}, is_voice={is_voice}")

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

        # now we create the system message:
        system_message = self.create_system_message(voice=is_voice)

        # now we construct the tools:
        if is_voice:
            # it seems order is important, putting voice close channel tool first:
            prompt_tools = tools.voice_tools + tools.general_tools
        else:
            prompt_tools = tools.general_tools
        
        #disabling tools for now:
        prompt_tools = None

        # debugging:
        # print(f"PROMPT TOOLS=\n\n {prompt_tools}")

        while True:
            #print(f"[DEBUG] Sending prompt to LLM: prompt={prompt}, system_message={system_message}, tools={prompt_tools}")
            full_response, tool_calls = self.send_to_ollama(prompt_text=prompt, prompt_system=system_message, prompt_tools=prompt_tools, session=session, raw_mode=True)

            if full_response:
                conversation_history.append({
                    'role': 'assistant',
                    'content': full_response
                })

            if tool_calls:
                # Debugging:
                # print(f"TOOL CALLS RECEIVED: {tool_calls}")

                for tool in tool_calls:
                    try:
                        function_to_call = self.available_functions[tool['name']]
                        function_response = function_to_call(tool_args=tool, session=session)

                        if function_response is not None:
                            # debugging:
                            print(f"core: Function response: {function_response}")

                            if conversation_history:
                                conversation_history.append({
                                        'role': 'tool',
                                        'content': function_response
                                })

                    except Exception as e:
                        print(f"Error in tool call: {e}")
                        conversation_history.append({
                                'role': 'tool',
                                'content': f'Error with tool, or bad use of tool: {e}',
                        })
                
                # leave the loop if close conversation is called:
                if any(tool.get("name") == "close_voice_channel" for tool in tool_calls):
                    #print("[DEBUG] Detected close_voice_channel in tool_calls; breaking loop.")
                    break

                # update the prompt for next spin around for tool call response routines:
                prompt = self.update_prompt(conversation_history)

            else:
                break

        # if we break out of loop, set that we've finished to calling thread:
        #self.send_whole_response(self.end_of_message, session)
        self.response_finished(session)
        #session['response_finished'].set()

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
        full_pre_context = self.pre_context

        if voice:
            full_pre_context += self.voice_pre_context

        # we add this each time so we have up to date info edited on the fly
        full_pre_context = self.add_kb_to_pre_context(full_pre_context)

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

    def send_to_ollama(self, prompt_text, prompt_system, prompt_tools, session, raw_mode=False):
        """Sends request to Ollama, processes return along with tool calls, streams response to message queue"""
        response_queue = session['response_queue']

        try:
            full_response = ""
            tool_calls = []

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
                print(f"{response_content}", end="")

                full_response += response_content

                if response_content:
                    for char in response_content:
                        # receiving code/not receiving code:
                        if char == '`':
                            backtick_buffer += '`'
                            if backtick_buffer == "```":
                                inside_code_block = not inside_code_block
                                backtick_buffer = "" # reset buffer
                        else:
                            backtick_buffer = ""  # this way we only accumulate on consecutive backticks

                        # receiving json:
                        if char == '{' and not inside_code_block:
                            if not json_collecting:
                                json_collecting = True
                                json_accumulator = ""
                            json_brackets += 1
                        if json_collecting:
                            json_accumulator += char
                        if char == '}' and not inside_code_block:
                            json_brackets -= 1
                            if json_brackets == 0:
                                try:
                                    response_json = json.loads(json_accumulator.strip())
                                    tool_calls.append(response_json)

                                except json.JSONDecodeError:
                                    pass

                    if not json_collecting and not inside_code_block:
                        # send the non-tool call response chunk back to the response thread live:
                        # print(f'{response_content}', end='')
                        response_queue.put(response_content)

            # return the full response when finished for chat history, along with tool calls to process:
            return full_response, tool_calls

        except Exception as e:
            response_queue.put(f"\nError in processing Ollama response: {e}")
            return f"Error in processing Ollama response: {e}", None

    def send_whole_response(self, response_text, session):
        session['response_queue'].put(f"{response_text}  \n")

    def response_finished(self, session):
        session['response_queue'].put(None)



