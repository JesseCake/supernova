"""
tools/memory.py — Vector memory plugin for Supernova.

Provides:
  - Automatic memory injection via provide_turn_context() before each user turn
  - Static memory capability description via provide_context() in system prompt
  - store_memory  — agent stores a fact to long-term memory
  - search_memory — agent explicitly searches memory
  - delete_memory — agent deletes memories on user request

Data lives in data/memory/chromadb/ via ToolBase.data_path().

Requires: pip install chromadb
"""

import uuid
from typing import Annotated

import chromadb
from pydantic import Field

from core.tool_base import ToolBase
from core.session_state import get_speaker, get_endpoint_id

log = ToolBase.logger('memory')

# ── ChromaDB — initialised once at module load ────────────────────────────────

_client:     chromadb.PersistentClient = None
_collection: chromadb.Collection       = None


def _get_collection(tool_config: dict):
    global _client, _collection
    if _collection is None:
        db_path     = ToolBase.data_path('memory', 'chromadb')
        _client     = chromadb.PersistentClient(path=db_path)
        _collection = _client.get_or_create_collection(
            name     = "memories",
            metadata = {"hnsw:space": "cosine"},
        )
        log.info("ChromaDB collection ready", extra={'data': db_path})
    return _collection


def _debug_collection(collection):
    """Log the full contents of the ChromaDB collection for debugging."""
    try:
        count = collection.count()
        log.info("ChromaDB state", extra={'data': f"total_memories={count}"})
        if count > 0:
            all_items = collection.get()
            for doc, meta, mid in zip(
                all_items['documents'],
                all_items['metadatas'],
                all_items['ids'],
            ):
                log.info("  stored", extra={
                    'data': f"[{mid[:8]}] user={meta.get('user_id')} tags={meta.get('tags')!r} → {doc}"
                })
    except Exception as e:
        log.error("ChromaDB debug failed", extra={'data': str(e)})


# ── User identity ─────────────────────────────────────────────────────────────

def _get_user_id(session: dict) -> str:
    """
    Resolve a stable user identity from the session.
    Voiceprint speaker name takes priority, then interface+endpoint.
    Falls back to 'unknown' if nothing is identifiable.
    """
    speaker = get_speaker(session)
    if speaker:
        return f"speaker_{speaker.lower().replace(' ', '_')}"

    endpoint  = get_endpoint_id(session)
    interface = session.get('interface', 'unknown')
    if endpoint:
        return f"{interface}_{endpoint}"

    return "unknown"


# ── Static system prompt injection ────────────────────────────────────────────

def provide_context(core, tool_config: dict, session: dict) -> str:
    """
    Injects a static description of the memory system into the top-level
    system prompt. Never changes turn-to-turn so cache-safe.
    """
    if not tool_config.get('enabled', True):
        return ""

    return (
        "## Memory\n"
        "You have persistent long-term memory across conversations via the "
        "store_memory, search_memory, and delete_memory tools.\n\n"
        "When a user tells you something worth remembering — a health condition, "
        "dietary preference, personal detail, habit, or any fact useful in future — "
        "call store_memory immediately. Do not just acknowledge it. Do not wait to be asked.\n\n"
        "Relevant memories are automatically surfaced before each response. "
        "If you see a [MEMORY] block in your context, treat it as important "
        "and factor it into your reply.\n\n"
        "Use search_memory when the user references past conversations, asks what "
        "you know about them, or when you are about to give personal advice and "
        "want to check for relevant stored context first."
    )


# ── Turn context injection ────────────────────────────────────────────────────

def provide_turn_context(core, tool_config: dict, session: dict, user_input: str) -> str | None:
    """
    Silently query vector memory using the user's message as the search query.
    Returns a formatted system message string, or None if nothing relevant found.
    Injected just before the user message each turn — cache-safe.
    """
    if not tool_config.get('enabled', True):
        return None

    user_id    = _get_user_id(session)
    collection = _get_collection(tool_config)
    top_k      = tool_config.get('inject_top_k', 4)

    _debug_collection(collection)  # temporary — remove once confirmed working

    try:
        snippets = []

        # User-private memories
        if user_id != 'unknown':
            private = collection.query(
                query_texts = [user_input],
                where       = {"user_id": user_id},
                n_results   = top_k - 1,
            )
            for doc, meta in zip(
                private['documents'][0],
                private['metadatas'][0],
            ):
                tag = f"[{meta['tags']}] " if meta.get('tags') else ""
                snippets.append(f"- {tag}{doc}")

        # Global/shared memories
        shared = collection.query(
            query_texts = [user_input],
            where       = {"user_id": "global"},
            n_results   = 2,
        )
        for doc, meta in zip(
            shared['documents'][0],
            shared['metadatas'][0],
        ):
            tag = f"[{meta['tags']}] " if meta.get('tags') else ""
            snippets.append(f"- {tag}{doc}")

        if not snippets:
            log.debug("Memory injection — nothing relevant found",
                      extra={'data': f"user={user_id} query={user_input[:60]!r}"})
            return None

        log.info("Memory injection",
                 extra={'data': f"user={user_id} query={user_input[:60]!r} snippets={len(snippets)}"})
        for s in snippets:
            log.info("  →", extra={'data': s})

        return "[MEMORY]\nRelevant context from past conversations:\n" + "\n".join(snippets)

    except Exception as e:
        log.error("Memory injection failed", extra={'data': str(e)})
        return None


