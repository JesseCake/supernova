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

from datetime import datetime, timedelta
from core.tool_base import ToolBase
from core.event_store import seconds_until

log = ToolBase.logger('timer')


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
    params      = ToolBase.params(tool_args)
    label       = str(params.get("label", "timer")).strip() or "timer"
    target_time = str(params.get("target_time", "")).strip()

    hours   = int(params.get("hours",   0))
    minutes = int(params.get("minutes", 0))
    seconds = int(params.get("seconds", 0))

    duration_seconds = hours * 3600 + minutes * 60 + seconds

    if target_time:
        now    = datetime.now()
        parsed = None
        formats = [
            "%Y-%m-%d %H:%M",
            "%d %B %Y %H:%M",
            "%d/%m/%Y %H:%M",
            "%H:%M",
            "%I:%M%p",
            "%I:%M %p",
            "%I%p",
            "%I %p",
        ]
        for fmt in formats:
            try:
                parsed = datetime.strptime(target_time, fmt)
                break
            except ValueError:
                continue

        if parsed is None:
            return ToolBase.error(core, 'set_timer',
                f"Could not understand the time '{target_time}'. "
                f"Try formats like '14:30', '2:30pm', or '2026-03-21 14:30'.")

        if parsed.year == 1900:
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
            if parsed <= now:
                parsed += timedelta(days=1)

        duration_seconds = int((parsed - now).total_seconds())

    if duration_seconds <= 0:
        return ToolBase.error(core, 'set_timer',
            "Please provide either a duration (e.g. '5 minutes') or a future target_time (e.g. '3pm').")

    endpoint_id = ToolBase.endpoint(session) or tool_config.get('default_endpoint', '')
    interface   = ToolBase.interface(session)

    if not endpoint_id:
        return ToolBase.error(core, 'set_timer',
            "No endpoint available to call back to when the timer fires.")

    duration_str = _format_duration(duration_seconds)
    announcement = (
        f"The '{label}' timer has finished. "
        f"It was set for {duration_str}. "
        f"Announce this to the user in a friendly, natural way."
    )

    log.info("Scheduling timer", extra={'data': f"label={label!r} duration={duration_str} endpoint={endpoint_id!r} interface={interface!r}"})

    event_id = ToolBase.schedule(
        core, session, tool_config,
        label         = label,
        delay_seconds = duration_seconds,
        announcement  = announcement,
        event_type    = 'timer',
        endpoint_id   = endpoint_id,
        interface     = interface,
    )

    ToolBase.speak(core, session, f"Setting {label} timer for {duration_str}.")

    return ToolBase.result(core, 'set_timer', {
        "status":      "set",
        "label":       label,
        "duration":    duration_str,
        "instruction": "Timer set successfully. Feedback already given. Acknowledge briefly and hangup.",
    })


def _execute_cancel(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    params   = ToolBase.params(tool_args)
    timer_id = str(params.get("timer_id", "")).strip()

    if not timer_id:
        return ToolBase.error(core, 'cancel_timer',
            "No timer_id provided. Call list_timers to find the id.")

    removed = ToolBase.cancel_schedule(core, timer_id)
    log.info("Timer cancelled" if removed else "Timer not found", extra={'data': f"id={timer_id}"})

    return ToolBase.result(core, 'cancel_timer', {
        "status":   "cancelled" if removed else "not_found",
        "timer_id": timer_id,
    })


def _execute_list(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    timers  = ToolBase.list_scheduled(core, event_type='timer')
    results = []

    for t in timers:
        secs_left = seconds_until(t.get('due_at', ''))
        results.append({
            "timer_id":  t.get('id'),
            "label":     t.get('label'),
            "time_left": _format_duration(max(0, int(secs_left))),
            "set_for":   t.get('duration_str', ''),
        })

    return ToolBase.result(core, 'list_timers', {
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