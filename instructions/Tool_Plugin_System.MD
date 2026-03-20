# Supernova Tool Plugin System

Tools in Supernova are self-contained Python files that live in the `tools/` folder.
The tool loader scans this folder automatically and registers any valid tool files —
no changes to core code needed. Tools can be added, edited, enabled, or disabled
without restarting the system.

---

## Folder Structure

```
tools/
    my_tool.py          ← tool implementation
config/
    my_tool.yaml        ← tool config and settings (optional but recommended)
```

The loader matches `.py` files in `tools/` with `.yaml` files of the same name in
`config/`. If no yaml exists, the tool is still loaded with an empty config dict.

---

## Minimal Single Tool Example

```python
# tools/my_tool.py

from typing import Annotated
from pydantic import Field


def my_tool(
    some_param: Annotated[str, Field(description="What this parameter does. Required.")],
) -> str:
    """
    One-line description of what this tool does and when to use it.
    This becomes the tool description the LLM reads to decide whether to call it.
    """
    ...  # schema stub — body is never called


def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = tool_args.get('parameters', {})
    value  = params.get('some_param', '')

    # Optional: send immediate feedback to the user before the LLM responds
    core.send_whole_response("Doing something...", session)

    # Do the actual work here
    result = f"Did something with {value}"

    # Always return a wrapped tool result
    return core._wrap_tool_result("my_tool", {"text": result})
```

---

## Schema Function

The schema function is **passed directly to Ollama** as the tool definition.
It is never actually called — it exists only to define the interface.

```python
def my_tool(
    required_param: Annotated[str, Field(description="Description. Required.")],
    optional_param: Annotated[int, Field(default=10, description="Description. Default is 10.")] = 10,
) -> str:
    """
    First line becomes the tool's main description.

    Additional lines are included in the description and provide
    more context about when and how to use the tool.
    """
    ...
```

### Type mapping

| Python type      | JSON Schema type          |
|------------------|---------------------------|
| `str`            | `string`                  |
| `int`            | `integer`                 |
| `float`          | `number`                  |
| `bool`           | `boolean`                 |
| `list[str]`      | `array` of `string`       |
| `list[int]`      | `array` of `integer`      |

All parameters without a default value are automatically marked as `required`.

### Adding constraints with `Field`

```python
from pydantic import Field
from typing import Annotated

# String with description
param: Annotated[str, Field(description="What it does")]

# Integer with default
count: Annotated[int, Field(default=5, description="How many")] = 5

# List with min/max length
tags: Annotated[list[str], Field(min_length=1, max_length=5, description="List of tags")]
```

---

## Execute Function

The execute function is called by core when the LLM invokes the tool.

```python
def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    ...
```

### Parameters

| Parameter     | Type   | Description                                              |
|---------------|--------|----------------------------------------------------------|
| `tool_args`   | `dict` | `{'name': 'tool_name', 'parameters': {'key': 'value'}}` |
| `session`     | `dict` | Current session state (see Session object below)         |
| `core`        | obj    | CoreProcessor instance (see Core interface below)        |
| `tool_config` | `dict` | Parsed yaml config for this tool                         |

### Accessing parameters

```python
def execute(tool_args, session, core, tool_config):
    params = tool_args.get('parameters', {})
    query  = params.get('query', '')
    count  = params.get('count', 10)
```

### Return value

Always return a wrapped tool result string using `core._wrap_tool_result()`.
Returning `None` is allowed for tools that have no meaningful result (e.g. hangup).

```python
# Simple text result
return core._wrap_tool_result("my_tool", {"text": "Done."})

# Structured result with instructions for the LLM
return core._wrap_tool_result("my_tool", {
    "text": "Here is the data...",
    "instructions": "Tell the user X and format it as Y."
})

# Error result
return core._wrap_tool_result("my_tool", {"text": f"Error: {str(e)}"})
```

---

## Core Interface (`core`)

What you can call on the `core` object inside `execute`:

```python
# Send immediate text to the user before the LLM generates its response.
# Useful for "Searching...", "Checking weather..." feedback.
core.send_whole_response("Fetching data...", session)

# Wrap a result dict into the standard tool result format
core._wrap_tool_result("tool_name", {"text": "result"})

# Log a timestamped message (appears in console output)
core._log("my message", session=session, extra="optional extra info")

# Access the app config (interfaces, ollama, server, asterisk settings)
core.config

# Access the ollama client directly if needed
core.ollama_client
```

---

## Session Object (`session`)

The session dict contains per-connection state:

```python
session['response_queue']       # queue.Queue — put text here to stream to user
session['close_voice_channel']  # threading.Event — set to hang up a voice call
session['cancel_event']         # threading.Event — set if user interrupted
session['conversation_history'] # list of message dicts
```

---

## Multi-Tool Files

If your file defines more than one tool, export a `TOOLS` list instead of
using the single-function convention:

```python
def tool_one(...) -> str:
    """Description of tool one."""
    ...

def tool_two(...) -> str:
    """Description of tool two."""
    ...

def execute_one(tool_args, session, core, tool_config):
    ...

def execute_two(tool_args, session, core, tool_config):
    ...

TOOLS = [
    {'schema': tool_one, 'name': 'tool_one', 'execute': execute_one},
    {'schema': tool_two, 'name': 'tool_two', 'execute': execute_two},
]
```

