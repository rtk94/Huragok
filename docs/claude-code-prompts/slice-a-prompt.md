# Huragok Phase 1 — Slice A: Synchronous Foundation

You are building the synchronous foundation of the Huragok Python orchestrator. This is the first of two slices in the Phase 1 MVP build. Slice A is everything that does NOT involve `asyncio`, subprocess, network, signals, or systemd. Slice B (separate session, later) will build the async orchestration layer on top of what you produce here.

Read this entire prompt before you start. Then read the ADRs listed below before you write any code. If anything in this prompt contradicts the ADRs, stop and ask — the ADRs are authoritative.

---

## Read first, in this order

1. `CLAUDE.md` at the repo root — project rules, architecture-first mandate, do-nots.
2. `docs/adr/ADR-0001-huragok-orchestration.md` — the charter.
3. `docs/adr/ADR-0002-orchestrator-daemon-internals.md` — the authoritative spec for everything you build. **D3 (state schemas) and D5 (CLI) are the two decisions Slice A implements most directly.** D9 (observability) is partially implemented here (structured-log setup) with the rest deferred.
4. `docs/adr/ADR-0003-agent-definitions.md` — so you understand what the state artifacts are for.
5. `.huragok/examples/task-example/` — the complete worked example. Your Pydantic models must validate every file in this directory cleanly.

Do not read `orchestrator/` — it is empty. You are creating everything inside it.

---

## Scope boundaries for Slice A

**In scope, build all of this:**

- `orchestrator/__init__.py`
- `orchestrator/constants.py`
- `orchestrator/paths.py`
- `orchestrator/config.py`
- `orchestrator/logging_setup.py`
- `orchestrator/state/__init__.py`
- `orchestrator/state/schemas.py`
- `orchestrator/state/io.py`
- `orchestrator/cli.py`
- Tests under `tests/` mirroring that layout.
- A `.env.example` at the repo root.
- Updates to `pyproject.toml` to add runtime deps and register the CLI entrypoint.

**Explicitly NOT in scope — do NOT build:**

- Anything under `orchestrator/session/`, `orchestrator/budget/`, `orchestrator/notifications/`, `orchestrator/supervisor/`. These folders should not exist after this slice.
- Anything that imports `asyncio`, `subprocess`, `socket`, `signal`, `httpx`, `aiohttp`, `telegram`, `systemd`.
- The CLI commands `run`, `start`, `stop`, `halt`, `reply`, `submit`, `logs`. Register them in the Typer app as stubs that print `"not implemented until Slice B"` and exit 1. Only `status`, `tasks`, and `show` are real in this slice.
- Notification code, Telegram code, rate-limit-log code, audit-log code beyond the append helper.
- Budget math. Schemas carry the `budget_consumed` dict; no logic operates on it yet.

If you find yourself reaching for an out-of-scope concept, stop. Scope creep here will contaminate Slice B.

---

## Dependencies

Add to `pyproject.toml` under `[project]` `dependencies`:

```toml
dependencies = [
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "pyyaml>=6.0",
    "structlog>=24.0",
    "typer>=0.12",
    "rich>=13.0",
]
```

Keep the existing `[dependency-groups] dev` list unchanged. Do not add dev deps in this slice.

Register the CLI entrypoint:

```toml
[project.scripts]
huragok = "orchestrator.cli:app"
```

After your changes, `uv sync` should complete cleanly with no deprecation warnings.

---

## Module-by-module spec

Each module below lists its exact contents. Stick to these signatures and docstrings. If you think a signature should be different, STOP and ask — don't silently deviate.

### `orchestrator/constants.py`