# ── Schema functions (Pydantic-annotated, matching recipes convention) ─────────

def store_memory(
    content: Annotated[str, Field(
        description=(
            "The fact to store. Write it as a clear, self-contained statement "
            "that will make sense when read back with no other context. "
            "Good: 'Jesse is vegan'. Good: 'Jesse has Hashimoto's thyroiditis'. "
            "Bad: 'health thing'. Bad: 'dietary preference'."
        )
    )],
    scope: Annotated[str, Field(
        description="'private' = this user only. 'shared' = all household users.",
    )],
    tags: Annotated[str, Field(
        default="",
        description="Optional comma-separated tags e.g. 'health,diet' or 'preference'.",
    )] = "",
) -> str:
    """
    Store a fact, preference, or observation to long-term memory for future recall.
    Call this proactively whenever you learn something meaningful about a user —
    health conditions, dietary needs, preferences, relationships, habits, or any
    fact useful in a future conversation.
    Do not wait for the user to ask you to remember something — if they mention
    a health condition, dietary preference, or personal detail, store it immediately.
    Use scope='private' for anything personal to this user.
    Use scope='shared' for household facts relevant to everyone.
    Examples: 'Jesse is vegan', 'Jesse has Hashimoto's thyroiditis',
    'Jesse prefers no supplements', 'Dean is allergic to nuts'.
    """
    ...


def search_memory(
    query: Annotated[str, Field(
        description=(
            "What to search for. Be specific — "
            "'dietary restrictions and health conditions' returns better "
            "results than just 'health'."
        )
    )],
    scope: Annotated[str, Field(
        default="all",
        description="'private' = this user only. 'shared' = household. 'all' = everything.",
    )] = "all",
) -> str:
    """
    Search long-term memory for relevant information.
    Relevant memories are automatically surfaced before each response, but use
    this when you need a more targeted search — for example:
    when the user asks what you remember about something specific,
    when the user references a past conversation or decision,
    when you are about to give advice on health, diet, or lifestyle,
    or when the user's message is ambiguous and past context would help.
    Always call this before responding to 'what do you know about me',
    'do you remember when', or 'based on what you know about me'.
    """
    ...


def delete_memory(
    ids: Annotated[list[str], Field(
        default=[],
        description="Memory IDs to delete. Get these from search_memory results first.",
    )] = [],
    delete_all_for_user: Annotated[bool, Field(
        default=False,
        description=(
            "If true, wipe ALL private memories for the current user. "
            "Only use when the user has explicitly confirmed they want everything deleted."
        ),
    )] = False,
) -> str:
    """
    Delete one or more memories from long-term storage.
    Always use search_memory first to find the relevant IDs.
    Always confirm with the user what will be deleted before calling this.
    Use delete_all_for_user=true only when explicitly confirmed by the user.
    """
    ...


# ── Executors ─────────────────────────────────────────────────────────────────

def _store_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    log.info("_store_execute called", extra={'data': str(tool_args)})

    params  = ToolBase.params(tool_args)
    content = params.get('content', '').strip()
    scope   = params.get('scope', 'private')
    tags    = params.get('tags', '')

    if not content:
        return ToolBase.error(core, 'store_memory', "No content provided.")

    user_id    = _get_user_id(session) if scope == 'private' else 'global'
    collection = _get_collection(tool_config)

    try:
        log.info("Attempting memory store",
                 extra={'data': f"user={user_id} content={content[:60]!r}"})

        collection.add(
            documents = [content],
            metadatas = [{"user_id": user_id, "tags": tags}],
            ids       = [str(uuid.uuid4())],
        )

        total = collection.count()
        log.info("Memory stored — confirmed",
                 extra={'data': f"user={user_id} scope={scope} tags={tags!r} total_in_store={total}"})
        log.info("  → stored content", extra={'data': content})

        # Immediate spoken/typed feedback before LLM response
        ToolBase.speak(core, session, "Memory Stored.")

        return ToolBase.result(core, 'store_memory', {
            "status":       "success",
            "stored":       content,
            "instructions": (
                "The memory was successfully stored. "
                "Confirm briefly to the user that you have remembered it. "
                "Do not say you lack memory tools — the storage succeeded."
            ),
        })

    except Exception as e:
        log.error("Memory store error: ChromaDB add() failed", extra={'data': str(e)})
        ToolBase.speak(core, session, "Memory store failure.")
        return ToolBase.error(core, 'store_memory',
                              f"Storage failed: {e}. Tell the user the memory could not be saved.")


