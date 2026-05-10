"""
tools/shopping_list.py — Household lists for Supernova.

Supports multiple named lists (shopping, picture frames, etc).
The 'shopping' list always exists and cannot be deleted.

Available list names are injected into the system prompt via provide_context
so the LLM can resolve fuzzy references without an extra tool call.

Tools:
  - add_to_list             — add items to a named list
  - remove_from_list        — remove items from a named list
  - get_list                — read a named list
  - clear_list              — empty a named list (keeps the list itself)
  - create_list             — create a new named list
  - delete_list             — delete a named list (shopping is protected)

Data: data/shopping_list/lists.json
"""

from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase

log       = ToolBase.logger('shopping_list')
TOOL_NAME = 'shopping_list'
FILENAME  = 'lists.json'
PROTECTED = 'shopping'


# ── Storage ───────────────────────────────────────────────────────────────────

def _load() -> dict:
    data = ToolBase.read_json(TOOL_NAME, FILENAME, default={})
    # Always ensure the default shopping list exists
    if PROTECTED not in data:
        data[PROTECTED] = []
        _save(data)
    return data


def _save(data: dict) -> bool:
    return ToolBase.write_json(TOOL_NAME, FILENAME, data)


def _canonical(name: str) -> str:
    """Normalise list name — lowercase and stripped."""
    return name.strip().lower()


# ── Context injection ─────────────────────────────────────────────────────────

def provide_context(core, tool_config: dict, session: dict) -> str:
    """
    Inject available list names into the system prompt at session start.
    Allows the LLM to resolve fuzzy list references without a tool call.
    """
    if not tool_config.get('enabled', True):
        return ""

    data  = _load()
    names = ', '.join(sorted(data.keys()))

    return (
        f"[LISTS]\n"
        f"Available lists: {names}.\n"
        f"When the user references a list by an approximate name, match it to "
        f"the closest available list before calling a tool."
    )


# ── Schemas ───────────────────────────────────────────────────────────────────

def add_to_list(
    items: Annotated[list[str], Field(
        description=(
            "One or more items to add. Each should be a short clear name, "
            "e.g. ['milk', '24x36cm oak frame']. Use lowercase unless a proper noun."
        )
    )],
    list_name: Annotated[str, Field(
        description=(
            "The name of the list to add to. Must match one of the available "
            "lists from context. Resolve fuzzy references before calling — "
            "e.g. 'picture frame list' → 'picture frames'."
        )
    )] = "shopping",
) -> str:
    """
    Add one or more items to a named list.
    Use when someone says 'add X to the shopping list', 'put X on my Y list',
    'we need X', 'pick up X', or similar.
    Default to 'shopping' if no list is specified.
    """
    ...


def remove_from_list(
    items: Annotated[list[str], Field(
        description=(
            "One or more items to remove. Partial matches are fine — "
            "'bread' will match 'sourdough bread'."
        )
    )],
    list_name: Annotated[str, Field(
        description="The name of the list to remove from."
    )] = "shopping",
) -> str:
    """
    Remove one or more items from a named list.
    Use when someone says 'remove X', 'take X off the list',
    'we don't need X anymore', or 'cross off X'.
    """
    ...


def get_list(
    list_name: Annotated[str, Field(
        description="The name of the list to read."
    )] = "shopping",
) -> str:
    """
    Read the contents of a named list.
    Use when someone asks 'what's on the shopping list', 'what's on my Y list',
    'what do we need', 'read me the list', or 'what are we shopping for'.
    Default to 'shopping' if no list is specified.
    """
    ...


def clear_list(
    list_name: Annotated[str, Field(
        description="The name of the list to clear. The list itself is kept, just emptied."
    )] = "shopping",
) -> str:
    """
    Empty a named list without deleting it.
    Use when someone says 'clear the list', 'empty the shopping list',
    or 'start the list fresh'. Always confirm before calling.
    """
    ...


def create_list(
    list_name: Annotated[str, Field(
        description=(
            "Name for the new list. Keep it short and descriptive, "
            "e.g. 'picture frames', 'tools', 'camping gear'."
        )
    )],
) -> str:
    """
    Create a new named list.
    Use when someone says 'create a new list called X', 'make a Y list',
    or 'start a list for X'.
    """
    ...


def delete_list(
    list_name: Annotated[str, Field(
        description="The name of the list to delete. The 'shopping' list cannot be deleted."
    )],
) -> str:
    """
    Delete a named list and all its contents.
    Use when someone says 'delete the X list', 'remove the Y list',
    or 'get rid of the X list'.
    Always confirm with the user before calling.
    The default shopping list cannot be deleted.
    """
    ...


# ── Executors ─────────────────────────────────────────────────────────────────

def _add_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params    = ToolBase.params(tool_args)
    new_items = [i.strip().lower() for i in params.get('items', []) if i.strip()]
    name      = _canonical(params.get('list_name', PROTECTED))

    if not new_items:
        return ToolBase.error(core, 'add_to_list', "No items provided.")

    data = _load()
    if name not in data:
        return ToolBase.error(core, 'add_to_list',
            f"No list called '{name}'. Available: {', '.join(sorted(data.keys()))}.")

    current = data[name]
    added   = []
    skipped = []

    for item in new_items:
        if item in current:
            skipped.append(item)
        else:
            current.append(item)
            added.append(item)

    if added:
        data[name] = current
        _save(data)
        log.info("Items added",
                 extra={'data': f"list={name} added={added} skipped={skipped}"})

    if added and skipped:
        msg = (f"Added {', '.join(added)} to {name}. "
               f"{', '.join(skipped)} {'was' if len(skipped) == 1 else 'were'} already there.")
    elif added:
        msg = f"Confirm you've added {', '.join(added)} to the {name} list."
    else:
        msg = f"{', '.join(skipped)} {'is' if len(skipped) == 1 else 'are'} already on the {name} list."

    return ToolBase.result(core, 'add_to_list', {
        "status":       "ok",
        "added":        added,
        "skipped":      skipped,
        "list":         name,
        "instructions": msg,
    })


