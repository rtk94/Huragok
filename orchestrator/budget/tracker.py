"""Live token / dollar / wall-clock accounting (ADR-0002 D4).

:class:`BudgetTracker` is the long-lived coroutine that consumes
:class:`~orchestrator.session.events.BudgetEvent` items from the session
runners, updates an in-memory :class:`BudgetSnapshot`, flushes into
``state.yaml`` at session end, and raises 80%/100% threshold signals on
the way through.

The optional :class:`CostReconciler` queries Anthropic's Cost API at
session end if an Admin API key is available and supersedes the local
table estimate with the authoritative figure.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from orchestrator.budget.pricing import (
    PricingMissingModelError,
    dollars_for_usage,
)
from orchestrator.notifications import LoggingDispatcher, Notification, NotificationDispatcher
from orchestrator.paths import state_file
from orchestrator.session.events import BudgetEvent, SessionContext
from orchestrator.session.runner import SessionResult
from orchestrator.session.stream import AssistantEvent, ResultEvent, UsageBlock
from orchestrator.state import (
    BudgetConsumed,
    PricingTable,
    append_audit,
    read_state,
    write_state,
)

__all__ = [
    "BudgetSnapshot",
    "BudgetTracker",
    "CostReconciler",
]


# Anthropic Cost API — a JSON endpoint that returns cumulative cost
# reports for an organization. Configured as a module constant so tests
# can monkeypatch it trivially. Endpoint and schema per the Anthropic
# admin API documentation; fetched at runtime by the reconciler.
COST_API_URL = "https://api.anthropic.com/v1/organizations/cost_report"


@dataclass(slots=True)
class BudgetSnapshot:
    """In-memory projection of what ``state.yaml.budget_consumed`` holds."""

    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    dollars: float = 0.0
    iterations: int = 0
    wall_clock_seconds: float = 0.0
    # Threshold crossings already notified, to prevent repeat-spam on
    # subsequent events after the line is crossed.
    _notified_80: bool = field(default=False, repr=False)
    _notified_100: bool = field(default=False, repr=False)

    def total_tokens(self) -> int:
        """Return input + output tokens (the standard aggregate metric)."""
        return self.tokens_input + self.tokens_output

    def to_budget_consumed(self) -> BudgetConsumed:
        """Render the snapshot as a :class:`BudgetConsumed` for state.yaml."""
        return BudgetConsumed(
            wall_clock_seconds=self.wall_clock_seconds,
            tokens_input=self.tokens_input,
            tokens_output=self.tokens_output,
            tokens_cache_read=self.tokens_cache_read,
            tokens_cache_write=self.tokens_cache_write,
            dollars=round(self.dollars, 4),
            iterations=self.iterations,
        )

    def apply_usage(self, usage: UsageBlock) -> None:
        """Accumulate a usage delta into the snapshot."""
        self.tokens_input += usage.input_tokens
        self.tokens_output += usage.output_tokens
        self.tokens_cache_read += usage.cache_read_input_tokens
        self.tokens_cache_write += usage.cache_creation_input_tokens


class BudgetTracker:
    """Long-lived coroutine that owns live budget state for the daemon.

    A single instance per daemon. Constructed by the supervisor at
    startup with references to the dispatcher (for threshold alerts),
    the pricing table (for live estimates), and the state root (for
    periodic flushes). Start by calling :meth:`run` on the main event
    loop.
    """

    def __init__(
        self,
        *,
        root: Any,  # Path — kept ``Any`` to avoid import cycle at type-check time
        pricing: PricingTable,
        dispatcher: NotificationDispatcher | None = None,
        max_tokens: int = 0,
        max_dollars: float = 0.0,
        max_wall_clock_seconds: float = 0.0,
        warn_threshold_pct: int = 80,
        reconciler: CostReconciler | None = None,
        batch_id: str | None = None,
    ) -> None:
        self._root = root
        self._pricing = pricing
        self._dispatcher = dispatcher or LoggingDispatcher()
        self._max_tokens = max_tokens
        self._max_dollars = max_dollars
        self._max_wall_clock_seconds = max_wall_clock_seconds
        self._warn_threshold = warn_threshold_pct / 100.0
        self._reconciler = reconciler
        self._batch_id = batch_id
        self._snapshot = BudgetSnapshot()
        self._live_session: SessionContext | None = None
        self._session_start_wall: float = 0.0
        self._batch_start: datetime | None = None
        self._log = structlog.get_logger(__name__).bind(component="budget-tracker")
        self._over_budget = False

    # ------------------------------------------------------------------
    # Public API.
    # ------------------------------------------------------------------

    @property
    def snapshot_ref(self) -> BudgetSnapshot:
        """Return the live snapshot object (by reference — do not mutate)."""
        return self._snapshot

    def snapshot(self) -> BudgetSnapshot:
        """Return a copy of the current budget state."""
        return BudgetSnapshot(
            tokens_input=self._snapshot.tokens_input,
            tokens_output=self._snapshot.tokens_output,
            tokens_cache_read=self._snapshot.tokens_cache_read,
            tokens_cache_write=self._snapshot.tokens_cache_write,
            dollars=self._snapshot.dollars,
            iterations=self._snapshot.iterations,
            wall_clock_seconds=self._snapshot.wall_clock_seconds,
        )

    def over_budget(self) -> bool:
        """True when any budget dimension has hit its 100% threshold."""
        return self._over_budget

    def seed_from_state(self, consumed: BudgetConsumed) -> None:
        """Replace the in-memory snapshot with the state.yaml contents."""
        self._snapshot.tokens_input = consumed.tokens_input
        self._snapshot.tokens_output = consumed.tokens_output
        self._snapshot.tokens_cache_read = consumed.tokens_cache_read
        self._snapshot.tokens_cache_write = consumed.tokens_cache_write
        self._snapshot.dollars = consumed.dollars
        self._snapshot.iterations = consumed.iterations
        self._snapshot.wall_clock_seconds = consumed.wall_clock_seconds

    def mark_batch_start(self, at: datetime | None = None) -> None:
        """Record the batch-start timestamp for wall-clock accounting."""
        self._batch_start = at if at is not None else datetime.now(UTC)

    async def run(
        self,
        event_queue: asyncio.Queue[BudgetEvent],
        stop_event: asyncio.Event,
    ) -> None:
        """Consume events from the queue until ``stop_event`` is set.

        When ``stop_event`` fires the coroutine drains any remaining
        events in the queue (non-blocking) and returns. The supervisor
        pauses on the same event, so by the time we return both
        coroutines have a consistent view of the final snapshot.
        """
        while True:
            get_task = asyncio.create_task(event_queue.get())
            stop_task = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait({get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

            if get_task in done:
                try:
                    event = get_task.result()
                except Exception:
                    stop_task.cancel()
                    raise
                await self._handle_event(event)
                if stop_task in done:
                    pass  # stop also fired; drain below
                else:
                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task
                    continue

            # Stop fired (possibly together with a get). Drain whatever is
            # pending without blocking so session-ended events are not
            # left on the queue at shutdown.
            if not get_task.done():
                get_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await get_task
            while not event_queue.empty():
                event = event_queue.get_nowait()
                await self._handle_event(event)
            return

    async def reconcile(
        self,
        session_id: str,
        session_start: datetime,
        session_end: datetime,
    ) -> None:
        """Query the Cost API (if configured) and supersede the live estimate.

        A missing reconciler, missing Admin API key, empty response, or
        network error is logged at WARN and leaves the local estimate in
        place — reconciliation is strictly additive.
        """
        if self._reconciler is None:
            return
        try:
            reconciled = await self._reconciler.fetch(
                session_start=session_start,
                session_end=session_end,
            )
        except CostReconciliationError as exc:
            self._log.warning(
                "budget.reconcile.failed",
                session_id=session_id,
                error=str(exc),
            )
            return
        if reconciled is None:
            self._log.info("budget.reconcile.empty", session_id=session_id)
            return

        before = self._snapshot.dollars
        self._snapshot.dollars = reconciled
        self._log.info(
            "budget.reconcile.applied",
            session_id=session_id,
            before=round(before, 4),
            after=round(reconciled, 4),
        )
        if self._batch_id is not None:
            append_audit(
                self._root,
                self._batch_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "kind": "cost-reconciliation",
                    "session_id": session_id,
                    "dollars_before": round(before, 4),
                    "dollars_after": round(reconciled, 4),
                },
            )
        self._flush_state()
        await self._check_thresholds()

    # ------------------------------------------------------------------
    # Event handling.
    # ------------------------------------------------------------------

    async def _handle_event(self, event: BudgetEvent) -> None:
        if event.kind == "session-started":
            self._on_session_started(event.ctx, event.at)
            return
        if event.kind == "stream-event":
            if event.stream_event is None:
                return
            await self._on_stream_event(event.ctx, event.stream_event)
            return
        if event.kind == "session-ended":
            await self._on_session_ended(event.ctx, event.at, event.session_result)
            return

    def _on_session_started(self, ctx: SessionContext, at: datetime) -> None:
        self._live_session = ctx
        if self._batch_start is None:
            self._batch_start = at
        self._session_start_wall = self._snapshot.wall_clock_seconds

    async def _on_stream_event(self, ctx: SessionContext, stream_event: object) -> None:
        # Only assistant/result events carry usage. Everything else is a no-op.
        if isinstance(stream_event, AssistantEvent):
            self._apply_event_usage(ctx, stream_event.usage, stream_event.model)
        elif isinstance(stream_event, ResultEvent):
            # Result event usage is applied additively, same as assistant
            # events. Today this is harmless because Claude Code's result
            # events carry empty usage; the totals live on the preceding
            # assistant events. If that convention changes, the test
            # test_tracker_applies_both_assistant_and_result_event_usage
            # will trip. See slice-b2-build-notes.md, amendment 2,  item 7.
            self._apply_event_usage(ctx, stream_event.usage, stream_event.model)
        await self._check_thresholds()

    async def _on_session_ended(
        self,
        ctx: SessionContext,
        at: datetime,
        result: SessionResult | None,
    ) -> None:
        if self._batch_start is not None:
            # Wall-clock grows by the actual session duration, not from the
            # last stream event (which is ``result`` and already observed).
            if result is not None:
                self._snapshot.wall_clock_seconds = (
                    self._session_start_wall + result.duration_seconds
                )
            else:
                self._snapshot.wall_clock_seconds = (at - self._batch_start).total_seconds()
        self._flush_state()
        await self._check_thresholds()
        if self._reconciler is not None:
            await self.reconcile(
                session_id=ctx.session_id,
                session_start=ctx.started_at,
                session_end=at,
            )
        self._live_session = None

    def _apply_event_usage(
        self,
        ctx: SessionContext,
        usage: UsageBlock | None,
        reported_model: str | None,
    ) -> None:
        if usage is None:
            return
        self._snapshot.apply_usage(usage)
        model = reported_model or ctx.model
        try:
            delta = dollars_for_usage(usage, model, self._pricing)
        except PricingMissingModelError as exc:
            # Startup check should have prevented this; log and skip the
            # dollar delta rather than letting it poison the snapshot.
            self._log.error("budget.pricing.missing", model=model, error=str(exc))
            return
        self._snapshot.dollars += delta

    # ------------------------------------------------------------------
    # Threshold / persistence plumbing.
    # ------------------------------------------------------------------

    def _flush_state(self) -> None:
        """Write the current snapshot back into ``state.yaml``."""
        path = state_file(self._root)
        try:
            state = read_state(self._root)
        except FileNotFoundError:
            return
        state.budget_consumed = self._snapshot.to_budget_consumed()
        state.last_checkpoint = datetime.now(UTC)
        write_state(self._root, state)
        self._log.debug(
            "budget.state.flushed",
            tokens=self._snapshot.total_tokens(),
            dollars=round(self._snapshot.dollars, 4),
            path=str(path),
        )

    async def _check_thresholds(self) -> None:
        await self._maybe_emit_threshold(
            usage=self._snapshot.total_tokens(),
            cap=self._max_tokens,
            dimension="tokens",
        )
        await self._maybe_emit_threshold(
            usage=self._snapshot.dollars,
            cap=self._max_dollars,
            dimension="dollars",
        )
        await self._maybe_emit_threshold(
            usage=self._snapshot.wall_clock_seconds,
            cap=self._max_wall_clock_seconds,
            dimension="wall_clock_seconds",
        )

    async def _maybe_emit_threshold(
        self,
        *,
        usage: float,
        cap: float,
        dimension: str,
    ) -> None:
        if cap <= 0:
            return
        pct = usage / cap
        if pct >= 1.0 and not self._snapshot._notified_100:
            self._snapshot._notified_100 = True
            self._over_budget = True
            await self._emit_threshold_notification(
                dimension=dimension,
                pct=100,
                usage=usage,
                cap=cap,
            )
            return
        if pct >= self._warn_threshold and not self._snapshot._notified_80:
            self._snapshot._notified_80 = True
            await self._emit_threshold_notification(
                dimension=dimension,
                pct=int(self._warn_threshold * 100),
                usage=usage,
                cap=cap,
            )

    async def _emit_threshold_notification(
        self,
        *,
        dimension: str,
        pct: int,
        usage: float,
        cap: float,
    ) -> None:
        summary = f"budget threshold crossed: {dimension} at {pct}% ({usage:.2f}/{cap:.2f})"
        notification = Notification.make(
            kind="budget-threshold",
            summary=summary,
            reply_verbs=["continue", "stop"],
        )
        await self._dispatcher.send(notification)
        self._log.warning(
            "budget.threshold.crossed",
            dimension=dimension,
            pct=pct,
            usage=usage,
            cap=cap,
        )
        if self._batch_id is not None:
            append_audit(
                self._root,
                self._batch_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "kind": "budget-threshold",
                    "dimension": dimension,
                    "pct": pct,
                    "usage": usage,
                    "cap": cap,
                },
            )


# ---------------------------------------------------------------------------
# Cost API reconciliation.
# ---------------------------------------------------------------------------


class CostReconciliationError(Exception):
    """Raised when the Cost API call fails for any reason."""


class CostReconciler:
    """Queries Anthropic's Cost API for a dollar figure superseding the table.

    Only constructed when ``ANTHROPIC_ADMIN_API_KEY`` is set. A separate
    class from :class:`BudgetTracker` so the tracker stays testable
    without HTTP and so the reconciler is trivially swappable for a mock
    in tests.
    """

    def __init__(
        self,
        *,
        admin_api_key: str,
        endpoint: str = COST_API_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._admin_api_key = admin_api_key
        self._endpoint = endpoint
        self._client = http_client
        self._owns_client = http_client is None
        self._log = structlog.get_logger(__name__).bind(component="cost-reconciler")

    async def fetch(
        self,
        *,
        session_start: datetime,
        session_end: datetime,
    ) -> float | None:
        """Return the reconciled dollar figure for ``[start, end]`` or None.

        Returns ``None`` if the Cost API responds with an empty result
        (common when the five-minute lag hasn't caught up with the
        session yet). Raises :class:`CostReconciliationError` on any
        transport or parse failure.
        """
        params = {
            "starting_at": session_start.astimezone(UTC).isoformat(),
            "ending_at": session_end.astimezone(UTC).isoformat(),
        }
        headers = {
            "x-api-key": self._admin_api_key,
            "anthropic-version": "2023-06-01",
        }

        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            try:
                response = await client.get(
                    self._endpoint,
                    params=params,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise CostReconciliationError(f"cost API transport error: {exc}") from exc

            if response.status_code >= 400:
                raise CostReconciliationError(
                    f"cost API returned {response.status_code}: {response.text[:200]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise CostReconciliationError(f"cost API returned non-JSON: {exc}") from exc
        finally:
            if self._owns_client:
                await client.aclose()

        return _extract_total_usd(payload)


def _extract_total_usd(payload: Any) -> float | None:
    """Best-effort extraction of total dollars from a Cost API response.

    The Anthropic Cost API response is a paginated list of per-bucket
    cost entries. We sum ``amount.value`` across buckets, scoped to USD.
    An empty ``data`` list returns ``None`` to signal "no authoritative
    figure yet" — the five-minute lag is normal. Unknown or malformed
    shapes raise :class:`CostReconciliationError`.
    """
    if not isinstance(payload, dict):
        raise CostReconciliationError("cost API response is not a JSON object")
    data = payload.get("data")
    if data is None:
        return None
    if not isinstance(data, list):
        raise CostReconciliationError("cost API 'data' is not a list")
    if not data:
        return None

    total = 0.0
    saw_any = False
    for entry in data:
        if not isinstance(entry, dict):
            continue
        results = entry.get("results")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            amount = result.get("amount")
            if isinstance(amount, dict):
                currency = amount.get("currency", "USD")
                if currency != "USD":
                    continue
                value = amount.get("value")
                if isinstance(value, int | float):
                    total += float(value)
                    saw_any = True

    return round(total, 4) if saw_any else None
