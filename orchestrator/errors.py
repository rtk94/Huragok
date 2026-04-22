"""Session-failure taxonomy and retry policy (ADR-0002 D7).

Every session outcome classifies into one of seven categories; each
category has its own retry policy. This module owns both decisions as
pure functions so the supervisor stays a thin dispatcher.

Usage::

    ctx = ClassificationContext.from_result(result)
    category = classify(result, ctx)
    action = decide_action(category, attempt_count, retry_after=ctx.retry_after_seconds)

Neither function does I/O. Tests can exercise every path by constructing
synthetic :class:`SessionResult` + :class:`ClassificationContext` pairs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from orchestrator.session.runner import SessionResult
from orchestrator.session.stream import ResultEvent

__all__ = [
    "CATEGORIES_COUNTING_ATTEMPTS",
    "NETWORK_CATEGORY",
    "ClassificationContext",
    "RetryAction",
    "RetryActionKind",
    "SessionFailureCategory",
    "classify",
    "count_attempts",
    "decide_action",
    "jitter_backoff",
]


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------


class SessionFailureCategory(StrEnum):
    """The D7 seven-category session-failure taxonomy.

    :class:`StrEnum` means each member is a string (``category.value``
    equals ``str(category)``), which lets audit-log JSON writes
    round-trip through :func:`json.dumps` without a custom encoder.
    """

    CLEAN_END = "clean-end"
    RATE_LIMITED = "rate-limited"
    CONTEXT_OVERFLOW = "context-overflow"
    SESSION_TIMEOUT = "session-timeout"
    SUBPROCESS_CRASH = "subprocess-crash"
    TRANSIENT_NETWORK = "transient-network"
    UNKNOWN = "unknown"


RetryActionKind = Literal[
    "advance",
    "retry_same",
    "retry_fresh",
    "backoff",
    "escalate",
    "halt",
]


@dataclass(frozen=True, slots=True)
class RetryAction:
    """The supervisor's marching orders after a session ends.

    ``kind`` is the primary verb; ``backoff_seconds`` is populated when
    ``kind == "backoff"`` (rate-limited retry, transient-network retry).
    """

    kind: RetryActionKind
    backoff_seconds: float | None = None


# Categories whose session outcomes count toward the fresh-retry cap in
# D7. Transient-network has its own 3-attempt cap (see
# :data:`NETWORK_CATEGORY`); rate-limited has no counter by policy.
CATEGORIES_COUNTING_ATTEMPTS: frozenset[str] = frozenset(
    {
        SessionFailureCategory.SESSION_TIMEOUT.value,
        SessionFailureCategory.SUBPROCESS_CRASH.value,
    }
)

NETWORK_CATEGORY: str = SessionFailureCategory.TRANSIENT_NETWORK.value


# ---------------------------------------------------------------------------
# Signal-extraction helpers.
# ---------------------------------------------------------------------------


# Substrings that, if observed in stderr or a recent stream event, mark
# the session as context-overflow. Conservative on purpose: ambiguous
# signals fall through to UNKNOWN (halt) rather than CONTEXT_OVERFLOW
# (also halt but with a different operator message).
_CONTEXT_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context length",
    "context window",
    "context exceeded",
    "context overflow",
    "prompt is too long",
    "too many tokens",
    "input is too long",
)

# stop_reason values that Anthropic and Claude Code emit for cases the
# agent ran out of room. ``max_tokens`` is ambiguous — it usually means
# the response hit its output cap, not that the context is full — but
# when accompanied by an explicit is_error or an overflow marker we
# treat it as CONTEXT_OVERFLOW.
_STOP_REASON_OVERFLOW: frozenset[str] = frozenset({"context_length", "context_overflow"})

# Substrings indicating HTTP 429 / rate-limit signals in stderr or raw
# event JSON. The runner already short-circuits to end_state=
# ``rate-limited`` when the terminal ``result`` event subtype mentions
# "rate"; this catches mid-stream 429s that kill the subprocess before
# the result event lands.
_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "429",
    "rate limit",
    "rate-limit",
    "rate_limit",
    "too many requests",
)

# Substrings indicating transient network failures. Deliberately narrow:
# we want false-positive UNKNOWN (conservative halt) before we want
# false-positive TRANSIENT_NETWORK (three retries).
_TRANSIENT_NETWORK_MARKERS: tuple[str, ...] = (
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection closed",
    "econnreset",
    "econnrefused",
    "etimedout",
    "enetunreach",
    "temporary failure in name resolution",
    "name or service not known",
    "dns lookup failed",
    "getaddrinfo",
    "ssl: ",
    "ssl error",
    "tls handshake",
    "tlsv1",
    "eof occurred in violation of protocol",
    "remote end closed connection",
    "read timed out",
)

_RETRY_AFTER_RE = re.compile(r"retry[_\- ]?after[:=\s]*(\d+(?:\.\d+)?)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ClassificationContext:
    """Signal bundle for :func:`classify`.

    Built via :meth:`from_result` from a :class:`SessionResult`;
    constructible directly for tests that want to exercise edge cases
    without building a full result.
    """

    exit_code: int | None
    timed_out: bool
    stderr_tail: tuple[str, ...]
    result_event: ResultEvent | None
    last_events: tuple[dict[str, Any], ...]
    last_stop_reason: str | None
    retry_after_seconds: float | None

    @classmethod
    def from_result(cls, result: SessionResult) -> ClassificationContext:
        """Derive a context from a session result. No I/O."""
        return cls(
            exit_code=result.exit_code,
            timed_out=result.end_state == "timeout",
            stderr_tail=tuple(result.stderr_tail),
            result_event=result.result_event,
            last_events=tuple(result.last_events),
            last_stop_reason=result.last_assistant_stop_reason,
            retry_after_seconds=_extract_retry_after(
                result.stderr_tail, result.last_events, result.result_event
            ),
        )


# ---------------------------------------------------------------------------
# classify() and decide_action() — the two public pure functions.
# ---------------------------------------------------------------------------


def classify(result: SessionResult, context: ClassificationContext) -> SessionFailureCategory:
    """Map a session outcome to a D7 category. Pure; no I/O.

    Precedence, roughly in confidence order:

    1. Runner's own ``end_state`` — clean / rate-limited / timeout are
       authoritative when the runner recorded them.
    2. Mid-stream 429 signatures → :attr:`SessionFailureCategory.RATE_LIMITED`.
    3. Context-overflow markers → :attr:`SessionFailureCategory.CONTEXT_OVERFLOW`.
    4. Transient-network markers → :attr:`SessionFailureCategory.TRANSIENT_NETWORK`.
    5. Subprocess crash (non-zero exit without a terminal result event
       or with an erroring result event) →
       :attr:`SessionFailureCategory.SUBPROCESS_CRASH`.
    6. Fallthrough → :attr:`SessionFailureCategory.UNKNOWN`.
    """
    # 1. Fast path on runner verdicts.
    if result.end_state == "clean":
        return SessionFailureCategory.CLEAN_END
    if result.end_state == "rate-limited":
        return SessionFailureCategory.RATE_LIMITED
    if result.end_state == "timeout" or context.timed_out:
        return SessionFailureCategory.SESSION_TIMEOUT

    # 2. Mid-stream 429 that killed the subprocess before a clean result.
    if _has_rate_limit_signal(context):
        return SessionFailureCategory.RATE_LIMITED

    # 3. Context-overflow.
    if _has_context_overflow_signal(context):
        return SessionFailureCategory.CONTEXT_OVERFLOW

    # 4. Transient network.
    if _has_transient_network_signal(context):
        return SessionFailureCategory.TRANSIENT_NETWORK

    # 5. Subprocess crash: non-zero exit (or no exit code — spawn fail)
    # without a clean terminal result event. This is where the B1
    # "dirty" end_state resolves most of the time.
    if _is_subprocess_crash(context):
        return SessionFailureCategory.SUBPROCESS_CRASH

    # 6. Fallthrough.
    return SessionFailureCategory.UNKNOWN


def decide_action(
    category: SessionFailureCategory,
    attempt_count: int,
    *,
    retry_after: float | None = None,
) -> RetryAction:
    """D7 retry-policy table encoded as a pure function.

    ``attempt_count`` is the per-task count of prior session outcomes in
    the relevant category set. For SESSION_TIMEOUT / SUBPROCESS_CRASH
    that's the count across both (per D7's cross-counted caps); for
    TRANSIENT_NETWORK it's the count of that category alone. Callers
    are responsible for passing the right count; :func:`count_attempts`
    is the companion helper.

    ``retry_after`` supplies the seconds-to-wait for RATE_LIMITED. When
    absent or non-positive, the fallback of 60 seconds applies per the
    B2 design guidance.
    """
    match category:
        case SessionFailureCategory.CLEAN_END:
            return RetryAction(kind="advance")
        case SessionFailureCategory.RATE_LIMITED:
            seconds = retry_after if retry_after is not None and retry_after > 0 else 60.0
            return RetryAction(kind="backoff", backoff_seconds=float(seconds))
        case SessionFailureCategory.CONTEXT_OVERFLOW:
            return RetryAction(kind="halt")
        case SessionFailureCategory.SESSION_TIMEOUT:
            if attempt_count < 2:
                return RetryAction(kind="retry_fresh")
            return RetryAction(kind="escalate")
        case SessionFailureCategory.SUBPROCESS_CRASH:
            if attempt_count < 2:
                return RetryAction(kind="retry_fresh")
            return RetryAction(kind="escalate")
        case SessionFailureCategory.TRANSIENT_NETWORK:
            if attempt_count < 3:
                return RetryAction(
                    kind="backoff",
                    backoff_seconds=_transient_backoff_base(attempt_count),
                )
            return RetryAction(kind="escalate")
        case SessionFailureCategory.UNKNOWN:
            return RetryAction(kind="halt")


# ---------------------------------------------------------------------------
# Supporting helpers — pure, exported for testing.
# ---------------------------------------------------------------------------


def count_attempts(history: object, categories: set[str] | frozenset[str]) -> int:
    """Return the number of history entries whose ``category`` is in ``categories``.

    ``history`` is typed as ``object`` to avoid a circular import against
    :mod:`orchestrator.state.schemas`; the caller passes the list of
    :class:`HistoryEntry` from a status file.
    """
    if not hasattr(history, "__iter__"):
        return 0
    count = 0
    for entry in history:
        cat = getattr(entry, "category", None)
        if isinstance(cat, str) and cat in categories:
            count += 1
    return count


def jitter_backoff(base_seconds: float, spread: float = 0.25) -> float:
    """Return ``base + random [0, base*spread]``. Imported lazily by tests."""
    import random

    if base_seconds <= 0:
        return 0.0
    return base_seconds + random.uniform(0.0, base_seconds * spread)


# ---------------------------------------------------------------------------
# Internal signal matchers.
# ---------------------------------------------------------------------------


def _has_rate_limit_signal(context: ClassificationContext) -> bool:
    """Mid-stream 429 detection. Looks at stderr and the last few events."""
    if _substring_in_any(_RATE_LIMIT_MARKERS, context.stderr_tail):
        return True
    return any(_substring_in_event(_RATE_LIMIT_MARKERS, event) for event in context.last_events)


def _has_context_overflow_signal(context: ClassificationContext) -> bool:
    """Conservative: requires a concrete marker, not just a stop_reason."""
    if _substring_in_any(_CONTEXT_OVERFLOW_MARKERS, context.stderr_tail):
        return True
    if any(_substring_in_event(_CONTEXT_OVERFLOW_MARKERS, event) for event in context.last_events):
        return True
    # stop_reason alone is a hint; only treat as overflow when it's one
    # of the explicit overflow reasons. ``max_tokens`` is deliberately
    # NOT in that set because it most often means the response was
    # truncated, not the context was full.
    if context.last_stop_reason in _STOP_REASON_OVERFLOW:
        return True
    # max_tokens is ambiguous: accept it as overflow only if the result
    # event also flagged is_error, which Claude Code sets when the
    # model truly ran out of room.
    return context.last_stop_reason == "max_tokens" and (
        context.result_event is not None and context.result_event.is_error
    )


def _has_transient_network_signal(context: ClassificationContext) -> bool:
    return _substring_in_any(_TRANSIENT_NETWORK_MARKERS, context.stderr_tail)


def _is_subprocess_crash(context: ClassificationContext) -> bool:
    if context.exit_code is None:
        # Spawn failure — ``run_session`` puts the error on stderr_tail.
        return True
    if context.exit_code == 0:
        return False
    # Non-zero exit. Count it as a crash when there's no clean result.
    return context.result_event is None or context.result_event.is_error


def _substring_in_any(markers: tuple[str, ...], lines: tuple[str, ...] | list[str]) -> bool:
    """Case-insensitive substring match against any element of ``lines``."""
    for line in lines:
        if not isinstance(line, str):
            continue
        lowered = line.lower()
        for marker in markers:
            if marker in lowered:
                return True
    return False


def _substring_in_event(markers: tuple[str, ...], event: dict[str, Any]) -> bool:
    """Case-insensitive substring match against the raw event's JSON text."""
    import json

    try:
        blob = json.dumps(event, default=str).lower()
    except (TypeError, ValueError):
        return False
    return any(marker in blob for marker in markers)


