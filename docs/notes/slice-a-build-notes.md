# Slice A — Synchronous Foundation: Build Notes

**Date:** 2026-04-21
**Author:** Claude Code (Opus 4.7, 1M context)
**Scope:** Phase 1 MVP, Slice A (no asyncio, subprocess, network, signals, systemd).
**Spec:** the full Slice A prompt shipped with this session.
**Reviewed against:** ADR-0001, ADR-0002, ADR-0003, `.huragok/examples/task-example/`.

## Summary

Shipped the synchronous foundation of the Huragok orchestrator:

- Runtime dependencies (`pydantic`, `pydantic-settings`, `pyyaml`, `structlog`, `typer`, `rich`) added to `pyproject.toml` and the `huragok` CLI entrypoint registered against `orchestrator.cli:app`.
- `orchestrator/` package with `__init__.py`, `constants.py`, `paths.py`, `config.py`, `logging_setup.py`, a `state/` sub-package (`schemas.py`, `io.py`, re-exporting `__init__.py`), and `cli.py`.
- Pydantic v2 models covering every schema in ADR-0002 D3 (`StateFile`, `BatchFile`, `StatusFile`, `ArtifactFrontmatter`, and every leaf model) with `extra="forbid"` and version validators.
- POSIX-atomic write protocol (`_atomic_write_yaml`) for `state.yaml` / `batch.yaml` / `status.yaml`, append-only helpers for `decisions.md` and per-batch audit JSONL, and a stale-tmp sweeper keyed off process liveness.
- `huragok` CLI with three real commands (`status`, `tasks`, `show`) rendering the ADR-0002 D9 status view, a filterable task table, and per-task summary/artifact dump. Seven Slice-B commands (`run`, `start`, `stop`, `halt`, `reply`, `submit`, `logs`) registered as stubs that exit 1 with a clear message.
- Test suite under `tests/` mirroring the package layout; **83 tests passing in 0.5 s**. Ruff lint and format are clean.
- `.env.example` documenting every variable loaded by `HuragokSettings`.

## Module-by-module notes

### `constants.py`
Straight transcription of the prompt. No surprises.

### `paths.py`
Straight transcription. `find_huragok_root` resolves the start path once via `Path.resolve()` so symlinks don't confuse the walk; the termination check is `current.parent == current`, which is the POSIX-safe way to detect the filesystem root without assumptions about path shape.

### `config.py`
`HuragokSettings` uses `SettingsConfigDict(env_prefix="HURAGOK_")` as the default. The three "external token" fields (`anthropic_api_key`, `anthropic_admin_api_key`, `telegram_bot_token`) are overridden with `Field(validation_alias="ANTHROPIC_API_KEY", ...)` etc., which disables prefix-derivation for those specific fields — pydantic-settings reads them from the unprefixed env var only. `AliasChoices` was considered for "accept either prefixed or unprefixed" but the prompt is unambiguous that they are read directly, so a single-value alias is what shipped.

`load_settings()` is wrapped with `@functools.cache`. The test `conftest.py` clears the cache in an autouse fixture so env overrides in one test don't leak into another.

### `logging_setup.py`
Single entry point `configure_logging(level, json_output)`. JSON rendering for prod, `ConsoleRenderer` for dev. Timestamps ride on `TimeStamper(fmt="iso", utc=True, key="ts")` to match the ADR-0002 D9 field name. Logs go to stdout per the ADR; the CLI commands emit no log records in Slice A, so this is zero-cost configuration that Slice B will inherit without changes.

The ADR mentions mirroring to `.huragok/logs/batch-<batch_id>.jsonl`; that's explicitly a Slice B concern per the prompt and is not implemented here.

### `state/schemas.py`

- Every model carries `model_config = ConfigDict(extra="forbid")`. `StateFile`, `BatchFile`, `StatusFile` each have a `@field_validator("version")` that rejects anything other than `SCHEMA_VERSION` (currently `1`) with a message naming the expected version.
- `HistoryEntry` uses `populate_by_name=True` and `Field(alias="from")` on `from_`, so YAML can write `from:` (the idiomatic key) while Python code uses `from_`. Round-trip tests confirm the serializer emits `from:` when `by_alias=True`.
- `current_agent` on `StateFile` is `Literal[...]` over the five agents from ADR-0003 — **not** including `"orchestrator"`, per ADR-0003 D7 and the prompt's explicit callout. ADR-0002 D3's YAML comment still lists "orchestrator" in its example; that's a stale comment in the ADR that doesn't affect the schema.
- `HistoryEntry.by` is `str`, deliberately loose, as the prompt dictates. The real `task-example` uses `"supervisor"` and role names; the schema accepts both without enumeration drift.
- `StateFile.pending_notifications` is typed `list[dict[str, Any]]`. The notification queue-item shape is a Slice B concern; marking the items opaque keeps the schema honest without pre-designing the dispatcher. `Any` is used with a module-level comment explaining the scope boundary.

