"""Tests for ``orchestrator.session.runner``."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from orchestrator.session import (
    BudgetEvent,
    ResultEvent,
    run_session,
)
from orchestrator.session.runner import default_session_env
from orchestrator.state import SessionBudget

FAKE_CLAUDE = Path(__file__).resolve().parent.parent / "fixtures" / "fake-claude.sh"


async def _collect_events(queue: asyncio.Queue[BudgetEvent]) -> list[BudgetEvent]:
    events: list[BudgetEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.fixture
def tmp_repo_root(tmp_path: Path) -> Path:
    """Minimal working directory for a subprocess; no agent files present."""
    return tmp_path


async def _run(
    *,
    root: Path,
    mode: str,
    timeout: int = 30,
    extra_env: dict[str, str] | None = None,
) -> tuple[object, asyncio.Queue[BudgetEvent]]:
    queue: asyncio.Queue[BudgetEvent] = asyncio.Queue()
    env = {"FAKE_CLAUDE_MODE": mode}
    if extra_env:
        env.update(extra_env)
    result = await run_session(
        root=root,
        task_id="task-test",
        role="architect",
        session_id="01TESTSESSIONID0000000000",
        model="claude-opus-4-7",
        session_timeout_seconds=timeout,
        session_budget=SessionBudget(),
        event_queue=queue,
        claude_binary=str(FAKE_CLAUDE),
        env=env,
    )
    return result, queue


async def test_clean_end_returns_result(tmp_repo_root: Path) -> None:
    result, queue = await _run(root=tmp_repo_root, mode="clean")
    assert result.end_state == "clean"
    assert result.exit_code == 0
    assert result.result_event is not None
    assert isinstance(result.result_event, ResultEvent)

    events = await _collect_events(queue)
    kinds = [ev.kind for ev in events]
    assert kinds[0] == "session-started"
    assert kinds[-1] == "session-ended"

    stream_events = [ev.stream_event for ev in events if ev.kind == "stream-event"]
    types = [type(e).__name__ for e in stream_events]
    assert "SystemEvent" in types
    assert "AssistantEvent" in types
    assert "ResultEvent" in types
    # The session-started context is populated.
    assert events[0].ctx.session_id == "01TESTSESSIONID0000000000"
    assert events[0].ctx.role == "architect"


async def test_dirty_end_on_crash(tmp_repo_root: Path) -> None:
    result, _queue = await _run(root=tmp_repo_root, mode="crash")
    assert result.end_state == "dirty"
    assert result.exit_code == 1
    assert result.result_event is None
    assert any("simulated crash" in line for line in result.stderr_tail)


async def test_timeout_triggers_sigterm(tmp_repo_root: Path) -> None:
    # 2s timeout; FAKE_CLAUDE_MODE=hang sleeps 600s.
    result, _queue = await _run(root=tmp_repo_root, mode="hang", timeout=2)
    assert result.end_state == "timeout"
    # Subprocess killed — exit code is non-zero / signal-driven.
    assert result.exit_code is not None and result.exit_code != 0


async def test_malformed_line_does_not_derail_session(tmp_repo_root: Path) -> None:
    result, queue = await _run(root=tmp_repo_root, mode="malformed")
    # Crash mode would be dirty; malformed mode still emits a valid result.
    assert result.end_state == "clean"
    assert result.result_event is not None

    events = await _collect_events(queue)
    # At least one AssistantEvent and one ResultEvent landed on the queue.
    types = [type(ev.stream_event).__name__ for ev in events if ev.kind == "stream-event"]
    assert "ResultEvent" in types


async def test_scrubbed_env_excludes_marker(
    tmp_repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Set an env var on the parent that MUST NOT leak to the subprocess.
    marker = "HURAGOK_TEST_LEAK_MARKER"
    monkeypatch.setenv(marker, "leaked")

    env = default_session_env(subagent_model="claude-sonnet-4-6")
    assert marker not in env
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet-4-6"

    # Parent has ANTHROPIC_API_KEY — ensure it's forwarded.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-value")
    env = default_session_env(subagent_model="claude-sonnet-4-6")
    assert env.get("ANTHROPIC_API_KEY") == "sk-test-value"
    # But if parent lacks it, it's absent.
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    env = default_session_env(subagent_model="claude-sonnet-4-6")
    assert "ANTHROPIC_API_KEY" not in env


async def test_scrubbed_env_forwards_claude_code_oauth_token(
    tmp_repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CLAUDE_CODE_OAUTH_TOKEN`` forwards like ``ANTHROPIC_API_KEY`` does.

    The systemd deployment path (isolated ``HOME``) can't fall back to
    cached OAuth creds, so the long-lived token from
    ``claude setup-token`` needs to reach the subprocess via the env.
    """
    # Neither key: neither forwarded.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    env = default_session_env(subagent_model="claude-sonnet-4-6")
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    # Only OAuth set: only OAuth forwarded.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    env = default_session_env(subagent_model="claude-sonnet-4-6")
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-test"
    assert "ANTHROPIC_API_KEY" not in env

    # Both set: both forwarded. The runner doesn't enforce precedence
    # between them — that's Claude Code's concern.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-api-test")
    env = default_session_env(subagent_model="claude-sonnet-4-6")
    assert env.get("ANTHROPIC_API_KEY") == "sk-api-test"
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-test"


