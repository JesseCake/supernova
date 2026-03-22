"""
tools/recipes.py — Recipe store for Supernova.

Recipes are stored as Markdown files with YAML frontmatter in
data/recipes/. Each file is human-editable and named after the recipe
title (slugified), e.g. data/recipes/hungarian_goulash.md.

Format:
    ---
    title: Hungarian Goulash
    serves: 4
    prep_time: 20 mins
    cook_time: 2 hours
    oven_temp: null
    tags: [beef, stew, hungarian]
    added: 2026-03-22
    ---

    ## Ingredients

    - 800g beef chuck, cubed
    ...

    ## Method

    1. Brown the beef...
    ...

    ## Notes

    Optional notes here.

Tools (3 total):
    recipe_search  — list all recipes, filter by tag, or search by ingredient(s)
    recipe_get     — retrieve a full recipe by name
    recipe_manage  — add, edit, or delete a recipe
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


def _build_recipe_markdown(
    title:       str,
    serves:      str,
    prep_time:   str,
    cook_time:   str,
    oven_temp:   str,
    tags:        list,
    ingredients: list[str],
    method:      list[str],
    notes:       str,
) -> str:
    """Build a complete recipe markdown string from structured fields."""
    fm = {
        'title':     title,
        'serves':    serves    or 'unknown',
        'prep_time': prep_time or 'unknown',
        'cook_time': cook_time or 'unknown',
        'oven_temp': oven_temp or None,
        'tags':      tags      or [],
        'added':     str(date.today()),
    }

    body_lines = ['## Ingredients', '']
    for ing in ingredients:
        body_lines.append(f"- {ing.strip()}")
    body_lines.append('')
    body_lines.append('## Method')
    body_lines.append('')
    for i, step in enumerate(method, 1):
        body_lines.append(f"{i}. {step.strip()}")
    if notes:
        body_lines.append('')
        body_lines.append('## Notes')
        body_lines.append('')
        body_lines.append(notes.strip())

    return _render(fm, '\n'.join(body_lines))


# ── Schema functions ──────────────────────────────────────────────────────────

def recipe_search(
    ingredients: Annotated[list[str], Field(
        default=[],
        description=(
            "Ingredients to search for, e.g. ['chicken', 'lemon']. "
            "Leave empty to list all recipes. "
            "Returns recipes containing ANY of the listed ingredients. "
            "Pass match_all=true to require ALL ingredients."
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


def recipe_get(
    title: Annotated[str, Field(description="The recipe name to retrieve. Partial matches accepted. Required.")],
) -> str:
    """
    Retrieve a full recipe by name.
    Use when the user asks for a specific recipe, wants to cook something,
    or asks to display a recipe on screen.
    Returns ingredients, method, and all recipe details.
    """
    ...


def recipe_manage(
    action: Annotated[str, Field(description=(
        "What to do. One of: "
        "'add' (save a new recipe), "
        "'edit' (modify an existing recipe), "
        "'delete' (remove a recipe). Required."
    ))],
    title: Annotated[str, Field(description="Recipe title. Required for all actions.")],
    # ── add fields ────────────────────────────────────────────────────────────
    ingredients: Annotated[list[str], Field(
        default=[],
        description="Ingredient list. Required for add."
    )] = [],
    method: Annotated[list[str], Field(
        default=[],
        description="Ordered method steps. Required for add."
    )] = [],
    serves:    Annotated[str, Field(default="", description="Serving size, e.g. '4'.")] = "",
    prep_time: Annotated[str, Field(default="", description="Prep time, e.g. '15 mins'.")] = "",
    cook_time: Annotated[str, Field(default="", description="Cook time, e.g. '1 hour'.")] = "",
    oven_temp: Annotated[str, Field(default="", description="Oven temperature, e.g. '180°C'.")] = "",
    tags:      Annotated[list[str], Field(default=[], description="Category tags.")] = [],
    notes:     Annotated[str, Field(default="", description="Tips or notes.")] = "",
    # ── edit fields ───────────────────────────────────────────────────────────
    operation: Annotated[str, Field(
        default="",
        description=(
            "Edit operation. One of: "
            "set_field, add_tag, remove_tag, "
            "add_ingredient, remove_ingredient, "
            "add_step, replace_step, remove_step, "
            "append_notes. Required for edit."
        )
    )] = "",
    field:       Annotated[str, Field(default="", description="Field name for set_field, e.g. 'serves'.")] = "",
    value:       Annotated[str, Field(default="", description="New value for the edit operation.")] = "",
    step_number: Annotated[int, Field(default=0,  description="Step number for replace_step/remove_step (1-indexed).")] = 0,
) -> str:
    """
    Add, edit, or delete a recipe.
 
    action='add': save a new recipe from ingredients and method steps.
      Use after the user describes or photographs a recipe.
      Returns an error if a recipe with that title already exists.
 
    action='edit': make a targeted change to one part of an existing recipe.
      Set operation to one of: set_field, add_tag, remove_tag,
      add_ingredient, remove_ingredient, add_step, replace_step,
      remove_step, append_notes.
      Use field+value for set_field, value for ingredient/tag/step changes,
      step_number+value for replace_step and remove_step.
 
    action='delete': remove a recipe permanently. Confirm with user first.
    """
    ...


# ── Executors ─────────────────────────────────────────────────────────────────

def _execute_search(tool_args: dict, session, core, tool_config: dict) -> str:
    params      = ToolBase.params(tool_args)
    ingredients = [i.lower().strip() for i in params.get('ingredients', []) if i.strip()]
    filter_tag  = str(params.get('tag', '')).lower().strip()
    match_all   = bool(params.get('match_all', False))

    files   = ToolBase.list_files('recipes', extension='.md')
    results = []

    ToolBase.speak(core, session, "Looking up recipes.")

    for fname in files:
        content  = ToolBase.read_text('recipes', fname)
        fm, body = _parse(content)
        title    = fm.get('title', fname.replace('.md', '').replace('_', ' ').title())
        tags     = [t.lower() for t in (fm.get('tags') or [])]

        # Tag filter
        if filter_tag and filter_tag not in tags:
            continue

        # Ingredient search
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

    # Sort by matched ingredients count if doing ingredient search
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
        return ToolBase.result(core, 'recipe_search', {
            "count":        0,
            "results":      [],
            "instructions": f"Tell the user: {msg}",
        })

    log.info("Recipe search", extra={'data': f"ingredients={ingredients} tag={filter_tag!r} found={len(results)}"})
    return ToolBase.result(core, 'recipe_search', {
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
        return ToolBase.error(core, 'recipe_get', "No recipe title provided.")

    fname = _find_recipe_file(title)
    if not fname:
        return ToolBase.error(core, 'recipe_get',
            f"No recipe found matching '{title}'. Try recipe_search to see what's available.")
    
    ToolBase.speak(core, session, f"Getting Recipe: {title}.")

    content  = ToolBase.read_text('recipes', fname)
    fm, body = _parse(content)

    log.info("Recipe retrieved", extra={'data': fname})
    return ToolBase.result(core, 'recipe_get', {
        "title":      fm.get('title', title),
        "serves":     fm.get('serves'),
        "prep_time":  fm.get('prep_time'),
        "cook_time":  fm.get('cook_time'),
        "oven_temp":  fm.get('oven_temp'),
        "tags":       fm.get('tags', []),
        "recipe":     body,
        "instructions": (
            "Present the recipe clearly. For voice, read the ingredients then method steps. "
            "For text, format it neatly with sections."
        ),
    })


def _execute_manage(tool_args: dict, session, core, tool_config: dict) -> str:
    params = ToolBase.params(tool_args)
    action = str(params.get('action', '')).strip().lower()
    title  = str(params.get('title', '')).strip()

    if not action:
        return ToolBase.error(core, 'recipe_manage', "No action provided. Use 'add', 'edit', or 'delete'.")
    if not title:
        return ToolBase.error(core, 'recipe_manage', "No recipe title provided.")

    # ── ADD ───────────────────────────────────────────────────────────────────
    if action == 'add':
        ingredients = params.get('ingredients', [])
        method      = params.get('method', [])

        if not ingredients:
            return ToolBase.error(core, 'recipe_manage', "No ingredients provided.")
        if not method:
            return ToolBase.error(core, 'recipe_manage', "No method steps provided.")

        existing = _find_recipe_file(title)
        if existing:
            return ToolBase.error(core, 'recipe_manage',
                f"A recipe called '{title}' already exists. "
                f"Use action='edit' to modify it, or choose a different name.")
        
        ToolBase.speak(core, session, f"Saving Recipe: {title}.")

        content  = _build_recipe_markdown(
            title       = title,
            serves      = str(params.get('serves', '')),
            prep_time   = str(params.get('prep_time', '')),
            cook_time   = str(params.get('cook_time', '')),
            oven_temp   = str(params.get('oven_temp', '')),
            tags        = params.get('tags', []),
            ingredients = ingredients,
            method      = method,
            notes       = str(params.get('notes', '')),
        )
        filename = _filename(title)
        ok       = ToolBase.write_text('recipes', filename, content)

        if not ok:
            return ToolBase.error(core, 'recipe_manage', f"Failed to save '{title}'.")

        log.info("Recipe saved", extra={'data': f"{title} → {filename}"})
        return ToolBase.result(core, 'recipe_manage', {
            "status":       "saved",
            "title":        title,
            "instructions": f"Tell the user the recipe '{title}' has been saved.",
        })

    # ── EDIT ──────────────────────────────────────────────────────────────────
    elif action == 'edit':
        operation  = str(params.get('operation', '')).strip()
        field      = str(params.get('field', '')).strip()
        value      = str(params.get('value', '')).strip()
        step_num   = int(params.get('step_number', 0))

        if not operation:
            return ToolBase.error(core, 'recipe_manage', "No operation provided for edit.")

        fname = _find_recipe_file(title)
        if not fname:
            return ToolBase.error(core, 'recipe_manage', f"No recipe found matching '{title}'.")
        
        ToolBase.speak(core, session, f"Updating Recipe {title}.")

        content  = ToolBase.read_text('recipes', fname)
        fm, body = _parse(content)

        try:
            if operation == 'set_field':
                if not field:
                    return ToolBase.error(core, 'recipe_manage', "No field specified for set_field.")
                fm[field] = value or None

            elif operation == 'add_tag':
                tags = fm.get('tags') or []
                if value and value not in tags:
                    tags.append(value)
                fm['tags'] = tags

            elif operation == 'remove_tag':
                fm['tags'] = [t for t in (fm.get('tags') or []) if t != value]

            elif operation == 'add_ingredient':
                if not value:
                    return ToolBase.error(core, 'recipe_manage', "No ingredient value provided.")
                lines  = body.splitlines()
                in_ing = False
                insert = None
                for i, line in enumerate(lines):
                    if line.strip() == '## Ingredients':
                        in_ing = True
                        continue
                    if in_ing and line.startswith('##'):
                        insert = i
                        break
                if insert is not None:
                    lines.insert(insert, f"- {value}")
                else:
                    lines.append(f"- {value}")
                body = '\n'.join(lines)

            elif operation == 'remove_ingredient':
                if not value:
                    return ToolBase.error(core, 'recipe_manage', "No ingredient value provided.")
                lines = body.splitlines()
                body  = '\n'.join(
                    l for l in lines
                    if not (l.startswith('-') and value.lower() in l.lower())
                )

            elif operation == 'add_step':
                if not value:
                    return ToolBase.error(core, 'recipe_manage', "No step value provided.")
                lines  = body.splitlines()
                in_mth = False
                insert = None
                for i, line in enumerate(lines):
                    if line.strip() == '## Method':
                        in_mth = True
                        continue
                    if in_mth and line.startswith('##'):
                        insert = i
                        break
                step_count = sum(1 for l in lines if re.match(r'^\d+\.', l.strip()))
                new_step   = f"{step_count + 1}. {value}"
                if insert is not None:
                    lines.insert(insert, new_step)
                else:
                    lines.append(new_step)
                body = '\n'.join(lines)

            elif operation == 'replace_step':
                if not step_num or not value:
                    return ToolBase.error(core, 'recipe_manage',
                        "step_number and value are required for replace_step.")
                lines = body.splitlines()
                count = 0
                for i, line in enumerate(lines):
                    if re.match(r'^\d+\.', line.strip()):
                        count += 1
                        if count == step_num:
                            lines[i] = f"{step_num}. {value}"
                            break
                body = '\n'.join(lines)

            elif operation == 'remove_step':
                if not step_num:
                    return ToolBase.error(core, 'recipe_manage',
                        "step_number is required for remove_step.")
                lines    = body.splitlines()
                count    = 0
                renumber = 0
                new_lines = []
                for line in lines:
                    if re.match(r'^\d+\.', line.strip()):
                        count += 1
                        if count == step_num:
                            continue
                        renumber += 1
                        line = re.sub(r'^\d+\.', f"{renumber}.", line)
                    new_lines.append(line)
                body = '\n'.join(new_lines)

            elif operation == 'append_notes':
                if not value:
                    return ToolBase.error(core, 'recipe_manage', "No notes value provided.")
                if '## Notes' in body:
                    body = body.rstrip() + f"\n{value}"
                else:
                    body = body.rstrip() + f"\n\n## Notes\n\n{value}"

            else:
                return ToolBase.error(core, 'recipe_manage',
                    f"Unknown operation '{operation}'. Valid: set_field, add_tag, remove_tag, "
                    "add_ingredient, remove_ingredient, add_step, replace_step, remove_step, append_notes.")

        except Exception as e:
            log.error("Edit operation failed", exc_info=True)
            return ToolBase.error(core, 'recipe_manage', f"Edit failed: {e}")

        ok = ToolBase.write_text('recipes', fname, _render(fm, body))
        if not ok:
            return ToolBase.error(core, 'recipe_manage', f"Failed to save changes to '{title}'.")

        log.info("Recipe edited", extra={'data': f"{title} operation={operation}"})
        return ToolBase.result(core, 'recipe_manage', {
            "status":       "updated",
            "title":        fm.get('title', title),
            "operation":    operation,
            "instructions": f"Tell the user the recipe '{fm.get('title', title)}' has been updated.",
        })

    # ── DELETE ────────────────────────────────────────────────────────────────
    elif action == 'delete':
        fname = _find_recipe_file(title)
        if not fname:
            return ToolBase.error(core, 'recipe_manage', f"No recipe found matching '{title}'.")

        path = ToolBase.data_path('recipes', fname)
        try:
            os.remove(path)
            log.info("Recipe deleted", extra={'data': fname})
            return ToolBase.result(core, 'recipe_manage', {
                "status":       "deleted",
                "title":        title,
                "instructions": f"Tell the user the recipe '{title}' has been deleted.",
            })
        except Exception as e:
            log.error("Failed to delete recipe", exc_info=True)
            return ToolBase.error(core, 'recipe_manage', f"Failed to delete '{title}': {e}")

    else:
        return ToolBase.error(core, 'recipe_manage',
            f"Unknown action '{action}'. Use 'add', 'edit', or 'delete'.")


# ── Tool registration ─────────────────────────────────────────────────────────

TOOLS = [
    {'name': 'recipe_search', 'schema': recipe_search, 'execute': _execute_search},
    {'name': 'recipe_get',    'schema': recipe_get,    'execute': _execute_get},
    {'name': 'recipe_manage', 'schema': recipe_manage, 'execute': _execute_manage},
]