"""
tools/memory.py — Unified memory plugin for Supernova.

Combines two concerns that belong together:

1. EPISODIC LOG (formerly conversation_log.py)
   Stores every conversation turn to SQLite with FTS5 full-text search.
   At session end, generates a concise summary via a headless LLM pass.
   Tools: recall_conversations, get_conversation_transcript

2. SEMANTIC FACTS (formerly memory.py)
   Stores atomic facts per-user in ChromaDB (vector DB).
   Relevant facts are injected silently before each user turn.
   At session end, a focused headless LLM pass extracts memorable facts
   from the transcript and stores them — resolving conflicts via tool
   result feedback rather than relying on the live LLM to call store_memory.
   Tools: store_memory, search_memory, delete_memory

The live LLM can still call store_memory directly if the user explicitly
asks it to remember something. The session-end extractor is the primary
accumulation path.

Data:
  data/memory/chromadb/   — ChromaDB vector store
  data/memory/history.db  — SQLite episodic log

Requires: pip install chromadb
"""

import uuid
import sqlite3
import json
from datetime import datetime, timedelta
from typing import Annotated

import chromadb
from pydantic import Field

from core.tool_base import ToolBase
from core.session_state import (
    get_speaker, get_endpoint_id, get_session_id, get_history,
)

log = ToolBase.logger('memory')


# ── ChromaDB — initialised once at module load ────────────────────────────────

_chroma_client:     chromadb.PersistentClient = None
_chroma_collection: chromadb.Collection       = None


def _get_collection(tool_config: dict) -> chromadb.Collection:
    global _chroma_client, _chroma_collection
    if _chroma_collection is None:
        db_path             = ToolBase.data_path('memory', 'chromadb')
        _chroma_client      = chromadb.PersistentClient(path=db_path)
        _chroma_collection  = _chroma_client.get_or_create_collection(
            name     = "memories",
            metadata = {"hnsw:space": "cosine"},
        )
        log.info("ChromaDB collection ready", extra={'data': db_path})
    return _chroma_collection


# ── SQLite — episodic log ─────────────────────────────────────────────────────

_db_path: str = None


