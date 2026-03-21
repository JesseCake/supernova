"""
tools/timer.py — Set a countdown timer that calls back via voice when done.

The tool itself is thin — it calls core.schedule_event() which handles
persistence, scheduling, reboot survival, and the voice callback.

Supports three operations exposed as separate LLM tools:
  - set_timer(label, duration, target_time)
      Set a timer by plain-English duration ("5 minutes", "1 hour 30 mins")
      or by clock time ("at 3pm", "2026-03-21 14:30").
  - cancel_timer(timer_id)
      Cancel a pending timer before it fires.
  - list_timers()
      List all pending timers with time remaining.

The TOOLS list at the bottom of this file registers all three with the
tool_loader using the multi-tool export convention.
"""

import re
from datetime import datetime, timedelta


# ── Schema functions (shown to the LLM as tool definitions) ──────────────────
# These functions have no body — their signature and docstring are what matters.
# The LLM reads these to understand what arguments to pass.

def set_timer(
    label:   str = "timer",
    hours:   int = 0,
    minutes: int = 0,
    seconds: int = 0,
    target_time: str = "",
):
    """
    Set a timer that announces through the speaker when it finishes.
    Provide either hours/minutes/seconds or a target_time — not both.
    The label is optional, do not ask for label information if not given.
    Use when requested to 'set a timer' or 'remind me in x to do something'

    Args:
        label: A short name for what the timer is for (if requested)
        hours: Number of hours. Default 0.
        minutes: Number of minutes. Default 0.
        seconds: Number of seconds. Default 0.
        target_time: A specific time to fire at, e.g. '14:30', '2:30pm',
                     or '2026-03-21 14:30'. Use for 'at 3pm' style requests.
                     Assumes today; rolls to tomorrow if already passed.
    """


def cancel_timer(timer_id: str):
    """
    Cancel a previously set timer before it fires.

    Args:
        timer_id: The id returned when the timer was set.
                  If the user doesn't know the id, call list_timers first
                  to find it.
    """


def list_timers():
    """
    List all currently pending timers, including their labels, ids,
    original duration, and how much time is remaining.
    """


# ── Executors (the actual implementation called when the LLM invokes a tool) ─

