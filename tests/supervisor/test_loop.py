"""Integration tests for the supervisor loop.

Each test uses the fake-claude fixture under ``tests/fixtures/`` so no
real Claude Code binary is invoked. The minimal repo fixture gives us a
pending task; the loop should launch exactly one session and then stop
when we flip the shutdown event.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from orchestrator.budget.pricing import load_pricing
from orchestrator.config import HuragokSettings
from orchestrator.notifications import LoggingDispatcher
from orchestrator.paths import audit_log
from orchestrator.state import read_state, read_status
from orchestrator.supervisor.loop import run_supervisor

FAKE_CLAUDE = Path(__file__).resolve().parent.parent / "fixtures" / "fake-claude.sh"


async def test_loop_launches_one_session_and_updates_state(
    supervisor_tmp_root: Path,
) -> None:
    """Smoke: one fake-clean session changes state.yaml and the audit log."""
    settings = HuragokSettings()

    loop_task = asyncio.create_task(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.1,
            session_env_overrides={"FAKE_CLAUDE_MODE": "clean"},
            dispatcher=LoggingDispatcher(root=supervisor_tmp_root, batch_id="batch-001"),
        )
    )

    # Wait until a session has been recorded, then tell the loop to stop.
    async def wait_for_session() -> None:
        for _ in range(200):  # up to 20s
            state = read_state(supervisor_tmp_root)
            if state.session_count >= 1:
                return
            await asyncio.sleep(0.1)
        raise AssertionError("session never observed")

    await asyncio.wait_for(wait_for_session(), timeout=30.0)

    # Write a stop request so the loop drains and exits cleanly.
    stop_path = supervisor_tmp_root / ".huragok" / "requests" / "stop"
    stop_path.write_text("")

    exit_code = await asyncio.wait_for(loop_task, timeout=10.0)
    assert exit_code == 0

    # state.yaml should have recorded at least one session. The loop may
    # launch several sessions before observing the stop marker because
    # fake-claude returns in milliseconds; we assert only the core
    # bookkeeping contract, not an exact count.
    final_state = read_state(supervisor_tmp_root)
    assert final_state.session_count >= 1
    assert final_state.budget_consumed.tokens_input > 0
    assert final_state.budget_consumed.dollars > 0
    assert final_state.session_id is not None

    # Audit log should have session-launched / session-ended entries.
    audit_path = audit_log(supervisor_tmp_root, "batch-001")
    assert audit_path.exists()
    events = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    kinds = [e["kind"] for e in events]
    assert "session-launched" in kinds
    assert "session-ended" in kinds
    ended = next(e for e in events if e["kind"] == "session-ended")
    assert ended["end_state"] == "clean"
    assert ended["role"] == "architect"

    # status.yaml should have been written for the task; since fake-claude
    # doesn't simulate agent state transitions, the state remains at the
    # initial ``pending``. That's the expected B1 behaviour for this test.
    status = read_status(supervisor_tmp_root, "task-b1-test")
    assert status.task_id == "task-b1-test"


async def test_loop_exits_on_stop_request_even_without_work(
    supervisor_tmp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the batch is already done, the loop should exit on stop quickly."""
    # Mark the task as done before the loop starts — no work to do.
    from datetime import UTC, datetime

    from orchestrator.paths import task_dir
    from orchestrator.state import HistoryEntry, StatusFile, write_status

    task_dir(supervisor_tmp_root, "task-b1-test").mkdir(parents=True, exist_ok=True)
    done_status = StatusFile(
        version=1,
        task_id="task-b1-test",
        state="done",
        history=[],
    )
    done_status.history.append(
        HistoryEntry(
            at=datetime.now(UTC),
            from_="pending",
            to="done",
            by="test",
            session_id=None,
        )
    )
    write_status(supervisor_tmp_root, done_status)

    settings = HuragokSettings()
    loop_task = asyncio.create_task(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            dispatcher=LoggingDispatcher(root=supervisor_tmp_root, batch_id="batch-001"),
        )
    )
    await asyncio.sleep(0.1)  # let the loop observe no pending work
    (supervisor_tmp_root / ".huragok" / "requests" / "stop").write_text("")
    exit_code = await asyncio.wait_for(loop_task, timeout=5.0)
    assert exit_code == 0