def _search_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    log.info("_search_execute called", extra={'data': str(tool_args)})

    params = ToolBase.params(tool_args)
    query  = params.get('query', '').strip()
    scope  = params.get('scope', 'all')

    if not query:
        return ToolBase.error(core, 'search_memory', "No query provided.")

    user_id    = _get_user_id(session)
    collection = _get_collection(tool_config)
    top_k      = tool_config.get('search_top_k', 6)

    try:
        if scope == 'private':
            where = {"user_id": user_id}
        elif scope == 'shared':
            where = {"user_id": "global"}
        else:
            where = None

        results = collection.query(
            query_texts = [query],
            where       = where,
            n_results   = top_k,
        )

        docs  = results['documents'][0] if results['documents'] else []
        metas = results['metadatas'][0] if results['metadatas'] else []
        ids   = results['ids'][0]       if results['ids']       else []

        if not docs:
            log.info("Memory search — no results",
                     extra={'data': f"user={user_id} query={query!r} scope={scope}"})
            return ToolBase.result(core, 'search_memory', {
                "results":      [],
                "instructions": "Tell the user you have no memories matching that query.",
            })

        log.info("Memory search results",
                 extra={'data': f"user={user_id} query={query!r} scope={scope} count={len(docs)}"})
        for doc, mid in zip(docs, ids):
            log.info("  →", extra={'data': f"[{mid[:8]}] {doc}"})

        formatted = [
            {
                "id":      mid,
                "content": doc,
                "tags":    meta.get('tags', ''),
                "scope":   "shared" if meta.get('user_id') == 'global' else "private",
            }
            for doc, meta, mid in zip(docs, metas, ids)
        ]

        return ToolBase.result(core, 'search_memory', {
            "results":      formatted,
            "instructions": "Summarise the relevant memories naturally in your response.",
        })

    except Exception as e:
        log.error("Memory search error", extra={'data': str(e)})
        return ToolBase.error(core, 'search_memory', f"Search failed: {e}")


def _delete_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    log.info("_delete_execute called", extra={'data': str(tool_args)})

    params     = ToolBase.params(tool_args)
    ids        = params.get('ids', [])
    delete_all = params.get('delete_all_for_user', False)
    user_id    = _get_user_id(session)
    collection = _get_collection(tool_config)

    try:
        if delete_all:
            collection.delete(where={"user_id": user_id})
            log.info("All memories deleted for user", extra={'data': user_id})
            ToolBase.speak(core, session, "Clearing all memories for you.")
            return ToolBase.result(core, 'delete_memory', {
                "status":       "deleted_all",
                "instructions": "Tell the user all their personal memories have been cleared.",
            })

        if ids:
            collection.delete(ids=ids)
            log.info("Memories deleted",
                     extra={'data': f"user={user_id} count={len(ids)} ids={ids}"})
            ToolBase.speak(core, session, f"{len(ids)} memory deleted." if len(ids) == 1 else f"{len(ids)} memories deleted.")
            return ToolBase.result(core, 'delete_memory', {
                "status":       "deleted",
                "count":        len(ids),
                "instructions": "Confirm the memories have been deleted.",
            })

        return ToolBase.error(core, 'delete_memory',
                              "Nothing to delete — provide ids or set delete_all_for_user.")

    except Exception as e:
        log.error("Memory delete error", extra={'data': str(e)})
        ToolBase.speak(core, session, "Memory delete error.")
        return ToolBase.error(core, 'delete_memory', f"Delete failed: {e}")


# ── Tool registration ─────────────────────────────────────────────────────────

TOOLS = [
    {'name': 'store_memory',  'schema': store_memory,  'execute': _store_execute},
    {'name': 'search_memory', 'schema': search_memory, 'execute': _search_execute},
    {'name': 'delete_memory', 'schema': delete_memory, 'execute': _delete_execute},
]