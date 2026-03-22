"""
Behaviour management plugin for Supernova.

Fully self-contained — owns its own state, persistence, and system message
injection. Does not require any behaviour-related code in core.py.

Tools:
    update_behaviour  — add a rule
    remove_behaviour  — remove a rule
    list_behaviour    — list all rules

Context provider:
    provide_context(core, tool_config) -> str
    Called by core on every request to inject active rules into the system prompt.
    Registered automatically by the tool loader when this file is loaded.

Config (config/behaviour.yaml):
    enabled: true
    context_priority: 10   # lower = earlier in system prompt
    rules_file: null       # defaults to personality/behaviour_overrides.json
"""

import os
import json
import threading
from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase

log = ToolBase.logger('behaviour')


# ── Module-level state singleton ──────────────────────────────────────────────
# Lives for the lifetime of the process. Survives tool reloads because the
# loader uses a stable module key (_supernova_tool_behaviour) in sys.modules.

class _BehaviourState:
    def __init__(self):
        self.lock  = threading.Lock()
        self.rules = []           # list of active rule strings
        self.path  = None         # path to the JSON file, set on first _ensure_loaded
        self.mtime = 0.0          # last known mtime of the file

_state = _BehaviourState()


# ── Persistence helpers ───────────────────────────────────────────────────────

def _resolve_path(tool_config: dict) -> str:
    """Return the absolute path to the rules JSON file."""
    configured = tool_config.get('rules_file')
    if configured:
        return os.path.abspath(configured)
    # Default: personality/behaviour_overrides.json relative to project root
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), '../personality/behaviour_overrides.json')
    )


def _ensure_loaded(tool_config: dict):
    """
    Load (or reload) rules from disk if the file has changed since last read.
    Safe to call on every request — only does IO when mtime changes.
    """
    path = _resolve_path(tool_config)

    # If path changed (e.g. config edited), reset state
    if _state.path != path:
        _state.path  = path
        _state.mtime = 0.0
        _state.rules = []

    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        _state.rules = []
        return

    if mtime == _state.mtime:
        return  # up to date, nothing to do

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        rules = data.get('global', [])
        # Sanitise: strings only, dedupe, cap length and count
        seen, out = set(), []
        for r in rules:
            if isinstance(r, str):
                r = r.strip()[:200]
                if r and r not in seen:
                    seen.add(r)
                    out.append(r)
        _state.rules = out[:20]
        _state.mtime = mtime
        log.info("Rules reloaded", extra={'data': f"{len(_state.rules)} rules from {path}"})
    except Exception as e:
        log.error("Load error", exc_info=True)


def _save(tool_config: dict):
    """Atomically write current rules to disk."""
    import tempfile
    path = _resolve_path(tool_config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"global": _state.rules[:20]}

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".beh.tmp.")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        # Update cached mtime so we don't immediately re-read what we just wrote
        try:
            _state.mtime = os.path.getmtime(path)
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


# ── Context provider ──────────────────────────────────────────────────────────

def provide_context(core, tool_config: dict) -> str:
    """
    Called by the tool loader on every request to inject active behaviour
    rules into the system prompt.

    Returns an empty string if no rules are active so nothing is appended.
    """
    with _state.lock:
        _ensure_loaded(tool_config)
        rules = list(_state.rules)

    if not rules:
        return ""

    return "[BEHAVIOUR_OVERRIDES]\n" + "\n".join(f"- {r}" for r in rules)


# ── Schema functions ──────────────────────────────────────────────────────────

def update_behaviour(
    rule: Annotated[str, Field(
        description="Short imperative rule e.g. 'Keep replies under 10 words.' or 'Be more sarcastic'. Required."
    )],
) -> str:
    """
    Add a rule to change your own future behaviour.
    Use this when asked to change the way you respond, speak, or behave.
    Keep rules short and instructional.
    """
    ...


def remove_behaviour(
    rule: Annotated[str, Field(
        description="Exact text of the rule to remove. Use list_behaviour first if unsure of wording. Required."
    )],
) -> str:
    """
    Remove a previously added behaviour rule by exact text match.
    Use list_behaviour first if you are unsure of the exact wording.
    """
    ...


def list_behaviour() -> str:
    """
    List all active behaviour rules currently applied to your responses.
    Use this to check what rules are in place before adding or removing one.
    """
    ...


# ── Executors ─────────────────────────────────────────────────────────────────

def execute_update(tool_args: dict, session, core, tool_config: dict) -> str:
    rule = ((tool_args.get("parameters") or {}).get("rule") or "").strip()
    if not rule:
        return ToolBase.error(core, 'update_behaviour', "No rule provided.")
    rule = rule[:200]

    with _state.lock:
        _ensure_loaded(tool_config)
        if rule not in _state.rules:
            _state.rules.append(rule)
            _save(tool_config)

    ToolBase.speak(core, session, "Added behaviour rule.")
    return ToolBase.result(core, 'update_behaviour', {"text": "Rule added"})


def execute_remove(tool_args: dict, session, core, tool_config: dict) -> str:
    rule = ((tool_args.get("parameters") or {}).get("rule") or "").strip()

    with _state.lock:
        _ensure_loaded(tool_config)
        if rule in _state.rules:
            _state.rules.remove(rule)
            _save(tool_config)
            msg = "Rule removed"
        else:
            msg = "Rule not found"

    ToolBase.speak(core, session, "Removed behaviour rule.")
    return ToolBase.result(core, 'remove_behaviour', {"text": msg})


def execute_list(tool_args: dict, session, core, tool_config: dict) -> str:
    with _state.lock:
        _ensure_loaded(tool_config)
        rules = list(_state.rules)

    if not rules:
        summary = "No behaviour rules are currently active."
    else:
        summary = "Current behaviour rules:\n" + "\n".join(f"- {r}" for r in rules)

    ToolBase.speak(core, session, "Listing behaviour rules.")
    return ToolBase.result(core, 'list_behaviour', {"rules": rules, "summary": summary})


# ── Multi-tool export ─────────────────────────────────────────────────────────

TOOLS = [
    {
        'schema':  update_behaviour,
        'name':    'update_behaviour',
        'execute': execute_update,
    },
    {
        'schema':  remove_behaviour,
        'name':    'remove_behaviour',
        'execute': execute_remove,
    },
    {
        'schema':  list_behaviour,
        'name':    'list_behaviour',
        'execute': execute_list,
    },
]