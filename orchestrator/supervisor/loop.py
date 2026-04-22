"""Top-level asyncio event loop for the Huragok daemon (ADR-0002 D1).

:func:`run` is the function the CLI ``huragok run`` command enters. It
wires up the budget tracker, the notification dispatcher, the
signal handlers, and the per-iteration state machine driver described
in ADR-0002 D1 and ADR-0003 D1.

The loop does not implement the full Phase-1 feature set — B2 adds the
reply-file → dispatcher handoff, the retry-policy beyond "two dirty ends
and block", and the live session breakdown in ``huragok status``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog
from uuid_v7.base import uuid7

from orchestrator.budget import (
    BudgetTracker,
    CostReconciler,
    RateLimitLog,
    load_pricing,
)
from orchestrator.budget.pricing import ensure_models_priced
from orchestrator.config import HuragokSettings
from orchestrator.constants import MIN_CLAUDE_CODE_VERSION
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
from orchestrator.logging_setup import close_file_sink, configure_logging
from orchestrator.notifications import (
    LoggingDispatcher,
    Notification,
    NotificationDispatcher,
    TelegramDispatcher,
)
from orchestrator.paths import batch_log, daemon_pid_file, task_dir
from orchestrator.session import BudgetEvent, SessionResult, run_session
from orchestrator.state import (
    AwaitingReply,
    HistoryEntry,
    SessionBudget,
    StateFile,
    StatusFile,
    append_audit,
    cleanup_stale_tmp,
    read_batch,
    read_state,
    read_status,
    write_state,
    write_status,
)
from orchestrator.supervisor.sd_notify import sd_notify
from orchestrator.supervisor.signals import (
    ParsedRequest,
    SignalState,
    install_signal_handlers,
    process_request_files,
    sleep_or_shutdown,
)

__all__ = [
    "DEFAULT_REACHABILITY_POLL_SECONDS",
    "DEFAULT_REQUEST_POLL_SECONDS",
    "DEFAULT_SHUTDOWN_GRACE_SECONDS",
    "ROLE_FOR_STATE",
    "SessionAttempt",
    "SupervisorContext",
    "build_dispatcher",
    "run",
    "run_supervisor",
]


DEFAULT_REQUEST_POLL_SECONDS: float = 1.5

# How often the main loop checks the dispatcher's reachable state to
# decide whether to transition in/out of ``paused``. One second is a
# rounding error compared to the 10-minute grace window.
DEFAULT_REACHABILITY_POLL_SECONDS: float = 1.0

# Maximum time the supervisor waits for long-lived coroutines (tracker,
# dispatcher) to drain after shutdown fires. Above this, stragglers are
# cancelled. Intentionally well below operator patience (~2s felt-lag
# for Ctrl-C) while above the worst-case tracker drain of a realistic
# queued-event burst.
DEFAULT_SHUTDOWN_GRACE_SECONDS: float = 1.0

# ADR-0003 D1: role chosen by the Supervisor from the current status.state.
# ``None`` indicates a non-session state (terminal or waiting on operator).
ROLE_FOR_STATE: dict[str, str | None] = {
    "pending": "architect",
    "speccing": "architect",
    "implementing": "implementer",
    "testing": "testwriter",
    "reviewing": "critic",
    "software-complete": None,
    "awaiting-human": None,
    "done": None,
    "blocked": None,
}

# Per ADR-0003 D4: model assignment by role.
MODEL_FOR_ROLE: dict[str, str] = {
    "architect": "claude-opus-4-7",
    "implementer": "claude-sonnet-4-6",
    "testwriter": "claude-sonnet-4-6",
    "critic": "claude-opus-4-7",
    "documenter": "claude-haiku-4-5-20251001",
}

# Version check: claude --version produces output like "2.1.91 (Claude Code)".
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


# ---------------------------------------------------------------------------
# Dataclasses.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionAttempt:
    """In-memory per-task attempt caches, recomputed from history on demand.

    B2 moved the durable counters into ``status.yaml.history`` so they
    survive daemon restarts (ADR-0002 D7). This dataclass still exists
    as a convenience so ``_post_session`` can bundle the two numbers it
    just computed without re-walking history later in the same tick.
    """

    task_id: str
    fresh_retry_count: int = 0
    network_retry_count: int = 0


@dataclass(slots=True)
class SupervisorContext:
    """Aggregated references passed to the inner iteration helpers.

    Bundled so that refactoring does not force every helper signature to
    change. Not exported beyond the module; tests construct one directly
    when exercising individual iterations.
    """

    root: Path
    settings: HuragokSettings
    dispatcher: NotificationDispatcher
    tracker: BudgetTracker
    rate_limit: RateLimitLog
    signal_state: SignalState
    event_queue: asyncio.Queue[BudgetEvent]
    claude_binary: str
    attempts: dict[str, SessionAttempt] = field(default_factory=dict)
    request_poll_seconds: float = DEFAULT_REQUEST_POLL_SECONDS
    # Extra env vars to merge onto each session's scrubbed env. Tests use
    # this to pass FAKE_CLAUDE_MODE through without polluting the default
    # inherit allowlist.
    session_env_overrides: dict[str, str] | None = None
    # Backoff suppression hook — tests use this to shortcut multi-second
    # sleeps. Defaults to the real ``asyncio.wait_for(stop_event, t)``
    # behaviour through :func:`sleep_or_shutdown` when ``None``.
    backoff_sleeper: object = None


# ---------------------------------------------------------------------------
# Entry points.
# ---------------------------------------------------------------------------


async def run(root: Path, settings: HuragokSettings) -> int:
    """Top-level daemon coroutine. Returns the process exit code.

    Covers the startup → main-loop → shutdown sequence described in
    ADR-0002 D1 and D8. Safe to call from ``asyncio.run`` in ``huragok
    run``; the CLI command translates the return code into ``sys.exit``.
    """
    log = structlog.get_logger(__name__).bind(component="supervisor", root=str(root))

    # 1. Sanity checks that must happen before any state mutation.
    version_ok, version_msg = _check_claude_version(settings)
    if not version_ok:
        log.error("supervisor.version.rejected", error=version_msg)
        return 1

    try:
        pricing = load_pricing()
    except Exception as exc:
        log.error("supervisor.pricing.load_failed", error=str(exc))
        return 1
    try:
        ensure_models_priced(pricing, MODEL_FOR_ROLE.values())
    except Exception as exc:
        log.error("supervisor.pricing.missing_model", error=str(exc))
        return 1

    # 2. State-root preparation.
    cleanup_stale_tmp(root)
    pid_path = daemon_pid_file(root)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n")

    # The test harness overrides the Claude binary via the same env var
    # used by the version check so a single knob controls both paths.
    claude_binary = os.environ.get("HURAGOK_CLAUDE_BINARY") or "claude"

    try:
        exit_code = await run_supervisor(
            root=root,
            settings=settings,
            pricing=pricing,
            claude_binary=claude_binary,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            pid_path.unlink()
        sd_notify("STOPPING=1")
        log.info("supervisor.stopped")

    return exit_code


async def run_supervisor(
    *,
    root: Path,
    settings: HuragokSettings,
    pricing: object,  # PricingTable — ``object`` to avoid an otherwise unused import
    claude_binary: str = "claude",
    request_poll_seconds: float = DEFAULT_REQUEST_POLL_SECONDS,
    session_env_overrides: dict[str, str] | None = None,
    rate_limit_window_cap: int | None = None,
    dispatcher: NotificationDispatcher | None = None,
) -> int:
    """Run the main loop given an already-validated pricing table.

    Split from :func:`run` so tests can construct the context directly
    with a fake ``claude_binary`` and skip the version / PID bookkeeping.
    """
    log = structlog.get_logger(__name__).bind(component="supervisor", root=str(root))

    loop = asyncio.get_running_loop()
    signal_state = SignalState()
    install_signal_handlers(loop, signal_state)

    if rate_limit_window_cap is None:
        rate_limit = RateLimitLog(root)
    else:
        rate_limit = RateLimitLog(root, window_cap=rate_limit_window_cap)
    rate_limit.load()

    batch_id = _peek_batch_id(root)
    # ADR-0002 D9: mirror JSON records to `.huragok/logs/batch-<id>.jsonl`
    # so `huragok logs` has something to tail without the operator
    # redirecting stdout themselves. No-op when there is no active batch
    # yet (the sink gets installed the first time a run starts against a
    # submitted batch).
    if batch_id is not None:
        configure_logging(
            level=settings.log_level,
            file_path=batch_log(root, batch_id),
        )
    if dispatcher is None:
        dispatcher = build_dispatcher(settings=settings, root=root, batch_id=batch_id)

    admin_key = (
        settings.anthropic_admin_api_key.get_secret_value()
        if settings.anthropic_admin_api_key is not None
        else None
    )
    reconciler = CostReconciler(admin_api_key=admin_key) if admin_key else None

    batch_budgets = _load_batch_budgets(root)
    tracker = BudgetTracker(
        root=root,
        pricing=pricing,  # type: ignore[arg-type]  # PricingTable at runtime
        dispatcher=dispatcher,
        max_tokens=batch_budgets.max_tokens if batch_budgets else 0,
        max_dollars=batch_budgets.max_dollars if batch_budgets else 0.0,
        max_wall_clock_seconds=(batch_budgets.wall_clock_hours * 3600) if batch_budgets else 0.0,
        warn_threshold_pct=batch_budgets.warn_threshold_pct if batch_budgets else 80,
        reconciler=reconciler,
        batch_id=_peek_batch_id(root),
    )
    try:
        current_state = read_state(root)
        tracker.seed_from_state(current_state.budget_consumed)
        if current_state.started_at:
            tracker.mark_batch_start(current_state.started_at)
    except FileNotFoundError:
        pass

    event_queue: asyncio.Queue[BudgetEvent] = asyncio.Queue()
    ctx = SupervisorContext(
        root=root,
        settings=settings,
        dispatcher=dispatcher,
        tracker=tracker,
        rate_limit=rate_limit,
        signal_state=signal_state,
        event_queue=event_queue,
        claude_binary=claude_binary,
        request_poll_seconds=request_poll_seconds,
        session_env_overrides=dict(session_env_overrides) if session_env_overrides else None,
    )

    # Wire up the long-lived component coroutines.
    tracker_task = asyncio.create_task(tracker.run(event_queue, signal_state.shutting_down))
    dispatcher_task = asyncio.create_task(dispatcher.start(signal_state.shutting_down))

    # Signal systemd that we are READY before entering the loop.
    sd_notify("READY=1")
    log.info("supervisor.started", pid=os.getpid())

    try:
        exit_code = await _main_loop(ctx)
    finally:
        signal_state.shutting_down.set()
        # Give the long-lived coroutines a short grace window to drain
        # cleanly; cancel stragglers. TelegramDispatcher's poll loop is
        # the motivating case — its ``getUpdates`` call can be blocked
        # on a 25-second long-poll that doesn't observe the stop event
        # until the current request completes. Without this cancel, a
        # SIGINT on an idle daemon spends up to 25s in this finally
        # block before returning control to the operator. The grace
        # window is generous enough that a reasonable clean-shutdown
        # (LoggingDispatcher and the tracker drain) completes within
        # it.
        await _shutdown_background_tasks(
            (tracker_task, dispatcher_task),
            grace_seconds=DEFAULT_SHUTDOWN_GRACE_SECONDS,
        )
        # Release the batch-log file handle even when the supervisor
        # aborts on an exception; leaving it open wedges the fd until
        # the process itself exits.
        close_file_sink()

    return exit_code


async def _shutdown_background_tasks(
    tasks: tuple[asyncio.Task[object], ...],
    *,
    grace_seconds: float,
) -> None:
    """Gather ``tasks`` with a bounded wait, then cancel stragglers.

    The main loop exits as soon as :attr:`SignalState.shutting_down`
    fires. The long-lived coroutines also observe the same event, but
    the Telegram dispatcher's in-flight ``getUpdates`` request is not
    interruptible — it continues until the HTTP server replies or the
    25-second server-side timeout elapses. Canceling after a short
    grace is the cheapest way to make SIGINT-on-idle feel instant
    without reshaping the dispatcher.
    """
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=grace_seconds,
        )
    except TimeoutError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------


async def _main_loop(ctx: SupervisorContext) -> int:
    """Drive the state machine until shutdown, halt, or terminal phase."""
    log = structlog.get_logger(__name__).bind(component="supervisor")
    idle_ticks = 0

    while not ctx.signal_state.shutting_down.is_set():
        # 1. Drain any stop/halt/reply requests and forward replies into
        # state.yaml so downstream helpers can observe operator intent.
        drained = process_request_files(ctx.root, ctx.signal_state)
        _apply_drained_requests(ctx, drained)
        if ctx.signal_state.shutting_down.is_set():
            break

        # 2. Inspect state and decide the next action.
        try:
            state = read_state(ctx.root)
        except FileNotFoundError:
            log.info("supervisor.idle.no_state")
            await sleep_or_shutdown(ctx.signal_state, ctx.request_poll_seconds)
            continue

        if state.phase in ("halted", "complete"):
            log.info("supervisor.phase.terminal", phase=state.phase)
            break

        # 2b. Reconcile dispatcher reachability with the state-machine
        # phase. An unreachable dispatcher with an outstanding
        # notification pauses launches; recovery resumes them.
        state = _reconcile_reachability(ctx, state)
        if state.phase == "paused" and state.halted_reason == _NOTIFICATION_UNREACHABLE_REASON:
            await sleep_or_shutdown(ctx.signal_state, DEFAULT_REACHABILITY_POLL_SECONDS)
            continue

        # 3. Budget / halt-after-session gating.
        if ctx.tracker.over_budget():
            _transition_to_halted(ctx, state, reason="budget-exceeded")
            break
        if ctx.signal_state.halt_after_session.is_set():
            _transition_to_halted(ctx, state, reason="halt-requested")
            break

        # 4. Find the next non-terminal task.
        next_task = _pick_next_task(ctx.root, state)
        if next_task is None:
            # All tasks are in terminal states (done / blocked) and
            # there's no in-flight session. Transition to `complete`,
            # emit audit + notification, and exit cleanly. The no-batch
            # case (batch.yaml absent) idles instead.
            if _batch_is_complete(ctx.root):
                await _transition_to_complete(ctx, state)
                break
            log.info("supervisor.idle.no_pending_tasks")
            await _idle_sleep(ctx, idle_ticks)
            idle_ticks += 1
            continue
        idle_ticks = 0

        role = ROLE_FOR_STATE.get(next_task.state)
        if role is None:
            # Terminal in-session state reached but task not marked done.
            # B1 marks trivially-terminal states done; B2's human gate
            # handles the foundational notification loop.
            if next_task.state == "software-complete":
                _mark_task_done(ctx, next_task)
                continue
            log.info(
                "supervisor.task.awaiting",
                task_id=next_task.task_id,
                state=next_task.state,
            )
            await _idle_sleep(ctx, idle_ticks)
            idle_ticks += 1
            continue

        # 5. Rate-limit pre-flight.
        decision = ctx.rate_limit.query()
        if decision.status == "defer":
            log.info(
                "supervisor.rate_limit.defer",
                seconds=decision.defer_seconds,
                count=decision.count_in_window,
            )
            await _dispatch_rate_limit_notification(ctx, decision.defer_seconds)
            interrupted = await sleep_or_shutdown(ctx.signal_state, decision.defer_seconds)
            if interrupted:
                break
            continue
        if decision.status == "warn":
            log.warning(
                "supervisor.rate_limit.warn",
                count=decision.count_in_window,
                cap=decision.window_cap,
            )

        # 6. Launch a session.
        await _launch_session(ctx, state, next_task, role)

    return 0


async def _idle_sleep(ctx: SupervisorContext, idle_ticks: int) -> None:
    """Back off a little when there is no work to do.

    Adds up to ~3 seconds of extra sleep for long idle runs so we do not
    pin the event loop in a tight poll.
    """
    base = ctx.request_poll_seconds
    extra = min(idle_ticks * 0.5, 3.0)
    await sleep_or_shutdown(ctx.signal_state, base + extra)


# ---------------------------------------------------------------------------
# Launch one session.
# ---------------------------------------------------------------------------


async def _launch_session(
    ctx: SupervisorContext,
    state: StateFile,
    task: StatusFile,
    role: str,
) -> None:
    """Run a single session for ``task`` at ``role`` and update on-disk state."""
    log = structlog.get_logger(__name__).bind(component="supervisor")
    session_id = str(uuid7())
    model = MODEL_FOR_ROLE.get(role, "claude-sonnet-4-6")

    batch_id = state.batch_id
    session_timeout_seconds = _session_timeout_seconds(ctx)

    # Persist session-launch metadata BEFORE spawning. ADR-0002 D2 treats
    # session_budget as an advisory hint written into state.yaml.
    state.current_task = task.task_id
    state.current_agent = role  # type: ignore[assignment]
    state.session_id = session_id
    state.session_count = state.session_count + 1
    state.phase = "running"
    state.last_checkpoint = datetime.now(UTC)
    state.session_budget = SessionBudget(
        remaining_tokens=None,
        remaining_dollars=None,
        timeout_seconds=session_timeout_seconds,
    )
    write_state(ctx.root, state)

    if batch_id is not None:
        append_audit(
            ctx.root,
            batch_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": "session-launched",
                "task_id": task.task_id,
                "role": role,
                "session_id": session_id,
                "model": model,
            },
        )

    ctx.rate_limit.record_launch()
    log.info(
        "supervisor.session.launch",
        task_id=task.task_id,
        role=role,
        session_id=session_id,
        model=model,
    )

    result: SessionResult = await run_session(
        root=ctx.root,
        task_id=task.task_id,
        role=role,
        session_id=session_id,
        model=model,
        session_timeout_seconds=session_timeout_seconds,
        session_budget=state.session_budget,
        event_queue=ctx.event_queue,
        claude_binary=ctx.claude_binary,
        env=ctx.session_env_overrides,
    )

    await _post_session(ctx, state, task, role, session_id, result)


async def _post_session(
    ctx: SupervisorContext,
    state: StateFile,
    task: StatusFile,
    role: str,
    session_id: str,
    result: SessionResult,
) -> None:
    """Apply the session outcome to on-disk state using the D7 taxonomy.

    Pipeline per ADR-0002 D7:

    1. Classify the session result into one of seven categories.
    2. Count prior attempts of the same "retry family" from
       ``status.yaml.history``.
    3. Ask :func:`decide_action` what to do next.
    4. Apply that action — advance / retry fresh / backoff / halt /
       escalate — with appropriate state transitions, history entries,
       and audit events.
    """
    log = structlog.get_logger(__name__).bind(component="supervisor")
    batch_id = state.batch_id
    now = datetime.now(UTC)

    context = ClassificationContext.from_result(result)
    category = classify(result, context)

    # Attempt counts come from history on disk, not an in-memory cache.
    try:
        fresh_task = read_status(ctx.root, task.task_id)
    except FileNotFoundError:
        fresh_task = task
    fresh_retry_count = count_attempts(fresh_task.history, CATEGORIES_COUNTING_ATTEMPTS)
    network_retry_count = count_attempts(fresh_task.history, {NETWORK_CATEGORY})
    attempt_count = _attempts_for_category(category, fresh_retry_count, network_retry_count)

    action = decide_action(
        category,
        attempt_count,
        retry_after=context.retry_after_seconds,
    )

    if batch_id is not None:
        append_audit(
            ctx.root,
            batch_id,
            {
                "ts": now.isoformat(),
                "kind": "session-ended",
                "task_id": task.task_id,
                "role": role,
                "session_id": session_id,
                "end_state": result.end_state,
                "category": category.value,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "attempt_count": attempt_count,
                "action": action.kind,
            },
        )

    log.info(
        "supervisor.session.end",
        task_id=task.task_id,
        role=role,
        session_id=session_id,
        end_state=result.end_state,
        category=category.value,
        attempt_count=attempt_count,
        action=action.kind,
    )

    # Cache for diagnostics; not authoritative.
    ctx.attempts[task.task_id] = SessionAttempt(
        task_id=task.task_id,
        fresh_retry_count=fresh_retry_count,
        network_retry_count=network_retry_count,
    )

    await _apply_retry_action(
        ctx,
        state=state,
        task=fresh_task,
        role=role,
        session_id=session_id,
        category=category,
        action=action,
        result=result,
    )


def _attempts_for_category(
    category: SessionFailureCategory,
    fresh_retry_count: int,
    network_retry_count: int,
) -> int:
    """Pick the right counter per D7's per-category caps."""
    if category == SessionFailureCategory.TRANSIENT_NETWORK:
        return network_retry_count
    if category in (
        SessionFailureCategory.SESSION_TIMEOUT,
        SessionFailureCategory.SUBPROCESS_CRASH,
    ):
        return fresh_retry_count
    return 0


