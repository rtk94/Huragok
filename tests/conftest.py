"""Shared pytest fixtures for the Huragok orchestrator test suite."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "state" / "fixtures"


@pytest.fixture
def tmp_huragok_root(tmp_path: Path) -> Path:
    """A tmp directory containing a valid, populated ``.huragok/`` subtree.

    Layout produced:

    - ``state.yaml``   — copied from ``state_valid.yaml``
    - ``batch.yaml``   — copied from ``batch_valid.yaml``
    - ``decisions.md`` — a minimal header so append tests can tell the
      prefix is preserved
    - ``work/task-example/`` — a full artifact set copied from the real
      ``.huragok/examples/task-example/`` so ``show task-example`` works
    - ``examples/task-example/`` — same content, mirrored for completeness
    - ``audit/``, ``logs/``, ``requests/``, ``retrospectives/`` — empty

    Returns the repo root path (parent of ``.huragok/``).
    """
    huragok = tmp_path / ".huragok"
    huragok.mkdir()
    for sub in ("audit", "logs", "requests", "retrospectives", "work", "examples"):
        (huragok / sub).mkdir()

    shutil.copy(FIXTURES_DIR / "state_valid.yaml", huragok / "state.yaml")
    shutil.copy(FIXTURES_DIR / "batch_valid.yaml", huragok / "batch.yaml")

    (huragok / "decisions.md").write_text(
        "# Huragok Agent Decisions — Append-Only Log\n\n",
        encoding="utf-8",
    )

    example_source = REPO_ROOT / ".huragok" / "examples" / "task-example"
    shutil.copytree(example_source, huragok / "work" / "task-example")
    shutil.copytree(example_source, huragok / "examples" / "task-example")

    return tmp_path


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Reset ``load_settings`` cache between tests so env overrides apply."""
    from orchestrator.config import load_settings

    load_settings.cache_clear()
    yield
    load_settings.cache_clear()
