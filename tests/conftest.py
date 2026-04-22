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


@pytest.fixture(autouse=True)
def _isolate_external_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env vars and disable ``.env`` loading for tests.

    The user's working-directory ``.env`` can set ``TELEGRAM_BOT_TOKEN``
    and ``HURAGOK_TELEGRAM_DEFAULT_CHAT_ID``; if either leaks into a
    test that exercises the supervisor, ``build_dispatcher`` constructs
    a real :class:`TelegramDispatcher` whose ``getUpdates`` long-poll
    blocks on the real network. We scrub both the live environment
    *and* the settings config so tests get a deterministic, offline
    view regardless of where they're run from. Tests that want to
    exercise Telegram construct a :class:`TelegramDispatcher` directly
    with an injected mock client.
    """
    from orchestrator.config import HuragokSettings

    for key in (
        "TELEGRAM_BOT_TOKEN",
        "HURAGOK_TELEGRAM_DEFAULT_CHAT_ID",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_ADMIN_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    # Disable `.env` discovery so tests never pick up an ambient dev file.
    monkeypatch.setitem(HuragokSettings.model_config, "env_file", None)