async def _apply_retry_action(
    ctx: SupervisorContext,
    *,
    state: StateFile,
    task: StatusFile,
    role: str,
    session_id: str,
    category: SessionFailureCategory,
    action: RetryAction,
    result: SessionResult,
) -> None:
    """Dispatch on :class:`RetryAction` kind and update state / history."""
    log = structlog.get_logger(__name__).bind(component="supervisor")

    if action.kind == "advance":
        # Clean end. Re-read the status to honour any agent-driven
        # transition that already fired; the supervisor doesn't mutate
        # on the happy path (ADR-0003 D2).
        with contextlib.suppress(FileNotFoundError):
            _ = read_status(ctx.root, task.task_id)
        return

    if action.kind == "backoff":
        delay = action.backoff_seconds or 0.0
        if category == SessionFailureCategory.TRANSIENT_NETWORK:
            # Per-call jitter (0-25%) so synchronised retries don't
            # stampede a recovering endpoint.
            delay = jitter_backoff(delay)
        _append_history(
            task,
            from_=task.state,
            to=task.state,
            category=category.value,
            session_id=session_id,
        )
        write_status(ctx.root, task)
        await _audit_retry(ctx, state, task, session_id, category, action, delay)
        log.info(
            "supervisor.retry.backoff",
            task_id=task.task_id,
            category=category.value,
            seconds=round(delay, 3),
        )
        await sleep_or_shutdown(ctx.signal_state, delay)
        return

    if action.kind == "retry_fresh":
        _append_history(
            task,
            from_=task.state,
            to=task.state,
            category=category.value,
            session_id=session_id,
        )
        write_status(ctx.root, task)
        await _audit_retry(ctx, state, task, session_id, category, action, None)
        log.info(
            "supervisor.retry.fresh",
            task_id=task.task_id,
            category=category.value,
        )
        return

    if action.kind == "escalate":
        await _escalate_task(
            ctx,
            state=state,
            task=task,
            role=role,
            session_id=session_id,
            category=category,
            result=result,
        )
        return

    if action.kind == "halt":
        await _halt_for_category(
            ctx,
            state=state,
            task=task,
            session_id=session_id,
            category=category,
            result=result,
        )
        return

    # retry_same — unused in Phase 1 (rate-limited uses backoff) but
    # kept in the taxonomy for symmetry with ADR-0002 D7. Treat as
    # retry_fresh.
    if action.kind == "retry_same":
        _append_history(
            task,
            from_=task.state,
            to=task.state,
            category=category.value,
            session_id=session_id,
        )
        write_status(ctx.root, task)
        await _audit_retry(ctx, state, task, session_id, category, action, None)
        return


