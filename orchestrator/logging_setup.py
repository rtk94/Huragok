"""Configure structlog per ADR-0002 D9. Call once at process start.

The supervisor re-calls :func:`configure_logging` once the active batch
id is known so the same JSON records land in
``.huragok/logs/batch-<batch_id>.jsonl`` as well as stdout (see
ADR-0002 D9: "All daemon log output is JSON Lines, emitted to stdout
(captured by journald under systemd) and optionally mirrored to a
rotating file"). Re-configuration is explicitly supported.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TextIO

import structlog

_LEVEL_NAMES: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


# Currently-installed file sink, tracked at module scope so repeated
# ``configure_logging`` calls (the supervisor re-configures once the
# batch id is known) can close the prior handle before wiring a new
# one. ``None`` when no file sink is active.
_ACTIVE_FILE_SINK: _FileTeeProcessor | None = None


class _FileTeeProcessor:
    """Structlog processor that appends the already-rendered record to a file.

    Sits at the tail of the processor chain, after the JSON renderer. The
    renderer emits the final string; this processor writes that string
    (plus a trailing newline) to an open file handle, then returns the
    string unchanged so the stdout :class:`~structlog.WriteLoggerFactory`
    still emits it. The result is that a single ``log.info(...)`` call
    lands in both sinks with identical content.

    If a write fails (disk full, EPIPE, permission revocation, etc.) the
    handle is closed and subsequent writes are dropped silently. Losing
    the mirror must not crash the daemon — stdout keeps flowing, and
    systemd / journald captures those records even when the file sink
    is dead.
    """

    def __init__(self, file_handle: TextIO) -> None:
        self._file: TextIO | None = file_handle

    def __call__(
        self,
        logger: object,
        method_name: str,
        event: object,
    ) -> object:
        if self._file is not None:
            try:
                self._file.write(str(event) + "\n")
            except OSError:
                # Permanent-looking failure on this handle — disable it.
                self.close()
        return event

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None


def close_file_sink() -> None:
    """Close the active batch-log file sink, if any.

    Called by the supervisor during shutdown so the mirrored log file
    gets flushed and released cleanly even when ``configure_logging``
    itself is not re-invoked.
    """
    global _ACTIVE_FILE_SINK
    if _ACTIVE_FILE_SINK is not None:
        _ACTIVE_FILE_SINK.close()
        _ACTIVE_FILE_SINK = None


def configure_logging(
    level: str = "info",
    json_output: bool = True,
    file_path: Path | None = None,
) -> None:
    """Configure structlog with JSON (prod) or console (dev) rendering.

    When ``json_output=True`` records are emitted as one JSON object per line
    to stdout. When ``False``, ``structlog.dev.ConsoleRenderer`` is used for
    human-readable output during development.

    When ``file_path`` is supplied the same JSON-rendered records are
    also appended to that path in UTF-8, line-buffered, append mode.
    The parent directory is created if missing. If the file cannot be
    opened (permission denied, disk full, etc.) a WARN is emitted to
    stdout and the daemon continues with stdout-only logging — the file
    sink is strictly additive, not load-bearing.

    Every record carries ``ts`` (ISO-8601 UTC), ``level``, plus whatever
    contextvars the caller has bound (e.g. ``component``).

    Safe to call more than once. Any previously-installed file sink is
    closed before the new configuration takes effect.
    ``cache_logger_on_first_use`` is disabled so re-configuration is
    observed by loggers that were bound prior to the call.
    """
    global _ACTIVE_FILE_SINK
    close_file_sink()

    numeric_level = _LEVEL_NAMES.get(level.lower(), logging.INFO)

    renderer: structlog.typing.Processor
    renderer = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        renderer,
    ]

    file_sink_error: str | None = None
    if file_path is not None:
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # The handle is intentionally long-lived: it belongs to the
            # active structlog processor chain and gets released via
            # ``close_file_sink()``. SIM115 does not apply here.
            handle = open(file_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        except OSError as exc:
            file_sink_error = f"{type(exc).__name__}: {exc}"
        else:
            tee = _FileTeeProcessor(handle)
            _ACTIVE_FILE_SINK = tee
            processors.append(tee)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.WriteLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )

    if file_sink_error is not None:
        structlog.get_logger(__name__).warning(
            "logging.file_sink.open_failed",
            path=str(file_path),
            error=file_sink_error,
        )