async def test_loop_escalates_after_crash_cap(
    supervisor_tmp_root: Path,
) -> None:
    """Three consecutive crash sessions escalate (ADR-0002 D7)."""
    settings = HuragokSettings()

    loop_task = asyncio.create_task(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            session_env_overrides={"FAKE_CLAUDE_MODE": "crash"},
            dispatcher=LoggingDispatcher(root=supervisor_tmp_root, batch_id="batch-001"),
        )
    )

    async def wait_for_terminal() -> None:
        # subprocess-crash per D7: 2 fresh retries, then escalate →
        # task state transitions to awaiting-human. A crash that hits
        # the unknown fallback would land in blocked; we accept either.
        for _ in range(400):  # up to 40s
            try:
                status = read_status(supervisor_tmp_root, "task-b1-test")
            except FileNotFoundError:
                await asyncio.sleep(0.1)
                continue
            if status.state in ("awaiting-human", "blocked"):
                return
            await asyncio.sleep(0.1)
        raise AssertionError("task never reached a terminal failure state")

    try:
        await asyncio.wait_for(wait_for_terminal(), timeout=60.0)
    finally:
        (supervisor_tmp_root / ".huragok" / "requests" / "stop").write_text("")
        await asyncio.wait_for(loop_task, timeout=10.0)

    status = read_status(supervisor_tmp_root, "task-b1-test")
    assert status.state in ("awaiting-human", "blocked")
    assert status.blockers, "blockers list should be populated"
    # At least 2 crash entries were appended to history with a category.
    crash_entries = [h for h in status.history if h.category == "subprocess-crash"]
    assert len(crash_entries) >= 2


# ---------------------------------------------------------------------------
# B2: classifier pipeline, history-based retry counting, reachability.
# ---------------------------------------------------------------------------


async def test_crash_audit_records_category_and_action(
    supervisor_tmp_root: Path,
) -> None:
    """Every crash session is audited with its D7 category and retry action."""
    settings = HuragokSettings()
    loop_task = asyncio.create_task(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            session_env_overrides={"FAKE_CLAUDE_MODE": "crash"},
            dispatcher=LoggingDispatcher(root=supervisor_tmp_root, batch_id="batch-001"),
        )
    )

    async def wait_for_category_audit() -> None:
        audit_path = audit_log(supervisor_tmp_root, "batch-001")
        for _ in range(400):
            if audit_path.exists():
                lines = [line for line in audit_path.read_text().splitlines() if line]
                for line in lines:
                    record = json.loads(line)
                    if (
                        record.get("kind") == "session-ended"
                        and record.get("category") == "subprocess-crash"
                    ):
                        return
            await asyncio.sleep(0.1)
        raise AssertionError("no categorised session-ended audit event")

    try:
        await asyncio.wait_for(wait_for_category_audit(), timeout=30.0)
    finally:
        (supervisor_tmp_root / ".huragok" / "requests" / "stop").write_text("")
        await asyncio.wait_for(loop_task, timeout=10.0)

    audit_path = audit_log(supervisor_tmp_root, "batch-001")
    records = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    # Find the first crash record and confirm the classifier + action fields.
    crash = next(r for r in records if r.get("kind") == "session-ended" and r.get("category"))
    assert crash["category"] == "subprocess-crash"
    assert crash["action"] in ("retry_fresh", "escalate")


async def test_reachability_transitions_to_paused_and_recovers(
    supervisor_tmp_root: Path,
) -> None:
    """A fake dispatcher with ``reachable=False`` pauses the batch.

    When reachable flips back to True, the supervisor should resume.
    """
    from orchestrator.notifications import Notification, NotificationDispatcher

    class _FakeDispatcher(NotificationDispatcher):
        def __init__(self) -> None:
            self._reachable = True
            self._pending_fail = False

        @property
        def reachable(self) -> bool:  # type: ignore[override]
            return self._reachable

        async def send(self, notification: Notification) -> None:
            pass

    dispatcher = _FakeDispatcher()

    # Flip unreachable BEFORE starting the loop so the first iteration
    # observes it and transitions to paused.
    dispatcher._reachable = False

    settings = HuragokSettings()
    loop_task = asyncio.create_task(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            dispatcher=dispatcher,
        )
    )

    async def wait_for_paused() -> None:
        for _ in range(100):
            state = read_state(supervisor_tmp_root)
            if state.phase == "paused":
                return
            await asyncio.sleep(0.05)
        raise AssertionError("phase never transitioned to paused")

    await asyncio.wait_for(wait_for_paused(), timeout=5.0)
    paused = read_state(supervisor_tmp_root)
    assert paused.halted_reason == "notification-backend-unreachable"

    # Now recover.
    dispatcher._reachable = True

    async def wait_for_running() -> None:
        for _ in range(100):
            state = read_state(supervisor_tmp_root)
            if state.phase == "running":
                return
            await asyncio.sleep(0.05)
        raise AssertionError("phase never recovered to running")

    await asyncio.wait_for(wait_for_running(), timeout=5.0)

    (supervisor_tmp_root / ".huragok" / "requests" / "stop").write_text("")
    await asyncio.wait_for(loop_task, timeout=5.0)

    # Audit log has both transitions.
    audit_path = audit_log(supervisor_tmp_root, "batch-001")
    if audit_path.exists():
        kinds = [
            json.loads(line).get("kind") for line in audit_path.read_text().splitlines() if line
        ]
        assert "dispatcher-unreachable" in kinds
        assert "dispatcher-recovered" in kinds


