# Supernova Tool Development Guide

Tools extend Supernova's capabilities by giving the LLM the ability to take actions — fetch data, control devices, send messages, set timers, and more. This guide covers everything you need to write a tool from scratch.

> **Hot reload** — the tool loader watches `tools/` and `config/` for file changes and reloads automatically on the next request. You never need to restart Supernova while developing a tool.

---

## Overview

A tool is a Python file in the `tools/` folder. When Supernova starts, the tool loader scans this folder and registers every tool it finds. The LLM is told about each tool via its schema, and when it decides to use one, the loader calls the tool's `execute()` function.

Everything a tool needs is provided through two sources:

- **`tool_args`** — the arguments the LLM passed when it called the tool
- **`tool_config`** — the tool's yaml config file, automatically loaded from `config/{tool_name}.yaml`

No imports from `core` are needed beyond `ToolBase`.

---

## Quick Start

The fastest way to write a new tool is to copy `tools/_example_tool.py` and rename it. The underscore prefix tells the loader to skip it, so it's safe to leave in place as a reference.

```
tools/
    _example_tool.py     ← reference template (not loaded)
    my_new_tool.py       ← your tool (loaded automatically)

config/
    my_new_tool.yaml     ← your tool's config (loaded automatically)
```

---

## Tool Structure

Every tool file follows the same pattern:

```python
from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase

log = ToolBase.logger('my_tool')


def my_tool(
    param: Annotated[str, Field(description="What this parameter is for.")],
) -> str:
    """What this tool does and when to use it."""
    ...


def execute(tool_args, session, core, tool_config) -> str:
    params = ToolBase.params(tool_args)
    value  = params.get('param', '')
    return ToolBase.result(core, 'my_tool', {"status": "done"})
```

That's the minimum. The sections below cover every feature available.

---

## The Schema Function

The schema function is what the LLM sees. Its name, parameters, type annotations, and docstring become the tool definition sent to Ollama. **The body should always be empty (`...`)** — the real logic lives in `execute()`.

Supernova uses **Pydantic-annotated function signatures** as schemas — not raw dicts. This is the canonical convention, matching the recipes, memory, and conversation_log plugins.

```python
from typing import Annotated
from pydantic import Field

def my_tool(
    required_param: Annotated[str, Field(description="What this is for. Required.")],
    optional_param: Annotated[str, Field(
        default="",
        description="Optional detail.",
    )] = "",
    count: int = 3,
) -> str:
    """
    One-line description of what this tool does.

    Be specific about when the LLM should use it — include example phrases
    that should trigger it, e.g. "Use when the user asks about X or Y."
    """
    ...
```

### Parameter tips

- Use `Annotated[type, Field(description="...")]` for rich descriptions — the LLM reads these
- Use plain type hints (`str`, `int`, `bool`) for simple parameters where the name is self-explanatory
- Always provide defaults for optional parameters
- Keep parameter names clear and unambiguous — the LLM uses them directly
- The schema function name must match the tool name exactly — the loader uses `__name__` to register it

---

## The Execute Function

`execute()` is called when the LLM invokes the tool. It receives four arguments:

| Argument | Type | Description |
|---|---|---|
| `tool_args` | `dict` | The raw arguments from the LLM, including `parameters` |
| `session` | `dict` | The current session state — who is speaking, which interface, etc. |
| `core` | `CoreProcessor` | The core processor — use via `ToolBase` methods only |
| `tool_config` | `dict` | Your tool's yaml config, automatically loaded |

```python
def execute(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    err = ToolBase.require_config(tool_config, 'api_key')   # if config is needed
    if err:
        return ToolBase.error(core, 'my_tool', err)

    params = ToolBase.params(tool_args)
    value  = params.get('required_param', '').strip()

    if not value:
        return ToolBase.error(core, 'my_tool', "required_param was not provided.")

    # ... do the work ...

    return ToolBase.result(core, 'my_tool', {
        "status":       "done",
        "instructions": "Tell the user it worked.",
    })
```