async def _escalate_task(
    ctx: SupervisorContext,
    *,
    state: StateFile,
    task: StatusFile,
    role: str,
    session_id: str,
    category: SessionFailureCategory,
    result: SessionResult,
) -> None:
    """Escalate to the operator: transition to awaiting-human + notify."""
    log = structlog.get_logger(__name__).bind(component="supervisor")
    now = datetime.now(UTC)

    _append_history(
        task,
        from_=task.state,
        to="awaiting-human",
        category=category.value,
        session_id=session_id,
    )
    task.state = "awaiting-human"
    blocker = f"escalated after cap hit for {category.value}"
    if blocker not in task.blockers:
        task.blockers.append(blocker)
    write_status(ctx.root, task)

    if state.batch_id is not None:
        append_audit(
            ctx.root,
            state.batch_id,
            {
                "ts": now.isoformat(),
                "kind": "task-escalated",
                "task_id": task.task_id,
                "session_id": session_id,
                "category": category.value,
                "role": role,
            },
        )

    log.warning(
        "supervisor.task.escalated",
        task_id=task.task_id,
        category=category.value,
    )

    notification = Notification.make(
        kind="blocker",
        summary=_escalation_summary(task, category, result),
        reply_verbs=["continue", "iterate", "stop", "escalate"],
        metadata={
            "task_id": task.task_id,
            "session_id": session_id,
            "category": category.value,
        },
    )
    await ctx.dispatcher.send(notification)
    _set_awaiting_reply(ctx, state, notification)


