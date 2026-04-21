"""Read, write, and append helpers for every file under ``.huragok/``.

Writes go through a temp-file-and-rename protocol so a SIGKILL at any
step leaves either the pre-existing file or a fully-written new file on
disk — never a partial write. Orphan temp files are cleaned up on daemon
startup via :func:`cleanup_stale_tmp`. See ADR-0002 D3 for the rationale.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from orchestrator.constants import HURAGOK_DIR, STATUS_FILE
from orchestrator.paths import (
    audit_log,
    batch_file,
    decisions_file,
    state_file,
    task_dir,
)
from orchestrator.state.schemas import (
    ArtifactFrontmatter,
    BatchFile,
    StateFile,
    StatusFile,
)


class AtomicWriteError(IOError):
    """Raised when an atomic write fails at any step of the protocol."""


class ArtifactFormatError(ValueError):
    """Raised when a markdown artifact's frontmatter is absent or malformed."""


# ---------------------------------------------------------------------------
# Atomic write protocol.
# ---------------------------------------------------------------------------


def _atomic_write_yaml(target: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as YAML to ``target`` atomically.

    Steps:

      1. Render YAML to bytes in memory.
      2. Create a temp file at ``<target>.tmp.<pid>.<uuid>``.
      3. Write bytes, fsync the file, close.
      4. ``os.rename`` to the target (POSIX-atomic within a filesystem).
      5. fsync the containing directory.

    A SIGKILL at any step leaves either the old file or the fully-written
    new file; never a partial write. Orphan temp files remain and are
    swept up by :func:`cleanup_stale_tmp` at the next daemon start.
    """
    rendered = yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).encode("utf-8")

    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"

    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        raise AtomicWriteError(f"could not create temp file {tmp}: {exc}") from exc

    try:
        try:
            os.write(fd, rendered)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise AtomicWriteError(f"could not write/fsync temp file {tmp}: {exc}") from exc

    try:
        os.rename(tmp, target)
    except OSError as exc:
        raise AtomicWriteError(f"could not rename {tmp} to {target}: {exc}") from exc

    # Best-effort directory fsync — durability belt-and-suspenders. A failure
    # here does not roll back the rename; just log at daemon level later.
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _pid_is_live(pid: int) -> bool:
    """Return True if ``pid`` refers to a running process on this host."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — still "live".
        return True
    except OSError:
        return False
    return True


def cleanup_stale_tmp(root: Path) -> int:
    """Delete orphan ``*.tmp.<pid>.<uuid>`` files under ``.huragok/``.

    A temp file whose embedded PID is no longer running is the on-disk
    evidence of a crashed writer. Temp files whose PID is still live
    belong to an in-flight writer and are left alone.

    Returns the number of files deleted.
    """
    huragok = root / HURAGOK_DIR
    if not huragok.is_dir():
        return 0

    deleted = 0
    for path in huragok.rglob("*.tmp.*.*"):
        parts = path.name.split(".")
        try:
            tmp_idx = parts.index("tmp")
        except ValueError:
            continue
        if tmp_idx + 2 >= len(parts):
            continue
        try:
            pid = int(parts[tmp_idx + 1])
        except ValueError:
            continue
        if _pid_is_live(pid):
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# state.yaml, batch.yaml, status.yaml round-trips.
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    with open(path, "rb") as fh:
        return yaml.safe_load(fh)


def read_state(root: Path) -> StateFile:
    """Read and validate ``.huragok/state.yaml``."""
    return StateFile.model_validate(_load_yaml(state_file(root)))


def write_state(root: Path, state: StateFile) -> None:
    """Atomically write ``.huragok/state.yaml``."""
    payload = state.model_dump(mode="json", by_alias=True)
    _atomic_write_yaml(state_file(root), payload)


def read_batch(root: Path) -> BatchFile:
    """Read and validate ``.huragok/batch.yaml``."""
    return BatchFile.model_validate(_load_yaml(batch_file(root)))


def write_batch(root: Path, batch: BatchFile) -> None:
    """Atomically write ``.huragok/batch.yaml``."""
    payload = batch.model_dump(mode="json", by_alias=True)
    _atomic_write_yaml(batch_file(root), payload)


def read_status(root: Path, task_id: str) -> StatusFile:
    """Read and validate ``.huragok/work/<task-id>/status.yaml``."""
    path = task_dir(root, task_id) / STATUS_FILE
    return StatusFile.model_validate(_load_yaml(path))


def write_status(root: Path, status: StatusFile) -> None:
    """Atomically write the given status file to its task folder."""
    path = task_dir(root, status.task_id) / STATUS_FILE
    payload = status.model_dump(mode="json", by_alias=True)
    _atomic_write_yaml(path, payload)


# ---------------------------------------------------------------------------
# Markdown artifact parsing.
# ---------------------------------------------------------------------------


_FRONTMATTER_DELIM = "---"


def read_artifact(path: Path) -> tuple[ArtifactFrontmatter, str]:
    """Read a markdown artifact; return its validated frontmatter and body.

    Raises :class:`ArtifactFormatError` if the leading/trailing ``---``
    delimiters are missing or the frontmatter fails schema validation.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    if not lines or lines[0].rstrip("\r\n") != _FRONTMATTER_DELIM:
        raise ArtifactFormatError(f"missing leading --- frontmatter delimiter in {path}")

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == _FRONTMATTER_DELIM:
            end_idx = idx
            break

    if end_idx is None:
        raise ArtifactFormatError(f"missing closing --- frontmatter delimiter in {path}")

    fm_text = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])

    try:
        fm_data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise ArtifactFormatError(f"could not parse frontmatter in {path}: {exc}") from exc

    if not isinstance(fm_data, dict):
        raise ArtifactFormatError(f"frontmatter must be a YAML mapping in {path}")

    try:
        frontmatter = ArtifactFrontmatter.model_validate(fm_data)
    except ValidationError as exc:
        raise ArtifactFormatError(f"invalid frontmatter in {path}: {exc}") from exc

    return frontmatter, body


# ---------------------------------------------------------------------------
# Append-only logs: decisions.md and per-batch audit JSONL.
# ---------------------------------------------------------------------------


def append_decisions(root: Path, block: str) -> None:
    """Append ``block`` (plus a trailing blank line) to ``decisions.md``.

    Uses ``O_APPEND`` and writes the whole payload in a single ``write``
    call to minimize interleaving with concurrent appenders.
    """
    body = block if block.endswith("\n") else block + "\n"
    payload = (body + "\n").encode("utf-8")

    target = decisions_file(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def append_audit(root: Path, batch_id: str, event: dict[str, Any]) -> None:
    """Append one JSON line to the per-batch audit file.

    Creates the ``audit/`` directory on first use. Each call writes
    exactly one newline-terminated JSON object.
    """
    target = audit_log(root, batch_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(event, sort_keys=True, default=str) + "\n"
    payload = line.encode("utf-8")

    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