def _remove_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params    = ToolBase.params(tool_args)
    to_remove = [i.strip().lower() for i in params.get('items', []) if i.strip()]
    name      = _canonical(params.get('list_name', PROTECTED))

    if not to_remove:
        return ToolBase.error(core, 'remove_from_list', "No items provided.")

    data = _load()
    if name not in data:
        return ToolBase.error(core, 'remove_from_list',
            f"No list called '{name}'. Available: {', '.join(sorted(data.keys()))}.")

    current   = data[name]
    removed   = []
    not_found = []

    for query in to_remove:
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
        data[name] = current
        _save(data)
        log.info("Items removed",
                 extra={'data': f"list={name} removed={removed} not_found={not_found}"})

    if removed and not_found:
        msg = (f"Removed {', '.join(removed)} from {name}. "
               f"Couldn't find {', '.join(not_found)} on the list.")
    elif removed:
        msg = f"Confirm you've removed {', '.join(removed)} from the {name} list."
    else:
        msg = f"Couldn't find {', '.join(not_found)} on the {name} list."

    return ToolBase.result(core, 'remove_from_list', {
        "status":       "ok",
        "removed":      removed,
        "not_found":    not_found,
        "list":         name,
        "instructions": msg,
    })


def _get_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = ToolBase.params(tool_args)
    name   = _canonical(params.get('list_name', PROTECTED))
    data   = _load()

    if name not in data:
        return ToolBase.error(core, 'get_list',
            f"No list called '{name}'. Available: {', '.join(sorted(data.keys()))}.")

    current = data[name]

    if not current:
        return ToolBase.result(core, 'get_list', {
            "list":         name,
            "items":        [],
            "total":        0,
            "instructions": f"Tell the user the {name} list is empty.",
        })

    log.info("List retrieved", extra={'data': f"list={name} total={len(current)}"})

    return ToolBase.result(core, 'get_list', {
        "list":         name,
        "items":        current,
        "total":        len(current),
        "instructions": (
            f"Read the {name} list naturally. "
            f"For voice, read as a simple spoken list. "
            f"For text, format as a clean bulleted list. "
            f"Do not add commentary beyond the items."
        ),
    })


def _clear_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = ToolBase.params(tool_args)
    name   = _canonical(params.get('list_name', PROTECTED))
    data   = _load()

    if name not in data:
        return ToolBase.error(core, 'clear_list',
            f"No list called '{name}'. Available: {', '.join(sorted(data.keys()))}.")

    count = len(data[name])

    if count == 0:
        return ToolBase.result(core, 'clear_list', {
            "status":       "already_empty",
            "list":         name,
            "instructions": f"Tell the user the {name} list was already empty.",
        })

    data[name] = []
    _save(data)
    log.info("List cleared", extra={'data': f"list={name} removed={count}"})

    return ToolBase.result(core, 'clear_list', {
        "status":       "cleared",
        "list":         name,
        "removed":      count,
        "instructions": f"Confirm you've cleared the {name} list ({count} items removed).",
    })


def _create_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = ToolBase.params(tool_args)
    name   = _canonical(params.get('list_name', ''))

    if not name:
        return ToolBase.error(core, 'create_list', "No list name provided.")

    data = _load()

    if name in data:
        return ToolBase.result(core, 'create_list', {
            "status":       "already_exists",
            "list":         name,
            "instructions": f"Tell the user a '{name}' list already exists.",
        })

    data[name] = []
    _save(data)
    log.info("List created", extra={'data': f"list={name}"})

    return ToolBase.result(core, 'create_list', {
        "status":       "created",
        "list":         name,
        "instructions": f"Confirm you've created a new '{name}' list.",
    })


def _delete_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = ToolBase.params(tool_args)
    name   = _canonical(params.get('list_name', ''))

    if not name:
        return ToolBase.error(core, 'delete_list', "No list name provided.")

    if name == PROTECTED:
        return ToolBase.error(core, 'delete_list',
            "The shopping list cannot be deleted, it is a base list. You can clear it instead.")

    data = _load()

    if name not in data:
        return ToolBase.error(core, 'delete_list',
            f"No list called '{name}'. Available: {', '.join(sorted(data.keys()))}.")

    count = len(data.pop(name))
    _save(data)
    log.info("List deleted", extra={'data': f"list={name} had={count} items"})

    return ToolBase.result(core, 'delete_list', {
        "status":       "deleted",
        "list":         name,
        "instructions": f"Confirm you've deleted the '{name}' list.",
    })


# ── Tool registration ─────────────────────────────────────────────────────────

TOOLS = [
    {'name': 'add_to_list',      'schema': add_to_list,      'execute': _add_execute},
    {'name': 'remove_from_list', 'schema': remove_from_list, 'execute': _remove_execute},
    {'name': 'get_list',         'schema': get_list,         'execute': _get_execute},
    {'name': 'clear_list',       'schema': clear_list,       'execute': _clear_execute},
    {'name': 'create_list',      'schema': create_list,      'execute': _create_execute},
    {'name': 'delete_list',      'schema': delete_list,      'execute': _delete_execute},
]