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
from core.tool_base import ToolBase

log = ToolBase.logger('my_tool')   # get a logger

def my_tool(param: str) -> str:    # schema — shown to the LLM
    """What this tool does and when to use it."""
    ...

def execute(tool_args, session, core, tool_config) -> str:   # implementation
    params = ToolBase.params(tool_args)
    value  = params.get('param', '')
    return ToolBase.result(core, 'my_tool', {"status": "done"})
```

That's the minimum. The sections below cover every feature available.

---

## The Schema Function

The schema function is what the LLM sees. Its name, parameters, type annotations, and docstring become the tool definition sent to Ollama. **The body should always be empty (`...`)** — the real logic lives in `execute()`.

```python
from typing import Annotated
from pydantic import Field

def my_tool(
    required_param: Annotated[str, Field(description="What this is for. Required.")],
    optional_param: Annotated[str, Field(default="", description="Optional detail.")] = "",
    count: int = 3,
) -> str:
    """
    One-line description of what this tool does.

    Be specific about when the LLM should use it — include example phrases
    that should trigger it, e.g. "Use when the user asks about X or Y."

    Args:
        required_param: Description shown to the LLM.
        optional_param: Description shown to the LLM.
        count:          How many results to return.
    """
    ...
```

### Parameter tips

- Use `Annotated[type, Field(description="...")]` for rich descriptions — the LLM reads these
- Use plain type hints (`str`, `int`, `bool`) for simple parameters where the name is self-explanatory
- Always provide defaults for optional parameters
- Keep parameter names clear and unambiguous — the LLM uses them directly

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
    # Always start with these two lines
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

## ToolBase Reference

Import once at the top of your tool file:

```python
from core.tool_base import ToolBase
```

### Logging

```python
log = ToolBase.logger('my_tool')
```

Returns a standard Python logger namespaced under `supernova.tools.my_tool`. Follows the same logging setup as the rest of Supernova — INFO and above goes to `logs/supernova.log`, DEBUG goes to `logs/debug.log` when verbose mode is on.

```python
log.debug("Detail only needed when debugging")
log.info("Normal operational message", extra={'data': f"value={x}"})
log.warning("Something unexpected but not fatal")
log.error("Something failed", exc_info=True)   # exc_info=True includes the traceback
```

Use `extra={'data': "..."}` to attach structured context alongside the message. Keep it short — key=value pairs work well.

---

### Config validation

```python
err = ToolBase.require_config(tool_config, 'api_key', 'url')
if err:
    return ToolBase.error(core, 'my_tool', err)
