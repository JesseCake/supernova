"""
tools/conversation_log.py — Episodic conversation history for Supernova.

Stores every conversation turn to SQLite with FTS5 full-text search.
Provides two tools:
  - recall_conversations        — find matching sessions by time + keywords,
                                  returns summaries with session IDs
  - get_conversation_transcript — pull the full transcript for a specific session

Complements vector memory (memory.py) — vector memory stores semantic facts,
this stores episodic conversation history verbatim including tool calls,
system injections, and assistant responses.

Data lives in data/conversation_log/history.db via ToolBase.data_path().
No extra dependencies — uses Python's built-in sqlite3.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from typing import Annotated
from pydantic import Field

from core.tool_base import ToolBase
from core.session_state import get_speaker, get_endpoint_id, get_session_id, get_history

log = ToolBase.logger('conversation_log')

# ── Database ──────────────────────────────────────────────────────────────────

_db_path: str = None


def _get_db() -> sqlite3.Connection:
    global _db_path
    if _db_path is None:
        _db_path = ToolBase.data_path('conversation_log', 'history.db')
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
    Resolve a canonical user_id using the presence registry where possible.
    Falls back to speaker name, then interface+endpoint, then 'unknown'.
    Canonical IDs match user_profiles.yaml keys e.g. 'jesse', 'dean'.
    """
    if core and hasattr(core, 'presence_registry'):
        registry = core.presence_registry

        # Match by speaker name against friendly_name in profiles
        speaker = get_speaker(session)
        if speaker:
            for uid in registry.all_users():
                if registry.get_friendly_name(uid).lower() == speaker.lower():
                    return uid

        # Match by endpoint ID against interface contact details
        endpoint  = get_endpoint_id(session)
        interface = session.get('interface', '')
        if endpoint and interface:
            uid = (
                registry.find_user_by_contact(interface, 'chat_id',   endpoint) or
                registry.find_user_by_contact(interface, 'endpoint_id', endpoint)
            )
            if uid:
                return uid

    # Fallback — not canonical but still useful for logging/grouping
    speaker = get_speaker(session)
    if speaker:
        return f"speaker_{speaker.lower().replace(' ', '_')}"

    endpoint  = get_endpoint_id(session)
    interface = session.get('interface', 'unknown')
    if endpoint:
        return f"{interface}_{endpoint}"

    return "unknown"


# ── Time parser ───────────────────────────────────────────────────────────────

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


# ── Turn logging ──────────────────────────────────────────────────────────────

def _log_turn(session: dict, role: str, content: str, tool_config: dict, core=None):
    """Write a single turn to the database."""
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

        if tool_config.get('debug', False):
            log.debug("Turn logged",
                      extra={'data': f"session={session_id[:8]} user={user_id} "
                                     f"role={role} content={content[:60]!r}"})

    except Exception as e:
        log.error("Failed to log turn", extra={'data': str(e)})

def _store_session_summary(session_id: str, session: dict, summary: str, core=None):
    """Store a generated summary for a completed session."""
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


def on_session_end(core, tool_config: dict, session: dict):
    """
    Fires when a session closes cleanly via CoreProcessor.close_session().
    Generates a summary of the conversation via a headless LLM pass
    and stores it alongside the session transcript in the DB.
    """
    if not tool_config.get('enabled', True):
        return

    session_id = get_session_id(session)
    history    = get_history(session)

    # Build plain text transcript from user/assistant turns only
    lines = []
    for msg in history:
        role    = msg.get('role', '')
        content = msg.get('content', '')
        if role in ('user', 'assistant') and content:
            lines.append(f"{role.capitalize()}: {content}")

    if not lines:
        log.debug("on_session_end — no turns to summarise",
                  extra={'data': f"session={session_id[:8]}"})
        return

    transcript = "\n".join(lines)
    prompt = (
        "Summarise the following conversation in ideally 2 sentences, 4 maximum. "
        "Focus on what was discussed, any decisions made, and any important "
        "facts mentioned. Be concise and factual. Speak in the third person. "
        "If nothing more than testing or simple time checks etc happen, summarise very simply as routine detail.\n\n"
        f"Transcript:\n{transcript}"
    )

    try:
        log.info("Generating session summary",
                 extra={'data': f"session={session_id[:8]} turns={len(lines)}"})
        summary = core.run_headless(prompt)
        if summary:
            _store_session_summary(session_id, session, summary, core)
    except Exception as e:
        log.error("Session summary failed", extra={'data': str(e)})


# ── Turn context hook — logs full history diff each cycle ─────────────────────