async def test_runner_forwards_extra_env(tmp_repo_root: Path) -> None:
    # ``FAKE_CLAUDE_MODE`` is an "extra" env var — confirm the runner
    # passes it through to the subprocess (the fake-claude script
    # depends on reading it).
    result, _queue = await _run(root=tmp_repo_root, mode="clean")
    assert result.end_state == "clean"


async def test_spawn_missing_binary_returns_dirty(tmp_repo_root: Path) -> None:
    queue: asyncio.Queue[BudgetEvent] = asyncio.Queue()
    result = await run_session(
        root=tmp_repo_root,
        task_id="task-test",
        role="architect",
        session_id="01MISSING",
        model="claude-opus-4-7",
        session_timeout_seconds=10,
        session_budget=SessionBudget(),
        event_queue=queue,
        claude_binary="/nonexistent/claude-binary-zzz",
    )
    assert result.end_state == "dirty"
    assert result.exit_code is None
    assert result.result_event is None
    events = await _collect_events(queue)
    assert events[-1].kind == "session-ended"


async def test_agent_file_is_appended_when_present(
    tmp_repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Create an agent file and confirm the runner reads it. We cannot
    # easily assert the subprocess argv, but reading the file should not
    # raise and the subprocess should still exit cleanly.
    agents = tmp_repo_root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "architect.md").write_text("agent prompt body\n")
    result, _queue = await _run(root=tmp_repo_root, mode="clean")
    assert result.end_state == "clean"


async def test_runner_enforces_timeout_without_hanging_test(
    tmp_repo_root: Path,
) -> None:
    # Explicit guard: the whole test must return quickly even though
    # hang mode sleeps 600s. Asyncio default-loop wait_for handles this.
    queue: asyncio.Queue[BudgetEvent] = asyncio.Queue()
    result = await asyncio.wait_for(
        run_session(
            root=tmp_repo_root,
            task_id="task-test",
            role="architect",
            session_id="01HANG",
            model="claude-opus-4-7",
            session_timeout_seconds=1,
            session_budget=SessionBudget(),
            event_queue=queue,
            claude_binary=str(FAKE_CLAUDE),
            env={"FAKE_CLAUDE_MODE": "hang"},
        ),
        timeout=60,  # pytest-side belt; should never be reached.
    )
    assert result.end_state == "timeout"


async def test_cleanup_stderr_tail_bounded(tmp_repo_root: Path) -> None:
    # Crash mode emits stderr — confirm the tail list is populated and
    # bounded to the configured limit.
    result, _queue = await _run(root=tmp_repo_root, mode="crash")
    assert 0 < len(result.stderr_tail) <= 50


async def test_subagent_model_set_on_env() -> None:
    env = default_session_env(subagent_model="claude-sonnet-4-6")
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet-4-6"
    # And extras override.
    env2 = default_session_env(
        subagent_model="claude-sonnet-4-6",
        extra={"FAKE_CLAUDE_MODE": "clean"},
    )
    assert env2["FAKE_CLAUDE_MODE"] == "clean"


def test_fake_claude_script_exists_and_executable() -> None:
    assert FAKE_CLAUDE.is_file()
    assert os.access(FAKE_CLAUDE, os.X_OK)