```

Checks that the specified keys exist and are non-empty in `tool_config`. Returns a human-readable error string if any are missing, or `None` if all present. Call this at the very top of `execute()` before doing any real work — it saves the user from cryptic `KeyError` crashes.

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

The payload dict is returned to the LLM as the tool result. The LLM uses it to formulate its response. Including an `"instructions"` key is optional but useful — it guides exactly how the LLM should respond, which produces more consistent and natural output.

**Error:**
```python
return ToolBase.error(core, 'my_tool', "Something went wrong — please try again.")
```

Returns a standardised error envelope. The LLM reads the message and relays it naturally to the user. Always return an error rather than raising an exception — exceptions bubble up and produce unfriendly output.

---

### Immediate feedback

```python
ToolBase.speak(core, session, "Fetching that now.")
```

Pushes text directly to the response queue — the user hears or sees it immediately, before the LLM has generated its response. Use this for:

- Operations that take a moment (searches, API calls, email sending)
- Confirmations that don't need LLM rewording ("Timer set.")
- Status updates during multi-step operations

The LLM still runs afterward and generates its own response. If you want to suppress the LLM's follow-up response, include this in your result:

```python
return ToolBase.result(core, 'my_tool', {
    "status":      "done",
    "instruction": "Feedback already given. Acknowledge briefly only.",
})
```

---

### Scheduling future callbacks

Use this to fire an event in the future — a timer, reminder, or any scheduled notification back to the user.

```python
event_id = ToolBase.schedule(
    core, session, tool_config,
    label         = "pasta",           # human-readable name shown in listings
    delay_seconds = 600,               # seconds from now
    announcement  = "The pasta timer has finished. Announce this naturally.",
    event_type    = 'timer',           # used for filtering in list_scheduled
)
```

The `announcement` text is injected as LLM context when the callback fires, so the response is natural rather than a flat canned message. Make it instructional: *"The pasta timer is done. Announce this to the user in a friendly way."*

The callback is automatically routed to the same interface and endpoint the user is currently on — voice, phone, or Telegram — without any extra configuration. If you need to override the destination:

```python
event_id = ToolBase.schedule(
    core, session, tool_config,
    label         = "reminder",
    delay_seconds = 3600,
    announcement  = "Your reminder.",
    event_type    = 'reminder',
    endpoint_id   = 'supernova-voice',   # override destination
    interface     = 'voice_remote',      # override interface
)
```

**Cancelling:**
```python
cancelled = ToolBase.cancel_schedule(core, event_id)
# Returns True if found and cancelled, False if not found
```

**Listing:**
```python
timers   = ToolBase.list_scheduled(core, event_type='timer')
all_events = ToolBase.list_scheduled(core)
```

---

### Session helpers

These give you context about the current user and interface:

```python
speaker   = ToolBase.speaker(session)    # "Jesse" or None
interface = ToolBase.interface(session)  # "voice_remote", "asterisk", "telegram", "web", "headless"
endpoint  = ToolBase.endpoint(session)   # "supernova-voice", "286047661", etc.
```

**`speaker`** — the identified speaker's name if voice ID has matched them, or None. Use this to personalise responses or look up user-specific data (e.g. their enrolled email address).

**`interface`** — which interface the user is on. Use this to format responses appropriately — voice responses should be short and spoken-word friendly, Telegram responses can use more text and formatting.

**`endpoint`** — the unique identifier for this user's current connection. For voice it's the satellite name, for Telegram it's the chat_id, for phone it's the endpoint name from config.

---

## Config File

Every tool can have a yaml config file at `config/{tool_name}.yaml`. It is loaded automatically — no code needed to read it. The full contents are passed as `tool_config` into `execute()` and `provide_context()`.

### Standard fields (recognised by the tool loader)

```yaml
enabled:          true    # Set false to disable without deleting the file
voice_only:       false   # true = only available on satellite voice (VoiceMode.SPEAKER)
phone_only:       false   # true = only available on phone calls (VoiceMode.PHONE)
requires_config:  ""      # Skip this tool if AppConfig lacks this attribute
context_priority: 50      # Lower number = earlier in system prompt (default 50)
```

### Your own fields

Any extra fields you add are passed straight through to your tool:

```yaml
enabled:          true
api_key:          "your-api-key-here"
default_location: "Brunswick, VIC, Australia"
timeout:          10
contacts:
  Jesse: jesse@example.com
  Dean:  dean@example.com
```

Access them in `execute()`:

```python
api_key  = tool_config.get('api_key', '')
contacts = tool_config.get('contacts', {})
timeout  = tool_config.get('timeout', 10)
```

### Interface availability

| yaml setting | Available on |
|---|---|
| *(nothing set)* | All interfaces |
| `voice_only: true` | Satellite voice only |
| `phone_only: true` | Phone calls only |
| `voice_only: false` `phone_only: false` | All interfaces (explicit) |

The `hangup_call` tool is a good example of `voice_only: true` — it only makes sense in a voice context.

---

## Context Injection

If your tool needs to inject information into the system prompt on every turn — a contacts list, the user's current state, known entity names — add a `provide_context()` function:

```python
def provide_context(core, tool_config: dict) -> str:
    contacts = tool_config.get('contacts', {})
    if not contacts:
        return ""
    names = ", ".join(contacts.keys())
    return f"[EMAIL CONTACTS]\nKnown contacts: {names}"