def _execute_set(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    """
    Resolve the timer arguments, calculate delay_seconds, and schedule the event.

    Accepts either:
      - hours/minutes/seconds integers  (relative: user says "5 minutes")
      - target_time string              (absolute: user says "at 3pm")

    If target_time is provided it takes precedence. Both paths resolve to a
    delay_seconds which is passed to core.schedule_event().
    """
    params      = tool_args.get("parameters", {})
    label       = str(params.get("label", "timer")).strip() or "timer"
    target_time = str(params.get("target_time", "")).strip()

    duration_seconds = 0

    hours   = int(params.get("hours",   0))
    minutes = int(params.get("minutes", 0))
    seconds = int(params.get("seconds", 0))

    duration_seconds = hours * 3600 + minutes * 60 + seconds

    # ── Resolve target_time to a delay ───────────────────────────────────────
    # target_time takes precedence over duration if both are somehow provided.
    if target_time:
        now    = datetime.now()
        parsed = None

        # Try common time/datetime formats in order of specificity.
        # Full datetime formats first, then time-only formats.
        formats = [
            "%Y-%m-%d %H:%M",   # 2026-03-21 14:30
            "%d %B %Y %H:%M",   # 21 March 2026 14:30
            "%d/%m/%Y %H:%M",   # 21/03/2026 14:30
            "%H:%M",            # 14:30
            "%I:%M%p",          # 2:30PM
            "%I:%M %p",         # 2:30 PM
            "%I%p",             # 2PM
            "%I %p",            # 2 PM
        ]
        for fmt in formats:
            try:
                parsed = datetime.strptime(target_time, fmt)
                break
            except ValueError:
                continue

        if parsed is None:
            return core._wrap_tool_result("set_timer", {
                "status":  "error",
                "message": (
                    f"Could not understand the time '{target_time}'. "
                    f"Try formats like '14:30', '2:30pm', or '2026-03-21 14:30'."
                ),
            })

        # If only a time was given (strptime defaults year to 1900),
        # fill in today's date. If that time has already passed today,
        # roll forward to tomorrow.
        if parsed.year == 1900:
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
            if parsed <= now:
                parsed += timedelta(days=1)

        duration_seconds = int((parsed - now).total_seconds())

    # ── Validate ──────────────────────────────────────────────────────────────
    if duration_seconds <= 0:
        return core._wrap_tool_result("set_timer", {
            "status":  "error",
            "message": (
                "Please provide either a duration (e.g. '5 minutes') or a "
                "future target_time (e.g. '3pm')."
            ),
        })

    # ── Resolve endpoint and interface ───────────────────────────────────────
    # The session carries both the endpoint_id (who to call back) and the
    # interface (which handler to use). Both are set by the interface when
    # the core session is created. New interfaces just set their own values
    # and register a matching handler in main.py — nothing here needs to change.
    endpoint_id = session.get('endpoint_id', '')
    interface   = session.get('interface', 'voice_remote')  # fallback to voice_remote for now

    if not endpoint_id:
        endpoint_id = tool_config.get('default_endpoint', '')

    if not endpoint_id:
        return core._wrap_tool_result("set_timer", {
            "status":  "error",
            "message": "No endpoint available to call back to when the timer fires.",
        })

    # ── Build announcement ────────────────────────────────────────────────────
    # This text is injected as LLM context when initiate_call() fires, so the
    # response is a natural-sounding announcement rather than a flat canned line.
    duration_str = _format_duration(duration_seconds)
    announcement = (
        f"The '{label}' timer has finished. "
        f"It was set for {duration_str}. "
        f"Announce this to the user in a friendly, natural way."
    )

    # ── Schedule ──────────────────────────────────────────────────────────────
    event_id = core.schedule_event(
        event_type    = 'timer',
        label         = label,
        delay_seconds = duration_seconds,
        endpoint_id   = endpoint_id,
        announcement  = announcement,
        extra         = {
            # Store duration metadata so list_timers can show original and remaining.
            'duration_seconds': duration_seconds,
            'duration_str':     duration_str,
            'callback_type':    interface,   # routes to the right handler in main.py
        },
    )

    # Immediate spoken feedback — heard before the LLM formulates its response
    core.send_whole_response(f"Setting {label} timer for {duration_str}.", session)

    return core._wrap_tool_result("set_timer", {
        "status":   "set",
        "label":    label,
        "duration": duration_str,
        "instruction": "Timer set successfully. Feedback already given. Acknowledge briefly and hangup.",
    })


def _execute_cancel(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    """
    Cancel a pending timer by its id.
    Returns 'cancelled' if found and removed, 'not_found' if the id was unknown
    (e.g. the timer already fired or the id was wrong).
    """
    params   = tool_args.get("parameters", {})
    timer_id = str(params.get("timer_id", "")).strip()

    if not timer_id:
        return core._wrap_tool_result("cancel_timer", {
            "status":  "error",
            "message": "No timer_id provided. Call list_timers to find the id.",
        })

    removed = core.cancel_event(timer_id)

    return core._wrap_tool_result("cancel_timer", {
        "status":   "cancelled" if removed else "not_found",
        "timer_id": timer_id,
    })


def _execute_list(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    """
    Return all pending timers with their label, id, original duration, and
    remaining time. Returns an empty list if no timers are set.
    """
    from core.event_store import seconds_until

    timers  = core.list_events(event_type='timer')
    results = []

    for t in timers:
        secs_left = seconds_until(t.get('due_at', ''))
        results.append({
            "timer_id":  t.get('id'),
            "label":     t.get('label'),
            "time_left": _format_duration(max(0, int(secs_left))),
            "set_for":   t.get('duration_str', ''),
        })

    return core._wrap_tool_result("list_timers", {
        "timers": results,
        "count":  len(results),
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_duration(seconds: int) -> str:
    """
    Format a number of seconds as a human-readable string with full words.
    Examples: 3661 → '1 hour 1 minute', 90 → '1 minute 30 seconds', 45 → '45 seconds'
    """
    if seconds >= 3600:
        h, remainder = divmod(seconds, 3600)
        m            = remainder // 60
        h_str        = f"{h} hour{'s' if h != 1 else ''}"
        m_str        = f"{m} minute{'s' if m != 1 else ''}"
        return f"{h_str} {m_str}" if m else h_str
    elif seconds >= 60:
        m, s  = divmod(seconds, 60)
        m_str = f"{m} minute{'s' if m != 1 else ''}"
        s_str = f"{s} second{'s' if s != 1 else ''}"
        return f"{m_str} {s_str}" if s else m_str
    else:
        return f"{seconds} second{'s' if seconds != 1 else ''}"


# ── Tool registration ─────────────────────────────────────────────────────────
# Must be at the bottom so all functions above are defined before this list
# is evaluated. The tool_loader reads TOOLS and registers each entry.

TOOLS = [
    {
        'name':    'set_timer',
        'schema':  set_timer,
        'execute': _execute_set,
    },
    {
        'name':    'cancel_timer',
        'schema':  cancel_timer,
        'execute': _execute_cancel,
    },
    {
        'name':    'list_timers',
        'schema':  list_timers,
        'execute': _execute_list,
    },
]