"""
tools/shopping_list.py — Household shopping list for Supernova.

Provides:
  - add_to_shopping_list    — add one or more items
  - remove_from_shopping_list — remove items by name (fuzzy match)
  - get_shopping_list       — read the current list
  - clear_shopping_list     — wipe the list entirely

Shared household list — not per-user.
Data lives in data/shopping_list/list.json via ToolBase.data_path().
"""

from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase

log = ToolBase.logger('shopping_list')

TOOL_NAME = 'shopping_list'


# ── Storage helpers ───────────────────────────────────────────────────────────

def _load() -> list[str]:
    return ToolBase.read_json(TOOL_NAME, 'list.json', default=[])


def _save(items: list[str]) -> bool:
    return ToolBase.write_json(TOOL_NAME, 'list.json', items)


# ── Schema functions ──────────────────────────────────────────────────────────

def add_to_shopping_list(
    items: Annotated[list[str], Field(
        description=(
            "One or more items to add to the shopping list. "
            "Each item should be a short, clear name e.g. ['milk', 'sourdough bread', 'olive oil']. "
            "Normalise capitalisation — use lowercase unless it's a proper noun."
        )
    )],
) -> str:
    """
    Add one or more items to the household shopping list.
    Use when someone says 'add X to the shopping list', 'we need X',
    'pick up X', 'put X on the list', or similar.
    Can add multiple items in a single call.
    """
    ...


def remove_from_shopping_list(
    items: Annotated[list[str], Field(
        description=(
            "One or more items to remove from the shopping list. "
            "Use the name as closely as possible to what's on the list. "
            "Partial matches are acceptable — 'bread' will match 'sourdough bread'."
        )
    )],
) -> str:
    """
    Remove one or more items from the household shopping list.
    Use when someone says 'remove X', 'take X off the list', 'we don't need X anymore',
    or 'cross off X'.
    """
    ...


def get_shopping_list() -> str:
    """
    Read the current household shopping list.
    Use when someone asks 'what's on the shopping list', 'what do we need',
    'read me the list', or 'what are we shopping for'.
    """
    ...


def clear_shopping_list() -> str:
    """
    Clear the entire shopping list.
    Only use when the user explicitly asks to clear or empty the whole list —
    e.g. 'clear the shopping list', 'empty the list', 'start the list fresh'.
    Always confirm with the user before calling this.
    """
    ...


# ── Executors ─────────────────────────────────────────────────────────────────

def _add_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params   = ToolBase.params(tool_args)
    new_items = [i.strip().lower() for i in params.get('items', []) if i.strip()]

    if not new_items:
        return ToolBase.error(core, 'add_to_shopping_list', "No items provided.")

    current = _load()

    # Avoid exact duplicates
    added    = []
    skipped  = []
    for item in new_items:
        if item in current:
            skipped.append(item)
        else:
            current.append(item)
            added.append(item)

    if added:
        _save(current)
        log.info("Items added", extra={'data': f"added={added} skipped={skipped} total={len(current)}"})

    if added and skipped:
        instructions = (
            f"Added {', '.join(added)} to the shopping list. "
            f"{', '.join(skipped)} {'was' if len(skipped) == 1 else 'were'} already on the list."
        )
    elif added:
        instructions = f"Confirm you've added {', '.join(added)} to the shopping list."
    else:
        instructions = f"{', '.join(skipped)} {'is' if len(skipped) == 1 else 'are'} already on the list — nothing new was added."

    return ToolBase.result(core, 'add_to_shopping_list', {
        "status":       "ok",
        "added":        added,
        "skipped":      skipped,
        "total":        len(current),
        "instructions": instructions,
    })


def _remove_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params      = ToolBase.params(tool_args)
    to_remove   = [i.strip().lower() for i in params.get('items', []) if i.strip()]

    if not to_remove:
        return ToolBase.error(core, 'remove_from_shopping_list', "No items provided.")

    current = _load()
    removed = []
    not_found = []

    for query in to_remove:
        # Find best match — exact first, then substring
        match = None
        if query in current:
            match = query
        else:
            for item in current:
                if query in item or item in query:
                    match = item
                    break

        if match:
            current.remove(match)
            removed.append(match)
        else:
            not_found.append(query)

    if removed:
        _save(current)
        log.info("Items removed", extra={'data': f"removed={removed} not_found={not_found} total={len(current)}"})

    if removed and not_found:
        instructions = (
            f"Removed {', '.join(removed)} from the shopping list. "
            f"Couldn't find {', '.join(not_found)} on the list."
        )
    elif removed:
        instructions = f"Confirm you've removed {', '.join(removed)} from the shopping list."
    else:
        instructions = f"Couldn't find {', '.join(not_found)} on the shopping list."

    return ToolBase.result(core, 'remove_from_shopping_list', {
        "status":       "ok",
        "removed":      removed,
        "not_found":    not_found,
        "total":        len(current),
        "instructions": instructions,
    })


def _get_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    current = _load()

    if not current:
        return ToolBase.result(core, 'get_shopping_list', {
            "items":        [],
            "total":        0,
            "instructions": "Tell the user the shopping list is empty.",
        })

    log.info("Shopping list retrieved", extra={'data': f"total={len(current)}"})

    return ToolBase.result(core, 'get_shopping_list', {
        "items":        current,
        "total":        len(current),
        "instructions": (
            "Read the shopping list naturally. "
            "For voice, read it as a simple spoken list. "
            "For text, format it as a clean list. "
            "Do not add commentary beyond the items unless the list is empty."
        ),
    })


def _clear_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    current = _load()
    count   = len(current)

    if count == 0:
        return ToolBase.result(core, 'clear_shopping_list', {
            "status":       "already_empty",
            "instructions": "Tell the user the shopping list was already empty.",
        })

    _save([])
    log.info("Shopping list cleared", extra={'data': f"removed={count}"})

    return ToolBase.result(core, 'clear_shopping_list', {
        "status":       "cleared",
        "removed":      count,
        "instructions": f"Confirm you've cleared the shopping list ({count} items removed).",
    })


# ── Tool registration ─────────────────────────────────────────────────────────

TOOLS = [
    {'name': 'add_to_shopping_list',     'schema': add_to_shopping_list,     'execute': _add_execute},
    {'name': 'remove_from_shopping_list','schema': remove_from_shopping_list,'execute': _remove_execute},
    {'name': 'get_shopping_list',        'schema': get_shopping_list,        'execute': _get_execute},
    {'name': 'clear_shopping_list',      'schema': clear_shopping_list,      'execute': _clear_execute},
]