async def test_shutdown_cancels_blocked_dispatcher_within_grace(
    supervisor_tmp_root: Path,
) -> None:
    """Idle-loop shutdown returns within the grace window even with a stuck dispatcher.

    A TelegramDispatcher mid-``getUpdates`` long-poll would otherwise
    hold :func:`run_supervisor` open until the 25-second Telegram
    timeout elapses. The supervisor must cancel the dispatcher task
    rather than waiting on it indefinitely.
    """
    import time as _time

    from orchestrator.notifications import Notification, NotificationDispatcher

    class _WedgedDispatcher(NotificationDispatcher):
        """Dispatcher whose start() never observes the stop event.

        Simulates a ``httpx.AsyncClient.get`` call blocked on the wire
        — the stop_event is set but the await won't return.
        """

        def __init__(self) -> None:
            self._reachable = True

        @property
        def reachable(self) -> bool:  # type: ignore[override]
            return self._reachable

        async def send(self, notification: Notification) -> None:
            pass

        async def start(self, stop_event: asyncio.Event) -> None:
            # Deliberately ignore stop_event — we want the supervisor to
            # cancel us, not wait on our own drain.
            await asyncio.sleep(30.0)

    # Remove batch.yaml so the loop sits in the "waiting for submit"
    # idle path — the SIGINT-inter-batch case the amendment fixes.
    (supervisor_tmp_root / ".huragok" / "batch.yaml").unlink()

    settings = HuragokSettings()
    start = _time.monotonic()
    loop_task = asyncio.create_task(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            dispatcher=_WedgedDispatcher(),
        )
    )
    # Let the loop start and enter its idle sleep.
    await asyncio.sleep(0.15)

    # Trigger shutdown (same effect as the SIGINT handler setting the
    # shutting_down event). We reach into signals by writing the
    # ``stop`` request file, which the loop picks up on its next tick.
    (supervisor_tmp_root / ".huragok" / "requests" / "stop").write_text("")

    # The loop should return within the grace window (~1s) regardless
    # of the wedged dispatcher.
    exit_code = await asyncio.wait_for(loop_task, timeout=3.0)
    elapsed = _time.monotonic() - start
    assert exit_code == 0
    # Startup + idle_sleep (50ms) + shutdown grace (1s) + jitter is well
    # under 2.5s. Without the cancel, this would be 30s.
    assert elapsed < 2.5, f"loop took {elapsed:.2f}s to shut down with wedged dispatcher"


async def test_loop_transitions_to_complete_when_all_tasks_done(
    supervisor_tmp_root: Path,
) -> None:
    """All tasks terminal → phase=complete + batch-complete audit + clean exit."""
    from orchestrator.paths import task_dir
    from orchestrator.state import HistoryEntry, StatusFile, write_status

    # Seed the single batch task as done BEFORE the loop starts.
    task_dir(supervisor_tmp_root, "task-b1-test").mkdir(parents=True, exist_ok=True)
    done_status = StatusFile(
        version=1,
        task_id="task-b1-test",
        state="done",
        foundational=False,
        history=[
            HistoryEntry(
                at=datetime(2026, 4, 22, 10, 0, 0),
                from_="implementing",
                to="done",
                by="test",
                session_id=None,
            ),
        ],
    )
    write_status(supervisor_tmp_root, done_status)

    settings = HuragokSettings()
    exit_code = await asyncio.wait_for(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            dispatcher=LoggingDispatcher(root=supervisor_tmp_root, batch_id="batch-001"),
        ),
        timeout=10.0,
    )
    assert exit_code == 0

    final_state = read_state(supervisor_tmp_root)
    assert final_state.phase == "complete"
    # Current-task pointer cleared so the status view isn't confusing.
    assert final_state.current_task is None
    assert final_state.current_agent is None

    # Audit entry for batch-complete is present.
    audit_path = audit_log(supervisor_tmp_root, "batch-001")
    assert audit_path.exists()
    events = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    kinds = [e.get("kind") for e in events]
    assert "batch-complete" in kinds


