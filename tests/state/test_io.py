"""Tests for ``orchestrator.state.io``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from orchestrator.paths import (
    audit_log,
    batch_file,
    decisions_file,
    state_file,
    task_dir,
)
from orchestrator.state import (
    ArtifactFormatError,
    AtomicWriteError,
    append_audit,
    append_decisions,
    cleanup_stale_tmp,
    read_artifact,
    read_batch,
    read_state,
    read_status,
    write_batch,
    write_state,
    write_status,
)
from orchestrator.state.io import _atomic_write_yaml

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Round-trip for the three top-level YAML files.
# ---------------------------------------------------------------------------


def test_state_round_trip(tmp_huragok_root: Path) -> None:
    original = read_state(tmp_huragok_root)
    write_state(tmp_huragok_root, original)
    reloaded = read_state(tmp_huragok_root)
    assert reloaded == original


def test_batch_round_trip(tmp_huragok_root: Path) -> None:
    original = read_batch(tmp_huragok_root)
    write_batch(tmp_huragok_root, original)
    reloaded = read_batch(tmp_huragok_root)
    assert reloaded == original


def test_status_round_trip(tmp_huragok_root: Path) -> None:
    original = read_status(tmp_huragok_root, "task-example")
    write_status(tmp_huragok_root, original)
    reloaded = read_status(tmp_huragok_root, "task-example")
    assert reloaded == original


def test_state_write_uses_from_alias(tmp_huragok_root: Path) -> None:
    # Writing a status.yaml must emit ``from:`` in the YAML (alias), not
    # ``from_:`` (Python field name), so the file remains human-readable.
    status = read_status(tmp_huragok_root, "task-example")
    write_status(tmp_huragok_root, status)

    raw = (task_dir(tmp_huragok_root, "task-example") / "status.yaml").read_text()
    assert "from:" in raw
    assert "from_:" not in raw


# ---------------------------------------------------------------------------
# Atomic write protocol.
# ---------------------------------------------------------------------------


def test_atomic_write_replaces_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "state.yaml"
    target.write_text("old: value\n")
    _atomic_write_yaml(target, {"new": "value", "nested": {"k": 1}})
    assert yaml.safe_load(target.read_text()) == {"new": "value", "nested": {"k": 1}}


def test_atomic_write_creates_new_file(tmp_path: Path) -> None:
    target = tmp_path / "fresh.yaml"
    assert not target.exists()
    _atomic_write_yaml(target, {"hello": "world"})
    assert yaml.safe_load(target.read_text()) == {"hello": "world"}


def test_atomic_write_crash_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.yaml"
    target.write_text("original: content\n")

    def failing_rename(*args: object, **kwargs: object) -> None:
        raise OSError("simulated crash during rename")

    monkeypatch.setattr("os.rename", failing_rename)

    with pytest.raises(AtomicWriteError):
        _atomic_write_yaml(target, {"new": "content"})

    # Target untouched.
    assert target.read_text() == "original: content\n"
    # A tmp file remains — cleanup_stale_tmp handles it on next daemon start.
    tmp_files = list(tmp_path.glob("*.tmp.*.*"))
    assert len(tmp_files) == 1


def test_atomic_write_write_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.yaml"

    def failing_write(fd: int, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr("os.write", failing_write)

    with pytest.raises(AtomicWriteError):
        _atomic_write_yaml(target, {"k": "v"})


# ---------------------------------------------------------------------------
# Stale tmp cleanup.
# ---------------------------------------------------------------------------


def _find_unused_pid() -> int:
    """Return a PID that is extremely unlikely to be in use."""
    # Walk down from 2^22 looking for a free PID. On Linux the default
    # kernel pid_max is 4_194_304; picking near-max and checking avoids
    # colliding with a real process.
    for candidate in (4_194_302, 4_194_300, 4_194_290, 999_999, 88_888):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
        except OSError:
            continue
    # Fallback — extremely unlikely we'll reach here.
    return 1_234_567


def test_cleanup_stale_tmp_removes_dead_pid(tmp_path: Path) -> None:
    huragok = tmp_path / ".huragok"
    huragok.mkdir()
    dead_pid = _find_unused_pid()
    stale = huragok / f"state.yaml.tmp.{dead_pid}.abc123"
    stale.write_text("junk")

    deleted = cleanup_stale_tmp(tmp_path)

    assert deleted == 1
    assert not stale.exists()


def test_cleanup_stale_tmp_keeps_live_pid(tmp_path: Path) -> None:
    huragok = tmp_path / ".huragok"
    huragok.mkdir()
    live = huragok / f"state.yaml.tmp.{os.getpid()}.abc123"
    live.write_text("still writing")

    deleted = cleanup_stale_tmp(tmp_path)

    assert deleted == 0
    assert live.exists()


def test_cleanup_stale_tmp_ignores_non_tmp_files(tmp_path: Path) -> None:
    huragok = tmp_path / ".huragok"
    huragok.mkdir()
    (huragok / "state.yaml").write_text("not a tmp")
    (huragok / "batch.yaml").write_text("also not a tmp")

    deleted = cleanup_stale_tmp(tmp_path)

    assert deleted == 0
    assert (huragok / "state.yaml").exists()
    assert (huragok / "batch.yaml").exists()


def test_cleanup_stale_tmp_handles_missing_huragok_dir(tmp_path: Path) -> None:
    # No .huragok/ at all: returns 0 silently.
    assert cleanup_stale_tmp(tmp_path) == 0


# ---------------------------------------------------------------------------
# Markdown artifact parsing.
# ---------------------------------------------------------------------------


def test_read_artifact_valid_returns_frontmatter_and_body() -> None:
    frontmatter, body = read_artifact(FIXTURES / "artifact_valid.md")
    assert frontmatter.task_id == "task-example"
    assert frontmatter.author_agent == "architect"
    assert "markdown body" in body
    assert "**bold**" in body


def test_read_artifact_no_frontmatter_raises() -> None:
    with pytest.raises(ArtifactFormatError):
        read_artifact(FIXTURES / "artifact_no_frontmatter.md")


def test_read_artifact_missing_closing_delim_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("---\ntask_id: foo\n\nstill in frontmatter with no closing\n")
    with pytest.raises(ArtifactFormatError):
        read_artifact(bad)


def test_read_artifact_invalid_frontmatter_fields_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text(
        "---\ntask_id: foo\nauthor_agent: not-a-real-role\n"
        "written_at: 2026-04-21T09:00:00Z\nsession_id: abc\n---\nbody\n"
    )
    with pytest.raises(ArtifactFormatError):
        read_artifact(bad)


# ---------------------------------------------------------------------------
# Append-only logs.
# ---------------------------------------------------------------------------


def test_append_decisions_preserves_existing(tmp_huragok_root: Path) -> None:
    target = decisions_file(tmp_huragok_root)
    before = target.read_text()

    append_decisions(
        tmp_huragok_root,
        "## 2026-04-21 14:00:00  architect  batch-001/task-0001\n\nPicked X over Y.",
    )

    after = target.read_text()
    assert after.startswith(before)
    assert "Picked X over Y." in after
    # Ends with a trailing blank line separator.
    assert after.endswith("\n\n")


def test_append_decisions_appends_multiple_blocks(tmp_huragok_root: Path) -> None:
    append_decisions(tmp_huragok_root, "first block")
    append_decisions(tmp_huragok_root, "second block")
    content = decisions_file(tmp_huragok_root).read_text()
    assert content.index("first block") < content.index("second block")


def test_append_audit_creates_directory(tmp_path: Path) -> None:
    (tmp_path / ".huragok").mkdir()
    audit_dir = tmp_path / ".huragok" / "audit"
    assert not audit_dir.exists()

    append_audit(tmp_path, "batch-001", {"kind": "status-transition", "task_id": "task-0001"})

    assert audit_dir.is_dir()
    assert (audit_dir / "batch-001.jsonl").is_file()


def test_append_audit_writes_one_line_per_call(tmp_path: Path) -> None:
    (tmp_path / ".huragok").mkdir()
    append_audit(tmp_path, "batch-001", {"event": "a"})
    append_audit(tmp_path, "batch-001", {"event": "b"})

    log_path = audit_log(tmp_path, "batch-001")
    raw = log_path.read_text()
    lines = raw.splitlines()
    assert len(lines) == 2
    assert raw.endswith("\n")
    # Each line is valid JSON and parseable.
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["event"] == "a"
    assert second["event"] == "b"


def test_append_audit_serialises_non_json_types(tmp_path: Path) -> None:
    (tmp_path / ".huragok").mkdir()
    from datetime import UTC, datetime

    append_audit(
        tmp_path,
        "batch-001",
        {"ts": datetime(2026, 4, 21, 9, 0, tzinfo=UTC), "kind": "test"},
    )
    line = audit_log(tmp_path, "batch-001").read_text().strip()
    payload = json.loads(line)
    # datetime serialised via ``default=str``; must be present and parseable.
    assert "2026-04-21" in payload["ts"]


# ---------------------------------------------------------------------------
# Read helpers propagate FileNotFoundError cleanly.
# ---------------------------------------------------------------------------


def test_read_state_missing_raises_file_not_found(tmp_path: Path) -> None:
    (tmp_path / ".huragok").mkdir()
    with pytest.raises(FileNotFoundError):
        read_state(tmp_path)


def test_read_batch_missing_raises_file_not_found(tmp_path: Path) -> None:
    (tmp_path / ".huragok").mkdir()
    with pytest.raises(FileNotFoundError):
        read_batch(tmp_path)


def test_state_file_path_resolves_correctly(tmp_huragok_root: Path) -> None:
    assert state_file(tmp_huragok_root).exists()
    assert batch_file(tmp_huragok_root).exists()