def provide_turn_context(core, tool_config: dict, session: dict, user_input: str) -> str | None:
    """
    Fires every turn. Logs all new history entries since last cycle
    (assistant responses, tool calls, tool results, system injections)
    then logs the incoming user message.

    Checks for recent prior sessions and injects a hint if one exists
    so the agent knows to use recall tools if the user references it.
    """
    if not tool_config.get('enabled', True):
        return None

    history    = get_history(session)
    session_id = get_session_id(session)
    user_id    = _get_user_id(session, core)
    interface  = session.get('interface', 'unknown')
    timestamp  = datetime.now().isoformat()

    # ── Log history diff ──────────────────────────────────────────────────────
    logged_key = '_conv_log_offset'
    offset     = session.get(logged_key, 0)
    new_turns  = history[offset:]

    if new_turns:
        try:
            conn = _get_db()

            for msg in new_turns:
                role    = msg.get('role', 'unknown')
                content = msg.get('content', '')

                # Tool calls — log each one separately
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

                # Tool results
                if role == 'tool':
                    tool_name = msg.get('tool_name', 'unknown')
                    entry     = f"[TOOL RESULT: {tool_name}] {content}"
                    conn.execute(
                        "INSERT INTO turns "
                        "(session_id, user_id, interface, role, content, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, user_id, interface, 'tool_result', entry, timestamp)
                    )

                # System injections
                elif role == 'system':
                    entry = f"[SYSTEM] {content}"
                    conn.execute(
                        "INSERT INTO turns "
                        "(session_id, user_id, interface, role, content, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, user_id, interface, 'system', entry, timestamp)
                    )

                # Regular user/assistant content
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

    # ── Log incoming user message ─────────────────────────────────────────────
    _log_turn(session, 'user', user_input, tool_config, core)

    # ── Recent session hint ───────────────────────────────────────────────────
    # If a prior session for this user ended recently, inject a hint so the
    # agent knows to use recall tools if the user references it.
    recency_minutes = tool_config.get('recency_hint_minutes', 120)
    since_iso       = (datetime.now() - timedelta(minutes=recency_minutes)).isoformat()

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
            hint = (
                f"[RECENT SESSION]\n"
                f"You spoke with {user_id} recently "
                f"({started} → {ended}, {turns} turns). "
                f"If they reference that conversation use "
                f"recall_conversations or get_conversation_transcript. "
                f"Session reference: {sid}"
            )
            log.info("Recent session hint injected",
                      extra={'data': f"session={sid[:8]} ended={ended} hint={hint}"})
            
            return hint

    except Exception as e:
        log.error("Recent session check failed", extra={'data': str(e)})

    return None


# ── Schema functions ──────────────────────────────────────────────────────────

def recall_conversations(
    since: Annotated[str, Field(
        default="today",
        description=(
            "How far back to search. Use natural expressions: "
            "'today', 'yesterday', 'this morning', "
            "'2 hours ago', '7 days ago', '1 week ago', '1 month ago'." 
            "Use this when the user references something from a past conversation or asks if you remember something they said before. If they are vague about time, default to 'today' to avoid irrelevant results from long ago conversations."
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
    'do you remember what I said about X'.
    'we were just talking about x'
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


# ── Executors ─────────────────────────────────────────────────────────────────

def _recall_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    log.info("_recall_execute called", extra={'data': str(tool_args)})

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
            log.info("Recall — no sessions found",
                     extra={'data': f"user={user_id} since={since!r} keywords={keywords!r}"})
            return ToolBase.result(core, 'recall_conversations', {
                "sessions":     [],
                "instructions": (
                    f"Tell the user you found no conversation history "
                    f"{'matching ' + repr(keywords) + ' ' if keywords else ''}"
                    f"since {since}."
                ),
            })

        # Build summaries — first and last user turn per session
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
                "their question. Do not mention session IDs to the user at all - these are internal to you only."
            ),
        })

    except Exception as e:
        log.error("Recall failed", extra={'data': str(e)})
        return ToolBase.error(core, 'recall_conversations', f"Recall failed: {e}")


def _get_transcript_execute(tool_args: dict, session, core, tool_config: dict) -> str:
    log.info("_get_transcript_execute called", extra={'data': str(tool_args)})

    params     = ToolBase.params(tool_args)
    target_sid = params.get('session_id', '').strip()

    if not target_sid:
        return ToolBase.error(core, 'get_conversation_transcript',
                              "No session_id provided.")

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

        if tool_config.get('debug', False):
            log.debug("Transcript preview",
                      extra={'data': transcript_text[:300]})

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


# ── Tool registration ─────────────────────────────────────────────────────────

TOOLS = [
    {'name': 'recall_conversations',        'schema': recall_conversations,         'execute': _recall_execute},
    {'name': 'get_conversation_transcript', 'schema': get_conversation_transcript, 'execute': _get_transcript_execute},
]