When a `TOOLS` list is present, the loader uses it exclusively and ignores
the single-function convention.

---

## Context Provider (System Prompt Injection)

Any tool can inject text into the system prompt on every request by defining
a `provide_context` function. This is useful for tools that need to make
the LLM aware of state or available options (e.g. behaviour rules, available
home automation entities).

```python
def provide_context(core, tool_config: dict) -> str:
    """
    Called on every request. Return a non-empty string to inject into
    the system prompt, or an empty string to inject nothing.
    """
    data = get_some_state()
    if not data:
        return ""
    return f"[MY_TOOL_CONTEXT]\n{data}"
```

The injection position in the system prompt is controlled by `context_priority`
in the tool's yaml. Lower number = earlier in the prompt.

```yaml
# config/my_tool.yaml
context_priority: 20   # appears before tools with priority 50 (the default)
```

---

## Yaml Sidecar Config

All yaml fields are optional. The full list of loader-recognised fields:

```yaml
# config/my_tool.yaml

enabled: true             # set false to disable without deleting the file
voice_only: false         # only include this tool when mode != PLAIN
requires_config: ptv      # only load if AppConfig has this attribute set and non-null
context_priority: 50      # system prompt injection order (lower = earlier, default 50)

# Any additional fields are passed through to execute() and provide_context()
# via tool_config and are entirely up to the tool to interpret:
api_key: "abc123"
default_location: "Melbourne, VIC, Australia"
cache_file: "./cache/my_cache.json"
```

---

## Voice-Only Tools

Set `voice_only: true` in the yaml to only include the tool when the interface
is operating in `SPEAKER` or `PHONE` mode (i.e. not plain text chat).

```yaml
# config/hangup_call.yaml
enabled: true
voice_only: true
```

---

## Module-Level State

If your tool needs to maintain state between calls (caches, locks, clients),
use a module-level singleton. The loader uses a stable module key so the
module survives reloads as long as the process is running:

```python
import threading

class _MyToolState:
    def __init__(self):
        self.lock   = threading.Lock()
        self.cache  = {}
        self.client = None

_state = _MyToolState()


def execute(tool_args, session, core, tool_config):
    with _state.lock:
        if _state.client is None:
            _state.client = create_client(tool_config['api_key'])
        # use _state.client...
```

---

## Lazy Imports

If your tool has heavy dependencies, import them inside `execute` rather than
at the top of the file. This keeps startup fast and prevents import errors
from breaking other tools:

```python
def execute(tool_args, session, core, tool_config):
    import some_heavy_library   # only imported when tool is actually called
    ...
```

---

## Complete Example with All Features

```python
"""
example_tool — demonstrates all plugin features.
Config: config/example_tool.yaml
"""
import threading
from typing import Annotated
from pydantic import Field

# Module-level state singleton
class _State:
    def __init__(self):
        self.lock  = threading.Lock()
        self.cache = {}

_state = _State()


# ── Schema ────────────────────────────────────────────────────────────────────

def example_tool(
    query: Annotated[str, Field(description="What to look up. Required.")],
    count: Annotated[int, Field(default=3, description="Number of results. Default 3.")] = 3,
) -> str:
    """
    Look something up and return results.
    Use when the user asks about X or needs information on Y.
    """
    ...


# ── Context provider ──────────────────────────────────────────────────────────

def provide_context(core, tool_config: dict) -> str:
    with _state.lock:
        items = list(_state.cache.keys())
    if not items:
        return ""
    return "[EXAMPLE_CONTEXT]\nKnown items: " + ", ".join(items)


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = tool_args.get('parameters', {})
    query  = params.get('query', '')
    count  = params.get('count', 3)

    api_key = tool_config.get('api_key', '')

    core.send_whole_response(f"Looking up '{query}'...", session)
    core._log("example_tool start", session=session, extra=f"q={query}")

    try:
        # Do the work
        results = [f"Result {i} for {query}" for i in range(count)]

        # Update state
        with _state.lock:
            _state.cache[query] = results

        return core._wrap_tool_result("example_tool", {
            "results": results,
            "instructions": "Present these results clearly to the user."
        })
    except Exception as e:
        return core._wrap_tool_result("example_tool", {"text": f"Error: {e}"})
```

```yaml
# config/example_tool.yaml
enabled: true
context_priority: 30
api_key: "your_key_here"
```

---

## Checklist for a New Tool

- [ ] File is in `tools/` and named with underscores e.g. `my_tool.py`
- [ ] Schema function name matches the filename (or `TOOLS` list is used)
- [ ] Schema function has type annotations on all parameters
- [ ] Schema function has a descriptive one-line docstring
- [ ] All parameters use `Annotated[type, Field(description="...")]`
- [ ] `execute(tool_args, session, core, tool_config)` function is defined
- [ ] Execute returns `core._wrap_tool_result(name, {...})` or `None`
- [ ] Yaml sidecar created in `config/` with at least `enabled: true`
- [ ] Sensitive values (API keys, URLs) are in the yaml, not hardcoded