---

## Plugin Hooks

Beyond `execute()`, tool files can export optional hook functions that the loader detects and registers automatically. These are how plugins integrate with the broader agent lifecycle without touching core code.

### `provide_context(core, tool_config, session) -> str`

Injects static text into the **system prompt** at the start of every session. Use this for information that is always relevant — capability descriptions, persistent rules, known entities. Because this text is part of the static system prompt it is **cache-safe** and does not change turn-to-turn.

```python
def provide_context(core, tool_config: dict, session: dict) -> str:
    if not tool_config.get('enabled', True):
        return ""
    return "[MY TOOL]\nRelevant context the LLM should always know."
```

The `context_priority` field in the yaml controls position — lower numbers appear earlier in the system prompt. Return an empty string `""` to inject nothing.

### `provide_turn_context(core, tool_config, session, user_input) -> str | None`

Injects dynamic text as a **system message immediately before each user message**. Unlike `provide_context`, this fires every turn and can vary based on the user's input. Because it is injected at the tail of the message list — not into the static system prompt — it does not bust the Ollama KV cache.

Use this for per-turn context that depends on what the user just said — memory retrieval, recent session hints, live state.

```python
def provide_turn_context(core, tool_config: dict, session: dict, user_input: str) -> str | None:
    if not tool_config.get('enabled', True):
        return None
    # ... query something based on user_input ...
    if relevant:
        return "[CONTEXT]\nSomething relevant to this turn."
    return None  # return None to inject nothing
```

The `turn_context_priority` field in the yaml controls ordering when multiple plugins provide turn context. Lower numbers fire first.

### `on_session_end(core, tool_config, session) -> None`

Fires when a session closes cleanly via `CoreProcessor.close_session()`. Use this for end-of-session work — generating summaries, flushing logs, persisting state. Runs in a daemon thread so it never blocks the interface.

```python
def on_session_end(core, tool_config: dict, session: dict):
    if not tool_config.get('enabled', True):
        return
    # ... do cleanup, summarisation, persistence ...
```

This hook is called by all interfaces that use `close_session()` — voice (on hangup), Telegram (on expiry or reset), and web (on clear). It is not called for headless sessions.

---

## ToolBase Reference

Import once at the top of your tool file:

```python
from core.tool_base import ToolBase
```

### Logging

```python
log = ToolBase.logger('my_tool')
```

Returns a standard Python logger namespaced under `supernova.tools.my_tool`.

```python
log.debug("Detail only needed when debugging")
log.info("Normal operational message", extra={'data': f"value={x}"})
log.warning("Something unexpected but not fatal")
log.error("Something failed", exc_info=True)
```

Use `extra={'data': "..."}` to attach structured context. Keep it short — `key=value` pairs work well.

---

### Config validation

```python
err = ToolBase.require_config(tool_config, 'api_key', 'url')
if err:
    return ToolBase.error(core, 'my_tool', err)
```

Checks that the specified keys exist and are non-empty in `tool_config`. Returns a human-readable error string if any are missing, or `None` if all present. Call at the top of `execute()` before doing any real work.

---

### Parameter extraction

```python
params = ToolBase.params(tool_args)
name   = params.get('name', '')
count  = int(params.get('count', 3))
```

Extracts the `parameters` dict from `tool_args`. The LLM always nests its arguments under `tool_args['parameters']` — this helper saves you from remembering that every time.

---

### Returning results

**Success:**
```python
return ToolBase.result(core, 'my_tool', {
    "status":       "done",
    "value":        42,
    "instructions": "Tell the user the answer is 42.",
})
```

The payload dict is returned to the LLM as the tool result. Including an `"instructions"` key guides exactly how the LLM should respond, producing more consistent output.

**Error:**
```python
return ToolBase.error(core, 'my_tool', "Something went wrong — please try again.")
```