```python
"""Constants with no runtime behavior. Pinned version numbers, schema
versions, and path conventions live here so every other module imports
one source of truth."""

from pathlib import Path
from typing import Final

# Claude Code minimum version; ADR-0002 D2.
MIN_CLAUDE_CODE_VERSION: Final[str] = "2.1.91"

# Schema version for every .huragok/*.yaml file; ADR-0002 D3.
SCHEMA_VERSION: Final[int] = 1

# Directory name used as the anchor for walk-up resolution; ADR-0002 D5.
HURAGOK_DIR: Final[str] = ".huragok"

# Relative paths inside .huragok/, as Path objects for composition.
STATE_FILE: Final[Path] = Path("state.yaml")
BATCH_FILE: Final[Path] = Path("batch.yaml")
DECISIONS_FILE: Final[Path] = Path("decisions.md")
WORK_DIR: Final[Path] = Path("work")
AUDIT_DIR: Final[Path] = Path("audit")
LOGS_DIR: Final[Path] = Path("logs")
RETROSPECTIVES_DIR: Final[Path] = Path("retrospectives")
REQUESTS_DIR: Final[Path] = Path("requests")
EXAMPLES_DIR: Final[Path] = Path("examples")
RATE_LIMIT_LOG: Final[Path] = Path("rate-limit-log.yaml")
DAEMON_PID_FILE: Final[Path] = Path("daemon.pid")

# Task-folder file names.
SPEC_FILE: Final[str] = "spec.md"
IMPLEMENTATION_FILE: Final[str] = "implementation.md"
TESTS_FILE: Final[str] = "tests.md"
REVIEW_FILE: Final[str] = "review.md"
UI_REVIEW_FILE: Final[str] = "ui-review.md"
STATUS_FILE: Final[str] = "status.yaml"
```

No functions. No classes. If another module needs a constant, it imports from here.

### `orchestrator/paths.py`

```python
"""Path resolution. The single home for `.huragok/` discovery and
task-folder path composition. Every other module asks this one."""
```

Required public API:

- `find_huragok_root(start: Path | None = None) -> Path` — walks up from `start` (defaulting to `Path.cwd()`) to find the nearest parent directory containing a `.huragok/` subdirectory. Returns the parent (the "repo root"), not the `.huragok/` directory itself. Raises `HuragokNotFoundError` if none is found by the time it hits the filesystem root. Uses `Path.resolve()` to handle symlinks.
- `huragok_dir(root: Path) -> Path` — returns `root / ".huragok"`.
- `task_dir(root: Path, task_id: str) -> Path` — returns the path for a task's folder (e.g. `root/.huragok/work/task-0042/`). Does not check existence.
- `state_file(root: Path) -> Path`
- `batch_file(root: Path) -> Path`
- `decisions_file(root: Path) -> Path`
- `audit_log(root: Path, batch_id: str) -> Path` — returns `root/.huragok/audit/<batch_id>.jsonl`.
- `batch_log(root: Path, batch_id: str) -> Path` — returns `root/.huragok/logs/batch-<batch_id>.jsonl`.
- `rate_limit_log(root: Path) -> Path`
- `daemon_pid_file(root: Path) -> Path`
- `requests_dir(root: Path) -> Path`

Define `class HuragokNotFoundError(Exception)` in this module.

All functions return `Path`, never `str`. No function touches the filesystem except `find_huragok_root`.

### `orchestrator/state/schemas.py`

Pydantic v2 models. One model per schema described in ADR-0002 D3. Plus the markdown frontmatter schema.

Use `pydantic.BaseModel` with `model_config = ConfigDict(extra="forbid")` on every model — unknown fields are errors, not silently accepted. Use `datetime` for timestamp fields (aware, UTC). Use `Literal` types for enumerated state fields so a typo in `phase` is caught at validation time.

**Models to define (exact names):**

