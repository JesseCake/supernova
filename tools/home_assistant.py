"""
tools/home_assistant.py — Home Assistant control via the official MCP Server integration.

Installation
------------
Install the "Model Context Protocol Server" integration in Home Assistant:
  Settings → Devices & Services → Add Integration → Model Context Protocol Server

Then create a Long-Lived Access Token:
  HA Profile → Security tab → Long-Lived Access Tokens → Create Token

Add both to config/home_assistant.yaml:
  url:   http://192.168.8.3:8123
  token: your_long_lived_token_here

How it works
------------
At load time this module connects to HA's MCP server, discovers every tool
it exposes, and dynamically builds a TOOLS list so each HA tool is registered
as a native Supernova tool. The LLM sees them directly — no wrapper layer.

On each tool call, a fresh async session is opened, the call is made, and
the session is closed. asyncio.run() handles the event loop — no persistent
thread or connection needed since HA is on the local network and calls are
fast.

Tool names are exposed as-is from HA (e.g. 'HassTurnOn', 'HassGetState').
Restrict to specific tools via the tools: list in the yaml config.

Caching
-------
Discovered tools are cached at module level for the lifetime of the process.
On hot-reload, the module re-executes and re-discovers. Call results are not
cached — every call hits HA live.
"""

import asyncio
import json
from typing import Annotated, Any
from pydantic import Field
from core.tool_base import ToolBase

log = ToolBase.logger('home_assistant')

TOOL_NAME = 'home_assistant'

# ── Module-level tool cache ───────────────────────────────────────────────────
# Populated at import time by _discover_tools().
# Each entry mirrors the mcp.types.Tool structure we got from HA.
_ha_tools: list = []   # list of mcp.types.Tool


# ── MCP helpers ───────────────────────────────────────────────────────────────

def _mcp_url(tool_config: dict) -> str:
    base = tool_config.get('url', '').rstrip('/')
    return f"{base}/api/mcp"


def _headers(tool_config: dict) -> dict:
    return {"Authorization": f"Bearer {tool_config.get('token', '')}"}


async def _async_discover(tool_config: dict) -> list:
    """Connect to HA MCP server and return list of Tool objects."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url     = _mcp_url(tool_config)
    headers = _headers(tool_config)

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


async def _async_call(tool_config: dict, tool_name: str, arguments: dict, debug: bool = False) -> str:
    """Open a fresh session, call a tool, return the text result."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url     = _mcp_url(tool_config)
    headers = _headers(tool_config)

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)

    if debug:
        block_info = "\n".join(
            f"  block[{i}] type={type(block).__name__} "
            f"attrs={[a for a in dir(block) if not a.startswith('_')]} "
            f"repr={block!r}"
            for i, block in enumerate(result.content)
        )
        log.debug(
            f"[HA DEBUG] {tool_name} — raw MCP result.content "
            f"[{type(result.content).__name__}, {len(result.content)} block(s)]:\n"
            f"{block_info}\n"
            f"  result.isError = {getattr(result, 'isError', '(no isError attr)')}"
        )

    # Extract text content from result
    texts = []
    for block in result.content:
        if hasattr(block, 'text'):
            texts.append(block.text)
        elif hasattr(block, 'json'):
            texts.append(json.dumps(block.json))
    return '\n'.join(texts) if texts else 'OK'


# ── Discovery ─────────────────────────────────────────────────────────────────

def _load_tool_config() -> dict:
    """Load our own yaml config directly — needed at module import time."""
    import os
    import yaml
    config_path = os.path.join(
        os.path.dirname(__file__), '..', 'config', 'home_assistant.yaml'
    )
    try:
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.error(f"Could not load home_assistant.yaml: {e}")
        return {}


def _discover_tools(tool_config: dict) -> list:
    """
    Synchronously discover tools from HA MCP server.
    Returns [] and logs a warning if HA is unreachable.
    """
    if not tool_config.get('url') or not tool_config.get('token'):
        log.warning("home_assistant: url or token not configured — no tools registered")
        return []

    try:
        tools = asyncio.run(_async_discover(tool_config))
        log.info(f"Discovered {len(tools)} tools from Home Assistant")
        return tools
    except Exception as e:
        log.warning(f"Could not connect to Home Assistant MCP server: {e}")
        return []


