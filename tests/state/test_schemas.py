"""Tests for ``orchestrator.state.schemas``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from orchestrator.constants import SCHEMA_VERSION
from orchestrator.state import read_artifact
from orchestrator.state.schemas import (
    ArtifactFrontmatter,
    AwaitingReply,
    BatchBudgets,
    BatchFile,
    BatchNotifications,
    BudgetConsumed,
    HistoryEntry,
    SessionBudget,
    StateFile,
    StatusFile,
    TaskEntry,
    UIReview,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE_TASK_DIR = REPO_ROOT / ".huragok" / "examples" / "task-example"


def _load(name: str) -> dict[str, Any]:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------
# Happy-path parsing for every model.
# ---------------------------------------------------------------------------


def test_state_file_valid() -> None:
    state = StateFile.model_validate(_load("state_valid.yaml"))
    assert state.phase == "running"
    assert state.batch_id == "batch-001"
    assert state.current_agent == "implementer"
    assert state.budget_consumed.tokens_input == 500_000
    assert state.session_budget.remaining_tokens == 4_000_000


def test_batch_file_valid() -> None:
    batch = BatchFile.model_validate(_load("batch_valid.yaml"))
    assert batch.batch_id == "batch-001"
    assert len(batch.tasks) == 2
    assert batch.tasks[0].id == "task-example"
    assert batch.budgets.wall_clock_hours == 12.0
    assert batch.notifications.warn_threshold_pct == 80


def test_status_file_done() -> None:
    status = StatusFile.model_validate(_load("status_done.yaml"))
    assert status.state == "done"
    assert status.task_id == "task-example"
    assert status.history[0].from_ == "pending"
    assert status.history[-1].to == "done"


def test_status_file_blocked() -> None:
    status = StatusFile.model_validate(_load("status_blocked.yaml"))
    assert status.state == "blocked"
    assert "Upstream schema change" in status.blockers[0]


# ---------------------------------------------------------------------------
# Default-value and leaf-model behavior.
# ---------------------------------------------------------------------------


def test_budget_consumed_defaults() -> None:
    bc = BudgetConsumed()
    assert bc.wall_clock_seconds == 0.0
    assert bc.tokens_input == 0
    assert bc.dollars == 0.0


def test_session_budget_defaults() -> None:
    sb = SessionBudget()
    assert sb.remaining_tokens is None
    assert sb.remaining_dollars is None
    assert sb.timeout_seconds is None


def test_awaiting_reply_defaults() -> None:
    ar = AwaitingReply()
    assert ar.notification_id is None
    assert ar.kind is None


def test_ui_review_defaults() -> None:
    ui = UIReview()
    assert ui.required is False
    assert ui.screenshots == []
    assert ui.preview_url is None
    assert ui.resolved is None


def test_task_entry_defaults() -> None:
    t = TaskEntry(
        id="task-0042",
        title="A task",
        kind="backend",
        priority=1,
        acceptance_criteria=["x"],
    )
    assert t.depends_on == []
    assert t.foundational is False


def test_batch_notifications_defaults() -> None:
    bn = BatchNotifications()
    assert bn.telegram_chat_id is None
    assert bn.warn_threshold_pct == 80


def test_batch_budgets_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        BatchBudgets()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Validator enforcement — version, phase, required fields, forbidden extras.
# ---------------------------------------------------------------------------


def test_state_file_wrong_version_mentions_expected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        StateFile.model_validate(_load("state_wrong_version.yaml"))
    assert str(SCHEMA_VERSION) in str(excinfo.value)


def test_state_file_unknown_phase_names_field() -> None:
    with pytest.raises(ValidationError) as excinfo:
        StateFile.model_validate(_load("state_unknown_phase.yaml"))
    assert "phase" in str(excinfo.value).lower()


def test_batch_file_missing_required_lists_fields() -> None:
    with pytest.raises(ValidationError) as excinfo:
        BatchFile.model_validate(_load("batch_missing_required.yaml"))
    message = str(excinfo.value).lower()
    # Pydantic reports each missing field in the error list.
    assert "budgets" in message
    assert "notifications" in message


def test_extra_field_forbidden_on_state_file() -> None:
    data = _load("state_valid.yaml")
    data["unexpected_field"] = "boom"
    with pytest.raises(ValidationError):
        StateFile.model_validate(data)


def test_extra_field_forbidden_on_task_entry() -> None:
    with pytest.raises(ValidationError):
        TaskEntry.model_validate(
            {
                "id": "task-0042",
                "title": "A task",
                "kind": "backend",
                "priority": 1,
                "acceptance_criteria": ["x"],
                "unexpected_field": True,
            }
        )


# ---------------------------------------------------------------------------
# HistoryEntry alias behaviour.
# ---------------------------------------------------------------------------


def test_history_entry_accepts_alias_and_python_name() -> None:
    ts = datetime(2026, 4, 21, 9, 0, tzinfo=UTC)

    via_alias = HistoryEntry.model_validate(
        {"at": ts, "from": "pending", "to": "speccing", "by": "supervisor"}
    )
    via_python = HistoryEntry(at=ts, from_="pending", to="speccing", by="supervisor")
    assert via_alias == via_python
    assert via_alias.from_ == "pending"


def test_history_entry_dumps_with_alias() -> None:
    ts = datetime(2026, 4, 21, 9, 0, tzinfo=UTC)
    entry = HistoryEntry(at=ts, from_="pending", to="speccing", by="supervisor")
    dumped = entry.model_dump(by_alias=True)
    assert "from" in dumped
    assert "from_" not in dumped


# ---------------------------------------------------------------------------
# Round-trip: Pydantic → YAML → Pydantic is identity.
# ---------------------------------------------------------------------------


def test_state_round_trip_is_identity() -> None:
    original = StateFile.model_validate(_load("state_valid.yaml"))
    dumped = original.model_dump(mode="json", by_alias=True)
    re_yaml = yaml.safe_dump(dumped, sort_keys=False)
    reloaded = StateFile.model_validate(yaml.safe_load(re_yaml))
    assert reloaded == original


def test_status_round_trip_is_identity() -> None:
    original = StatusFile.model_validate(_load("status_done.yaml"))
    dumped = original.model_dump(mode="json", by_alias=True)
    reloaded = StatusFile.model_validate(yaml.safe_load(yaml.safe_dump(dumped)))
    assert reloaded == original


def test_batch_round_trip_is_identity() -> None:
    original = BatchFile.model_validate(_load("batch_valid.yaml"))
    dumped = original.model_dump(mode="json", by_alias=True)
    reloaded = BatchFile.model_validate(yaml.safe_load(yaml.safe_dump(dumped)))
    assert reloaded == original


# ---------------------------------------------------------------------------
# The real examples/task-example/ artifacts must parse cleanly.
# ---------------------------------------------------------------------------


def test_example_status_yaml_parses() -> None:
    with open(EXAMPLE_TASK_DIR / "status.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    status = StatusFile.model_validate(data)
    assert status.task_id == "task-example"
    assert status.state == "done"


@pytest.mark.parametrize(
    "filename,expected_agent",
    [
        ("spec.md", "architect"),
        ("implementation.md", "implementer"),
        ("tests.md", "testwriter"),
        ("review.md", "critic"),
    ],
)
def test_example_artifact_frontmatter_parses(filename: str, expected_agent: str) -> None:
    frontmatter, body = read_artifact(EXAMPLE_TASK_DIR / filename)
    assert isinstance(frontmatter, ArtifactFrontmatter)
    assert frontmatter.task_id == "task-example"
    assert frontmatter.author_agent == expected_agent
    assert body.strip()  # non-empty body