Returns a standardised error envelope. The LLM reads the message and relays it naturally to the user. Always return an error rather than raising an exception.

---

### Immediate feedback

```python
ToolBase.speak(core, session, "Fetching that now.")
```

Pushes text directly to the response queue — the user hears or sees it immediately, before the LLM has generated its response. Use for operations that take a moment, or for confirmations that don't need LLM rewording. The LLM still runs afterward and generates its own response.

---

### Scheduling future callbacks

```python
event_id = ToolBase.schedule(
    core, session, tool_config,
    label         = "pasta",
    delay_seconds = 600,
    announcement  = "The pasta timer has finished. Announce this naturally.",
    event_type    = 'timer',
)
```

The callback is automatically routed to the same interface and endpoint the user is currently on. The `announcement` text is injected as LLM context when the callback fires.

**Cancelling:**
```python
cancelled = ToolBase.cancel_schedule(core, event_id)
```

**Listing:**
```python
timers = ToolBase.list_scheduled(core, event_type='timer')
all    = ToolBase.list_scheduled(core)
```

---

### Session helpers

```python
speaker   = ToolBase.speaker(session)    # "Jesse" or None
interface = ToolBase.interface(session)  # "speaker", "phone", "general", "headless"
endpoint  = ToolBase.endpoint(session)   # "supernova-voice", "286047661", etc.
```

**`speaker`** — the voice-identified speaker name, or None. Use to personalise responses or look up user-specific data.

**`interface`** — which interface the user is on. Use to format responses appropriately — voice responses should be short and spoken-word friendly, Telegram and web can use more text and markdown.

**`endpoint`** — the unique identifier for this connection. For voice it's the satellite name, for Telegram it's the chat_id.

---

### File storage

All tool data lives under `data/{tool_name}/` at the project root. Use `data_path()` as the foundation — never hardcode paths.

```python
# Get the directory
dir_path  = ToolBase.data_path('my_tool')

# Get a specific file path (directory created automatically)
file_path = ToolBase.data_path('my_tool', 'store.db')

# JSON helpers
state = ToolBase.read_json('my_tool', 'state.json', default={})
ToolBase.write_json('my_tool', 'state.json', state)   # atomic write

# Text/markdown helpers
content = ToolBase.read_text('my_tool', 'notes.md')
ToolBase.write_text('my_tool', 'notes.md', content)   # atomic write

# List files
all_files = ToolBase.list_files('my_tool')
md_files  = ToolBase.list_files('my_tool', extension='.md')
```

All writes are atomic — they use a `.tmp` rename so partial writes never corrupt files.

---

### Session history and mode

```python
history = ToolBase.get_history(session)        # list of message dicts
ToolBase.set_history(session, history)         # replace history
ToolBase.clear_history(session)                # wipe history

mode = ToolBase.get_agent_mode(session)        # current AgentMode
ToolBase.set_agent_mode(session, 'deep_research')  # switch mode

ToolBase.request_hangup(session)               # close voice channel after response
```

---

## Config File

Every tool can have a yaml config file at `config/{tool_name}.yaml`. It is loaded automatically — no code needed to read it. The full contents are passed as `tool_config` into `execute()`, `provide_context()`, `provide_turn_context()`, and `on_session_end()`.

### Standard fields recognised by the tool loader

```yaml
enabled:               true   # false to disable without deleting the file
interfaces:            []     # [] = all interfaces, or restrict: [speaker, phone, general]
agent_modes:           []     # [] = general mode only, [all] = every mode, [deep_research] = whitelist
blocked_modes:         []     # exclude from specific modes e.g. [relay]
requires_config:       ""     # skip tool if AppConfig lacks this attribute
context_priority:      50     # position in system prompt — lower appears earlier (default 50)
turn_context_priority: 50     # position in per-turn injection order (default 50)
```

### Interface availability