async def _halt_for_category(
    ctx: SupervisorContext,
    *,
    state: StateFile,
    task: StatusFile,
    session_id: str,
    category: SessionFailureCategory,
    result: SessionResult,
) -> None:
    """Halt the batch for an unrecoverable category (context-overflow / unknown)."""
    log = structlog.get_logger(__name__).bind(component="supervisor")

    _append_history(
        task,
        from_=task.state,
        to="blocked",
        category=category.value,
        session_id=session_id,
    )
    task.state = "blocked"
    blocker = f"halt: {category.value}"
    if blocker not in task.blockers:
        task.blockers.append(blocker)
    write_status(ctx.root, task)

    notification = Notification.make(
        kind="blocker",
        summary=_escalation_summary(task, category, result),
        reply_verbs=["iterate", "stop", "escalate"],
        metadata={
            "task_id": task.task_id,
            "session_id": session_id,
            "category": category.value,
        },
    )
    await ctx.dispatcher.send(notification)
    _set_awaiting_reply(ctx, state, notification)

    _transition_to_halted(ctx, state, reason=f"halt-{category.value}")
    log.warning(
        "supervisor.halted.category",
        task_id=task.task_id,
        category=category.value,
    )


def _escalation_summary(
    task: StatusFile,
    category: SessionFailureCategory,
    result: SessionResult,
) -> str:
    """Short operator-facing summary for escalation / halt notifications."""
    tail = " ".join(result.stderr_tail[-3:]) if result.stderr_tail else ""
    tail = tail.strip().replace("\n", " ")
    if len(tail) > 160:
        tail = tail[:157] + "..."
    body = f"task {task.task_id} — {category.value}"
    if tail:
        body += f" — {tail}"
    return body