def _filter_tools(tools: list, tool_config: dict) -> list:
    """Apply the tools: whitelist from yaml if present."""
    whitelist = tool_config.get('tools', [])
    if not whitelist:
        return tools
    allowed = set(whitelist)
    filtered = [t for t in tools if t.name in allowed]
    log.info(f"Filtered to {len(filtered)} tools (whitelist: {sorted(allowed)})")
    return filtered


# ── Dynamic schema builder ────────────────────────────────────────────────────

def _build_schema_fn(ha_tool) -> callable:
    """
    Build a Pydantic-annotated schema function from an MCP Tool object.

    HA tools use JSON Schema for their inputSchema. We extract the properties
    and required fields to build proper Annotated parameters so Ollama sees
    rich descriptions for each argument.
    """
    tool_name   = ha_tool.name
    description = ha_tool.description or f"Home Assistant tool: {tool_name}"
    schema      = ha_tool.inputSchema or {}
    properties  = schema.get('properties', {})
    required    = set(schema.get('required', []))

    # Build parameter annotations dynamically
    annotations = {}
    defaults    = {}

    for param_name, param_schema in properties.items():
        param_desc     = param_schema.get('description', param_name)
        param_type_str = param_schema.get('type', 'string')

        # Map JSON Schema types to Python types
        type_map = {
            'string':  str,
            'integer': int,
            'number':  float,
            'boolean': bool,
            'array':   list,
            'object':  dict,
        }
        py_type = type_map.get(param_type_str, str)

        if param_name in required:
            annotations[param_name] = Annotated[py_type, Field(description=param_desc)]
        else:
            annotations[param_name] = Annotated[py_type, Field(
                default=None,
                description=f"{param_desc} (optional)",
            )]
            defaults[param_name] = None

    # Dynamically create the schema function
    # The function body is always ... — execute() does the real work
    def schema_fn(**kwargs): ...

    schema_fn.__name__        = tool_name
    schema_fn.__qualname__    = tool_name
    schema_fn.__doc__         = description
    schema_fn.__annotations__ = {**annotations, 'return': str}

    # Attach defaults as function defaults via a wrapper
    # Pydantic reads __annotations__ and Field defaults, so this is enough
    return schema_fn


# ── Debug helpers ─────────────────────────────────────────────────────────────

def _typed_repr(d: dict) -> str:
    """Render a dict as 'key = value [type]' lines, one per param, for debug logs."""
    if not d:
        return "  (empty)"
    return "\n".join(f"  {k!r} = {v!r}  [{type(v).__name__}]" for k, v in d.items())


# ── Spoken progress feedback ───────────────────────────────────────────────────

_PROGRESS_PHRASES = {
    'GetLiveContext':         "Checking devices",
    'HassGetState':           "Checking devices",
    'HassListEntities':       "Checking devices",
    'HassSearchEntities':     "Checking devices",
    'HassTurnOn':             "Turning on {name}",
    'HassTurnOff':            "Turning off {name}",
    'HassLightSet':           "Adjusting {name}",
    'HassVacuumStart':        "Starting {name}",
    'HassVacuumReturnToBase': "Sending {name} back to base",
}


def _progress_phrase(ha_tool_name: str, arguments: dict) -> str:
    """
    Build a short spoken-feedback phrase for an HA tool call, fired before
    the (network-bound) call goes out so the user hears that work is
    happening rather than silence.
    """
    template = _PROGRESS_PHRASES.get(ha_tool_name, "Working on it")
    if '{name}' in template:
        name = arguments.get('name')
        if name:
            return template.format(name=name)
        # No name available (rare) — drop back to a generic phrase for this action.
        generic = template.split('{name}')[0].strip()
        return generic or "Working on it"
    return template


# ── Executor factory ──────────────────────────────────────────────────────────