### `state/io.py`

- `_atomic_write_yaml(target, payload)` implements the ADR-0002 D3 protocol exactly: render YAML to bytes; `os.open(tmp, O_WRONLY|O_CREAT|O_EXCL)` so a stale tmp won't be overwritten silently; `os.write` + `os.fsync`; `os.rename`; directory `fsync`. Tmp filename is `<target>.tmp.<pid>.<uuid>` using `target.parent / f"{target.name}..."` rather than `Path.with_suffix()` — more predictable when a target has multiple dots.
- On any `OSError` during create/write/rename the function raises `AtomicWriteError` without cleaning up the tmp file. That's intentional: the crash-simulation test expects the tmp to survive, and a live daemon would (after the exception) call `cleanup_stale_tmp` on next start. Eagerly unlinking would defeat the stale-sweeper design.
- `cleanup_stale_tmp(root)` uses `rglob("*.tmp.*.*")` to find temp files, parses the PID out of the filename, and calls `os.kill(pid, 0)` to check liveness. `PermissionError` is treated as "live but owned by someone else" → keep. A test uses `os.getpid()` to assert live-PID tmps are preserved; another walks from near-max PID to find a definitely-dead PID without race conditions.
- `read_artifact` splits on the literal `---` delimiter lines (leading and closing). Malformed frontmatter, an absent delimiter, or a non-mapping YAML body all raise `ArtifactFormatError`. Pydantic's `ValidationError` is caught and re-raised as `ArtifactFormatError` so callers have one exception type to handle.
- `append_decisions` and `append_audit` use `O_APPEND | O_CREAT`, write a single block per call (minimizes interleaving with concurrent appenders), and auto-create parent directories. `append_audit` serializes with `json.dumps(..., sort_keys=True, default=str)` so `datetime` instances pass through cleanly.

### `cli.py`

- Typer app with `no_args_is_help=True` so bare `huragok` prints help instead of silently doing nothing.
- `status`, `tasks`, `show` are real commands; the remainder call a shared `_stub(name)` helper that emits the canonical Slice-B placeholder to stderr and raises `typer.Exit(1)`. Argument signatures for `reply`, `submit`, `logs` mirror their Slice-B shapes so migration is a body-swap rather than a reshape.
- The status view is rendered from `state.yaml` + `batch.yaml` + every `work/<task-id>/status.yaml`. Token, wall-clock, and dollar percentages come from `budget_consumed` vs. `batch.budgets`. When no batch is on disk, the view prints `idle — no batch in flight` per the prompt. When `phase=="paused"` and `halted_reason` is set, the header shows `paused — <reason>`. The rendered output matches the ADR-0002 D9 layout closely; the "sessions launched, N clean, M retry" breakdown is deferred to Slice B (requires audit-log analysis).
- `tasks` uses a `rich.Table`. Tasks without a `status.yaml` on disk are implicitly `pending` — matches the state machine's initial state from ADR-0003 D1. `--state <state>` filters purely on the `status.yaml.state` field (or the implicit `pending` fallback).
- `show TASK_ID` extracts the human title from the first `# Heading` in `spec.md`'s body, then prints state/foundational/blockers/ui_review/artifact-list. `--full` inlines every present artifact body under a `## <filename>` heading. Missing artifacts are silently skipped, malformed ones produce a stderr warning but don't fail the command.
- All commands call `_resolve_root()` (which maps `HuragokNotFoundError` to an exit-1 error message) and `_init_logging()` (which loads settings and binds `component="cli"` context).

## Tests

**83 passed, 0 skipped, 0 failures.**

- `tests/test_paths.py` — 16 tests covering every public helper, walk-up behavior (nested, default-cwd, filesystem-root stop), and the "not a huragok repo" failure mode.
- `tests/state/test_schemas.py` — 26 tests. One per model (happy-path construction), defaults for every optional field, validator enforcement (version, phase, missing required, forbidden extras), `HistoryEntry` alias round-trip, Python→YAML→Python identity for all three file types, and a separately-parameterized test that ensures every artifact in the real `.huragok/examples/task-example/` folder parses against the schemas.
- `tests/state/test_io.py` — 24 tests. Round-trips for `StateFile`/`BatchFile`/`StatusFile`; write-status emits `from:` (alias), not `from_:`; atomic-write replace-existing and fresh-file cases; **crash-simulation via `monkeypatch.setattr("os.rename", raise)`** verifying the target is untouched and a tmp remains; a second failure test for `os.write`; stale-tmp sweep covering dead-PID, live-PID, ignore-non-tmp, and missing-`.huragok/`; `read_artifact` happy path and three failure modes; `append_decisions` preserving prefix and multi-block ordering; `append_audit` creating the directory, newline-terminating each line, and serialising datetimes through `default=str`; missing-file errors for `read_state`/`read_batch`.
- `tests/test_cli.py` — 17 tests. `status` human output (asserts `batch-001`, `Elapsed:`, `Tokens:`, `Dollars:`, `Tasks:` substrings) and `--json` output (parses and checks key fields); "outside huragok repo" produces an error on stderr and exits 1; `tasks` lists all IDs, filters by `done` (task-example only) and by `pending` (task-0001 only), and handles an empty batch; `show task-example` produces the task title, `--full` inlines `## spec.md` / `## implementation.md` and contains the word `healthz`; `show nonexistent-task` exits 1; `run` exits 1 with the stub message; parametrized over every other Slice-B stub.