| `interfaces` value | Available on |
|---|---|
| `[]` or absent | All interfaces (default) |
| `[speaker]` | Voice satellite only |
| `[speaker, phone]` | Voice + phone |
| `[general]` | Text interfaces only (Telegram, web) |
| `[phone]` | Phone calls only |

### Agent mode filtering

| `agent_modes` value | Available in |
|---|---|
| `[]` or absent | General mode only |
| `[all]` | Every agent mode |
| `[deep_research]` | That mode only (whitelist) |

`blocked_modes` takes precedence over `agent_modes` if both are set.

### Your own fields

Any extra fields are passed straight through to your tool:

```yaml
enabled:          true
api_key:          "your-api-key-here"
default_location: "Brunswick, VIC, Australia"
timeout:          10
debug:            false
```

Access them in any hook or executor:

```python
api_key = tool_config.get('api_key', '')
debug   = tool_config.get('debug', False)
timeout = tool_config.get('timeout', 10)
```

---

## Multi-Tool Files

If your file defines more than one tool, export a `TOOLS` list instead of a single schema function. The loader checks for this list and registers each entry separately.

```python
def tool_one(param: str) -> str:
    """Description of tool one."""
    ...

def tool_two(param: str) -> str:
    """Description of tool two."""
    ...

def _execute_one(tool_args, session, core, tool_config): ...
def _execute_two(tool_args, session, core, tool_config): ...

TOOLS = [
    {'name': 'tool_one', 'schema': tool_one, 'execute': _execute_one},
    {'name': 'tool_two', 'schema': tool_two, 'execute': _execute_two},
]
```

The `TOOLS` list must be at the **bottom** of the file. The schema function name must match the `name` field exactly — the loader uses the function reference, and Ollama uses its `__name__` as the tool identifier. Name executor functions with a leading underscore to make it clear they are not part of the public interface.

Plugin hooks (`provide_context`, `provide_turn_context`, `on_session_end`) work normally in multi-tool files — export them at module level alongside the `TOOLS` list.

---

## Dynamic Schemas

For tools that need runtime data in their descriptions (e.g. a list of available modes, devices, or contacts), export a `get_schema` hook instead of a plain schema function:

```python
def get_schema(tool_config: dict, core) -> callable:
    """Return a schema function built with current runtime data."""
    options = ["option_a", "option_b"]  # fetch from config or core

    def my_tool(choice: str = "option_a") -> str:
        f"""Choose from: {', '.join(options)}."""
        ...

    return my_tool
```

The loader calls `get_schema` at reload time and registers the returned callable as the schema.

---

## Stateful Tools

Tools that need to maintain state across calls can use module-level variables. These persist for the lifetime of the server process. Always protect shared state with a lock since multiple sessions can call tools concurrently.

```python
import threading

class _State:
    def __init__(self):
        self.lock  = threading.Lock()
        self.cache = {}

_state = _State()

def execute(tool_args, session, core, tool_config):
    with _state.lock:
        # safe to read/write _state.cache here
        ...
```

---

## Lazy-Loaded Dependencies

For heavy dependencies (large client libraries, database connections, ML models) use a module-level singleton with lazy initialisation:

```python
_client = None

def _get_client(tool_config):
    global _client
    if _client is None:
        from some_heavy_library import Client
        _client = Client(tool_config['url'], tool_config['api_key'])
    return _client

def execute(tool_args, session, core, tool_config):
    client = _get_client(tool_config)
    ...
```

This keeps startup fast and only pays the initialisation cost on first use.

---

## Return Value Reference

`execute()` must return one of:

| Return | Effect |
|---|---|
| `ToolBase.result(core, name, payload)` | Success — LLM receives payload and formulates response |
| `ToolBase.error(core, name, message)` | Error — LLM relays message to user |
| `None` | Success with no data — LLM receives `"ok"` |

Useful keys in the `payload` dict:

| Key | Purpose |
|---|---|
| `"status"` | Machine-readable status string |
| `"instructions"` | Explicit guidance for how the LLM should respond |
| `"text"` | Plain text the LLM can use directly |
| anything else | Structured data the LLM reads and interprets |

---

## Tips and Patterns

**Validate early.** Check required config and parameters at the top of `execute()` before making any API calls. Use `ToolBase.require_config()` for config fields and an explicit check for required parameters.

**Give immediate feedback for slow operations.** If your tool makes an HTTP request or does anything that takes more than a fraction of a second, call `ToolBase.speak()` first so the user knows something is happening.

**Guide the LLM's response.** Include an `"instructions"` key in your result payload. Without it the LLM improvises — explicit instructions produce more consistent results and prevent the LLM from doing things like reading out internal IDs or reformatting data poorly.

**Log at the right level.** Use `log.debug()` for per-call detail. Use `log.info()` for normal operational events. Use `log.warning()` for unexpected but recoverable situations. Use `log.error()` with `exc_info=True` for exceptions.

**Keep schemas honest.** The LLM reads your docstring to decide when to call the tool. Be specific about what triggers it. Include example phrases. Vague descriptions lead to wrong tool selection or missed opportunities.

**Don't raise exceptions.** Always catch exceptions in `execute()` and return `ToolBase.error()`. Unhandled exceptions produce unhelpful output for the user.

**Use `interfaces` to restrict voice-specific tools.** Tools like `hangup_call` only make sense on voice interfaces. Setting `interfaces: [speaker, phone]` keeps them out of Telegram and web sessions.

**Disable without deleting.** Set `enabled: false` in the yaml to temporarily disable a tool without removing its file. Hot-reload picks this up immediately.

**Use `debug` flags in yaml.** For tools with verbose logging (like the memory and conversation_log plugins), gate noisy log lines on `tool_config.get('debug', False)` and expose a `debug: false` field in the yaml. Users can flip it to `true` without touching code and the change takes effect on the next request.

---

## Complete Example

A minimal but complete tool:

**`tools/tell_joke.py`**
```python
"""
tell_joke — fetches a random joke from an API.
"""
import requests
from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase

log = ToolBase.logger('tell_joke')


def tell_joke(
    category: Annotated[str, Field(
        default="general",
        description="Joke category. Options: 'general', 'programming', 'dad'.",
    )] = "general",
) -> str:
    """
    Fetch and tell a random joke.
    Use when the user asks for a joke, something funny, or to cheer them up.
    """
    ...


def execute(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    params   = ToolBase.params(tool_args)
    category = params.get('category', 'general')

    log.info("Fetching joke", extra={'data': f"category={category!r}"})
    ToolBase.speak(core, session, "Let me find a good one.")

    try:
        response = requests.get(
            "https://example-joke-api.com/joke",
            params={"category": category},
            timeout=tool_config.get('timeout', 5),
        )
        response.raise_for_status()
        joke = response.json().get('joke', 'No joke found.')

        return ToolBase.result(core, 'tell_joke', {
            "joke":         joke,
            "instructions": "Tell the user this joke naturally and with good timing.",
        })

    except Exception as e:
        log.error("Failed to fetch joke", exc_info=True)
        return ToolBase.error(core, 'tell_joke', "Couldn't fetch a joke right now.")
```

**`config/tell_joke.yaml`**
```yaml
enabled:   true
interfaces: []
timeout:   5
```

---

## Hook Summary

| Hook | Signature | When it fires | Injection point |
|---|---|---|---|
| `provide_context` | `(core, tool_config, session) -> str` | Every turn, system prompt build | Static system prompt (cache-safe) |
| `provide_turn_context` | `(core, tool_config, session, user_input) -> str \| None` | Every turn, just before user message | Dynamic system message before user turn |
| `on_session_end` | `(core, tool_config, session) -> None` | When session closes cleanly | Daemon thread, no return value |
| `get_schema` | `(tool_config, core) -> callable` | At tool reload time | Returns a schema function |