def _append_history(
    task: StatusFile,
    *,
    from_: str,
    to: str,
    category: str | None,
    session_id: str | None,
) -> None:
    task.history.append(
        HistoryEntry(
            at=datetime.now(UTC),
            from_=from_,
            to=to,
            by="supervisor",
            session_id=session_id,
            category=category,
        )
    )


async def _audit_retry(
    ctx: SupervisorContext,
    state: StateFile,
    task: StatusFile,
    session_id: str,
    category: SessionFailureCategory,
    action: RetryAction,
    backoff_seconds: float | None,
) -> None:
    if state.batch_id is None:
        return
    append_audit(
        ctx.root,
        state.batch_id,
        {
            "ts": datetime.now(UTC).isoformat(),
            "kind": "session-retry",
            "task_id": task.task_id,
            "session_id": session_id,
            "category": category.value,
            "action": action.kind,
            "backoff_seconds": backoff_seconds,
        },
    )


def _set_awaiting_reply(
    ctx: SupervisorContext,
    state: StateFile,
    notification: Notification,
) -> None:
    """Record the notification as outstanding in ``state.yaml.awaiting_reply``."""
    state.awaiting_reply = AwaitingReply(
        notification_id=notification.id,
        sent_at=notification.created_at,
        kind=notification.kind,
        deadline=None,
    )
    write_state(ctx.root, state)