No tests skipped. Every acceptance-criterion listed in the spec's "Tests" section maps to at least one test above.

**Tooling:**
- `uv run ruff check .` → `All checks passed!`
- `uv run ruff format --check .` → `16 files already formatted`
- `uv sync` completed without deprecation warnings.

## Deviations from the spec

**None intended.** Places where the spec left a judgment call rather than dictating:

1. **Tmp file naming.** The spec says `.huragok/<path>.tmp.<pid>.<uuid>`. I used `Path.parent / f"{Path.name}.tmp.<pid>.<uuid>"` rather than `Path.with_suffix(...)` because `with_suffix` replaces the last suffix; for a target like `state.yaml` both produce the same result, but the former is more obviously correct if a target ever has a dotted name. Matches the `*.tmp.*.*` glob the sweeper uses.
2. **`pending_notifications` item type.** Spec leaves it at `[]` in the YAML. I chose `list[dict[str, Any]]` because `Any` inside a list is the most honest way to say "shape TBD in Slice B" while still letting Pydantic validate the outer container. The field is documented inline with a rationale comment.
3. **CLI status sessions breakdown.** The ADR-0002 D9 example shows `"7 launched, 6 clean, 1 retry"`. I render only the launch count (`"3 launched"`) in Slice A because the clean/retry split requires audit-log analysis which is Slice B. The test asserts substrings, not the full line.
4. **Ruff formatter rewrite.** After `ruff format .`, a few lines were rewrapped (notably in `orchestrator/cli.py` and the longer test names). No behavioural change; just style normalisation.

## Known issues and Slice-B placeholders

- **Logging destination is stdout.** Matches ADR-0002 D9. The CLI doesn't emit log records in Slice A so stdout stays clean for `status --json` / `tasks` / `show` consumers. When Slice B wires in the daemon, the batch-log file mirror (`.huragok/logs/batch-<batch_id>.jsonl`) will be added; keep the existing `configure_logging(level, json_output)` signature and add a file-sink processor.
- **`huragok status` against a scaffolded-but-empty `state.yaml`.** The checked-in `.huragok/state.yaml` is a human-annotated stub with `phase: idle, batch_id: null` and no `version:` field. Running `huragok status` against that file today raises a `ValidationError` (no `version` field). Slice B will populate a valid `state.yaml` on daemon start. If operators need the CLI to be friendly against a pre-daemon repo, one option is a `huragok init` command that writes a valid default `state.yaml` — that's an ADR-0007 concern per ADR-0001 D10.
- **`BatchFile` strictness.** The checked-in `.huragok/batch.yaml` is also a stub (just `tasks: []`), so `read_batch` against a fresh-init repo fails. Same Slice B fix: the daemon writes a valid stub on start, or `huragok submit` writes a validated batch.
- **No path-scoped write enforcement on agents.** Per ADR-0003 D3, path scoping for agent writes is prompt-only in Phase 1; audit-log review catches drift post-hoc. Slice A doesn't touch this.
- **No file locking.** Deliberately, per the spec ("Don't implement file locking"). The atomic-rename protocol is the only concurrency primitive at the file layer. If Slice B's supervisor ever grows a need for locks, this is where it would hook in.
- **`cleanup_stale_tmp` is unexported from the CLI.** It's in `orchestrator.state.cleanup_stale_tmp` and the Slice B daemon will call it at startup per ADR-0002 D3. A `huragok clean` command could expose it manually — not in scope for Slice A.
- **`huragok show` title extraction.** Parses the first `# ` line from `spec.md`'s body. If the spec is missing or has no leading H1, the title line is omitted. Matches the Open Question in ADR-0002 D9 about which summary fields to render; behaviour can be refined in a follow-up PR.

## What's next for Slice B

The shape of the synchronous foundation was chosen to make Slice B an additive extension:

- `orchestrator/session/` (asyncio subprocess runners) will import `read_state`/`write_state` and use `append_audit` for status-transition events.
- `orchestrator/budget/` will mutate `state.budget_consumed` via `write_state` — all schema enforcement already in place.
- `orchestrator/notifications/` can populate `state.pending_notifications` and `state.awaiting_reply`; the models are ready.
- `orchestrator/supervisor/` wires the above together and replaces the CLI stubs in `cli.py` one at a time.

The filesystem-based CLI↔daemon contract (ADR-0002 D5) is likewise Slice B's problem; `requests_dir(root)` is ready to be written to.
