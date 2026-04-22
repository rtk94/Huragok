"""Tests for ``orchestrator.errors`` — the D7 taxonomy + retry policy.

Every category and every decide_action branch is exercised against a
synthetic :class:`SessionResult` + :class:`ClassificationContext`
pair. The module is pure — no I/O — so these tests are fast and
deterministic (except :func:`jitter_backoff`, which is deliberately
randomised and has its own targeted test).
"""

from __future__ import annotations

import random

from orchestrator.errors import (
    CATEGORIES_COUNTING_ATTEMPTS,
    NETWORK_CATEGORY,
    ClassificationContext,
    RetryAction,
    SessionFailureCategory,
    classify,
    count_attempts,
    decide_action,
    jitter_backoff,
)
from orchestrator.session.runner import SessionResult
from orchestrator.session.stream import ResultEvent

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_result(
    *,
    end_state: str = "dirty",
    exit_code: int | None = 1,
    result_event: ResultEvent | None = None,
    stderr_tail: list[str] | None = None,
    last_events: list[dict[str, object]] | None = None,
    last_stop_reason: str | None = None,
    duration_seconds: float = 0.1,
) -> SessionResult:
    return SessionResult(
        session_id="01TEST",
        end_state=end_state,  # type: ignore[arg-type]
        exit_code=exit_code,
        result_event=result_event,
        stderr_tail=stderr_tail if stderr_tail is not None else [],
        duration_seconds=duration_seconds,
        last_events=last_events if last_events is not None else [],
        last_assistant_stop_reason=last_stop_reason,
    )


def _ctx(result: SessionResult) -> ClassificationContext:
    return ClassificationContext.from_result(result)


# ---------------------------------------------------------------------------
# classify() — one test per category, plus edge cases.
# ---------------------------------------------------------------------------


