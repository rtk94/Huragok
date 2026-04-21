"""Path resolution. The single home for ``.huragok/`` discovery and
task-folder path composition. Every other module asks this one."""

from pathlib import Path

from orchestrator.constants import (
    AUDIT_DIR,
    BATCH_FILE,
    DAEMON_PID_FILE,
    DECISIONS_FILE,
    HURAGOK_DIR,
    LOGS_DIR,
    RATE_LIMIT_LOG,
    REQUESTS_DIR,
    STATE_FILE,
    WORK_DIR,
)


class HuragokNotFoundError(Exception):
    """Raised when ``.huragok/`` cannot be found walking up from a starting path."""


def find_huragok_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` to the nearest parent containing ``.huragok/``.

    Returns the parent (the "repo root"), not the ``.huragok/`` directory itself.
    Raises :class:`HuragokNotFoundError` if no ancestor is found before the
    filesystem root.
    """
    origin = Path.cwd() if start is None else start
    current = origin.resolve()
    while True:
        if (current / HURAGOK_DIR).is_dir():
            return current
        if current.parent == current:
            raise HuragokNotFoundError(f"no .huragok/ directory found walking up from {origin}")
        current = current.parent


def huragok_dir(root: Path) -> Path:
    """Return ``<root>/.huragok``."""
    return root / HURAGOK_DIR


def task_dir(root: Path, task_id: str) -> Path:
    """Return the on-disk path for a task's folder. Does not check existence."""
    return root / HURAGOK_DIR / WORK_DIR / task_id


def state_file(root: Path) -> Path:
    """Return the path to ``.huragok/state.yaml``."""
    return root / HURAGOK_DIR / STATE_FILE


def batch_file(root: Path) -> Path:
    """Return the path to ``.huragok/batch.yaml``."""
    return root / HURAGOK_DIR / BATCH_FILE


def decisions_file(root: Path) -> Path:
    """Return the path to ``.huragok/decisions.md``."""
    return root / HURAGOK_DIR / DECISIONS_FILE


def audit_log(root: Path, batch_id: str) -> Path:
    """Return ``.huragok/audit/<batch_id>.jsonl``."""
    return root / HURAGOK_DIR / AUDIT_DIR / f"{batch_id}.jsonl"


def batch_log(root: Path, batch_id: str) -> Path:
    """Return ``.huragok/logs/batch-<batch_id>.jsonl``."""
    return root / HURAGOK_DIR / LOGS_DIR / f"batch-{batch_id}.jsonl"


def rate_limit_log(root: Path) -> Path:
    """Return the path to ``.huragok/rate-limit-log.yaml``."""
    return root / HURAGOK_DIR / RATE_LIMIT_LOG


def daemon_pid_file(root: Path) -> Path:
    """Return the path to ``.huragok/daemon.pid``."""
    return root / HURAGOK_DIR / DAEMON_PID_FILE


def requests_dir(root: Path) -> Path:
    """Return the path to ``.huragok/requests/``."""
    return root / HURAGOK_DIR / REQUESTS_DIR