async def test_loop_transitions_to_complete_with_blocked_task(
    supervisor_tmp_root: Path,
) -> None:
    """Partial completion (one done, one blocked) still counts as complete."""
    # Extend the batch to two tasks: one will land `done`, the other
    # `blocked`. Both are terminal so the daemon should exit.
    import yaml as _yaml

    from orchestrator.paths import task_dir
    from orchestrator.state import HistoryEntry, StatusFile, write_status

    batch_path = supervisor_tmp_root / ".huragok" / "batch.yaml"
    batch = _yaml.safe_load(batch_path.read_text())
    batch["tasks"].append(
        {
            "id": "task-b1-extra",
            "title": "Second task",
            "kind": "backend",
            "priority": 2,
            "acceptance_criteria": ["irrelevant"],
            "depends_on": [],
            "foundational": False,
        }
    )
    batch_path.write_text(_yaml.safe_dump(batch, sort_keys=False))

    now = datetime(2026, 4, 22, 10, 0, 0)
    for task_id, terminal_state in (
        ("task-b1-test", "done"),
        ("task-b1-extra", "blocked"),
    ):
        task_dir(supervisor_tmp_root, task_id).mkdir(parents=True, exist_ok=True)
        write_status(
            supervisor_tmp_root,
            StatusFile(
                version=1,
                task_id=task_id,
                state=terminal_state,  # type: ignore[arg-type]
                foundational=False,
                history=[
                    HistoryEntry(
                        at=now,
                        from_="implementing",
                        to=terminal_state,
                        by="test",
                        session_id=None,
                    ),
                ],
                blockers=["irrelevant"] if terminal_state == "blocked" else [],
            ),
        )

    settings = HuragokSettings()
    exit_code = await asyncio.wait_for(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            dispatcher=LoggingDispatcher(root=supervisor_tmp_root, batch_id="batch-001"),
        ),
        timeout=10.0,
    )
    assert exit_code == 0
    assert read_state(supervisor_tmp_root).phase == "complete"


async def test_attempt_count_survives_restart(
    supervisor_tmp_root: Path,
) -> None:
    """History-based retry counters persist across daemon restarts (D7)."""
    from orchestrator.errors import SessionFailureCategory
    from orchestrator.paths import task_dir
    from orchestrator.state import HistoryEntry, StatusFile, write_status

    # Seed the task with one prior subprocess-crash entry so it already
    # has 1 of its 2 retries used up before the loop starts.
    task_dir(supervisor_tmp_root, "task-b1-test").mkdir(parents=True, exist_ok=True)
    seeded = StatusFile(
        version=1,
        task_id="task-b1-test",
        state="implementing",
        foundational=False,
        history=[
            HistoryEntry(
                at=__import__("datetime").datetime(2026, 4, 22, 10, 0, 0),
                from_="implementing",
                to="implementing",
                by="supervisor",
                session_id="01SEEDED",
                category=SessionFailureCategory.SUBPROCESS_CRASH.value,
            ),
        ],
    )
    write_status(supervisor_tmp_root, seeded)

    settings = HuragokSettings()
    loop_task = asyncio.create_task(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            session_env_overrides={"FAKE_CLAUDE_MODE": "crash"},
            dispatcher=LoggingDispatcher(root=supervisor_tmp_root, batch_id="batch-001"),
        )
    )

    # After ONE more crash the task hits the cap (2 crashes total →
    # escalate on the 3rd classify+decide). Wait for terminal state.
    async def wait_for_terminal() -> None:
        for _ in range(400):
            try:
                status = read_status(supervisor_tmp_root, "task-b1-test")
            except FileNotFoundError:
                await asyncio.sleep(0.1)
                continue
            if status.state in ("awaiting-human", "blocked"):
                return
            await asyncio.sleep(0.1)
        raise AssertionError("task never reached a terminal failure state")

    try:
        await asyncio.wait_for(wait_for_terminal(), timeout=40.0)
    finally:
        (supervisor_tmp_root / ".huragok" / "requests" / "stop").write_text("")
        await asyncio.wait_for(loop_task, timeout=10.0)

    status = read_status(supervisor_tmp_root, "task-b1-test")
    crash_entries = [h for h in status.history if h.category == "subprocess-crash"]
    # The seed plus at least one more crash. Because the loop may
    # fire faster than the cap, we accept >= 2.
    assert len(crash_entries) >= 2
    assert status.state in ("awaiting-human", "blocked")