def test_classify_clean_end() -> None:
    result = _make_result(
        end_state="clean",
        exit_code=0,
        result_event=ResultEvent(raw={"type": "result"}, is_error=False),
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.CLEAN_END


def test_classify_rate_limited_runner_verdict() -> None:
    # The runner sets end_state=rate-limited when the terminal result
    # subtype mentions "rate".
    result = _make_result(
        end_state="rate-limited",
        exit_code=0,
        result_event=ResultEvent(
            raw={"type": "result", "subtype": "error_rate_limit"},
            subtype="error_rate_limit",
            is_error=True,
        ),
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.RATE_LIMITED


def test_classify_rate_limited_mid_stream_429() -> None:
    # Subprocess crashed mid-session with a 429 on stderr — the runner
    # saw no terminal result so flagged dirty; the classifier upgrades
    # to RATE_LIMITED.
    result = _make_result(
        end_state="dirty",
        exit_code=1,
        stderr_tail=["HTTP 429 Too Many Requests — retry_after: 30"],
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.RATE_LIMITED


def test_classify_context_overflow_stderr_marker() -> None:
    result = _make_result(
        end_state="dirty",
        exit_code=1,
        stderr_tail=["Error: context length exceeded for model claude-opus-4-7"],
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.CONTEXT_OVERFLOW


def test_classify_context_overflow_stop_reason() -> None:
    result = _make_result(
        end_state="dirty",
        exit_code=0,
        last_stop_reason="context_length",
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.CONTEXT_OVERFLOW


def test_classify_max_tokens_is_ambiguous_without_error_flag() -> None:
    # max_tokens alone is NOT context-overflow per the conservative
    # rule in the classifier docstring.
    result = _make_result(
        end_state="dirty",
        exit_code=1,
        last_stop_reason="max_tokens",
    )
    # Falls through to SUBPROCESS_CRASH since exit code is non-zero and
    # there's no result event.
    assert classify(result, _ctx(result)) == SessionFailureCategory.SUBPROCESS_CRASH


def test_classify_max_tokens_with_error_flag_is_overflow() -> None:
    result = _make_result(
        end_state="dirty",
        exit_code=0,
        result_event=ResultEvent(raw={"type": "result"}, is_error=True),
        last_stop_reason="max_tokens",
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.CONTEXT_OVERFLOW


def test_classify_session_timeout() -> None:
    result = _make_result(
        end_state="timeout",
        exit_code=-15,
        stderr_tail=["bash: sleep: terminated"],
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.SESSION_TIMEOUT


def test_classify_subprocess_crash() -> None:
    result = _make_result(
        end_state="dirty",
        exit_code=1,
        stderr_tail=["fatal: unhandled exception\nTraceback..."],
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.SUBPROCESS_CRASH


def test_classify_spawn_failure_is_crash() -> None:
    # exit_code=None is the runner's spawn-failure path.
    result = _make_result(
        end_state="dirty",
        exit_code=None,
        stderr_tail=["spawn failed: [Errno 2] No such file or directory: 'claude'"],
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.SUBPROCESS_CRASH


def test_classify_transient_network_connection_reset() -> None:
    result = _make_result(
        end_state="dirty",
        exit_code=1,
        stderr_tail=["ConnectionResetError: [Errno 104] Connection reset by peer"],
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.TRANSIENT_NETWORK


def test_classify_transient_network_dns_failure() -> None:
    result = _make_result(
        end_state="dirty",
        exit_code=1,
        stderr_tail=["getaddrinfo failed: Temporary failure in name resolution"],
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.TRANSIENT_NETWORK


def test_classify_unknown_when_nothing_matches() -> None:
    # Clean exit 0 but no result event — weird. Neither clean nor any
    # of the failure signatures. Should land in UNKNOWN.
    result = _make_result(
        end_state="dirty",
        exit_code=0,
        result_event=ResultEvent(raw={"type": "result"}, is_error=False),
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.UNKNOWN


def test_classify_unknown_with_zero_byte_stderr() -> None:
    # No stderr, no result, weird exit code → UNKNOWN is explicitly
    # preferred over blind retry per D7.
    result = _make_result(
        end_state="dirty",
        exit_code=0,
        result_event=None,
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.UNKNOWN


def test_classify_timeout_beats_rate_limit_on_conflict() -> None:
    # Edge case from the prompt: a session that timed out with a 429
    # upstream. Timeout wins because that's the runner's authoritative
    # signal — a retry_fresh is appropriate, not a backoff.
    result = _make_result(
        end_state="timeout",
        exit_code=-15,
        stderr_tail=["HTTP 429 rate limit reached — retry_after: 60"],
    )
    assert classify(result, _ctx(result)) == SessionFailureCategory.SESSION_TIMEOUT


# ---------------------------------------------------------------------------
# decide_action() — one test per (category, attempt_count) combination.
# ---------------------------------------------------------------------------


def test_decide_action_clean_end_advances() -> None:
    assert decide_action(SessionFailureCategory.CLEAN_END, 0) == RetryAction(kind="advance")


def test_decide_action_rate_limited_uses_retry_after() -> None:
    action = decide_action(SessionFailureCategory.RATE_LIMITED, 0, retry_after=30.0)
    assert action.kind == "backoff"
    assert action.backoff_seconds == 30.0


def test_decide_action_rate_limited_default_60s() -> None:
    action = decide_action(SessionFailureCategory.RATE_LIMITED, 5)
    assert action.kind == "backoff"
    assert action.backoff_seconds == 60.0


def test_decide_action_rate_limited_attempt_count_ignored() -> None:
    # Per D7: no attempt counter for rate-limited.
    a1 = decide_action(SessionFailureCategory.RATE_LIMITED, 99, retry_after=45.0)
    a2 = decide_action(SessionFailureCategory.RATE_LIMITED, 0, retry_after=45.0)
    assert a1 == a2


def test_decide_action_context_overflow_halts() -> None:
    assert decide_action(SessionFailureCategory.CONTEXT_OVERFLOW, 0).kind == "halt"
    assert decide_action(SessionFailureCategory.CONTEXT_OVERFLOW, 5).kind == "halt"


def test_decide_action_session_timeout_retries_twice_then_escalates() -> None:
    assert decide_action(SessionFailureCategory.SESSION_TIMEOUT, 0).kind == "retry_fresh"
    assert decide_action(SessionFailureCategory.SESSION_TIMEOUT, 1).kind == "retry_fresh"
    assert decide_action(SessionFailureCategory.SESSION_TIMEOUT, 2).kind == "escalate"


def test_decide_action_subprocess_crash_retries_twice_then_escalates() -> None:
    assert decide_action(SessionFailureCategory.SUBPROCESS_CRASH, 0).kind == "retry_fresh"
    assert decide_action(SessionFailureCategory.SUBPROCESS_CRASH, 1).kind == "retry_fresh"
    assert decide_action(SessionFailureCategory.SUBPROCESS_CRASH, 2).kind == "escalate"


def test_decide_action_transient_network_backoff_schedule() -> None:
    a0 = decide_action(SessionFailureCategory.TRANSIENT_NETWORK, 0)
    a1 = decide_action(SessionFailureCategory.TRANSIENT_NETWORK, 1)
    a2 = decide_action(SessionFailureCategory.TRANSIENT_NETWORK, 2)
    a3 = decide_action(SessionFailureCategory.TRANSIENT_NETWORK, 3)
    assert a0.kind == "backoff"
    assert a0.backoff_seconds == 1.0
    assert a1.backoff_seconds == 2.0
    assert a2.backoff_seconds == 4.0
    assert a3.kind == "escalate"


def test_decide_action_unknown_halts() -> None:
    assert decide_action(SessionFailureCategory.UNKNOWN, 0).kind == "halt"


# ---------------------------------------------------------------------------
# count_attempts() and helpers.
# ---------------------------------------------------------------------------


class _FakeHistoryEntry:
    def __init__(self, category: str | None) -> None:
        self.category = category


def test_count_attempts_counts_matching_categories() -> None:
    history = [
        _FakeHistoryEntry("subprocess-crash"),
        _FakeHistoryEntry("session-timeout"),
        _FakeHistoryEntry(None),  # agent-driven happy-path transition
        _FakeHistoryEntry("transient-network"),
        _FakeHistoryEntry("subprocess-crash"),
    ]
    assert count_attempts(history, CATEGORIES_COUNTING_ATTEMPTS) == 3
    assert count_attempts(history, {NETWORK_CATEGORY}) == 1


def test_count_attempts_tolerates_missing_attribute() -> None:
    # A history-like object without a category attribute should be
    # treated as zero — resilient to future schema drift.
    class _NoCategory:
        pass

    assert count_attempts([_NoCategory()], CATEGORIES_COUNTING_ATTEMPTS) == 0


def test_count_attempts_empty_history() -> None:
    assert count_attempts([], CATEGORIES_COUNTING_ATTEMPTS) == 0


# ---------------------------------------------------------------------------
# jitter_backoff().
# ---------------------------------------------------------------------------


def test_jitter_backoff_returns_value_in_expected_range() -> None:
    random.seed(42)
    for base in (1.0, 2.0, 4.0):
        for _ in range(50):
            value = jitter_backoff(base)
            assert base <= value <= base * 1.25


def test_jitter_backoff_zero_returns_zero() -> None:
    assert jitter_backoff(0.0) == 0.0
    assert jitter_backoff(-1.0) == 0.0


# ---------------------------------------------------------------------------
# Purity guard.
# ---------------------------------------------------------------------------


def test_classify_is_pure() -> None:
    # Calling classify repeatedly on the same input yields the same
    # category — no hidden state.
    result = _make_result(
        end_state="dirty",
        exit_code=1,
        stderr_tail=["connection refused"],
    )
    ctx = _ctx(result)
    a = classify(result, ctx)
    b = classify(result, ctx)
    assert a == b == SessionFailureCategory.TRANSIENT_NETWORK


def test_retry_after_extracted_from_stderr() -> None:
    # ClassificationContext.from_result should surface a Retry-After
    # value found in stderr so the caller can pass it to decide_action.
    result = _make_result(
        end_state="dirty",
        exit_code=1,
        stderr_tail=["HTTP 429 — Retry-After: 45"],
    )
    ctx = _ctx(result)
    assert ctx.retry_after_seconds == 45.0
