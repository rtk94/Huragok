"""Configure structlog per ADR-0002 D9. Call once at process start."""

import logging
import sys

import structlog

_LEVEL_NAMES: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def configure_logging(level: str = "info", json_output: bool = True) -> None:
    """Configure structlog with JSON (prod) or console (dev) rendering.

    When ``json_output=True`` records are emitted as one JSON object per line
    to stdout. When ``False``, ``structlog.dev.ConsoleRenderer`` is used for
    human-readable output during development.

    Every record carries ``ts`` (ISO-8601 UTC), ``level``, plus whatever
    contextvars the caller has bound (e.g. ``component``).
    """
    numeric_level = _LEVEL_NAMES.get(level.lower(), logging.INFO)

    renderer: structlog.typing.Processor
    renderer = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.WriteLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
