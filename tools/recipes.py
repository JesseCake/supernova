"""
tools/recipes.py — Recipe store for Supernova.

Recipes are stored as Markdown files with YAML frontmatter in
data/recipes/. Each file is human-editable and named after the recipe
title (slugified), e.g. data/recipes/hungarian_goulash.md.

Format:
    ---
    title: Hungarian Goulash
    serves: 4
    cook_time: 2 hours
    tags: [beef, stew, hungarian]
    added: 2026-03-22
    ---

    Ingredients:
    - 800g beef chuck, cubed
    - 2 onions, diced
    ...

    Method:
    1. Brown the beef...
    2. Soften the onions...

    Notes:
    Best made the day before.

Tools (3 total):
    search_recipes — list all recipes, filter by tag, or search by ingredient(s)
    get_recipe     — retrieve a full recipe by name
    manage_recipe  — save, edit, or delete a recipe
"""

import re
import os
from datetime import date
from typing import Annotated
from pydantic import Field
import frontmatter

from core.tool_base import ToolBase

log = ToolBase.logger('recipes')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(title: str) -> str:
    """Convert a recipe title to a safe filename slug."""
    slug = title.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_-]+', '_', slug)
    return slug.strip('_') or 'recipe'


def _filename(title: str) -> str:
    return f"{_slugify(title)}.md"


def _parse(content: str) -> tuple[dict, str]:
    """Parse a recipe file into (frontmatter_dict, body_str)."""
    try:
        post = frontmatter.loads(content)
        return dict(post.metadata), post.content
    except Exception:
        return {}, content


def _render(fm: dict, body: str) -> str:
    """Render frontmatter dict + body back to a markdown string."""
    post = frontmatter.Post(body.strip(), **fm)
    return frontmatter.dumps(post) + '\n'


def _find_recipe_file(title: str) -> str | None:
    """
    Find a recipe file by title — exact slug match first,
    then case-insensitive partial match on stored titles.
    Returns the filename (not full path) or None.
    """
    exact = _filename(title)
    if os.path.exists(ToolBase.data_path('recipes', exact)):
        return exact

    for fname in ToolBase.list_files('recipes', extension='.md'):
        content = ToolBase.read_text('recipes', fname)
        fm, _   = _parse(content)
        stored  = str(fm.get('title', '')).lower()
        if title.lower() in stored or stored in title.lower():
            return fname

    return None


# ── Schema functions ──────────────────────────────────────────────────────────

def search_recipes(
    ingredients: Annotated[list[str], Field(
        default=[],
        description=(
            "Ingredients to search for, e.g. ['chicken', 'lemon']. "
            "Leave empty to list all recipes. "
            "Returns recipes containing ANY listed ingredient. "
            "Set match_all=true to require ALL ingredients."
        )
    )] = [],
    tag: Annotated[str, Field(
        default="",
        description="Optional tag to filter by, e.g. 'pasta' or 'quick'."
    )] = "",
    match_all: Annotated[bool, Field(
        default=False,
        description="If true, only return recipes containing ALL listed ingredients."
    )] = False,
) -> str:
    """
    Search or list recipes.
    - No arguments: list all saved recipes.
    - tag only: list recipes with that tag.
    - ingredients: find recipes that use those ingredients.
    - ingredients + tag: find recipes using those ingredients filtered by tag.
    Use when the user asks what recipes are saved, wants to browse,
    or asks 'what can I make with X'.
    """
    ...


def get_recipe(
    title: Annotated[str, Field(description="Recipe name to retrieve. Partial matches accepted. Required.")],
) -> str:
    """
    Retrieve a full recipe by name.
    Use when the user asks for a specific recipe, wants to cook something,
    or asks to display a recipe.
    Returns the full recipe text including ingredients and method.
    """
    ...


def manage_recipe(
    action: Annotated[str, Field(description=(
        "What to do: 'save' (store a new recipe), "
        "'edit' (update an existing recipe), "
        "'delete' (remove a recipe). Required."
    ))],
    title: Annotated[str, Field(description="Recipe title. Required for all actions.")],
    body: Annotated[str, Field(
        default="",
        description=(
            "The full recipe text. Required for save. "
            "Write it naturally — list ingredients, then numbered steps, "
            "then any notes. The text is stored as-is so write it clearly. "
            "For edit, provide the updated full recipe text, or leave empty "
            "and use the notes field to just append a note."
        )
    )] = "",
    serves:    Annotated[str, Field(default="", description="Serving size, e.g. '4' or '4-6'.")] = "",
    cook_time: Annotated[str, Field(default="", description="Total cook time, e.g. '1 hour 30 mins'.")] = "",
    tags:      Annotated[list[str], Field(default=[], description="Category tags, e.g. ['pasta', 'quick'].")] = [],
    notes:     Annotated[str, Field(
        default="",
        description="A note to append to the recipe. Used with edit when you only want to add a note rather than rewrite the whole recipe."
    )] = "",
) -> str:
    """
    Save, edit, or delete a recipe.

    action='save': store a new recipe.
      Provide the full recipe in the body field — write it naturally as
      you would on a recipe card. Ingredients, method steps, notes.
      Returns an error if a recipe with that title already exists.

    action='edit': update an existing recipe.
      To rewrite the recipe, provide the updated text in body.
      To just add a note, leave body empty and use the notes field.
      Metadata (serves, cook_time, tags) can be updated independently.

    action='delete': remove a recipe permanently. Confirm with user first.
    """
    ...


