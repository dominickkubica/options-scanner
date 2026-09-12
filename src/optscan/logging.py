"""Structured logging setup.

Console renderer in dev, JSON in prod. Every log line carries a timestamp and the
logger name, so a snapshot job line can be traced back to the run that wrote it.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import structlog

from optscan.config import Settings, get_settings

# Mutable module state, deliberately in a container so callers cannot rebind it.
_state = {"configured": False}


def _command_name() -> str:
    """The subcommand this process was started for: `snapshot` from
    `pythonw -m optscan snapshot --universe`. Reduced to a safe file name."""
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            return re.sub(r"[^A-Za-z0-9_-]", "_", arg) or "optscan"
    return "optscan"


def attach_log_when_windowless(settings: Settings, command: str | None = None) -> Path | None:
    """Give a process with no console somewhere to write, and return where.

    The scheduled tasks run under pythonw.exe so there is no console window in the
    afternoon for somebody to close: closing one killed the 2026-09-10 universe capture
    80 symbols in. pythonw has no console at all, so sys.stdout and sys.stderr are None.

    This lives here rather than in the CLI because of when logging starts. Modules call
    `get_logger` at import time, which configures logging, which asks the console
    whether it is a terminal, all before `main()` has run a line. The first attempt put
    this in `main()` and a real pythonw run died on import with no log to say so. Here it
    runs before anything touches a stream, whatever the entry point.

    One appended file per command, under the data directory. A run with a console is
    left exactly as it was.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    folder = settings.data_path / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    name = command or _command_name()
    path = folder / f"{name}.log"
    # Kept open for the life of the process on purpose: it replaces the console.
    handle = path.open("a", encoding="utf-8", buffering=1)
    handle.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} {name} =====\n")
    sys.stdout = handle
    sys.stderr = handle
    return path


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
    # First, before anything below reads sys.stdout. See attach_log_when_windowless.
    attach_log_when_windowless(settings)
    level = getattr(logging, settings.log_level)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    renderer: structlog.types.Processor
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _state["configured"] = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Configures with defaults if the app never did."""
    if not _state["configured"]:
        configure_logging(get_settings())
    return structlog.get_logger(name)
