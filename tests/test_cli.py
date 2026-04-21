"""Tests for ``orchestrator.cli``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_human_view(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stderr
    assert "batch-001" in result.stdout
    assert "Elapsed:" in result.stdout
    assert "Tokens:" in result.stdout
    assert "Dollars:" in result.stdout
    assert "Tasks:" in result.stdout


def test_status_json(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["version"] == 1
    assert parsed["phase"] == "running"
    assert parsed["batch_id"] == "batch-001"
    assert "budget_consumed" in parsed


def test_status_outside_huragok_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    # Error goes to stderr, not stdout.
    assert "error" in result.stderr.lower()
    assert "huragok" in result.stderr.lower()


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


def test_tasks_lists_all_ids(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["tasks"])
    assert result.exit_code == 0, result.stderr
    assert "task-example" in result.stdout
    assert "task-0001" in result.stdout


def test_tasks_filter_by_state(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["tasks", "--state", "done"])
    assert result.exit_code == 0, result.stderr
    # task-example's status.yaml says state=done; task-0001 has no status
    # file and so is implicitly pending — it should be filtered out.
    assert "task-example" in result.stdout
    assert "task-0001" not in result.stdout


def test_tasks_filter_pending(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["tasks", "--state", "pending"])
    assert result.exit_code == 0, result.stderr
    assert "task-0001" in result.stdout
    assert "task-example" not in result.stdout


def test_tasks_empty_batch(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace batch.yaml with one that has no tasks.
    empty_batch = tmp_huragok_root / ".huragok" / "batch.yaml"
    empty_batch.write_text(
        "version: 1\n"
        "batch_id: batch-empty\n"
        "created: 2026-04-21T09:00:00Z\n"
        "description: empty\n"
        "budgets:\n"
        "  wall_clock_hours: 1.0\n"
        "  max_tokens: 1000\n"
        "  max_dollars: 1.0\n"
        "  max_iterations: 1\n"
        "  session_timeout_minutes: 10\n"
        "notifications:\n"
        "  telegram_chat_id: null\n"
        "  warn_threshold_pct: 80\n"
        "tasks: []\n"
    )
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["tasks"])
    assert result.exit_code == 0
    assert "no batch in flight" in result.stdout


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_task_example_summary(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["show", "task-example"])
    assert result.exit_code == 0, result.stderr
    assert "task-example" in result.stdout
    # Title extracted from the spec.md body: "# Add `/healthz` endpoint"
    assert "healthz" in result.stdout.lower()
    assert "state:" in result.stdout


def test_show_task_example_full(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["show", "task-example", "--full"])
    assert result.exit_code == 0, result.stderr
    # spec.md body mentions healthz; should appear multiple times
    # (header + bullet points).
    assert "healthz" in result.stdout.lower()
    # Every artifact should be inlined under a heading.
    assert "## spec.md" in result.stdout
    assert "## implementation.md" in result.stdout


def test_show_nonexistent_task(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["show", "nonexistent-task"])
    assert result.exit_code == 1
    assert "not found" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Stubs.
# ---------------------------------------------------------------------------


def test_run_stub_exits_1(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "not implemented" in result.stderr.lower()


@pytest.mark.parametrize(
    "cmd",
    [
        ["start"],
        ["stop"],
        ["halt"],
        ["reply", "continue"],
        ["submit", "some-batch.yaml"],
        ["logs"],
    ],
)
def test_slice_b_stubs_exit_1(
    cmd: list[str], tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, cmd)
    assert result.exit_code == 1
    assert "not implemented until Slice B" in result.stderr