def _make_executor(ha_tool_name: str, allowed_params: set,
                   array_params: set = None, debug: bool = False):
    """Return an execute function closed over the HA tool name."""
    array_params = array_params or set()

    def execute(tool_args: dict, session: dict, core, tool_config: dict) -> str:
        params = ToolBase.params(tool_args)

        if debug:
            log.debug(
                f"[HA DEBUG] {ha_tool_name} — raw tool_args received:\n"
                f"{_typed_repr(tool_args)}\n"
                f"[HA DEBUG] {ha_tool_name} — params after ToolBase.params():\n"
                f"{_typed_repr(params)}"
            )

        # Ollama sometimes wraps all arguments as a JSON string inside a
        # 'kwargs' key: {'kwargs': '{"entity_id": "switch.xyz"}'}.
        # Detect and unpack this before filtering. When we unpack kwargs,
        # bypass the allowed_params filter — the unpacked keys are the real
        # arguments and the schema advertised 'kwargs' as a catch-all.
        kwargs_unpacked = False
        if list(params.keys()) == ['kwargs']:
            try:
                params = json.loads(params['kwargs'])
                kwargs_unpacked = True
                log.debug(f"Unpacked kwargs for {ha_tool_name}: {params}")
                if debug:
                    log.debug(
                        f"[HA DEBUG] {ha_tool_name} — params after kwargs unpack:\n"
                        f"{_typed_repr(params)}"
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        # Strip None values. If we unpacked from kwargs, skip the
        # allowed_params filter since the schema listed 'kwargs' not the
        # real param names. Otherwise filter strictly against HA's schema.
        if kwargs_unpacked:
            arguments = {k: v for k, v in params.items() if v is not None}
        else:
            arguments = {
                k: v for k, v in params.items()
                if v is not None and (not allowed_params or k in allowed_params)
            }

        # Coerce string → list for params HA expects as arrays.
        # Ollama sometimes sends a plain string where a list is required.
        for k in array_params:
            if k in arguments and isinstance(arguments[k], str):
                arguments[k] = [arguments[k]]
                log.debug(f"Coerced {k} to list for {ha_tool_name}")

        log.info(f"Calling HA tool: {ha_tool_name}",
                 extra={'data': str(arguments)})

        if debug:
            log.debug(
                f"[HA DEBUG] {ha_tool_name} — FULL COMMAND about to be sent:\n"
                f"  tool: {ha_tool_name}\n"
                f"  kwargs_unpacked: {kwargs_unpacked}\n"
                f"  allowed_params: {sorted(allowed_params) if allowed_params else '(none — unrestricted)'}\n"
                f"  array_params: {sorted(array_params) if array_params else '(none)'}\n"
                f"  arguments (final, post-coercion):\n"
                f"{_typed_repr(arguments)}"
            )

        # Immediate spoken/typed feedback so the user hears/sees that work is
        # happening before the (network-bound) HA call returns.
        #ToolBase.speak(core, session, _progress_phrase(ha_tool_name, arguments))

        try:
            result_text = asyncio.run(
                _async_call(tool_config, ha_tool_name, arguments, debug)
            )

            if debug:
                log.debug(
                    f"[HA DEBUG] {ha_tool_name} — FULL RESPONSE received:\n"
                    f"  result_text [{type(result_text).__name__}]:\n"
                    f"{result_text}"
                )

            # Tailor instructions based on whether this was a discovery
            # call or a control call, so the agent knows what to do next.
            is_discovery = ha_tool_name in ('GetLiveContext', 'HassGetState',
                                            'HassListEntities', 'HassSearchEntities')
            if is_discovery:
                instructions = (
                    f"The above lists all devices. Match the user's request against "
                    f"the device names, check the domain, then call the right tool:\n"
                    f"  switch → HassTurnOn/HassTurnOff(name='...')\n"
                    f"  light  → HassTurnOn/HassTurnOff/HassLightSet(name='...')\n"
                    f"  scene  → HassTurnOn(name='...') only\n"
                    f"  vacuum → HassVacuumStart/HassVacuumReturnToBase(name='...')\n"
                    f"Always use name=, never entity_id. "
                    f"Do not tell the user you are looking things up — just act."
                )
            else:
                instructions = (
                    f"The Home Assistant action '{ha_tool_name}' completed. "
                    f"Result: {result_text}. "
                    f"Tell the user what happened naturally and concisely."
                )

            # If HA returned an error string in the result, treat it as
            # a recoverable failure and tell the agent to re-discover.
            ha_error = (
                isinstance(result_text, str) and
                result_text.lower().startswith('error')
            )
            if ha_error and not is_discovery:
                instructions = (
                    f"The action '{ha_tool_name}' failed with: {result_text}. "
                    f"You may have used entity_id instead of name — HA tools use "
                    f"the 'name' parameter, not entity_id. "
                    f"Call GetLiveContext with no arguments, find the correct device "
                    f"name, then retry using name='Device Name'. "
                    f"Do not tell the user about this — just retry silently."
                )

            return ToolBase.result(core, ha_tool_name, {
                "result":       result_text,
                "instructions": instructions,
            })
        except Exception as e:
            log.error(f"HA tool call failed: {ha_tool_name}", exc_info=True)
            return ToolBase.result(core, ha_tool_name, {
                "result": f"Error: {e}",
                "instructions": (
                    f"The action '{ha_tool_name}' failed with an unexpected error: {e}. "
                    f"Call GetLiveContext to verify the correct device names, "
                    f"then retry using the name parameter not entity_id. "
                    f"Do not tell the user about this error — just retry."
                ),
            })

    return execute


# ── Context provider ──────────────────────────────────────────────────────────

def provide_context(core, tool_config: dict, session: dict) -> str:
    """Inject available HA tool names and usage rules into the system prompt."""
    if not _ha_tools:
        return ""
    names = ', '.join(t.name for t in _ha_tools)
    return (
        f"[HOME ASSISTANT]\n"
        f"You have direct control of the user's Home Assistant smart home.\n"
        f"Available tools: {names}\n\n"
        f"IMPORTANT RULES FOR HOME ASSISTANT:\n"
        f"1. Always call GetLiveContext with NO arguments first to see all devices.\n"
        f"2. GetLiveContext returns each device with a 'domain' field. "
        f"Use the domain to pick the right tool, passing name= exactly as returned:\n"
        f"   domain=switch  → HassTurnOn(name='...') or HassTurnOff(name='...')\n"
        f"   domain=light   → HassTurnOn / HassTurnOff / HassLightSet(name='...')\n"
        f"   domain=scene   → HassTurnOn(name='...') to activate (never turn off)\n"
        f"   domain=vacuum  → HassVacuumStart / HassVacuumReturnToBase(name='...')\n"
        f"3. Always use the name parameter — never use entity_id.\n"
        f"4. Never skip GetLiveContext even if you think you know the device name."
    )


# ── TOOLS list — built at import time ─────────────────────────────────────────

def _build_tools_list() -> list:
    tool_config = _load_tool_config()

    if not tool_config.get('enabled', True):
        return []

    discovered = _discover_tools(tool_config)
    filtered   = _filter_tools(discovered, tool_config)

    # Cache for provide_context
    global _ha_tools
    _ha_tools = filtered

    debug = tool_config.get('debug', False)

    if debug:
        for ha_tool in filtered:
            log.debug(
                f"HA tool discovered: {ha_tool.name}\n"
                f"  description: {ha_tool.description}\n"
                f"  inputSchema: {ha_tool.inputSchema}"
            )

    entries = []
    for ha_tool in filtered:
        schema_fn     = _build_schema_fn(ha_tool)
        props         = (ha_tool.inputSchema or {}).get('properties', {})
        allowed       = set(props.keys())
        array_params  = {k for k, v in props.items() if v.get('type') == 'array'}
        executor      = _make_executor(ha_tool.name, allowed, array_params, debug)
        entries.append({
            'name':    ha_tool.name,
            'schema':  schema_fn,
            'execute': executor,
        })
        log.debug(f"Registered HA tool: {ha_tool.name}")

    return entries


TOOLS = _build_tools_list()