"""Tests for ``orchestrator.paths``."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.paths import (
    HuragokNotFoundError,
    audit_log,
    batch_file,
    batch_log,
    daemon_pid_file,
    decisions_file,
    find_huragok_root,
    huragok_dir,
    rate_limit_log,
    requests_dir,
    state_file,
    task_dir,
)


def test_find_huragok_root_at_root(tmp_huragok_root: Path) -> None:
    assert find_huragok_root(tmp_huragok_root).resolve() == tmp_huragok_root.resolve()


def test_find_huragok_root_from_nested(tmp_huragok_root: Path) -> None:
    nested = tmp_huragok_root / "src" / "api" / "handlers"
    nested.mkdir(parents=True)
    assert find_huragok_root(nested).resolve() == tmp_huragok_root.resolve()


def test_find_huragok_root_defaults_to_cwd(
    tmp_huragok_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_huragok_root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert find_huragok_root().resolve() == tmp_huragok_root.resolve()


def test_find_huragok_root_missing(tmp_path: Path) -> None:
    with pytest.raises(HuragokNotFoundError):
        find_huragok_root(tmp_path)


def test_find_huragok_root_stops_at_filesystem_root(tmp_path: Path) -> None:
    # A deep nested directory with no .huragok/ anywhere in its ancestry
    # must raise rather than loop indefinitely.
    nested = tmp_path / "a" / "b" / "c" / "d"
    nested.mkdir(parents=True)
    with pytest.raises(HuragokNotFoundError):
        find_huragok_root(nested)


def test_huragok_dir(tmp_huragok_root: Path) -> None:
    assert huragok_dir(tmp_huragok_root) == tmp_huragok_root / ".huragok"


def test_task_dir(tmp_huragok_root: Path) -> None:
    assert (
        task_dir(tmp_huragok_root, "task-0042")
        == tmp_huragok_root / ".huragok" / "work" / "task-0042"
    )


def test_task_dir_does_not_check_existence(tmp_huragok_root: Path) -> None:
    # task-9999 does not exist in the fixture.
    path = task_dir(tmp_huragok_root, "task-9999")
    assert not path.exists()
    assert path.name == "task-9999"


def test_state_file(tmp_huragok_root: Path) -> None:
    assert state_file(tmp_huragok_root) == tmp_huragok_root / ".huragok" / "state.yaml"


def test_batch_file(tmp_huragok_root: Path) -> None:
    assert batch_file(tmp_huragok_root) == tmp_huragok_root / ".huragok" / "batch.yaml"


def test_decisions_file(tmp_huragok_root: Path) -> None:
    assert decisions_file(tmp_huragok_root) == tmp_huragok_root / ".huragok" / "decisions.md"


def test_audit_log(tmp_huragok_root: Path) -> None:
    assert (
        audit_log(tmp_huragok_root, "batch-001")
        == tmp_huragok_root / ".huragok" / "audit" / "batch-001.jsonl"
    )


def test_batch_log(tmp_huragok_root: Path) -> None:
    assert (
        batch_log(tmp_huragok_root, "batch-001")
        == tmp_huragok_root / ".huragok" / "logs" / "batch-batch-001.jsonl"
    )


def test_rate_limit_log(tmp_huragok_root: Path) -> None:
    assert rate_limit_log(tmp_huragok_root) == tmp_huragok_root / ".huragok" / "rate-limit-log.yaml"


def test_daemon_pid_file(tmp_huragok_root: Path) -> None:
    assert daemon_pid_file(tmp_huragok_root) == tmp_huragok_root / ".huragok" / "daemon.pid"


def test_requests_dir(tmp_huragok_root: Path) -> None:
    assert requests_dir(tmp_huragok_root) == tmp_huragok_root / ".huragok" / "requests"
