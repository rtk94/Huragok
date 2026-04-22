"""Tests for ``orchestrator.cli``."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.paths import audit_log, batch_log, requests_dir

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


def test_status_fresh_huragok_with_no_state_yaml_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.huragok/`` dir without ``state.yaml`` exits 0 with a pointer message."""
    (tmp_path / ".huragok").mkdir()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stderr
    assert "no batch submitted" in result.stdout.lower()
    assert "huragok submit" in result.stdout


def test_status_json_fresh_huragok_with_no_state_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json variant returns a minimal shape rather than a traceback."""
    (tmp_path / ".huragok").mkdir()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload.get("phase") == "no-batch"
    assert payload.get("batch_id") is None


def test_tasks_fresh_huragok_with_no_batch_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``huragok tasks`` in a fresh ``.huragok/`` exits 0 with a pointer message."""
    (tmp_path / ".huragok").mkdir()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["tasks"])
    assert result.exit_code == 0, result.stderr
    assert "no batch" in result.stdout.lower()


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
# Lifecycle commands: stop / halt.
# ---------------------------------------------------------------------------


def test_stop_without_daemon_is_friendly(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert "no daemon running" in result.stdout.lower()


def test_stop_clears_stale_pid_file(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.paths import daemon_pid_file

    pid_path = daemon_pid_file(tmp_huragok_root)
    # Pick a PID that should not exist.
    pid_path.write_text("4194302\n")
    monkeypatch.chdir(tmp_huragok_root)

    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert "stale" in result.stdout.lower()
    assert not pid_path.exists()


def test_halt_writes_request_file(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.paths import requests_dir

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["halt"])
    assert result.exit_code == 0
    halt_marker = requests_dir(tmp_huragok_root) / "halt"
    assert halt_marker.exists()


# ---------------------------------------------------------------------------
# start — doc-pointer stub.
# ---------------------------------------------------------------------------


def test_start_exits_1_with_doc_pointer(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1
    assert "systemctl --user start huragok.service" in result.stderr
    assert "docs/deployment.md" in result.stderr


# ---------------------------------------------------------------------------
# submit — validated batch write.
# ---------------------------------------------------------------------------


def _write_valid_batch(path: Path, *, batch_id: str = "batch-042") -> None:
    payload = {
        "version": 1,
        "batch_id": batch_id,
        "created": "2026-04-22T10:00:00Z",
        "description": "CLI submit test",
        "budgets": {
            "wall_clock_hours": 8.0,
            "max_tokens": 2_000_000,
            "max_dollars": 25.0,
            "max_iterations": 2,
            "session_timeout_minutes": 30,
        },
        "notifications": {
            "telegram_chat_id": None,
            "warn_threshold_pct": 80,
        },
        "tasks": [
            {
                "id": "task-new-001",
                "title": "A new task",
                "kind": "backend",
                "priority": 1,
                "acceptance_criteria": ["returns 200"],
                "depends_on": [],
                "foundational": False,
            }
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def test_submit_valid_batch_writes_files(
    tmp_huragok_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mark state idle so the submit path is unblocked.
    state_path = tmp_huragok_root / ".huragok" / "state.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["phase"] = "idle"
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    batch_path = tmp_path / "new-batch.yaml"
    _write_valid_batch(batch_path, batch_id="batch-042")
    monkeypatch.chdir(tmp_huragok_root)

    result = runner.invoke(app, ["submit", str(batch_path)])
    assert result.exit_code == 0, result.stderr
    assert "submitted batch-042" in result.stdout

    # batch.yaml was updated.
    new_batch = yaml.safe_load((tmp_huragok_root / ".huragok" / "batch.yaml").read_text())
    assert new_batch["batch_id"] == "batch-042"

    # state.yaml was reset and points at the new batch.
    new_state = yaml.safe_load((tmp_huragok_root / ".huragok" / "state.yaml").read_text())
    assert new_state["batch_id"] == "batch-042"
    assert new_state["phase"] == "idle"
    assert new_state["session_count"] == 0


def test_submit_rejects_invalid_batch(
    tmp_huragok_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_path = tmp_path / "bad-batch.yaml"
    bad_path.write_text("version: 1\nbatch_id: batch-x\n")  # missing required fields

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["submit", str(bad_path)])
    assert result.exit_code == 1
    assert "invalid batch file" in result.stderr.lower()


def test_submit_rejects_missing_file(
    tmp_huragok_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["submit", str(tmp_path / "does-not-exist.yaml")])
    assert result.exit_code == 1
    assert "not found" in result.stderr.lower()


def test_submit_refuses_to_overwrite_running_batch(
    tmp_huragok_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fixture's state.yaml is already phase=running.
    batch_path = tmp_path / "new-batch.yaml"
    _write_valid_batch(batch_path, batch_id="batch-should-not-land")
    monkeypatch.chdir(tmp_huragok_root)

    result = runner.invoke(app, ["submit", str(batch_path)])
    assert result.exit_code == 1
    assert "currently running" in result.stderr.lower()

    # The original batch.yaml was not clobbered.
    current = yaml.safe_load((tmp_huragok_root / ".huragok" / "batch.yaml").read_text())
    assert current["batch_id"] == "batch-001"


def test_submit_archives_prior_work_directory(
    tmp_huragok_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_huragok_root / ".huragok" / "state.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["phase"] = "idle"
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    batch_path = tmp_path / "fresh.yaml"
    _write_valid_batch(batch_path, batch_id="batch-archive-test")
    monkeypatch.chdir(tmp_huragok_root)

    result = runner.invoke(app, ["submit", str(batch_path)])
    assert result.exit_code == 0, result.stderr

    archived = tmp_huragok_root / ".huragok" / "work.archived" / "batch-001"
    # The archive folder exists and contains the old task-example.
    assert archived.is_dir()
    assert (archived / "task-example").is_dir()
    # The new work dir is empty.
    work = tmp_huragok_root / ".huragok" / "work"
    assert work.is_dir()
    assert not any(work.iterdir())


# ---------------------------------------------------------------------------
# reply — single pending / no pending / explicit id.
# ---------------------------------------------------------------------------


def test_reply_without_pending_exits_0_quietly(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ensure nothing is awaiting reply.
    state_path = tmp_huragok_root / ".huragok" / "state.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["awaiting_reply"] = {
        "notification_id": None,
        "sent_at": None,
        "kind": None,
        "deadline": None,
    }
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["reply", "continue"])
    assert result.exit_code == 0
    assert "no pending" in result.stdout.lower()


def test_reply_with_single_pending_writes_reply_file(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_huragok_root / ".huragok" / "state.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["awaiting_reply"] = {
        "notification_id": "01HXYZ",
        "sent_at": "2026-04-22T10:00:00Z",
        "kind": "blocker",
        "deadline": None,
    }
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["reply", "continue"])
    assert result.exit_code == 0, result.stderr

    reply_path = requests_dir(tmp_huragok_root) / "reply-01HXYZ.yaml"
    assert reply_path.exists()
    payload = yaml.safe_load(reply_path.read_text())
    assert payload["verb"] == "continue"
    assert payload["notification_id"] == "01HXYZ"
    assert payload["source"] == "cli"


def test_reply_with_explicit_id_wins(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["reply", "stop", "01EXPLICIT"])
    assert result.exit_code == 0, result.stderr
    reply_path = requests_dir(tmp_huragok_root) / "reply-01EXPLICIT.yaml"
    assert reply_path.exists()


def test_reply_alias_continue_is_accepted(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_huragok_root / ".huragok" / "state.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["awaiting_reply"] = {
        "notification_id": "01HALIAS",
        "sent_at": "2026-04-22T10:00:00Z",
        "kind": "blocker",
        "deadline": None,
    }
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["reply", "c"])
    assert result.exit_code == 0, result.stderr

    reply = yaml.safe_load((requests_dir(tmp_huragok_root) / "reply-01HALIAS.yaml").read_text())
    assert reply["verb"] == "continue"


def test_reply_rejects_unknown_verb(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["reply", "gibberish", "01HXYZ"])
    assert result.exit_code == 1
    assert "unknown verb" in result.stderr.lower()


def test_reply_sends_signal_when_daemon_alive(
    tmp_huragok_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend a daemon is running at our own pid (always alive from
    # this process's perspective).
    import os

    from orchestrator.paths import daemon_pid_file

    pid_path = daemon_pid_file(tmp_huragok_root)
    pid_path.write_text(f"{os.getpid()}\n")

    state_path = tmp_huragok_root / ".huragok" / "state.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["awaiting_reply"] = {
        "notification_id": "01SIGNAL",
        "sent_at": "2026-04-22T10:00:00Z",
        "kind": "blocker",
        "deadline": None,
    }
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    signals_seen: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        signals_seen.append(sig)

    monkeypatch.setattr("orchestrator.cli.os.kill", fake_kill)
    monkeypatch.chdir(tmp_huragok_root)

    result = runner.invoke(app, ["reply", "iterate"])
    assert result.exit_code == 0, result.stderr
    assert signal.SIGUSR1 in signals_seen


# ---------------------------------------------------------------------------
# logs — tail, follow, filter.
# ---------------------------------------------------------------------------


def test_logs_without_batch_in_flight(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Clear the batch_id so state says no batch in flight.
    state_path = tmp_huragok_root / ".huragok" / "state.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["batch_id"] = None
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 0
    assert "no batch in flight" in result.stdout.lower()


def test_logs_without_log_file_on_disk(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 0
    assert "no batch log on disk yet" in result.stdout.lower()


def test_logs_tails_last_records(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = batch_log(tmp_huragok_root, "batch-001")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ts": "2026-04-22T10:00:00Z", "level": "info", "event": f"entry-{i}"})
        for i in range(5)
    ]
    log_path.write_text("\n".join(lines) + "\n")

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 0, result.stderr
    for i in range(5):
        assert f"entry-{i}" in result.stdout


def test_logs_level_filter(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = batch_log(tmp_huragok_root, "batch-001")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps({"ts": "2026-04-22T10:00:00Z", "level": "debug", "event": "skip-me"})
        + "\n"
        + json.dumps({"ts": "2026-04-22T10:00:01Z", "level": "error", "event": "important"})
        + "\n"
    )

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["logs", "--level", "warn"])
    assert result.exit_code == 0, result.stderr
    assert "important" in result.stdout
    assert "skip-me" not in result.stdout


def test_logs_unknown_level_rejected(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["logs", "--level", "supernoisy"])
    assert result.exit_code == 1
    assert "unknown log level" in result.stderr.lower()


# ---------------------------------------------------------------------------
# status breakdown line.
# ---------------------------------------------------------------------------


def test_status_sessions_breakdown(tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_path = audit_log(tmp_huragok_root, "batch-001")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"kind": "session-launched"},
        {"kind": "session-launched"},
        {"kind": "session-launched"},
        {"kind": "session-ended", "end_state": "clean"},
        {"kind": "session-ended", "end_state": "clean"},
        {"kind": "session-ended", "end_state": "dirty"},
        {"kind": "budget-threshold"},  # unrelated — ignored
    ]
    audit_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stderr
    assert "3 launched, 2 clean, 1 retry" in result.stdout


def test_status_exposes_cache_token_sublines(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache tokens render as labelled sub-lines under Tokens:."""
    # Rewrite state.yaml with nonzero cache counters so the sub-lines
    # have distinguishable values. Values match the rough shape seen in
    # the 2026-04-22 smoke-001 run.
    state_path = tmp_huragok_root / ".huragok" / "state.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["budget_consumed"] = {
        "wall_clock_seconds": 240.0,
        "tokens_input": 186,
        "tokens_output": 13_100,
        "tokens_cache_read": 2_560_000,
        "tokens_cache_write": 367_000,
        "dollars": 6.67,
        "iterations": 0,
    }
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    monkeypatch.chdir(tmp_huragok_root)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stderr

    # The four sub-lines must each be present.
    assert "input:" in result.stdout
    assert "output:" in result.stdout
    assert "cache read:" in result.stdout
    assert "cache write:" in result.stdout

    # Humanised cache figures make it into the rendered output.
    assert "2.56M" in result.stdout  # cache read
    assert "367.0K" in result.stdout  # cache write

    # The percent-of-cap on the main Tokens line is still input+output only.
    # (186 + 13_100 = 13.3K; batch fixture cap is 5M.)
    assert "13.3K" in result.stdout
