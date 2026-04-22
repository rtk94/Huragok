"""Tests for :mod:`orchestrator.logging_setup`.

Cover the new ``file_path`` sink added by the 2026-04-22 amendment to
Slice B2: stdout-only behaviour still works, file mirroring emits
identical records on both sinks, open() failures fall back to
stdout-only with a WARN, missing parents auto-create, and
re-configuration closes the prior sink cleanly.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from orchestrator.logging_setup import close_file_sink, configure_logging


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Ensure each test starts from a clean structlog state."""
    close_file_sink()
    structlog.reset_defaults()
    yield
    close_file_sink()
    structlog.reset_defaults()


def _stdout_records(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def _file_records(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_stdout_only_when_no_file_path(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="info", json_output=True)
    log = structlog.get_logger("test").bind(component="tests")
    log.info("no_file_event", key="value")

    records = _stdout_records(capsys.readouterr().out)
    assert len(records) == 1
    assert records[0]["event"] == "no_file_event"
    assert records[0]["component"] == "tests"
    assert records[0]["level"] == "info"
    assert records[0]["key"] == "value"
    assert "ts" in records[0]


def test_file_sink_mirrors_stdout_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each log call emits one matching JSON record to BOTH sinks."""
    log_path = tmp_path / "batch-001.jsonl"

    configure_logging(level="info", json_output=True, file_path=log_path)
    log = structlog.get_logger("test").bind(component="tests")
    log.info("mirrored_event", identifier=42)

    stdout_records = _stdout_records(capsys.readouterr().out)
    file_records = _file_records(log_path)
    assert len(stdout_records) == 1
    assert len(file_records) == 1
    assert stdout_records[0] == file_records[0]
    assert stdout_records[0]["event"] == "mirrored_event"
    assert stdout_records[0]["identifier"] == 42


def test_missing_parent_directory_is_created(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "deeply" / "nested" / "batch-007.jsonl"
    assert not nested.parent.exists()

    configure_logging(level="info", json_output=True, file_path=nested)
    structlog.get_logger("test").bind(component="tests").info("nested_event")

    assert nested.parent.is_dir()
    records = _file_records(nested)
    assert len(records) == 1
    assert records[0]["event"] == "nested_event"


def test_open_failure_falls_back_to_stdout_with_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission error on the log file must not crash the daemon."""
    bad_path = tmp_path / "forbidden" / "batch.jsonl"
    real_open = builtins.open

    def failing_open(file: object, *args: object, **kwargs: object) -> object:
        if str(file) == str(bad_path):
            raise PermissionError(f"simulated permission denied: {file}")
        return real_open(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.open", failing_open)

    configure_logging(level="info", json_output=True, file_path=bad_path)
    structlog.get_logger("test").bind(component="tests").info("post_failure_event")

    assert not bad_path.exists()
    records = _stdout_records(capsys.readouterr().out)
    events = [record["event"] for record in records]
    assert "logging.file_sink.open_failed" in events
    assert "post_failure_event" in events
    failure = next(r for r in records if r["event"] == "logging.file_sink.open_failed")
    assert failure["level"] == "warning"
    assert failure["path"] == str(bad_path)
    assert "PermissionError" in str(failure["error"])


def test_reconfigure_does_not_duplicate_stdout_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Calling configure_logging twice must not stack stdout sinks."""
    log_path = tmp_path / "batch.jsonl"

    configure_logging(level="info", json_output=True)
    configure_logging(level="info", json_output=True, file_path=log_path)

    structlog.get_logger("test").bind(component="tests").info("once_only")

    stdout_records = _stdout_records(capsys.readouterr().out)
    file_records = _file_records(log_path)
    assert len(stdout_records) == 1
    assert len(file_records) == 1
    assert stdout_records[0]["event"] == "once_only"
    assert file_records[0]["event"] == "once_only"


def test_reconfigure_switches_file_sink_cleanly(tmp_path: Path) -> None:
    """Switching file_path between calls closes the first handle."""
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"

    configure_logging(level="info", json_output=True, file_path=first)
    structlog.get_logger("test").bind(component="tests").info("into_first")

    configure_logging(level="info", json_output=True, file_path=second)
    structlog.get_logger("test").bind(component="tests").info("into_second")

    first_records = _file_records(first)
    second_records = _file_records(second)
    assert [r["event"] for r in first_records] == ["into_first"]
    assert [r["event"] for r in second_records] == ["into_second"]


def test_close_file_sink_flushes_and_releases(tmp_path: Path) -> None:
    """close_file_sink must leave the file readable with prior writes intact."""
    log_path = tmp_path / "released.jsonl"

    configure_logging(level="info", json_output=True, file_path=log_path)
    structlog.get_logger("test").bind(component="tests").info("before_close")
    close_file_sink()

    records = _file_records(log_path)
    assert len(records) == 1
    assert records[0]["event"] == "before_close"

    # After close, further log calls still succeed on stdout but do not
    # write anything more to the (now-released) file.
    structlog.get_logger("test").bind(component="tests").info("after_close")
    assert len(_file_records(log_path)) == 1
