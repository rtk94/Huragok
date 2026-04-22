"""Verify the daemon mirrors structured logs to ``.huragok/logs/batch-<id>.jsonl``.

Amendment to Slice B2 (2026-04-22): :func:`configure_logging` now accepts a
``file_path`` parameter and :func:`run_supervisor` wires it in as soon
as the active batch id is known. This test asserts that end-to-end
contract; the unit coverage for ``configure_logging`` itself lives in
``tests/test_logging_setup.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orchestrator.budget.pricing import load_pricing
from orchestrator.config import HuragokSettings
from orchestrator.notifications import LoggingDispatcher
from orchestrator.paths import batch_log
from orchestrator.state import read_state
from orchestrator.supervisor.loop import run_supervisor

FAKE_CLAUDE = Path(__file__).resolve().parent.parent / "fixtures" / "fake-claude.sh"


async def test_supervisor_writes_batch_log_file(
    supervisor_tmp_root: Path,
) -> None:
    """Running a short batch populates ``.huragok/logs/batch-001.jsonl`` with valid JSON."""
    settings = HuragokSettings()
    loop_task = asyncio.create_task(
        run_supervisor(
            root=supervisor_tmp_root,
            settings=settings,
            pricing=load_pricing(),
            claude_binary=str(FAKE_CLAUDE),
            request_poll_seconds=0.05,
            session_env_overrides={"FAKE_CLAUDE_MODE": "clean"},
            dispatcher=LoggingDispatcher(root=supervisor_tmp_root, batch_id="batch-001"),
        )
    )

    async def wait_for_session() -> None:
        for _ in range(200):
            state = read_state(supervisor_tmp_root)
            if state.session_count >= 1:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("session never observed")

    try:
        await asyncio.wait_for(wait_for_session(), timeout=20.0)
    finally:
        (supervisor_tmp_root / ".huragok" / "requests" / "stop").write_text("")
        await asyncio.wait_for(loop_task, timeout=10.0)

    log_path = batch_log(supervisor_tmp_root, "batch-001")
    assert log_path.exists(), f"batch log file not created at {log_path}"

    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "batch log file is empty"

    records = [json.loads(line) for line in lines]
    for record in records:
        assert "event" in record
        assert "level" in record
        assert "ts" in record

    events = {record["event"] for record in records}
    # One of these is guaranteed to fire during any normal startup +
    # session cycle; either alone proves the sink is wired.
    assert events & {"supervisor.started", "supervisor.session.launch", "supervisor.session.end"}