def _mark_task_done(ctx: SupervisorContext, task: StatusFile) -> None:
    """Transition a ``software-complete`` task to ``done`` (non-UI path).

    B1 uses this for the trivial case where a task has no UI review
    requirement. The foundational UI gate is ADR-0001 D6 / ADR-0004.
    """
    log = structlog.get_logger(__name__).bind(component="supervisor")
    if task.ui_review.required:
        log.info("supervisor.task.awaiting_ui_review", task_id=task.task_id)
        return
    now = datetime.now(UTC)
    task.history.append(
        HistoryEntry(
            at=now,
            from_=task.state,
            to="done",
            by="supervisor",
            session_id=None,
        )
    )
    task.state = "done"
    write_status(ctx.root, task)
    log.info("supervisor.task.done", task_id=task.task_id)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _pick_next_task(root: Path, state: StateFile) -> StatusFile | None:
    """Return the first non-done, non-blocked task's status, or None."""
    try:
        batch = read_batch(root)
    except FileNotFoundError:
        return None

    # Prefer the current_task if one is live on state.yaml.
    ordered = list(batch.tasks)
    ordered.sort(key=lambda t: (t.priority, t.id))

    for task_entry in ordered:
        try:
            status = read_status(root, task_entry.id)
        except FileNotFoundError:
            status = StatusFile(
                version=1,
                task_id=task_entry.id,
                state="pending",
                foundational=task_entry.foundational,
            )
            # write_status relies on the parent directory existing — a
            # fresh task's folder may not be on disk yet, so create it.
            task_dir(root, task_entry.id).mkdir(parents=True, exist_ok=True)
            write_status(root, status)
        if status.state in ("done", "blocked"):
            continue
        return status
    return None


def _load_batch_budgets(root: Path) -> _BudgetRef | None:
    """Return a simplified view over the batch.yaml budgets section."""
    try:
        batch = read_batch(root)
    except FileNotFoundError:
        return None
    return _BudgetRef(
        max_tokens=batch.budgets.max_tokens,
        max_dollars=batch.budgets.max_dollars,
        wall_clock_hours=batch.budgets.wall_clock_hours,
        warn_threshold_pct=batch.notifications.warn_threshold_pct,
        session_timeout_minutes=batch.budgets.session_timeout_minutes,
    )


def _peek_batch_id(root: Path) -> str | None:
    """Return ``state.yaml.batch_id`` if the file exists; else ``None``."""
    try:
        return read_state(root).batch_id
    except FileNotFoundError:
        return None


def _session_timeout_seconds(ctx: SupervisorContext) -> int:
    """Resolve the session timeout from batch.yaml, with a 45-minute fallback."""
    budgets = _load_batch_budgets(ctx.root)
    if budgets is None:
        return 45 * 60
    return budgets.session_timeout_minutes * 60


def _transition_to_halted(
    ctx: SupervisorContext,
    state: StateFile,
    *,
    reason: str,
) -> None:
    """Atomically move the batch into ``halted`` with a reason."""
    log = structlog.get_logger(__name__).bind(component="supervisor")
    state.phase = "halted"
    state.halted_reason = reason
    state.last_checkpoint = datetime.now(UTC)
    write_state(ctx.root, state)
    log.warning("supervisor.halted", reason=reason)
    if state.batch_id is not None:
        append_audit(
            ctx.root,
            state.batch_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": "batch-halted",
                "reason": reason,
            },
        )


def _batch_is_complete(root: Path) -> bool:
    """True when batch.yaml exists and every task's state is terminal.

    Terminal states per ADR-0001 D6 / ADR-0002 D3 are ``done`` and
    ``blocked`` — the only two that the pipeline cannot drive forward
    without operator input. ``awaiting-human`` is deliberately excluded:
    the daemon is still waiting on a reply. A task without a status.yaml
    on disk is treated as pending (not terminal), which keeps the
    "submit then never run" case idling instead of prematurely completing.
    """
    try:
        batch = read_batch(root)
    except FileNotFoundError:
        return False
    if not batch.tasks:
        # Empty-task batches are a misconfiguration, not something to
        # auto-complete. Leave them idle so the operator notices.
        return False
    for task_entry in batch.tasks:
        try:
            status = read_status(root, task_entry.id)
        except FileNotFoundError:
            return False
        if status.state not in ("done", "blocked"):
            return False
    return True


