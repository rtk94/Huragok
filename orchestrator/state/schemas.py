"""Pydantic v2 models for every file under ``.huragok/``.

The schemas are the single source of truth for the on-disk state layout
(ADR-0002 D3). YAML files validate against these models on every read,
and writes serialize from them with a deterministic YAML dumper.

Every model enforces ``extra="forbid"`` so typos in a state file surface
as validation errors instead of being silently dropped.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.constants import SCHEMA_VERSION

# Type aliases for the enumerated state fields. Declared at module level so
# a typo anywhere in the schema is caught at validation time, not runtime.
Phase = Literal["idle", "running", "paused", "halted", "complete"]
AgentRole = Literal["architect", "implementer", "testwriter", "critic", "documenter"]
NotificationKind = Literal[
    "foundational-gate",
    "budget-threshold",
    "blocker",
    "batch-complete",
    "error",
    "rate-limit",
]
TaskKind = Literal["backend", "frontend", "fullstack", "docs"]
TaskState = Literal[
    "pending",
    "speccing",
    "implementing",
    "testing",
    "reviewing",
    "software-complete",
    "awaiting-human",
    "done",
    "blocked",
]
UIResolved = Literal["approved", "rejected"]


def _require_schema_version(v: int, file_name: str) -> int:
    """Validator helper: reject any version other than the current one."""
    if v != SCHEMA_VERSION:
        raise ValueError(f"{file_name} schema version must be {SCHEMA_VERSION}, got {v}")
    return v


class BudgetConsumed(BaseModel):
    """Running totals of every budget dimension the orchestrator tracks."""

    model_config = ConfigDict(extra="forbid")

    wall_clock_seconds: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    dollars: float = 0.0
    iterations: int = 0


class SessionBudget(BaseModel):
    """Advisory per-session budget written before each session launch."""

    model_config = ConfigDict(extra="forbid")

    remaining_tokens: int | None = None
    remaining_dollars: float | None = None
    timeout_seconds: int | None = None


class AwaitingReply(BaseModel):
    """Marker for a notification the operator still needs to answer."""

    model_config = ConfigDict(extra="forbid")

    notification_id: str | None = None
    sent_at: datetime | None = None
    kind: NotificationKind | None = None
    deadline: datetime | None = None


class StateFile(BaseModel):
    """Model for ``.huragok/state.yaml`` — the orchestrator's durable state."""

    model_config = ConfigDict(extra="forbid")

    version: int
    phase: Phase
    batch_id: str | None = None
    current_task: str | None = None
    current_agent: AgentRole | None = None
    session_count: int = 0
    session_id: str | None = None
    started_at: datetime | None = None
    last_checkpoint: datetime | None = None
    halted_reason: str | None = None
    budget_consumed: BudgetConsumed = Field(default_factory=BudgetConsumed)
    session_budget: SessionBudget = Field(default_factory=SessionBudget)
    # Queue item shape is defined in Slice B; keep it opaque here so the
    # schema doesn't lag the yet-to-be-built dispatcher.
    pending_notifications: list[dict[str, Any]] = Field(default_factory=list)
    awaiting_reply: AwaitingReply = Field(default_factory=AwaitingReply)

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        return _require_schema_version(v, "state.yaml")


class BatchBudgets(BaseModel):
    """Per-batch budget ceilings (ADR-0001 D4)."""

    model_config = ConfigDict(extra="forbid")

    wall_clock_hours: float
    max_tokens: int
    max_dollars: float
    max_iterations: int
    session_timeout_minutes: int


class BatchNotifications(BaseModel):
    """Per-batch notification configuration."""

    model_config = ConfigDict(extra="forbid")

    telegram_chat_id: str | None = None
    warn_threshold_pct: int = 80


class TaskEntry(BaseModel):
    """One task in a batch's task list."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    kind: TaskKind
    priority: int
    acceptance_criteria: list[str]
    depends_on: list[str] = Field(default_factory=list)
    foundational: bool = False


class BatchFile(BaseModel):
    """Model for ``.huragok/batch.yaml`` — the batch manifest."""

    model_config = ConfigDict(extra="forbid")

    version: int
    batch_id: str
    created: datetime
    description: str
    budgets: BatchBudgets
    notifications: BatchNotifications
    tasks: list[TaskEntry]

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        return _require_schema_version(v, "batch.yaml")


class HistoryEntry(BaseModel):
    """One row of ``status.yaml.history`` — a task state transition."""

    # ``from`` is a Python keyword; accept either ``from`` (YAML-native) or
    # ``from_`` (Python-safe) on input and emit ``from`` on output.
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    at: datetime
    from_: str = Field(alias="from")
    to: str
    # ``by`` is a role or the special value ``supervisor``; left as str so
    # ADR-0003 can evolve the allowed roles without invalidating old history.
    by: str
    session_id: str | None = None


class UIReview(BaseModel):
    """UI-gate status for UI-touching tasks (ADR-0001 D6)."""

    model_config = ConfigDict(extra="forbid")

    required: bool = False
    screenshots: list[str] = Field(default_factory=list)
    preview_url: str | None = None
    resolved: UIResolved | None = None


class StatusFile(BaseModel):
    """Model for ``.huragok/work/<task-id>/status.yaml`` — per-task state."""

    model_config = ConfigDict(extra="forbid")

    version: int
    task_id: str
    state: TaskState
    foundational: bool = False
    history: list[HistoryEntry] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    ui_review: UIReview = Field(default_factory=UIReview)

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        return _require_schema_version(v, "status.yaml")


class ArtifactFrontmatter(BaseModel):
    """Top-of-file frontmatter carried by every Markdown artifact."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    author_agent: AgentRole
    written_at: datetime
    session_id: str