def _extract_retry_after(
    stderr_tail: list[str] | tuple[str, ...],
    last_events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    result_event: ResultEvent | None,
) -> float | None:
    """Best-effort extraction of a Retry-After value from available signal."""
    # 1. Look for a ``retry_after`` or ``Retry-After`` signal in stderr.
    for line in stderr_tail:
        if not isinstance(line, str):
            continue
        m = _RETRY_AFTER_RE.search(line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    # 2. Look for retry_after anywhere in the last events' raw JSON.
    for event in last_events:
        value = _lookup_retry_after_in_event(event)
        if value is not None:
            return value
    # 3. Terminal result event may carry a top-level ``retry_after``.
    if result_event is not None:
        value = _lookup_retry_after_in_event(result_event.raw)
        if value is not None:
            return value
    return None


def _lookup_retry_after_in_event(event: dict[str, Any]) -> float | None:
    """Walk a stream-event dict looking for a retry-after style field."""
    if not isinstance(event, dict):
        return None
    for key in ("retry_after", "retryAfter", "Retry-After", "retry-after"):
        value = event.get(key)
        if isinstance(value, int | float) and value > 0:
            return float(value)
        if isinstance(value, str):
            m = _RETRY_AFTER_RE.search(value) or re.search(r"(\d+(?:\.\d+)?)", value)
            if m:
                try:
                    parsed = float(m.group(1))
                    if parsed > 0:
                        return parsed
                except ValueError:
                    continue
    # Recurse into nested dicts (e.g. ``message``, ``error``).
    for value in event.values():
        if isinstance(value, dict):
            nested = _lookup_retry_after_in_event(value)
            if nested is not None:
                return nested
    return None


def _transient_backoff_base(attempt_count: int) -> float:
    """Return the base backoff for TRANSIENT_NETWORK retry ``attempt_count``.

    Per B2 design: 1s, 2s, 4s. Caller is responsible for adding jitter.
    """
    return float(2**attempt_count)