def _get_db() -> sqlite3.Connection:
    global _db_path
    if _db_path is None:
        _db_path = ToolBase.data_path('memory', 'history.db')
    conn = sqlite3.connect(_db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS turns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            interface   TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            timestamp   TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
            content,
            content='turns',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
            INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE INDEX IF NOT EXISTS idx_turns_user_time
            ON turns(user_id, timestamp);

        CREATE INDEX IF NOT EXISTS idx_turns_session
            ON turns(session_id);

        CREATE INDEX IF NOT EXISTS idx_turns_role
            ON turns(role);
    """)
    conn.commit()


# ── User identity ─────────────────────────────────────────────────────────────

def _get_user_id(session: dict, core=None) -> str:
    """
    Resolve a canonical user_id via the presence registry where possible.
    Falls back to speaker name, then interface+endpoint, then 'unknown'.
    Canonical IDs match user_profiles.yaml keys e.g. 'jesse', 'dean'.
    """
    if core and hasattr(core, 'presence_registry'):
        registry = core.presence_registry

        speaker = get_speaker(session)
        if speaker:
            for uid in registry.all_users():
                if registry.get_friendly_name(uid).lower() == speaker.lower():
                    return uid

        endpoint  = get_endpoint_id(session)
        interface = session.get('interface', '')
        if endpoint and interface:
            uid = (
                registry.find_user_by_contact(interface, 'chat_id',     endpoint) or
                registry.find_user_by_contact(interface, 'endpoint_id', endpoint)
            )
            if uid:
                return uid

    speaker = get_speaker(session)
    if speaker:
        return f"speaker_{speaker.lower().replace(' ', '_')}"

    endpoint  = get_endpoint_id(session)
    interface = session.get('interface', 'unknown')
    if endpoint:
        return f"{interface}_{endpoint}"

    return "unknown"


# ── ChromaDB debug helper ─────────────────────────────────────────────────────

def _debug_collection(collection: chromadb.Collection):
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
                    'data': f"[{mid[:8]}] user={meta.get('user_id')} "
                            f"tags={meta.get('tags')!r} → {doc}"
                })
    except Exception as e:
        log.error("ChromaDB debug failed", extra={'data': str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — SYSTEM PROMPT & TURN CONTEXT INJECTION
# ══════════════════════════════════════════════════════════════════════════════

def provide_context(core, tool_config: dict, session: dict) -> str:
    """
    Injects a static description of the memory system into the system prompt.
    Cache-safe — never changes turn-to-turn.
    """
    if not tool_config.get('enabled', True):
        return ""

    return (
        "## Memory\n"
        "You have persistent long-term memory across conversations via the "
        "store_memory, search_memory, and delete_memory tools.\n\n"
        "When a user explicitly asks you to remember something, call store_memory "
        "immediately. Do not just acknowledge it. Do not wait to be asked again.\n\n"
        "Relevant memories are automatically surfaced before each response. "
        "If you see a [MEMORY] block in your context, treat it as important "
        "and factor it into your reply.\n\n"
        "Use search_memory when the user references past conversations, asks what "
        "you know about them, or when you are about to give personal advice and "
        "want to check for relevant stored context first."
    )


def provide_turn_context(core, tool_config: dict, session: dict, user_input: str) -> str | None:
    """
    Fires every turn. Does two things:

    1. Logs all new history entries since last cycle to SQLite (episodic log),
       then logs the incoming user message.
    2. Queries ChromaDB with the user's message and injects relevant facts
       as a [MEMORY] block if anything passes the similarity threshold.

    Also injects a [RECENT SESSION] hint if a prior session for this user
    ended recently, so the agent knows to use recall tools if referenced.
    """
    if not tool_config.get('enabled', True):
        return None

    history    = get_history(session)
    session_id = get_session_id(session)
    user_id    = _get_user_id(session, core)
    interface  = session.get('interface', 'unknown')
    timestamp  = datetime.now().isoformat()

    # ── 1a. Log history diff to SQLite ───────────────────────────────────────
    logged_key = '_memory_log_offset'
    offset     = session.get(logged_key, 0)
    new_turns  = history[offset:]

    if new_turns:
        try:
            conn = _get_db()
            for msg in new_turns:
                role    = msg.get('role', 'unknown')
                content = msg.get('content', '')

                if msg.get('tool_calls'):
                    for tc in msg['tool_calls']:
                        try:
                            fn_name = tc.function.name
                            fn_args = dict(tc.function.arguments) if tc.function.arguments else {}
                            entry   = f"[TOOL CALL] {fn_name}({json.dumps(fn_args)})"
                        except Exception:
                            entry = f"[TOOL CALL] {tc}"
                        conn.execute(
                            "INSERT INTO turns "
                            "(session_id, user_id, interface, role, content, timestamp) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (session_id, user_id, interface, 'tool_call', entry, timestamp)
                        )

                elif role == 'tool':
                    tool_name = msg.get('tool_name', 'unknown')
                    entry     = f"[TOOL RESULT: {tool_name}] {content}"
                    conn.execute(
                        "INSERT INTO turns "
                        "(session_id, user_id, interface, role, content, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, user_id, interface, 'tool_result', entry, timestamp)
                    )

                elif role == 'system':
                    entry = f"[SYSTEM] {content}"
                    conn.execute(
                        "INSERT INTO turns "
                        "(session_id, user_id, interface, role, content, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, user_id, interface, 'system', entry, timestamp)
                    )

                elif content and content.strip():
                    conn.execute(
                        "INSERT INTO turns "
                        "(session_id, user_id, interface, role, content, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, user_id, interface, role, content.strip(), timestamp)
                    )

            conn.commit()
            conn.close()
            session[logged_key] = offset + len(new_turns)

            if tool_config.get('debug', False):
                log.debug("History diff logged",
                          extra={'data': f"session={session_id[:8]} new={len(new_turns)}"})

        except Exception as e:
            log.error("Failed to log history diff", extra={'data': str(e)})

    # ── 1b. Log incoming user message ────────────────────────────────────────
    _log_turn(session, 'user', user_input, tool_config, core)

    # ── 2. Semantic memory injection ─────────────────────────────────────────
    injections = []

    collection = _get_collection(tool_config)
    top_k      = tool_config.get('inject_top_k', 4)
    threshold  = tool_config.get('similarity_threshold', 0.4)

    if tool_config.get('debug', False):
        _debug_collection(collection)

    try:
        if user_id != 'unknown':
            private = collection.query(
                query_texts = [user_input],
                where       = {"user_id": user_id},
                n_results   = top_k - 1,
                include     = ["documents", "metadatas", "distances"],
            )
            for doc, meta, dist in zip(
                private['documents'][0],
                private['metadatas'][0],
                private['distances'][0],
            ):
                if dist <= threshold:
                    tag = f"[{meta['tags']}] " if meta.get('tags') else ""
                    injections.append(f"- {tag}{doc}")
                    log.info("Memory injected",
                             extra={'data': f"user={user_id} dist={dist:.3f} content={doc[:60]!r}"})
                elif tool_config.get('debug', False):
                    log.debug("Memory skipped — below threshold",
                              extra={'data': f"dist={dist:.3f} content={doc[:60]!r}"})

        shared = collection.query(
            query_texts = [user_input],
            where       = {"user_id": "global"},
            n_results   = 2,
            include     = ["documents", "metadatas", "distances"],
        )
        for doc, meta, dist in zip(
            shared['documents'][0],
            shared['metadatas'][0],
            shared['distances'][0],
        ):
            if dist <= threshold:
                tag = f"[{meta['tags']}] " if meta.get('tags') else ""
                injections.append(f"- {tag}{doc}")
                log.info("Memory injected (shared)",
                         extra={'data': f"dist={dist:.3f} content={doc[:60]!r}"})
            elif tool_config.get('debug', False):
                log.debug("Shared memory skipped — below threshold",
                          extra={'data': f"dist={dist:.3f} content={doc[:60]!r}"})

    except Exception as e:
        log.error("Memory injection query failed", extra={'data': str(e)})

    # ── 3. Recent session hint ────────────────────────────────────────────────
    recency_minutes = tool_config.get('recency_hint_minutes', 120)
    since_iso       = (datetime.now() - timedelta(minutes=recency_minutes)).isoformat()
    recent_hint     = None

    try:
        conn   = _get_db()
        recent = conn.execute("""
            SELECT session_id,
                   MIN(timestamp) as started,
                   MAX(timestamp) as ended,
                   COUNT(*)       as turn_count
            FROM turns
            WHERE user_id    = ?
              AND session_id != ?
              AND timestamp  >= ?
            GROUP BY session_id
            ORDER BY ended DESC
            LIMIT 1
        """, (user_id, session_id, since_iso)).fetchone()
        conn.close()

        if recent:
            started = recent['started'][:16].replace('T', ' ')
            ended   = recent['ended'][:16].replace('T', ' ')
            turns   = recent['turn_count']
            sid     = recent['session_id']
            recent_hint = (
                f"[RECENT SESSION]\n"
                f"You spoke with {user_id} recently "
                f"({started} → {ended}, {turns} turns). "
                f"If they reference that conversation use "
                f"recall_conversations or get_conversation_transcript. "
                f"Session reference: {sid}"
            )
            log.info("Recent session hint injected",
                     extra={'data': f"session={sid[:8]} ended={ended}"})

    except Exception as e:
        log.error("Recent session check failed", extra={'data': str(e)})

    # ── Assemble final injection ──────────────────────────────────────────────
    parts = []

    if injections:
        log.info("Memory injection complete",
                 extra={'data': f"user={user_id} query={user_input[:60]!r} "
                                f"snippets={len(injections)}"})
        parts.append(
            "[MEMORY]\nRelevant context from past conversations:\n"
            + "\n".join(injections)
        )
    else:
        log.debug("Memory injection — nothing relevant found",
                  extra={'data': f"user={user_id} query={user_input[:60]!r}"})

    if recent_hint:
        parts.append(recent_hint)

    return "\n\n".join(parts) if parts else None


# ── SQLite helpers ────────────────────────────────────────────────────────────

def _log_turn(session: dict, role: str, content: str, tool_config: dict, core=None):
    """Write a single turn to SQLite."""
    if not content or not content.strip():
        return

    user_id    = _get_user_id(session, core)
    interface  = session.get('interface', 'unknown')
    session_id = get_session_id(session)
    timestamp  = datetime.now().isoformat()

    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO turns (session_id, user_id, interface, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, interface, role, content.strip(), timestamp)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Failed to log turn", extra={'data': str(e)})


def _store_session_summary(session_id: str, session: dict, summary: str, core=None):
    """Store a generated summary as a special role in SQLite."""
    user_id   = _get_user_id(session, core)
    interface = session.get('interface', 'unknown')
    timestamp = datetime.now().isoformat()

    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO turns "
            "(session_id, user_id, interface, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, interface, 'summary',
             f"[SUMMARY] {summary}", timestamp)
        )
        conn.commit()
        conn.close()
        log.info("Session summary stored",
                 extra={'data': f"session={session_id[:8]} preview={summary[:80]!r}"})
    except Exception as e:
        log.error("Failed to store summary", extra={'data': str(e)})


# ── Time parser (for recall tool) ─────────────────────────────────────────────

def _parse_since(since: str) -> str:
    """Parse a natural time expression into an ISO timestamp for SQL."""
    now   = datetime.now()
    since = since.lower().strip()

    if since in ('today', 'this morning', 'this afternoon', 'this evening'):
        dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif since == 'yesterday':
        dt = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif 'hour' in since:
        try:
            hours = int(''.join(c for c in since if c.isdigit()) or '1')
        except ValueError:
            hours = 1
        dt = now - timedelta(hours=hours)
    elif 'day' in since:
        try:
            days = int(''.join(c for c in since if c.isdigit()) or '7')
        except ValueError:
            days = 7
        dt = now - timedelta(days=days)
    elif 'week' in since:
        try:
            weeks = int(''.join(c for c in since if c.isdigit()) or '1')
        except ValueError:
            weeks = 1
        dt = now - timedelta(weeks=weeks)
    elif 'month' in since:
        dt = now - timedelta(days=30)
    else:
        dt = now - timedelta(hours=24)

    return dt.isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SESSION END: SUMMARY + FACT EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def on_session_end(core, tool_config: dict, session: dict):
    """
    Fires when a session closes cleanly via CoreProcessor.close_session().

    Phase 1 — Summary:
        A headless LLM pass generates a 2-4 sentence summary of the session
        and stores it in SQLite alongside the transcript.

    Phase 2 — Fact extraction:
        A second focused headless pass reads the transcript and is offered
        only the store_memory tool. It extracts memorable facts and stores
        them into ChromaDB. Conflict resolution happens via tool result
        feedback — if a similar fact already exists, the executor returns
        a conflict notice and the LLM decides whether to overwrite or skip.
    """
    if not tool_config.get('enabled', True):
        return

    session_id = get_session_id(session)
    history    = get_history(session)
    user_id    = _get_user_id(session, core)

    # Build plain-text transcript from user/assistant turns only
    lines = []
    for msg in history:
        role    = msg.get('role', '')
        content = msg.get('content', '')
        if role in ('user', 'assistant') and content:
            lines.append(f"{role.capitalize()}: {content}")

    if not lines:
        log.debug("on_session_end — no turns to process",
                  extra={'data': f"session={session_id[:8]}"})
        return

    transcript = "\n".join(lines)

    # ── Phase 1: Summary ──────────────────────────────────────────────────────
    summary_prompt = (
        "Summarise the following conversation in ideally 2 sentences, 4 maximum. "
        "Focus on what was discussed, any decisions made, and any important "
        "facts mentioned. Be concise and factual. Speak in the third person. "
        "If nothing more than testing or simple time checks happen, summarise "
        "very simply as routine detail.\n\n"
        f"Transcript:\n{transcript}"
    )

    try:
        log.info("Phase 1: generating session summary",
                 extra={'data': f"session={session_id[:8]} turns={len(lines)}"})
        summary = core.run_headless(summary_prompt)
        if summary:
            _store_session_summary(session_id, session, summary, core)
    except Exception as e:
        log.error("Session summary failed", extra={'data': str(e)})

    # ── Phase 2: Fact extraction ──────────────────────────────────────────────
    # Only run if this session had a known user (no point storing facts for
    # 'unknown' — they'd never be retrievable in a useful way).
    if user_id == 'unknown':
        log.info("Phase 2: skipping fact extraction — user_id unknown",
                 extra={'data': f"session={session_id[:8]}"})
        return

    # Pass identity into the headless session so _get_user_id resolves correctly
    speaker    = get_speaker(session)
    endpoint   = get_endpoint_id(session)
    overrides  = {
        'speaker':      speaker,
        'endpoint_id':  endpoint,
        'interface':    session.get('interface', 'headless'),
    }

    extraction_prompt = (
        "You are a memory extraction assistant. Your only job is to identify "
        "facts from the conversation transcript below that are worth remembering "
        "long-term about the user, and store each one using the store_memory tool.\n\n"
        "Extract facts that are:\n"
        "- Personal details (name, age, location, occupation)\n"
        "- Preferences and habits (diet, hobbies, routines, likes/dislikes)\n"
        "- Health or lifestyle information\n"
        "- Important decisions or commitments made\n"
        "- Anything the user explicitly asked to be remembered\n\n"
        "Do NOT extract:\n"
        "- Transient information (today's weather, what time it is)\n"
        "- Things that were only relevant to this specific conversation\n"
        "- Greetings, small talk, or test messages\n\n"
        "Store each fact as a short, clear, self-contained statement. "
        "Store them one at a time. If nothing worth remembering was said, "
        "do not call store_memory at all.\n\n"
        "IMPORTANT: you do not need to explain yourself beyond the actions you take using tools as this is a headless session."
        f"The user in this conversation is: {user_id}\n\n"
        f"Transcript:\n{transcript}"
    )

    try:
        log.info("Phase 2: extracting facts from session",
                 extra={'data': f"session={session_id[:8]} user={user_id}"})
        core.run_headless(
            prompt            = extraction_prompt,
            tools             = [store_memory],      # schema function only — scoped tool set
            session_overrides = overrides,
        )
        log.info("Phase 2: fact extraction complete",
                 extra={'data': f"session={session_id[:8]} user={user_id}"})
    except Exception as e:
        log.error("Fact extraction failed", extra={'data': str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — TOOL SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

def store_memory(
    content: Annotated[str, Field(
        description=(
            "The fact or information to remember. Write it as a short, clear, "
            "self-contained statement, e.g. 'Jesse is vegan', "
            "'Dean prefers tea over coffee', 'The household has two cats named Luna and Mochi'."
        )
    )],
    tags: Annotated[str, Field(
        default="",
        description=(
            "Optional comma-separated topic tags to help with future recall, "
            "e.g. 'diet', 'pets', 'health'. Leave empty if no obvious category."
        )
    )] = "",
    scope: Annotated[str, Field(
        default="private",
        description="'private' = this user only. 'shared' = all household members.",
    )] = "private",
) -> str:
    """
    Store a fact or piece of information to long-term memory.
    Call this when the user tells you something worth remembering —
    a preference, personal detail, health condition, habit, or decision.
    Also called by the session-end fact extractor to persist facts
    discovered in the conversation transcript.
    """
    ...


def search_memory(
    query: Annotated[str, Field(
        description=(
            "What to search for. Use natural language — e.g. 'diet preferences', "
            "'pets', 'health conditions'. The search uses semantic similarity "
            "so exact wording is not required."
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


def recall_conversations(
    since: Annotated[str, Field(
        default="today",
        description=(
            "How far back to search. Use natural expressions: "
            "'today', 'yesterday', 'this morning', "
            "'2 hours ago', '7 days ago', '1 week ago', '1 month ago'. "
            "Default to 'today' if the user is vague about time."
        )
    )] = "today",
    keywords: Annotated[str, Field(
        default="",
        description=(
            "Optional keywords to filter by. Leave empty to retrieve all sessions "
            "in the time range. Use specific words from the conversation you are "
            "trying to recall, e.g. 'diet', 'carnivore', 'recipe'."
        )
    )] = "",
    scope: Annotated[str, Field(
        default="user",
        description=(
            "'user' = only this user's sessions. "
            "'all' = entire household conversation history in this time range."
        )
    )] = "user",
) -> str:
    """
    Find past conversation sessions by time range and optional keywords.
    Returns a summary of each matching session with its session ID.
    Use get_conversation_transcript to retrieve the full transcript of a
    specific session once you have identified the relevant session ID.
    Use when the user references something from a past conversation:
    'this morning we were talking about X',
    'yesterday you helped me with Y',
    'last week we discussed Z',
    'do you remember what I said about X',
    'we were just talking about x'.
    After getting summaries, use get_conversation_transcript with the
    relevant session ID to retrieve the full transcript before responding.
    """
    ...


def get_conversation_transcript(
    session_id: Annotated[str, Field(
        description=(
            "The session ID to retrieve. Get this from recall_conversations results. "
            "Returns the full chronological transcript for that session including "
            "tool calls, system injections, and all turns."
        )
    )],
) -> str:
    """
    Retrieve the full transcript for a specific past conversation by session ID.
    Always call recall_conversations first to find the relevant session ID.
    Use this to get the complete exchange so you can accurately recall
    what was discussed, decided, or said in that conversation.
    Be specific and accurate when referencing the transcript rather than vague.
    """
    ...


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — EXECUTORS
# ══════════════════════════════════════════════════════════════════════════════

def _store_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params  = ToolBase.params(tool_args)
    content = params.get('content', '').strip()
    scope   = params.get('scope', 'private')
    tags    = params.get('tags', '')

    if not content:
        return ToolBase.error(core, 'store_memory', "No content provided.")

    user_id    = _get_user_id(session, core) if scope != 'shared' else 'global'
    collection = _get_collection(tool_config)

    try:
        # ── Conflict check ────────────────────────────────────────────────────
        # Query for the single closest existing fact for this user.
        # If it's within the conflict threshold, return a conflict notice
        # instead of writing — the LLM decides whether to proceed.
        #
        # The 'overwrite_id' param is set by the LLM on a second call to
        # signal it has seen the conflict and wants to replace the old fact.
        overwrite_id = params.get('overwrite_id', '').strip()

        if not overwrite_id and user_id != 'global':
            try:
                conflict_threshold = tool_config.get('conflict_threshold', 0.15)
                existing = collection.query(
                    query_texts = [content],
                    where       = {"user_id": user_id},
                    n_results   = 1,
                    include     = ["documents", "metadatas", "distances"],
                )
                # ids are not a valid include parameter — they are returned
                # separately and always present in the result dict
                docs      = existing['documents'][0]  if existing['documents'] else []
                distances = existing['distances'][0]  if existing['distances'] else []
                ids       = existing['ids'][0]        if existing['ids']       else []

                if docs and distances and distances[0] <= conflict_threshold:
                    existing_fact = docs[0]
                    existing_id   = ids[0]
                    log.info("Conflict detected",
                             extra={'data': f"new={content[:60]!r} "
                                            f"existing={existing_fact[:60]!r} "
                                            f"dist={distances[0]:.3f}"})
                    return ToolBase.result(core, 'store_memory', {
                        "status":      "conflict",
                        "existing_id": existing_id,
                        "existing":    existing_fact,
                        "proposed":    content,
                        "instructions": (
                            "A similar fact already exists in memory: "
                            f"'{existing_fact}'. "
                            "If your new fact REPLACES or CONTRADICTS this, "
                            "call store_memory again with the same content AND "
                            "set overwrite_id to the existing_id value provided. "
                            "If both facts can coexist (they cover different details), "
                            "call store_memory again without overwrite_id to store both. "
                            "If the existing fact is already accurate and your new fact "
                            "adds nothing, do not call store_memory again."
                        ),
                    })
            except Exception as e:
                # If the conflict check itself fails (e.g. empty collection),
                # log and continue to write — don't block on a check error.
                log.debug("Conflict check skipped", extra={'data': str(e)})

        # ── Overwrite: delete old fact before writing new one ─────────────────
        if overwrite_id:
            try:
                collection.delete(ids=[overwrite_id])
                log.info("Overwrite: deleted old fact",
                         extra={'data': f"id={overwrite_id[:8]}"})
            except Exception as e:
                log.warning("Overwrite delete failed — writing anyway",
                            extra={'data': str(e)})

        # ── Write ─────────────────────────────────────────────────────────────
        collection.add(
            documents = [content],
            metadatas = [{"user_id": user_id, "tags": tags}],
            ids       = [str(uuid.uuid4())],
        )

        total = collection.count()
        log.info("Memory stored",
                 extra={'data': f"user={user_id} scope={scope} tags={tags!r} "
                                f"total={total} overwrite={bool(overwrite_id)}"})
        log.info("  → stored content", extra={'data': content})

        # Only speak aloud during live sessions — not during headless extraction
        if not session.get('_headless'):
            ToolBase.speak(core, session, "Memory stored.")

        return ToolBase.result(core, 'store_memory', {
            "status":  "success",
            "stored":  content,
            "instructions": (
                "The memory was successfully stored. "
                "Confirm briefly to the user that you have remembered it. "
                "Do not say you lack memory tools — the storage succeeded."
            ),
        })

    except Exception as e:
        log.error("Memory store error", extra={'data': str(e)})
        if not session.get('_headless'):
            ToolBase.speak(core, session, "Memory store failure.")
        return ToolBase.error(core, 'store_memory',
                              f"Storage failed: {e}. Tell the user the memory could not be saved.")


def _search_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = ToolBase.params(tool_args)
    query  = params.get('query', '').strip()
    scope  = params.get('scope', 'all')

    if not query:
        return ToolBase.error(core, 'search_memory', "No query provided.")

    user_id    = _get_user_id(session, core)
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
            return ToolBase.result(core, 'search_memory', {
                "results":      [],
                "instructions": "Tell the user you have no memories matching that query.",
            })

        log.info("Memory search results",
                 extra={'data': f"user={user_id} query={query!r} count={len(docs)}"})

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
            "instructions": (
                "Summarise the relevant memories naturally in your response. "
                "Memory IDs are internal — never read them out to the user. "
                "They are only used if the user asks to delete a specific memory."
            ),
        })

    except Exception as e:
        log.error("Memory search error", extra={'data': str(e)})
        return ToolBase.error(core, 'search_memory', f"Search failed: {e}")


def _delete_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params     = ToolBase.params(tool_args)
    ids        = params.get('ids', [])
    delete_all = params.get('delete_all_for_user', False)
    user_id    = _get_user_id(session, core)
    collection = _get_collection(tool_config)

    try:
        if delete_all:
            collection.delete(where={"user_id": user_id})
            log.info("All memories deleted", extra={'data': user_id})
            ToolBase.speak(core, session, "Clearing all memories for you.")
            return ToolBase.result(core, 'delete_memory', {
                "status":       "deleted_all",
                "instructions": "Tell the user all their personal memories have been cleared.",
            })

        if ids:
            collection.delete(ids=ids)
            log.info("Memories deleted",
                     extra={'data': f"user={user_id} count={len(ids)}"})
            ToolBase.speak(core, session,
                           f"{len(ids)} memory deleted."
                           if len(ids) == 1 else f"{len(ids)} memories deleted.")
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


def _recall_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params   = ToolBase.params(tool_args)
    since    = params.get('since', 'today')
    keywords = params.get('keywords', '').strip()
    scope    = params.get('scope', 'user')
    user_id  = _get_user_id(session, core)
    curr_sid = get_session_id(session)

    ToolBase.speak(core, session, "Searching conversations.")

    try:
        since_iso = _parse_since(since)
        conn      = _get_db()

        if keywords:
            if scope == 'user':
                rows = conn.execute("""
                    SELECT DISTINCT t.session_id,
                           MIN(t.timestamp) as started,
                           MAX(t.timestamp) as ended,
                           COUNT(*)         as turn_count
                    FROM turns t
                    JOIN turns_fts f ON t.id = f.rowid
                    WHERE t.user_id    = ?
                      AND t.session_id != ?
                      AND t.timestamp  >= ?
                      AND turns_fts MATCH ?
                    GROUP BY t.session_id
                    ORDER BY started DESC
                    LIMIT 5
                """, (user_id, curr_sid, since_iso, keywords)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT DISTINCT t.session_id,
                           MIN(t.timestamp) as started,
                           MAX(t.timestamp) as ended,
                           COUNT(*)         as turn_count
                    FROM turns t
                    JOIN turns_fts f ON t.id = f.rowid
                    WHERE t.session_id != ?
                      AND t.timestamp  >= ?
                      AND turns_fts MATCH ?
                    GROUP BY t.session_id
                    ORDER BY started DESC
                    LIMIT 5
                """, (curr_sid, since_iso, keywords)).fetchall()
        else:
            if scope == 'user':
                rows = conn.execute("""
                    SELECT session_id,
                           MIN(timestamp) as started,
                           MAX(timestamp) as ended,
                           COUNT(*)       as turn_count
                    FROM turns
                    WHERE user_id    = ?
                      AND session_id != ?
                      AND timestamp  >= ?
                    GROUP BY session_id
                    ORDER BY started DESC
                    LIMIT 5
                """, (user_id, curr_sid, since_iso)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT session_id,
                           MIN(timestamp) as started,
                           MAX(timestamp) as ended,
                           COUNT(*)       as turn_count
                    FROM turns
                    WHERE session_id != ?
                      AND timestamp  >= ?
                    GROUP BY session_id
                    ORDER BY started DESC
                    LIMIT 5
                """, (curr_sid, since_iso)).fetchall()

        if not rows:
            conn.close()
            return ToolBase.result(core, 'recall_conversations', {
                "sessions":     [],
                "instructions": (
                    f"Tell the user you found no conversation history "
                    f"{'matching ' + repr(keywords) + ' ' if keywords else ''}"
                    f"since {since}."
                ),
            })

        summaries = []
        for row in rows:
            sid = row['session_id']
            first_user = conn.execute("""
                SELECT content FROM turns
                WHERE session_id = ? AND role = 'user'
                ORDER BY timestamp ASC LIMIT 1
            """, (sid,)).fetchone()
            last_user = conn.execute("""
                SELECT content FROM turns
                WHERE session_id = ? AND role = 'user'
                ORDER BY timestamp DESC LIMIT 1
            """, (sid,)).fetchone()

            started = row['started'][:16].replace('T', ' ')
            ended   = row['ended'][:16].replace('T', ' ')

            summaries.append({
                "session_id": sid,
                "started":    started,
                "ended":      ended,
                "turns":      row['turn_count'],
                "first_turn": first_user['content'] if first_user else "",
                "last_turn":  last_user['content']  if last_user  else "",
            })

        conn.close()
        log.info("Recall — sessions found",
                 extra={'data': f"user={user_id} since={since!r} count={len(summaries)}"})

        return ToolBase.result(core, 'recall_conversations', {
            "sessions":     summaries,
            "instructions": (
                "Summarise the matching sessions for the user naturally — "
                "describe them by time and topic only, never read out session IDs. "
                "Say things like 'earlier today around 6pm' or 'this morning you asked about X'. "
                "Then call get_conversation_transcript with the most relevant "
                "session_id to retrieve the full transcript before answering "
                "their question. Do not mention session IDs to the user at all."
            ),
        })

    except Exception as e:
        log.error("Recall failed", extra={'data': str(e)})
        return ToolBase.error(core, 'recall_conversations', f"Recall failed: {e}")


def _get_transcript_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params     = ToolBase.params(tool_args)
    target_sid = params.get('session_id', '').strip()

    if not target_sid:
        return ToolBase.error(core, 'get_conversation_transcript', "No session_id provided.")

    ToolBase.speak(core, session, "Recalling conversation.")

    try:
        conn = _get_db()
        rows = conn.execute("""
            SELECT role, content, timestamp
            FROM turns
            WHERE session_id = ?
            ORDER BY timestamp ASC
        """, (target_sid,)).fetchall()
        conn.close()

        if not rows:
            return ToolBase.error(core, 'get_conversation_transcript',
                                  f"No transcript found for session {target_sid}.")

        transcript = []
        for row in rows:
            ts      = row['timestamp'][:16].replace('T', ' ')
            role    = row['role'].capitalize()
            content = row['content']
            transcript.append(f"[{ts}] {role}: {content}")

        transcript_text = "\n".join(transcript)
        log.info("Transcript retrieved",
                 extra={'data': f"session={target_sid[:8]} turns={len(rows)}"})

        return ToolBase.result(core, 'get_conversation_transcript', {
            "session_id":   target_sid,
            "turn_count":   len(rows),
            "transcript":   transcript_text,
            "instructions": (
                "Use this transcript to accurately answer the user's question "
                "about the past conversation. Reference specific things that were "
                "said — be precise rather than vague."
            ),
        })

    except Exception as e:
        log.error("get_conversation_transcript failed", extra={'data': str(e)})
        return ToolBase.error(core, 'get_conversation_transcript',
                              f"Failed to retrieve transcript: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — TOOL REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

TOOLS = [
    {'name': 'store_memory',                'schema': store_memory,                'execute': _store_execute},
    {'name': 'search_memory',               'schema': search_memory,               'execute': _search_execute},
    {'name': 'delete_memory',               'schema': delete_memory,               'execute': _delete_execute},
    {'name': 'recall_conversations',        'schema': recall_conversations,        'execute': _recall_execute},
    {'name': 'get_conversation_transcript', 'schema': get_conversation_transcript, 'execute': _get_transcript_execute},
]