# ── Executors ─────────────────────────────────────────────────────────────────

def _execute_search(tool_args: dict, session, core, tool_config: dict) -> str:
    params      = ToolBase.params(tool_args)
    ingredients = [i.lower().strip() for i in params.get('ingredients', []) if i.strip()]
    filter_tag  = str(params.get('tag', '')).lower().strip()
    match_all   = bool(params.get('match_all', False))

    ToolBase.speak(core, session, "Looking up recipes.")

    files   = ToolBase.list_files('recipes', extension='.md')
    results = []

    for fname in files:
        content  = ToolBase.read_text('recipes', fname)
        fm, body = _parse(content)
        title    = fm.get('title', fname.replace('.md', '').replace('_', ' ').title())
        tags     = [t.lower() for t in (fm.get('tags') or [])]

        # Tag filter
        if filter_tag and filter_tag not in tags:
            continue

        # Ingredient search — search the full body text
        if ingredients:
            body_lower = body.lower()
            found = [ing for ing in ingredients if ing in body_lower]
            if match_all and len(found) < len(ingredients):
                continue
            if not match_all and not found:
                continue
            results.append({
                "title":     title,
                "serves":    fm.get('serves'),
                "cook_time": fm.get('cook_time'),
                "tags":      fm.get('tags', []),
                "matched":   found,
            })
        else:
            results.append({
                "title":     title,
                "serves":    fm.get('serves'),
                "cook_time": fm.get('cook_time'),
                "tags":      fm.get('tags', []),
            })

    # Sort by matched ingredient count if doing ingredient search
    if ingredients:
        results.sort(key=lambda x: len(x.get('matched', [])), reverse=True)

    if not results:
        if ingredients:
            ing_list = ', '.join(ingredients)
            msg = f"No recipes found containing {ing_list}."
        elif filter_tag:
            msg = f"No recipes found with tag '{filter_tag}'."
        else:
            msg = "No recipes saved yet."
        return ToolBase.result(core, 'search_recipes', {
            "count":        0,
            "results":      [],
            "instructions": f"Tell the user: {msg}",
        })

    log.info("Recipe search", extra={'data': f"ingredients={ingredients} tag={filter_tag!r} found={len(results)}"})
    return ToolBase.result(core, 'search_recipes', {
        "count":        len(results),
        "results":      results,
        "instructions": (
            "List the matching recipes naturally. Include cook time and serves if available. "
            "If this was an ingredient search, mention which ingredients each recipe uses."
        ),
    })


def _execute_get(tool_args: dict, session, core, tool_config: dict) -> str:
    params = ToolBase.params(tool_args)
    title  = str(params.get('title', '')).strip()

    if not title:
        return ToolBase.error(core, 'get_recipe', "No recipe title provided.")

    fname = _find_recipe_file(title)
    if not fname:
        return ToolBase.error(core, 'get_recipe',
            f"No recipe found matching '{title}'. Try search_recipes to see what's available.")

    ToolBase.speak(core, session, f"Getting {title}.")

    content  = ToolBase.read_text('recipes', fname)
    fm, body = _parse(content)

    log.info("Recipe retrieved", extra={'data': fname})
    return ToolBase.result(core, 'get_recipe', {
        "title":      fm.get('title', title),
        "serves":     fm.get('serves'),
        "cook_time":  fm.get('cook_time'),
        "tags":       fm.get('tags', []),
        "recipe":     body,
        "instructions": (
            "Present the recipe clearly. For voice, read the ingredients then "
            "the method steps. For text, display the recipe body as-is."
        ),
    })