async def _transition_to_complete(
    ctx: SupervisorContext,
    state: StateFile,
) -> None:
    """Move the batch into ``complete`` and emit the batch-complete signals.

    Called when every task reached ``done`` or ``blocked`` and no
    session is in flight. Clears ``current_task`` / ``current_agent``
    so operators reading status don't see a dangling "last task"
    pointer. Also appends a ``batch-complete`` audit event and
    dispatches a ``batch-complete`` notification (no reply verbs — this
    is an FYI, not a gate).
    """
    log = structlog.get_logger(__name__).bind(component="supervisor")
    now = datetime.now(UTC)
    state.phase = "complete"
    state.current_task = None
    state.current_agent = None
    state.session_id = None
    state.halted_reason = None
    state.last_checkpoint = now
    write_state(ctx.root, state)
    log.info("supervisor.batch.complete", batch_id=state.batch_id)
    if state.batch_id is not None:
        append_audit(
            ctx.root,
            state.batch_id,
            {
                "ts": now.isoformat(),
                "kind": "batch-complete",
                "batch_id": state.batch_id,
                "session_count": state.session_count,
            },
        )
    notification = Notification.make(
        kind="batch-complete",
        summary=f"batch {state.batch_id} complete",
        reply_verbs=[],
        metadata={"batch_id": state.batch_id} if state.batch_id is not None else None,
    )
    await ctx.dispatcher.send(notification)


async def _dispatch_rate_limit_notification(
    ctx: SupervisorContext,
    defer_seconds: int,
) -> None:
    """Emit a rate-limit notification via the dispatcher.

    ADR-0001 D5 says rate-limit pauses longer than 30 minutes become a
    Telegram notification. B1 emits unconditionally so the logging
    dispatcher records every deferral; B2 will add the threshold.
    """
    notification = Notification.make(
        kind="rate-limit",
        summary=f"rate-limit pause: sleeping {defer_seconds}s before next session",
        reply_verbs=["continue", "stop"],
    )
    await ctx.dispatcher.send(notification)


# ---------------------------------------------------------------------------
# Claude Code version check.
# ---------------------------------------------------------------------------


def _check_claude_version(settings: HuragokSettings) -> tuple[bool, str]:
    """Return ``(ok, message)`` for ``claude --version``.

    ``message`` is populated on failure with an operator-facing
    diagnostic. The minimum version lives in
    :data:`~orchestrator.constants.MIN_CLAUDE_CODE_VERSION`.
    """
    binary = os.environ.get("HURAGOK_CLAUDE_BINARY") or "claude"
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, f"{binary!r} not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"{binary!r} did not respond to --version within 10s"

    raw = completed.stdout.strip() or completed.stderr.strip()
    m = _VERSION_RE.search(raw)
    if not m:
        return False, f"could not parse version from {raw!r}"
    observed = tuple(int(part) for part in m.groups())
    required = tuple(int(part) for part in MIN_CLAUDE_CODE_VERSION.split("."))
    if observed < required:
        return False, (
            f"claude version {'.'.join(str(p) for p in observed)} is below minimum "
            f"{MIN_CLAUDE_CODE_VERSION}"
        )
    _ = settings  # Settings reserved for future use (e.g. version-override flag).
    return True, f"claude version {'.'.join(str(p) for p in observed)} accepted"


# Simple record for the internal _load_batch_budgets helper.
@dataclass(frozen=True, slots=True)
class _BudgetRef:
    max_tokens: int
    max_dollars: float
    wall_clock_hours: float
    warn_threshold_pct: int
    session_timeout_minutes: int


# Type alias for backwards readability.
Phase = Literal["idle", "running", "paused", "halted", "complete"]


# ---------------------------------------------------------------------------
# Reachability reconciliation (ADR-0002 D6 closing paragraph).
# ---------------------------------------------------------------------------


_NOTIFICATION_UNREACHABLE_REASON: str = "notification-backend-unreachable"


def _reconcile_reachability(ctx: SupervisorContext, state: StateFile) -> StateFile:
    """Flip ``state.phase`` between paused and running based on dispatcher health.

    The dispatcher's ``reachable`` property implements the "outstanding
    notification + 10m of failures on both directions" rule; this
    helper translates that into a durable phase transition and the
    matching audit events. Returns the (possibly updated) state object.
    """
    log = structlog.get_logger(__name__).bind(component="supervisor")
    reachable_prop = getattr(ctx.dispatcher, "reachable", True)
    # ``reachable`` may be a property (Telegram) or a plain attribute.
    reachable = bool(reachable_prop() if callable(reachable_prop) else reachable_prop)

    if not reachable and state.phase in ("idle", "running"):
        state.phase = "paused"
        state.halted_reason = _NOTIFICATION_UNREACHABLE_REASON
        state.last_checkpoint = datetime.now(UTC)
        write_state(ctx.root, state)
        log.critical(
            "supervisor.dispatcher.unreachable",
            reason=_NOTIFICATION_UNREACHABLE_REASON,
        )
        if state.batch_id is not None:
            append_audit(
                ctx.root,
                state.batch_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "kind": "dispatcher-unreachable",
                    "reason": _NOTIFICATION_UNREACHABLE_REASON,
                },
            )
        return state

    if reachable and (
        state.phase == "paused" and state.halted_reason == _NOTIFICATION_UNREACHABLE_REASON
    ):
        state.phase = "running"
        state.halted_reason = None
        state.last_checkpoint = datetime.now(UTC)
        write_state(ctx.root, state)
        log.warning("supervisor.dispatcher.recovered")
        if state.batch_id is not None:
            append_audit(
                ctx.root,
                state.batch_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "kind": "dispatcher-recovered",
                },
            )
    return state