- `BudgetConsumed` — `wall_clock_seconds: float`, `tokens_input: int`, `tokens_output: int`, `tokens_cache_read: int`, `tokens_cache_write: int`, `dollars: float`, `iterations: int`. All default to 0.
- `SessionBudget` — `remaining_tokens: int | None`, `remaining_dollars: float | None`, `timeout_seconds: int | None`. All default to None.
- `AwaitingReply` — `notification_id: str | None`, `sent_at: datetime | None`, `kind: Literal["foundational-gate", "budget-threshold", "blocker", "batch-complete", "error", "rate-limit"] | None`, `deadline: datetime | None`. All default to None.
- `StateFile` — exactly matches the ADR-0002 D3 state.yaml schema. The `phase` field is `Literal["idle", "running", "paused", "halted", "complete"]`. The `current_agent` field is `Literal["architect", "implementer", "testwriter", "critic", "documenter"] | None` (note: no "orchestrator" entry, per ADR-0003 D7). Required top-level field: `version: int` (must equal `SCHEMA_VERSION`). Validator: reject if `version != SCHEMA_VERSION`, with a clear error message naming the expected version.
- `BatchBudgets` — `wall_clock_hours: float`, `max_tokens: int`, `max_dollars: float`, `max_iterations: int`, `session_timeout_minutes: int`. No defaults; all required.
- `BatchNotifications` — `telegram_chat_id: str | None = None`, `warn_threshold_pct: int = 80`.
- `TaskEntry` — `id: str`, `title: str`, `kind: Literal["backend", "frontend", "fullstack", "docs"]`, `priority: int`, `acceptance_criteria: list[str]`, `depends_on: list[str] = []`, `foundational: bool = False`.
- `BatchFile` — `version: int`, `batch_id: str`, `created: datetime`, `description: str`, `budgets: BatchBudgets`, `notifications: BatchNotifications`, `tasks: list[TaskEntry]`. Same version validator as StateFile.
- `HistoryEntry` — `at: datetime`, `from_: str` (alias `from` via `Field(alias="from")`), `to: str`, `by: str`, `session_id: str | None`.
- `UIReview` — `required: bool = False`, `screenshots: list[str] = []`, `preview_url: str | None = None`, `resolved: Literal["approved", "rejected"] | None = None`.
- `StatusFile` — `version: int`, `task_id: str`, `state: Literal["pending", "speccing", "implementing", "testing", "reviewing", "software-complete", "awaiting-human", "done", "blocked"]`, `foundational: bool = False`, `history: list[HistoryEntry] = []`, `blockers: list[str] = []`, `ui_review: UIReview`. Same version validator.
- `ArtifactFrontmatter` — `task_id: str`, `author_agent: Literal["architect", "implementer", "testwriter", "critic", "documenter"]`, `written_at: datetime`, `session_id: str`.

For all models where `from` is used as a field name, use Pydantic's `Field(alias="from")` pattern and set `model_config = ConfigDict(populate_by_name=True, extra="forbid")` so the model can be constructed from either the Python-safe name or the YAML alias.

**HistoryEntry validation note:** the `by` field is a role or the special value `supervisor`. Don't over-specify — accept any string for now; ADR-0003 may evolve the allowed roles and we don't want the schema to lag.

### `orchestrator/state/io.py`

Read, write, append. Atomic per ADR-0002 D3.

Required public API:

- `read_state(root: Path) -> StateFile`
- `write_state(root: Path, state: StateFile) -> None`
- `read_batch(root: Path) -> BatchFile`
- `write_batch(root: Path, batch: BatchFile) -> None`
- `read_status(root: Path, task_id: str) -> StatusFile`
- `write_status(root: Path, status: StatusFile) -> None`
- `read_artifact(path: Path) -> tuple[ArtifactFrontmatter, str]` — reads a markdown artifact file, splits the YAML frontmatter from the body. Returns the parsed frontmatter and the body as a string. Raises `ArtifactFormatError` if frontmatter is absent or malformed.
- `append_decisions(root: Path, block: str) -> None` — opens `decisions.md` with `O_APPEND`, writes `block`, followed by a blank line. Single `write()` call for the whole payload to minimize interleaving with concurrent appenders.
- `append_audit(root: Path, batch_id: str, event: dict) -> None` — appends one JSON line to the per-batch audit file. Serializes with `json.dumps(event, sort_keys=True, default=str)` followed by `\n`. Creates `audit/` directory if missing.
- `cleanup_stale_tmp(root: Path) -> int` — walks `.huragok/` for `*.tmp.*.*` files and deletes any where the embedded PID is not a live process. Returns count deleted. Called at daemon startup in Slice B; ship it now so we're not retrofitting later.

Define two exceptions in this module: `AtomicWriteError(IOError)` and `ArtifactFormatError(ValueError)`.