def _execute_manage(tool_args: dict, session, core, tool_config: dict) -> str:
    params = ToolBase.params(tool_args)
    action = str(params.get('action', '')).strip().lower()
    title  = str(params.get('title', '')).strip()

    if not action:
        return ToolBase.error(core, 'manage_recipe',
            "No action provided. Use 'save', 'edit', or 'delete'.")
    if not title:
        return ToolBase.error(core, 'manage_recipe', "No recipe title provided.")

    # ── SAVE ──────────────────────────────────────────────────────────────────
    if action == 'save':
        body = str(params.get('body', '')).strip()
        if not body:
            return ToolBase.error(core, 'manage_recipe',
                "No recipe body provided. Write the full recipe text in the body field.")

        existing = _find_recipe_file(title)
        if existing:
            return ToolBase.error(core, 'manage_recipe',
                f"A recipe called '{title}' already exists. "
                f"Use action='edit' to modify it, or choose a different name.")

        ToolBase.speak(core, session, f"Saving {title}.")

        fm = {
            'title':     title,
            'serves':    str(params.get('serves', ''))    or None,
            'cook_time': str(params.get('cook_time', '')) or None,
            'tags':      params.get('tags', []),
            'added':     str(date.today()),
        }
        content  = _render(fm, body)
        filename = _filename(title)
        ok       = ToolBase.write_text('recipes', filename, content)

        if not ok:
            return ToolBase.error(core, 'manage_recipe', f"Failed to save '{title}'.")

        log.info("Recipe saved", extra={'data': f"{title} → {filename}"})
        return ToolBase.result(core, 'manage_recipe', {
            "status":       "saved",
            "title":        title,
            "instructions": f"Tell the user the recipe '{title}' has been saved.",
        })

    # ── EDIT ──────────────────────────────────────────────────────────────────
    elif action == 'edit':
        fname = _find_recipe_file(title)
        if not fname:
            return ToolBase.error(core, 'manage_recipe',
                f"No recipe found matching '{title}'.")

        ToolBase.speak(core, session, f"Updating {title}.")

        content  = ToolBase.read_text('recipes', fname)
        fm, body = _parse(content)

        # Update metadata fields if provided
        if params.get('serves'):
            fm['serves']    = str(params['serves'])
        if params.get('cook_time'):
            fm['cook_time'] = str(params['cook_time'])
        if params.get('tags'):
            fm['tags']      = params['tags']

        # Replace body if provided
        new_body = str(params.get('body', '')).strip()
        if new_body:
            body = new_body

        # Append note if provided
        note = str(params.get('notes', '')).strip()
        if note:
            if 'Notes' in body or 'notes' in body:
                body = body.rstrip() + f"\n- {note}"
            else:
                body = body.rstrip() + f"\n\nNotes:\n- {note}"

        if not new_body and not note and not any([
            params.get('serves'), params.get('cook_time'), params.get('tags')
        ]):
            return ToolBase.error(core, 'manage_recipe',
                "Nothing to update. Provide a new body, a note to append, or updated metadata.")

        ok = ToolBase.write_text('recipes', fname, _render(fm, body))
        if not ok:
            return ToolBase.error(core, 'manage_recipe',
                f"Failed to save changes to '{title}'.")

        log.info("Recipe edited", extra={'data': title})
        return ToolBase.result(core, 'manage_recipe', {
            "status":       "updated",
            "title":        fm.get('title', title),
            "instructions": f"Tell the user the recipe '{fm.get('title', title)}' has been updated.",
        })

    # ── DELETE ────────────────────────────────────────────────────────────────
    elif action == 'delete':
        fname = _find_recipe_file(title)
        if not fname:
            return ToolBase.error(core, 'manage_recipe',
                f"No recipe found matching '{title}'.")

        ToolBase.speak(core, session, f"Deleting {title}.")

        path = ToolBase.data_path('recipes', fname)
        try:
            os.remove(path)
            log.info("Recipe deleted", extra={'data': fname})
            return ToolBase.result(core, 'manage_recipe', {
                "status":       "deleted",
                "title":        title,
                "instructions": f"Tell the user the recipe '{title}' has been deleted.",
            })
        except Exception as e:
            log.error("Failed to delete recipe", exc_info=True)
            return ToolBase.error(core, 'manage_recipe',
                f"Failed to delete '{title}': {e}")

    else:
        return ToolBase.error(core, 'manage_recipe',
            f"Unknown action '{action}'. Use 'save', 'edit', or 'delete'.")


# ── Tool registration ─────────────────────────────────────────────────────────

TOOLS = [
    {'name': 'search_recipes', 'schema': search_recipes, 'execute': _execute_search},
    {'name': 'get_recipe',     'schema': get_recipe,     'execute': _execute_get},
    {'name': 'manage_recipe',  'schema': manage_recipe,  'execute': _execute_manage},
]