```

The `context_priority` field in the yaml controls where this text appears in the system prompt — lower numbers appear earlier. The default is `50`. Use a low number (e.g. `10`) for critical context the LLM needs to read first.

Return an empty string `""` when there's nothing to inject — the loader skips empty strings.

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

The `TOOLS` list must be at the **bottom** of the file — all functions it references must be defined before it. Name executor functions with a leading underscore (e.g. `_execute_one`) to make it clear they're not part of the public interface.

---

## Stateful Tools

Tools that need to maintain state across calls (e.g. a behaviour rules list, a cache) can use module-level variables. These persist for the lifetime of the server process. The tool loader uses a stable module key so state survives hot-reloads when you edit the tool file.

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

Always protect shared state with a `threading.Lock()` — multiple sessions can call tools concurrently.

---

## Lazy-Loaded Dependencies

If your tool has a heavy dependency (a large client library, a model, a database connection) that you don't want to load at startup, use a module-level singleton with lazy initialisation:

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

The `payload` dict in `ToolBase.result()` is passed to the LLM verbatim. Useful keys:

| Key | Purpose |
|---|---|
| `"text"` | Plain text the LLM can use directly |
| `"status"` | A machine-readable status string |
| `"instructions"` | Explicit guidance for how the LLM should respond |
| `"instruction"` | Same as above (both spellings work) |
| *(anything else)* | Structured data the LLM reads and interprets |

---

## Tips and Patterns

**Validate early.** Check required config and parameters at the top of `execute()` before making any API calls or doing any work. Use `ToolBase.require_config()` for config and an explicit check for required parameters.

**Give immediate feedback for slow operations.** If your tool makes an HTTP request or does anything that takes more than a fraction of a second, call `ToolBase.speak()` first so the user knows something is happening.

**Guide the LLM's response.** Include an `"instructions"` key in your result payload to tell the LLM exactly how to present the data. Without it, the LLM improvises — which is usually fine, but explicit instructions produce more consistent results.

**Log at the right level.** Use `log.debug()` for per-call detail you'd only want when actively debugging. Use `log.info()` for normal operational events (tool called, result returned). Use `log.warning()` for unexpected but recoverable situations. Use `log.error()` with `exc_info=True` for exceptions.

**Keep schemas honest.** The LLM reads your schema function's docstring to decide when to call the tool. Be specific about what triggers it — vague descriptions lead to the LLM calling the wrong tool or missing opportunities to use the right one.

**Don't raise exceptions.** Always catch exceptions in `execute()` and return `ToolBase.error()`. Unhandled exceptions propagate up and produce unhelpful output for the user.

**Use `voice_only: true` for voice-specific tools.** Tools like `hangup_call` only make sense on voice interfaces. Setting `voice_only: true` in the yaml keeps them out of Telegram and web sessions where they'd be confusing.

**Disable without deleting.** Set `enabled: false` in the yaml to temporarily disable a tool without removing its file.

---

## Complete Example

A minimal but complete tool that fetches a joke from an API:

**`tools/tell_joke.py`**
```python
"""
tell_joke tool — fetches a random joke from an API.
"""
import requests
from core.tool_base import ToolBase

log = ToolBase.logger('tell_joke')


def tell_joke(category: str = "general") -> str:
    """
    Fetch and tell a random joke.
    Use when the user asks for a joke, something funny, or to cheer them up.

    Args:
        category: The joke category. Options: 'general', 'programming', 'dad'.
    """
    ...


def execute(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    params   = ToolBase.params(tool_args)
    category = params.get('category', 'general')

    log.info("Fetching joke", extra={'data': f"category={category!r}"})

    try:
        response = requests.get(
            f"https://example-joke-api.com/joke",
            params={"category": category},
            timeout=5,
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
enabled: true
voice_only: false
```

That's it — the tool loader watches for file changes and reloads automatically on the next request. No restart needed.