# ---------------------------------------------------------------------------
# Reply-request application (inbound from dispatcher / CLI).
# ---------------------------------------------------------------------------


def _apply_drained_requests(
    ctx: SupervisorContext,
    drained: list[ParsedRequest],
) -> None:
    """Apply ``reply-<id>.yaml`` payloads to ``state.yaml.awaiting_reply``.

    Stop / halt markers were already applied by
    :func:`process_request_files`; here we just handle replies. A reply
    whose ``notification_id`` matches the current awaiting-reply entry
    clears it; non-matching replies are kept as a diagnostic audit
    entry but don't override state.
    """
    replies = [r for r in drained if r.kind == "reply"]
    if not replies:
        return
    log = structlog.get_logger(__name__).bind(component="supervisor")
    try:
        state = read_state(ctx.root)
    except FileNotFoundError:
        return

    changed = False
    for reply in replies:
        notification_id = reply.payload.get("notification_id")
        verb = reply.payload.get("verb")
        annotation = reply.payload.get("annotation")
        source = reply.payload.get("source", "unknown")

        log.info(
            "supervisor.reply.received",
            notification_id=notification_id,
            verb=verb,
            source=source,
        )
        if state.batch_id is not None:
            append_audit(
                ctx.root,
                state.batch_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "kind": "notification-reply-applied",
                    "notification_id": notification_id,
                    "verb": verb,
                    "annotation": annotation,
                    "source": source,
                },
            )
        awaiting = state.awaiting_reply.notification_id
        if awaiting is not None and awaiting == notification_id:
            state.awaiting_reply = AwaitingReply()
            changed = True
            if verb == "stop":
                ctx.signal_state.shutting_down.set()
            elif verb == "iterate":
                # ADR-0002 D7: iterate resets attempt counters. We
                # achieve this by truncating history entries whose
                # category is in the counted set on the matching task.
                task_id = reply.payload.get("task_id") or ctx.attempts.keys()
                _reset_task_retry_counters(ctx, state, task_id)
            elif verb == "escalate":
                # Task already in awaiting-human if the escalation was
                # automatic; if it's operator-initiated, mark it so the
                # operator can drive a live session next.
                pass
            elif verb == "continue":
                # Advance past the failure. Restore task state from
                # awaiting-human back to the role's "next" state.
                _resume_task_after_continue(ctx, state, reply.payload)
    if changed:
        write_state(ctx.root, state)


def _reset_task_retry_counters(
    ctx: SupervisorContext,
    state: StateFile,
    task_id: object,
) -> None:
    """Clear the ``category``-annotated history rows for a task on ``iterate``."""
    if isinstance(task_id, str):
        targets = [task_id]
    elif task_id is None:
        return
    else:
        targets = list(task_id) if isinstance(task_id, list | tuple | set) else []
        if not targets and state.current_task is not None:
            targets = [state.current_task]
    for t in targets:
        try:
            status = read_status(ctx.root, t)
        except FileNotFoundError:
            continue
        status.history = [h for h in status.history if h.category is None]
        status.blockers = []
        if status.state in ("awaiting-human", "blocked"):
            # Return to the role-appropriate state. The simplest rule
            # is to hand back to the Implementer — that matches D7's
            # "reset task state and try fresh" semantics.
            status.state = "implementing"
        write_status(ctx.root, status)


def _resume_task_after_continue(
    ctx: SupervisorContext,
    state: StateFile,
    payload: dict[str, object],
) -> None:
    """Advance a task past the failure when the operator replies ``continue``."""
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) and state.current_task is not None:
        task_id = state.current_task
    if not isinstance(task_id, str):
        return
    try:
        status = read_status(ctx.root, task_id)
    except FileNotFoundError:
        return
    # Operator override: drop the escalation-era blockers and move the
    # task forward to the state implied by the last observed role. If
    # we can't tell, default to implementing so the pipeline continues.
    if status.state in ("awaiting-human", "blocked"):
        status.state = "implementing"
        status.blockers = []
        status.history.append(
            HistoryEntry(
                at=datetime.now(UTC),
                from_=status.state,
                to="implementing",
                by="operator",
                session_id=None,
                category="operator-override",
            )
        )
    write_status(ctx.root, status)


# ---------------------------------------------------------------------------
# Dispatcher factory.
# ---------------------------------------------------------------------------


def build_dispatcher(
    *,
    settings: HuragokSettings,
    root: Path,
    batch_id: str | None,
) -> NotificationDispatcher:
    """Build the notification dispatcher for the current daemon run.

    Returns a :class:`TelegramDispatcher` when ``TELEGRAM_BOT_TOKEN``
    is configured; otherwise a :class:`LoggingDispatcher` that still
    appends to the per-batch audit log. The choice is made once per
    daemon invocation — changing ``.env`` mid-run requires a restart.
    """
    log = structlog.get_logger(__name__).bind(component="supervisor")
    if settings.telegram_bot_token is None or not settings.telegram_default_chat_id:
        if settings.telegram_bot_token is not None and not settings.telegram_default_chat_id:
            log.warning(
                "supervisor.dispatcher.telegram_missing_chat_id",
                hint="set HURAGOK_TELEGRAM_DEFAULT_CHAT_ID to enable Telegram",
            )
        return LoggingDispatcher(root=root, batch_id=batch_id)
    log.info("supervisor.dispatcher.telegram_enabled")
    return TelegramDispatcher(
        bot_token=settings.telegram_bot_token,
        default_chat_id=settings.telegram_default_chat_id,
        root=root,
        batch_id=batch_id,
    )
