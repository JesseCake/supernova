"""
core/logger.py — Centralised logging for Supernova.

Sets up a single root logger with:
  - Coloured console output (by level)
  - Rolling file output to logs/supernova.log (INFO and above)
  - Optional debug file output to logs/debug.log (all levels)

Usage:
    from core.logger import get_logger
    log = get_logger(__name__)          # standard module logger
    log = get_logger('voice_remote')    # named component logger

    log.debug("VAD rising edge")
    log.info("Session created", extra={'data': 'id=abc123'})
    log.warning("Speaker ID no match")
    log.error("Connection failed", exc_info=True)

All loggers are children of the 'supernova' root logger so they all
inherit the same handlers and formatting.

Call setup_logging() once at startup in main.py:
    from core.logger import setup_logging
    setup_logging(debug=config.debug.verbose, log_dir='logs')
"""

import logging
import logging.handlers
import os
import sys


# ── Colour map for console output ─────────────────────────────────────────────

COLOURS = {
    'DEBUG':    '\033[36m',    # cyan
    'INFO':     '\033[32m',    # green
    'WARNING':  '\033[33m',    # yellow
    'ERROR':    '\033[31m',    # red
    'CRITICAL': '\033[35m',    # magenta
}
RESET  = '\033[0m'
BOLD   = '\033[1m'
DIM    = '\033[2m'


# ── Formatters ────────────────────────────────────────────────────────────────

class ColouredFormatter(logging.Formatter):
    """
    Console formatter with colour-coded level names and dimmed timestamps.

    Format:
        2026-03-22 09:15:23.451  INFO      voice_remote  Session created | id=abc123
    """

    FORMAT = '{time}  {level:<9} {name:<16} {message}'

    def format(self, record: logging.LogRecord) -> str:
        colour    = COLOURS.get(record.levelname, '')
        time_str  = self.formatTime(record, '%Y-%m-%d %H:%M:%S')
        # Add milliseconds
        time_str  = f"{time_str}.{record.msecs:03.0f}"

        # Shorten logger name — strip 'supernova.' prefix if present
        name = record.name.replace('supernova.', '')

        # Build the extra data string if present
        data = getattr(record, 'data', None)
        msg  = record.getMessage()
        if data:
            msg = f"{msg} | {data}"

        # Format exception if present
        exc = ''
        if record.exc_info:
            exc = '\n' + self.formatException(record.exc_info)

        line = self.FORMAT.format(
            time    = DIM + time_str + RESET,
            level   = colour + BOLD + record.levelname + RESET,
            name    = DIM + name + RESET,
            message = msg,
        )
        return line + exc


class PlainFormatter(logging.Formatter):
    """
    Plain formatter for file output — no colour codes.

    Format:
        2026-03-22 09:15:23.451 | INFO     | voice_remote     | Session created | id=abc123
    """

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, '%Y-%m-%d %H:%M:%S')
        time_str = f"{time_str}.{record.msecs:03.0f}"

        name = record.name.replace('supernova.', '')
        msg  = record.getMessage()
        data = getattr(record, 'data', None)
        if data:
            msg = f"{msg} | {data}"

        line = f"{time_str} | {record.levelname:<8} | {name:<16} | {msg}"

        if record.exc_info:
            line += '\n' + self.formatException(record.exc_info)

        return line


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_logging(debug: bool = False, log_dir: str = 'logs'):
    """
    Configure the 'supernova' root logger.

    Call once at startup in main.py before any interfaces are created.

    Args:
        debug:   If True, sets console to DEBUG level and writes a separate
                 debug.log file with all levels.
        log_dir: Directory for log files. Created if it doesn't exist.
    """
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger('supernova')
    root.setLevel(logging.DEBUG)   # capture everything, handlers filter by level
    root.handlers.clear()

    # ── Console handler ───────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(ColouredFormatter())
    root.addHandler(console)

    # ── Rolling file handler — INFO and above ─────────────────────────────────
    info_path = os.path.join(log_dir, 'supernova.log')
    info_file = logging.handlers.RotatingFileHandler(
        info_path,
        maxBytes    = 10 * 1024 * 1024,   # 10MB per file
        backupCount = 5,
        encoding    = 'utf-8',
    )
    info_file.setLevel(logging.INFO)
    info_file.setFormatter(PlainFormatter())
    root.addHandler(info_file)

    # ── Debug file handler — all levels (only when debug=True) ────────────────
    if debug:
        debug_path = os.path.join(log_dir, 'debug.log')
        debug_file = logging.handlers.RotatingFileHandler(
            debug_path,
            maxBytes    = 20 * 1024 * 1024,   # 20MB per file
            backupCount = 3,
            encoding    = 'utf-8',
        )
        debug_file.setLevel(logging.DEBUG)
        debug_file.setFormatter(PlainFormatter())
        root.addHandler(debug_file)

    root.info(
        f"Logging started",
        extra={'data': f"level={'DEBUG' if debug else 'INFO'} log_dir={log_dir}"}
    )


# ── Public API ────────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """
    Get a logger namespaced under 'supernova'.

    Examples:
        get_logger('core')           → supernova.core
        get_logger('voice_remote')   → supernova.voice_remote
        get_logger(__name__)         → supernova.core.core (if called from core/core.py)
    """
    # If caller passes __name__ it may already include the package path —
    # just prefix with supernova if not already there
    if not name.startswith('supernova'):
        name = f'supernova.{name}'
    return logging.getLogger(name)