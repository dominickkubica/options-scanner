"""Structured logging setup.

Console renderer in dev, JSON in prod. Every log line carries a timestamp and the
logger name, so a snapshot job line can be traced back to the run that wrote it.
"""

from __future__ import annotations

import logging
import sys

import structlog

from optscan.config import Settings, get_settings

# Mutable module state, deliberately in a container so callers cannot rebind it.
_state = {"configured": False}


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
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