**Atomic write implementation** (this is the critical function; it's used by `write_state`, `write_batch`, `write_status`, and the batch YAML used in `BatchFile` writes):

```python
def _atomic_write_yaml(target: Path, payload: dict) -> None:
    """Write `payload` as YAML to `target` atomically.

    Steps:
      1. Render YAML to bytes in memory.
      2. Create a temp file at `target.with_suffix(f".tmp.{pid}.{uuid}")`.
      3. Write bytes, fsync the file, close.
      4. os.rename(temp, target) — POSIX-atomic.
      5. fsync the containing directory.

    A kill -9 at any step leaves either the old file or the fully-written
    new file; never a partial write.
    """
```

Use `yaml.safe_dump(..., sort_keys=False, default_flow_style=False, allow_unicode=True)`. Round-trip via `yaml.safe_load`. Top-level keys must preserve the order from the Pydantic model — use `model.model_dump(mode="json", by_alias=True)` then feed into `safe_dump`.

**Frontmatter parsing** for `read_artifact`:

```
---
<yaml>
---
<markdown body>
```

The frontmatter delimiters are exactly `---` on their own line. Anything else is `ArtifactFormatError`. Parse the middle section with `yaml.safe_load`, validate it with `ArtifactFrontmatter(**parsed)`, return that plus the body (everything after the closing `---\n`).

**Don't** implement file locking. Don't implement `fcntl.flock`. The atomic-rename protocol is the only concurrency primitive at the file layer; locking is a Slice B concern if it's needed at all.

### `orchestrator/config.py`

Settings loaded from env and `.env`.

```python
"""Runtime configuration. Loaded once at process start from .env plus
process environment. Never mutated after load."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class HuragokSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="HURAGOK_",
        case_sensitive=False,
        extra="ignore",
    )

    # Required for Slice B; optional in Slice A so the CLI can run
    # against fixtures without an API key.
    anthropic_api_key: SecretStr | None = None
    anthropic_admin_api_key: SecretStr | None = None  # ADR-0002 D4, optional
    telegram_bot_token: SecretStr | None = None
    telegram_default_chat_id: str | None = None

    # Logging.
    log_level: str = "info"  # debug | info | warn | error | critical

    # Operational.
    data_dir: str | None = None  # override .huragok/ root discovery (testing)


def load_settings() -> HuragokSettings:
    """Load settings from environment; cached so all callers get the same instance."""
```

`load_settings` should use `functools.cache`. Env vars without the `HURAGOK_` prefix are the exceptions: `ANTHROPIC_API_KEY` and `ANTHROPIC_ADMIN_API_KEY` (read directly) and `TELEGRAM_BOT_TOKEN` (read directly). To support this, override those three fields with explicit `Field(validation_alias=...)` so `pydantic-settings` reads them without the prefix. Hint: `AliasChoices` lets you accept multiple env var names per field.

Create `.env.example` at the repo root documenting every variable with a one-line comment. Include a header noting that this file is for human reference and `.env` should never be committed.

### `orchestrator/logging_setup.py`

```python
"""Configure structlog per ADR-0002 D9. Call once at process start."""

def configure_logging(level: str = "info", json_output: bool = True) -> None:
    """Configure structlog with JSON output at the given level.

    When json_output=True (production), records are emitted as one
    JSON object per line. When False (development), use a human-readable
    console renderer instead.

    Fields injected into every record: ts (ISO-8601 UTC), level, component
    (set via structlog.contextvars per caller), plus whatever kwargs the
    caller passes.
    """
```

Implementation: route structlog through a single JSON renderer in prod or structlog's `ConsoleRenderer` in dev. Timestamps as ISO-8601 UTC with microseconds. The `component` field is set per-caller via `structlog.contextvars.bind_contextvars(component="cli")` (for example) at module entry.

Do not configure any file handlers. Output is stdout only. The batch log file redirection in ADR-0002 D9 is a Slice B concern.

### `orchestrator/state/__init__.py`

Re-export the public API:

```python
from orchestrator.state.io import (
    read_state, write_state,
    read_batch, write_batch,
    read_status, write_status,
    read_artifact,
    append_decisions, append_audit,
    cleanup_stale_tmp,
    AtomicWriteError, ArtifactFormatError,
)
from orchestrator.state.schemas import (
    StateFile, BatchFile, StatusFile,
    TaskEntry, HistoryEntry, UIReview,
    BudgetConsumed, SessionBudget, AwaitingReply,
    BatchBudgets, BatchNotifications,
    ArtifactFrontmatter,
)

__all__ = [...]  # populate with everything above
```

### `orchestrator/cli.py`

Typer application. Entry point is `app`.

```python
"""Huragok CLI. Slice A implements status, tasks, show (all read-only).
The rest are registered as Slice-B stubs that exit 1."""

import typer

app = typer.Typer(
    name="huragok",
    help="Autonomous multi-agent development orchestration for Claude Code.",
    no_args_is_help=True,
)
```

Commands to implement fully in this slice:

- `huragok status [--json]` — reads `.huragok/state.yaml` and renders the status view from ADR-0002 D9. When `--json` is passed, emit the raw `StateFile.model_dump(mode="json")` as JSON to stdout. Otherwise, render the human-readable box shown in the ADR using `rich`. Percentages in the human view are computed from `budget_consumed` vs. `batch.yaml.budgets`; if no batch is active, render "idle — no batch in flight". When `phase == "paused"`, include `halted_reason` in the header.
- `huragok tasks [--state STATE]` — reads `batch.yaml` and each matching `status.yaml`, prints a table (columns: ID, State, Kind, Priority, Title). Filtering via `--state` matches the `status.yaml.state` field. When no batch is active, print "no batch in flight" and exit 0.
- `huragok show TASK_ID [--full]` — renders the summary from Slice A planning. `--full` additionally inlines the body (stripped of frontmatter) of every artifact present in the task folder, each under a `## <filename>` heading. Use `rich` for formatting.

Commands to stub (exit 1 with a clear message):

- `run`, `start`, `stop`, `halt`, `reply`, `submit`, `logs`

Each stub:

```python
@app.command()
def run() -> None:
    """Start the orchestrator daemon (Slice B — not yet implemented)."""
    typer.secho("huragok run: not implemented until Slice B", err=True, fg=typer.colors.RED)
    raise typer.Exit(1)
```

All commands call `find_huragok_root()` first, via a helper function, and handle `HuragokNotFoundError` with a clean error message and exit 1. They all call `configure_logging()` once, reading the level from `load_settings().log_level`.

### `orchestrator/__init__.py`

```python
"""Huragok — autonomous multi-agent development orchestration for Claude Code."""

__version__ = "0.1.0"
```

---

## Tests

Place tests in:

```
tests/
├── __init__.py
├── conftest.py
├── test_paths.py
├── test_cli.py
└── state/
    ├── __init__.py
    ├── fixtures/
    │   ├── state_valid.yaml
    │   ├── state_wrong_version.yaml
    │   ├── state_unknown_phase.yaml
    │   ├── batch_valid.yaml
    │   ├── batch_missing_required.yaml
    │   ├── status_done.yaml
    │   ├── status_blocked.yaml
    │   ├── artifact_valid.md
    │   └── artifact_no_frontmatter.md
    ├── test_schemas.py
    └── test_io.py
```

Fixtures are real YAML / markdown content, checked in to the repo.

**`conftest.py`** provides:

- `tmp_huragok_root` — fixture that yields a `Path` to a temp directory with a pre-populated `.huragok/` subtree copied from `tests/state/fixtures/` by way of a skeleton layout. Uses `tmp_path`. Includes the full `examples/task-example/` copied from the real repo so `show task-example` works.

**`test_paths.py`** — one test per public function in `paths.py`. Key tests:
- `find_huragok_root` walks up correctly from a nested subdirectory.
- `find_huragok_root` raises `HuragokNotFoundError` when no ancestor has `.huragok/`.
- `find_huragok_root` stops at filesystem root (doesn't loop forever on an unrooted path).
- `task_dir`, `state_file`, etc. produce expected paths given a known root.

**`test_schemas.py`** — one test per model at minimum, plus:
- Every example YAML in the real `.huragok/examples/task-example/` parses cleanly.
- `state_wrong_version.yaml` raises a ValidationError with a message mentioning the expected version number.
- `state_unknown_phase.yaml` raises a ValidationError naming the invalid phase.
- `batch_missing_required.yaml` raises a ValidationError listing the missing fields.
- Extra fields raise a ValidationError (tests the `extra="forbid"` behavior).
- Round-trip: `StateFile(...).model_dump(mode="json")` → `yaml.safe_dump` → `yaml.safe_load` → `StateFile(**...)` is the identity.

**`test_io.py`** — the atomic-write and round-trip tests:
- Round-trip for `StateFile`, `BatchFile`, `StatusFile`.
- `_atomic_write_yaml` on a target that exists: old content is fully replaced, new content is present.
- `_atomic_write_yaml` stale-tmp simulation: manually create a `.tmp.99999.uuid` file (dead PID), call `cleanup_stale_tmp`, assert it was deleted. Also test that a tmp file whose PID IS live (use `os.getpid()`) is NOT deleted.
- `read_artifact` on `artifact_valid.md` returns expected frontmatter and body.
- `read_artifact` on `artifact_no_frontmatter.md` raises `ArtifactFormatError`.
- `append_decisions` adds to the existing file without truncating.
- `append_audit` creates the `audit/` directory on first call.
- `append_audit` writes exactly one line per call, newline-terminated.

**Crash-simulation for atomic write** (this is the important one): use `monkeypatch` on `os.rename` to raise `OSError` mid-call. Assert that the target file still contains its original content (no partial write observable) and that a `.tmp.*.*` file exists in the directory afterward (to be cleaned up by `cleanup_stale_tmp`).

**`test_cli.py`** — CLI tests using `typer.testing.CliRunner`:
- `huragok status` against a fixture repo produces the expected human output (assert key substrings: `batch-`, `Elapsed:`, `Tokens:`).
- `huragok status --json` produces valid JSON matching the `StateFile` schema.
- `huragok status` in a directory with no `.huragok/` ancestor produces a clear error on stderr and exits 1.
- `huragok tasks` lists task IDs from the fixture batch.
- `huragok tasks --state done` filters correctly.
- `huragok show task-example` produces output containing the task title.
- `huragok show task-example --full` contains the word "healthz" (from the example spec.md body).
- `huragok show nonexistent-task` produces a clear error and exits 1.
- `huragok run` exits 1 with the stub message.

---

## Conventions

- **Line length 100.** Per the existing `pyproject.toml` ruff config.
- **Type hints on every function signature, including return types.** No `Any` without explicit rationale in a comment.
- **Docstrings on every public function and class.** One-line summary required; body optional. Internal helpers (leading underscore) can skip docstrings if the name is self-explanatory.
- **No `print()` in library code.** Use structlog everywhere except the CLI's user-facing output (rich + typer.echo are fine there).
- **No catching bare `Exception`.** Always name the specific exception type.
- **No `# type: ignore`.** If mypy complains, fix the types properly.
- **No TODO comments.** If something is deferred, raise a `NotImplementedError` with a descriptive message, or leave it out of this slice entirely. TODOs rot.

Run `uv run ruff check .` and `uv run ruff format .` before declaring the slice done. All tests must pass via `uv run pytest`.

---

## Deliverable: `implementation.md` equivalent

Because Huragok isn't running against itself yet (that's Phase 2), there's no `.huragok/work/` folder for this work. Instead, when you're done, write a file at `docs/notes/slice-a-build-notes.md` with:

- Summary: what shipped.
- Module-by-module notes: any non-obvious choice you made, especially if you deviated from the spec above (deviations should be zero, but if you had to, say why).
- Tests: pass count, any skipped tests and why.
- Known issues: things that work but have rough edges, or Slice-B assumptions you needed to stub.

This file replaces an `implementation.md` artifact for this out-of-band work and will be reviewed alongside the code before commit.

---

## Stop conditions

Stop and ask the operator before proceeding if any of these occur:

- An ADR says something contradictory to this prompt.
- A module needs a dependency not listed above.
- A schema field in the ADRs doesn't have an obvious Pydantic type (e.g. something we'd need a custom validator for beyond what's specified).
- You find a bug in an existing file (e.g. `CLAUDE.md` references something incorrectly, or an ADR has a typo affecting implementation).
- You can't get tests to pass cleanly and the reason is structural rather than a simple bug.

Otherwise: build it. Run the tests. Format the code. Write the notes. Report back with a summary of what shipped and any surprises.
