"""
Behaviour management plugin for Supernova.

Fully self-contained — owns its own state, persistence, and system message
injection. Does not require any behaviour-related code in core.py.

Tools:
    update_behaviour  — add a rule (optionally targeted to specific interfaces)
    remove_behaviour  — remove a rule
    list_behaviour    — list all rules

Context provider:
    provide_context(core, tool_config, session) -> str
    Called by core on every request to inject active rules into the system prompt.
    Filters rules by current interface_mode so voice-only rules don't appear in
    text sessions and vice versa.

Storage:
    data/behaviour/behaviours.json       — live editable rules list
    personality/default_behaviours.json  — factory defaults, seeded on first run

Rule format:
    [
        {"rule": "Be brief.", "interfaces": ["speaker", "phone"]},
        {"rule": "No emojis."}   // no interfaces field = applies to all
    ]

Config (config/behaviour.yaml):
    enabled: true
    context_priority: 10
"""

import os
import json
import threading
from typing import Annotated, Optional
from pydantic import Field
from core.tool_base import ToolBase
from core.interface_mode import InterfaceMode
from core.session_state import get_interface_mode

log = ToolBase.logger('behaviour')


# ── Module-level state singleton ──────────────────────────────────────────────

class _BehaviourState:
    def __init__(self):
        self.lock  = threading.Lock()
        self.rules = []     # list of rule dicts: {"rule": str, "interfaces": list}
        self.path  = None
        self.mtime = 0.0

_state = _BehaviourState()


# ── Path helpers ──────────────────────────────────────────────────────────────

def _rules_path() -> str:
    """Path to the live rules file."""
    return ToolBase.data_path('behaviour', 'behaviours.json')

def _defaults_path() -> str:
    """Path to the factory defaults file in personality/."""
    return os.path.join(
        os.path.dirname(__file__), '../personality/default_behaviours.json'
    )


# ── Persistence helpers ───────────────────────────────────────────────────────

def _seed_from_defaults(path: str):
    """
    Copy default_behaviours.json → behaviours.json on first run.
    Only seeds if the target doesn't exist yet.
    """
    if os.path.exists(path):
        return
    defaults = _defaults_path()
    if not os.path.exists(defaults):
        log.warning("No default_behaviours.json found, starting with empty rules")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([], f, indent=2)
        return
    try:
        with open(defaults, 'r', encoding='utf-8') as f:
            data = json.load(f)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info("Seeded behaviours from defaults", extra={'data': path})
    except Exception as e:
        log.error("Failed to seed defaults", exc_info=True)


def _ensure_loaded():
    """
    Load (or reload) rules from disk if the file has changed.
    Safe to call on every request — only does IO when mtime changes.
    """
    path = _rules_path()

    if _state.path != path:
        _state.path  = path
        _state.mtime = 0.0
        _state.rules = []

    # Seed from defaults if file doesn't exist yet
    _seed_from_defaults(path)

    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        _state.rules = []
        return

    if mtime == _state.mtime:
        return  # up to date

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Support both new format (list of dicts) and old format ({"global": [...]})
        if isinstance(data, list):
            raw_rules = data
        elif isinstance(data, dict) and 'global' in data:
            # Migrate old format — treat all as global rules with no interface filter
            log.info("Migrating old behaviour format to new format")
            raw_rules = [{"rule": r} for r in data['global'] if isinstance(r, str)]
        else:
            raw_rules = []

        # Sanitise
        seen, out = set(), []
        for entry in raw_rules:
            if isinstance(entry, str):
                entry = {"rule": entry}
            if not isinstance(entry, dict):
                continue
            rule = entry.get("rule", "").strip()[:400]
            if not rule or rule in seen:
                continue
            seen.add(rule)
            interfaces = entry.get("interfaces", [])
            if not isinstance(interfaces, list):
                interfaces = []
            out.append({"rule": rule, "interfaces": interfaces})

        _state.rules = out
        _state.mtime = mtime
        log.info("Rules reloaded", extra={'data': f"{len(_state.rules)} rules from {path}"})
    except Exception as e:
        log.error("Load error", exc_info=True)


def _save():
    """Atomically write current rules to disk."""
    import tempfile
    path = _rules_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".beh.tmp.")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(_state.rules, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
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

def provide_context(core, tool_config: dict, session: dict) -> str:
    """
    Called by the tool loader on every request to inject active behaviour
    rules into the system prompt.

    Filters rules by the current interface_mode so voice-only rules don't
    appear in text sessions and vice versa.
    """
    interface_mode = get_interface_mode(session)

    with _state.lock:
        _ensure_loaded()
        rules = list(_state.rules)

    active = []
    for entry in rules:
        interfaces = entry.get("interfaces", [])
        if not interfaces:
            # No interface filter — applies everywhere
            active.append(entry["rule"])
        elif interface_mode.value in interfaces:
            active.append(entry["rule"])

    if not active:
        return ""

    return "[BEHAVIOUR_OVERRIDES]\n" + "\n".join(f"- {r}" for r in active)


# ── Schema functions ──────────────────────────────────────────────────────────

def update_behaviour(
    rule: Annotated[str, Field(
        description="Short imperative rule e.g. 'Keep replies under 10 words.' or 'Be more sarcastic'. Required."
    )],
    interfaces: Annotated[Optional[str], Field(
        description="Comma-separated list of interfaces to apply this rule to: 'speaker', 'phone', 'general'. Leave empty to apply to all interfaces."
    )] = None,
) -> str:
    """
    Add a rule to change your own future behaviour.
    Use this when asked to change the way you respond, speak, or behave.
    Keep rules short and instructional.
    Optionally restrict the rule to specific interfaces (speaker, phone, general).
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
    params = tool_args.get("parameters") or {}
    rule   = (params.get("rule") or "").strip()
    if not rule:
        return ToolBase.error(core, 'update_behaviour', "No rule provided.")
    rule = rule[:400]

    # Parse optional interfaces parameter
    interfaces_raw = (params.get("interfaces") or "").strip()
    interfaces = []
    if interfaces_raw:
        for part in interfaces_raw.split(","):
            part = part.strip().lower()
            if part in ("speaker", "phone", "general"):
                interfaces.append(part)

    with _state.lock:
        _ensure_loaded()
        existing_rules = [e["rule"] for e in _state.rules]
        if rule not in existing_rules:
            _state.rules.append({"rule": rule, "interfaces": interfaces})
            _save()

    ToolBase.speak(core, session, "Added behaviour rule.")
    return ToolBase.result(core, 'update_behaviour', {"text": "Rule added"})


def execute_remove(tool_args: dict, session, core, tool_config: dict) -> str:
    rule = ((tool_args.get("parameters") or {}).get("rule") or "").strip()

    with _state.lock:
        _ensure_loaded()
        before = len(_state.rules)
        _state.rules = [e for e in _state.rules if e["rule"] != rule]
        if len(_state.rules) < before:
            _save()
            msg = "Rule removed"
        else:
            msg = "Rule not found"

    ToolBase.speak(core, session, "Removed behaviour rule.")
    return ToolBase.result(core, 'remove_behaviour', {"text": msg})


def execute_list(tool_args: dict, session, core, tool_config: dict) -> str:
    with _state.lock:
        _ensure_loaded()
        rules = list(_state.rules)

    if not rules:
        summary = "No behaviour rules are currently active."
    else:
        lines = []
        for entry in rules:
            line = f"- {entry['rule']}"
            if entry.get("interfaces"):
                line += f" ({', '.join(entry['interfaces'])})"
            lines.append(line)
        summary = "Current behaviour rules:\n" + "\n".join(lines)

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