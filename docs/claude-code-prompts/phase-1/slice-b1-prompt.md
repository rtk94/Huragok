# Huragok Phase 1 — Slice B1: Daemon Core

You are building the core of the Huragok orchestrator daemon — the asyncio event loop, subprocess session runners, and budget accounting. This is the first of two sub-slices in Slice B. B1 ships the daemon that can launch, observe, and account for Claude Code sessions. B2 (later, separate session) adds Telegram notifications, the full error taxonomy, the systemd unit, and the remaining CLI commands.

Read this entire prompt before starting. Read the ADRs it names. If anything here contradicts an ADR, STOP and ask — the ADRs are authoritative.

---

## Read first, in this order

1. `CLAUDE.md` at repo root.
2. `docs/adr/ADR-0001-huragok-orchestration.md`.
3. `docs/adr/ADR-0002-orchestrator-daemon-internals.md`. **This is the authoritative spec for everything you build in B1.** Decisions D1 (process model), D2 (session invocation pipeline), and D4 (budget accounting) are the most directly implemented here. D5 (CLI) is partially promoted in this slice. D7 (error taxonomy) is stubbed in B1 — only the clean/dirty distinction needed for session-end detection. D9 (observability) — audit events are emitted in B1; the status view's sessions breakdown is B2.
4. `docs/adr/ADR-0003-agent-definitions.md` — for context on what the daemon is orchestrating. You are NOT editing agent files.
5. `docs/notes/slice-a-build-notes.md` — what Slice A shipped, its known-issues list, and the Slice B handoff notes.
6. Skim `orchestrator/` and `tests/` to see what exists.

Do not modify any file under `orchestrator/state/`, `orchestrator/config.py`, `orchestrator/constants.py`, `orchestrator/paths.py`, `orchestrator/logging_setup.py`, or `.claude/agents/`. These are Slice A foundations that B1 consumes; if you need a change to them, STOP and ask.

---

## Scope boundaries for B1

**In scope:**

- `orchestrator/session/` package — subprocess spawning, stream-json parsing, session-end detection, timeout enforcement, per-task retry counter
- `orchestrator/budget/` package — per-event token/dollar accounting, pricing table loader, wall-clock tracker, rate-limit log (with 7-day truncation on startup), Cost API reconciliation (optional, activated when `ANTHROPIC_ADMIN_API_KEY` is set)
- `orchestrator/supervisor/` package — asyncio event loop, signal handler, state machine driver, request-file ingestion, role-to-agent mapping per ADR-0003 D1, `sd_notify` (stub if systemd unavailable)
- `orchestrator/notifications/` package — interface only. A `NotificationDispatcher` base class with a `LoggingDispatcher` implementation that writes notifications to the structured log instead of sending them anywhere. No Telegram. No HTTP. B2 replaces this with a real dispatcher.
- `orchestrator/pricing.yaml` — the initial pricing table per ADR-0002 D4
- Promoted CLI commands in `orchestrator/cli.py`:
  - `huragok run` — real; launches the daemon in the foreground
  - `huragok stop` — real; sends SIGUSR1 + writes request file to trigger graceful shutdown
  - `huragok halt` — real; writes request file to halt after in-flight session
- Tests under `tests/`
- A deterministic fake-claude script at `tests/fixtures/fake-claude.sh` that emits canned stream-json, used by session-runner tests
- Update `docs/notes/` with a B1 build-notes file

**Explicitly NOT in scope — do NOT build:**

- Any Telegram code. No `httpx` Telegram client. No `getUpdates` polling. No bot-token usage. If `TELEGRAM_BOT_TOKEN` is set in `.env`, ignore it; B1 reads it zero times.
- The full D7 error taxonomy beyond what's needed for session-end classification. Specifically B1 needs:
  - `clean-end` — subprocess exit 0 with `result` event received
  - `dirty-end` — anything else
  - `session-timeout` — wall-clock timeout triggered SIGTERM
  - A stub for `rate-limited` (detected but not fully handled — log and treat as dirty-end for now)
  - The full seven-category taxonomy is B2's job.
- `huragok reply` / `huragok submit` / `huragok logs` — B2 promotes these
- `scripts/systemd/huragok.service` — B2 ships this
- Updated "sessions breakdown" line in `huragok status` — B2 adds audit-log analysis for this
- An implementation of D6 beyond the stubbed `LoggingDispatcher`
- Any real `claude -p` invocation in test code. Tests invoke `tests/fixtures/fake-claude.sh` exclusively.
- Parallel session execution. ADR-0005. Phase 1 is strictly sequential.

If you find yourself reaching for any of those, stop. Scope creep here contaminates B2.

---

## Dependencies

Add to `pyproject.toml` under `[project]` `dependencies`:

```toml
dependencies = [
    # ... existing Slice A deps ...
    "httpx>=0.27",        # for Cost API (optional path) and B2 Telegram
    "uuid-v7>=0.2",       # UUIDv7 for session_id and notification_id
]
```

Everything else is already installed from Slice A. Do not add any other deps without stopping to ask.

Dev deps unchanged.

After your changes, `uv sync` should complete with no warnings.

---

## Design guidance, not prescription

Unlike Slice A, B1 leaves most design choices to you. Some rails, though:

**Process model (ADR-0002 D1).** Single `asyncio` event loop. Long-lived coroutines for Supervisor, Budget Tracker, and Notification Dispatcher. Short-lived coroutines for Session Runner. Communication via `asyncio.Queue`, not shared mutable state. This is a hard rail.

**Stream-json parsing.** Parse line-by-line. Dispatch on `type` field. The canonical types for B1 are `system`, `assistant`, `user`, `result`. Unknown types log at INFO and are ignored. The parser's internal shape — dataclass event hierarchy? dict-based? state machine? — is your call, but it must be testable against the fake-claude fixture.

**Session lifecycle.** A Session Runner coroutine is responsible for one subprocess from spawn through exit. It parses the stream, emits events to the Budget Tracker, records session-end state (clean vs dirty vs timeout), and returns a result object to the Supervisor. When the session ends, the runner is done. No resumption inside one runner.

**State machine driver.** The Supervisor reads `state.yaml` and the current task's `status.yaml`, maps `status.state` to a role per ADR-0003 D1, launches a Session Runner for that role, waits for it, updates `state.yaml` + `status.yaml` based on the outcome, and moves to the next iteration. After each session: atomic write of `state.yaml` (budget_consumed updated), atomic write of `status.yaml` (state transition), append to the per-batch audit log.

**Signal handling.** SIGTERM installs a `shutting_down` event. All coroutines check it each loop iteration or at async-yield points. The in-flight session is allowed to finish cleanly (do NOT SIGTERM the subprocess on parent SIGTERM — that's a different halt mode). A second SIGTERM is SIGKILL. SIGUSR1 triggers request-file processing (see CLI section below).

**Per-task retry counters.** Live in `status.yaml.history` per ADR-0002 D7. You need enough of this in B1 to not infinitely retry a broken task: a task that hits two dirty-ends in a row transitions to `blocked`. The full retry policy with all seven categories is B2; B1 only needs the cap.

**Budget enforcement.** Live estimate from pricing table every time you see a usage block. Check threshold after each update. At 80%, emit a `budget-threshold` notification (goes to `LoggingDispatcher`, which just logs it). At 100%, set `state.phase = halted`, let the in-flight session finish, then exit the main loop cleanly.

**Cost API reconciliation.** If `ANTHROPIC_ADMIN_API_KEY` is set in settings, the Budget Tracker, at session-end and batch-end, queries `https://api.anthropic.com/v1/organizations/cost_report` with a time window covering the session. The returned cost supersedes the table estimate in `state.budget_consumed.dollars`, with an audit event `cost-reconciliation`. Expect a ~5-minute lag; fail gracefully if reconciliation returns empty or errors (log warn, keep table estimate). The full schema of the Cost API response can be fetched at runtime during development; for tests, mock the response.

**Notification interface.** Define an abstract `NotificationDispatcher` with `async def send(self, notification: Notification) -> None`. Ship `LoggingDispatcher` that logs the notification at the appropriate level and records a `notification-sent` audit event. B2 will ship `TelegramDispatcher` subclassing the same base. The interface is what matters in B1; keep it clean.

**UUIDv7 for session_id and notification_id.** Time-ordered, sortable, idempotency-friendly. Use `uuid_v7.uuid7()`.

---

## Key module contracts

Full internal design is yours; these are the public contracts B1 must expose.

### `orchestrator/session/runner.py`

```python
async def run_session(
    *,
    root: Path,
    task_id: str,
    role: str,                      # "architect" | "implementer" | ...
    session_id: str,                # UUIDv7 string
    model: str,                     # e.g. "claude-opus-4-7"
    session_timeout_seconds: int,   # from batch budgets
    session_budget: SessionBudget,  # advisory, written to state.yaml before call
    event_queue: asyncio.Queue,     # Budget Tracker consumes
    claude_binary: str = "claude",  # overridden in tests to the fake
    subagent_model: str = "claude-sonnet-4-6",
    env: dict[str, str] | None = None,  # override or extend process env
) -> SessionResult:
    """Spawn claude -p, pump stream-json, return the outcome."""
```

`SessionResult` is a dataclass: `session_id`, `end_state` (`"clean" | "dirty" | "timeout" | "rate-limited"`), `exit_code`, `result_event` (the parsed terminal `result` event, or None on dirty-end), `stderr_tail` (last ~50 lines), `duration_seconds`.

The runner:
- Sets `CLAUDE_CODE_SUBAGENT_MODEL` to `subagent_model` per ADR-0002 D2
- Passes `ANTHROPIC_API_KEY` from settings
- Uses a scrubbed env dict (do NOT inherit the entire parent env)
- Spawns with `cwd=root`
- Emits events onto `event_queue` for every parsed stream line (the events are a dataclass hierarchy you define — enough info for the Budget Tracker)
- On parent SIGTERM: does NOT cancel the subprocess; finishes the current line, records state, exits
- On timeout: SIGTERM the subprocess, wait 30s, SIGKILL, return `end_state="timeout"`

### `orchestrator/session/stream.py`

Stream-json parser. Pure sync, no I/O. Takes a line (str) or byte buffer, returns a parsed event (your dataclass). Handles malformed lines by raising a specific `StreamParseError` — the runner catches and logs and continues. Unknown `type` values return a generic `UnknownEvent` that the rest of the pipeline ignores.

### `orchestrator/budget/tracker.py`

```python
class BudgetTracker:
    async def run(self, event_queue: asyncio.Queue, stop_event: asyncio.Event) -> None:
        """Consume events, update state.budget_consumed, emit threshold signals."""

    def snapshot(self) -> BudgetSnapshot:
        """Current budget state; used by the status view later."""

    async def reconcile(self, session_id: str, session_start: datetime, session_end: datetime) -> None:
        """If admin key present, call Cost API; update state.yaml.budget_consumed.dollars."""
```

`BudgetSnapshot` is a dataclass with all four budgets and their current values. The tracker holds the live counters in-memory; periodically (after each session, at a minimum) it flushes to `state.yaml` via `write_state`.

### `orchestrator/budget/pricing.py`

Loader for `orchestrator/pricing.yaml`. Validates against a Pydantic model (define `PricingTable` in `orchestrator/state/schemas.py` — YES, this is a Slice A exception we're making explicit in this prompt; add it to schemas.py alongside the existing models). Raises if a model referenced elsewhere is missing from the table (ADR-0002 D4 says the daemon refuses to start in that case — enforce at Supervisor startup).

### `orchestrator/budget/rate_limit.py`

Persistent counter at `.huragok/rate-limit-log.yaml`. On daemon startup: truncate entries older than 7 days. Before each session launch: query `ok | warn | defer <seconds>`. Implementation is simple for B1: track 5-hour rolling window of session launches; always return `ok` unless the count in the window approaches a configurable threshold. The full weekly-cap logic can be stubbed — B1 only needs the rolling-5h check.

### `orchestrator/supervisor/loop.py`

The main async loop. `async def run(root: Path, settings: HuragokSettings) -> int:` — returns exit code. Startup sequence: validate Claude Code version (call `claude --version`, parse, compare to `MIN_CLAUDE_CODE_VERSION`; refuse to start if below), call `cleanup_stale_tmp(root)`, write PID file, `sd_notify(READY=1)`, enter loop. Shutdown sequence: finish in-flight session, write halt markers, delete PID file, `sd_notify(STOPPING=1)`, return.

The loop body per iteration is approximately:
1. Check signal events (`SIGTERM`, `SIGUSR1`-triggered request files). If shutting down, exit loop.
2. Read `state.yaml`.
3. If `phase == halted` or `complete`, exit loop.
4. If `awaiting_reply` is set, wait for reply (poll `requests/reply-*.yaml`); no real reply handling in B1 since there's no real notification backend — but reading reply files and surfacing them to the supervisor is in scope.
5. Pick next task (first non-done, non-blocked).
6. Pick role for its state per ADR-0003 D1. If state is a terminal, mark task done, move on.
7. Check budgets; halt if any exceeded.
8. Ask rate_limit: `ok | warn | defer`. Handle `defer` by sleeping.
9. Launch Session Runner for (task, role). Await. Process `SessionResult`.
10. Update state.yaml and status.yaml atomically. Append audit events. Potentially emit notifications via the dispatcher. Loop.

### `orchestrator/supervisor/signals.py`

Signal installation, the `shutting_down` asyncio event, request-file directory watcher. Do NOT use `watchdog` or similar — a simple poll of `requests_dir(root)` every 1–2s is enough for B1. The Supervisor drains the directory, applies requests, deletes handled files atomically.

### `orchestrator/notifications/base.py`

```python
class NotificationDispatcher(ABC):
    @abstractmethod
    async def send(self, notification: Notification) -> None: ...
    async def start(self, stop_event: asyncio.Event) -> None:
        """Optional long-running coroutine (Telegram polls from B2 override this)."""
        await stop_event.wait()
```

`Notification` is a dataclass: `id: str` (UUIDv7), `kind: Literal[...]` (use the ADR-0002 D5 list), `summary: str`, `artifact_path: str | None`, `reply_verbs: list[str]`, `created_at: datetime`.

### `orchestrator/notifications/logging.py`

`LoggingDispatcher(NotificationDispatcher)`. On `send()`, emit a structured log line at INFO + append an audit event of kind `notification-sent`. No external I/O.

### Updated `orchestrator/cli.py`

The `run` command becomes real. Typer command signature:

```python
@app.command()
def run() -> None:
    """Start the orchestrator daemon in the foreground."""
    # discover root, load settings, configure logging
    # call asyncio.run(supervisor.loop.run(root, settings))
    # exit with whatever the loop returned
```

`stop` and `halt` become real:

- `stop` — read PID from `.huragok/daemon.pid`, send SIGTERM; if the daemon isn't running, exit 0 with a friendly message (not an error).
- `halt` — write an empty file `.huragok/requests/halt`; send SIGUSR1 to the daemon to wake it up; return immediately.

Keep the existing `status`, `tasks`, `show` behavior from Slice A untouched except: `status` output's "Sessions:" line remains "N launched" for B1. The clean/retry breakdown is B2.

`submit`, `reply`, `logs` stay stubbed — unchanged from Slice A.

---

## The fake-claude fixture

Create `tests/fixtures/fake-claude.sh`:

A bash script that reads environment variables to decide what stream-json to emit. Behaviors:

- `FAKE_CLAUDE_MODE=clean` — emit `system` event, a couple of `assistant` events with realistic usage blocks (small counts, specific model ID), then `result`, exit 0.
- `FAKE_CLAUDE_MODE=crash` — emit `system`, then `assistant`, then exit 1 with no `result` event.
- `FAKE_CLAUDE_MODE=hang` — emit `system`, sleep long enough that the test timeout triggers.
- `FAKE_CLAUDE_MODE=malformed` — emit valid JSON lines mixed with one unparseable line.
- `FAKE_CLAUDE_MODE=version` — respond to `--version` with `"2.1.91 (fake)"`. Tests of the version check use this.

The script must be deterministic per mode and robust enough to be called from pytest subprocesses. Test it manually once before wiring into tests.

Tests override `claude_binary` to point at this script.

---

## Tests

Aim for coverage of:

- Stream parser: known types, unknown types, malformed lines, empty input.
- Budget tracker: per-event updates, threshold crossings (80% and 100%), `flush to state.yaml`, `reconcile()` with a mocked Cost API, missing-admin-key path that skips reconciliation cleanly.
- Pricing table: loads cleanly, rejects unknown models, refuses to start when a model is missing.
- Rate limit log: truncates old entries on startup, returns `ok`/`warn`/`defer` correctly, persists across daemon restart.
- Session runner: clean end, dirty end, timeout (use a small timeout like 2s against `FAKE_CLAUDE_MODE=hang`), malformed stream line handling, scrubbed env (confirm `ANTHROPIC_API_KEY` is passed but `HOME` or a marker variable you set is not — pick any non-essential env var to verify scrubbing).
- Supervisor loop: one-iteration integration test — set up a minimal repo, launch the loop, run one fake session, assert state.yaml and status.yaml were updated correctly, assert audit events appended. Use `FAKE_CLAUDE_MODE=clean` for the fake.
- CLI: `huragok run` start/stop lifecycle — start in a thread/subprocess, `stop` from another, assert clean exit. Real subprocess test, not mocked.
- CLI: `huragok halt` writes the expected request file and sends the signal; assert the daemon picks it up.
- Version check: version-too-old refuses to start with a clear error.
- `sd_notify`: stub it for non-systemd test environments. If `NOTIFY_SOCKET` is unset, the call is a no-op; if set, it writes the expected message. Test the no-op path.

Place tests under `tests/session/`, `tests/budget/`, `tests/supervisor/`, `tests/notifications/`, matching the module structure. Reuse the `tmp_huragok_root` fixture pattern from Slice A's `conftest.py`; extend it to populate a realistic batch.yaml and status.yaml for supervisor tests.

Do not write tests that hit the real Anthropic API. Do not write tests that hit Telegram.

---

## Conventions

Same as Slice A:

- Line length 100, target `py312`.
- Full type hints; no `Any` without rationale comment.
- Docstrings on every public function / class.
- No `print()` outside CLI user-facing output.
- No bare `except Exception`.
- No `# type: ignore`.
- No TODOs in code. Defer via `NotImplementedError("B2: ...")` or raise explicitly.

Run `uv run ruff check .` and `uv run ruff format .` before declaring the slice done. All tests must pass.

---

## Deliverable: `docs/notes/slice-b1-build-notes.md`

Mirroring the Slice A notes:

- Summary of what shipped
- Module-by-module notes, especially any non-obvious design choices
- Tests: pass count, skipped tests and why
- Deviations from this prompt (expected to be low but not zero; be explicit)
- Known issues and B2-boundary notes
- Any micro-ADR you think we should write before committing (see "Micro-ADR candidates" below)

### Micro-ADR candidates to flag

Two design decisions in B1 are plausibly worth a short ADR:

1. The **stream-json parser internal shape** (dataclass hierarchy vs. dict-based dispatch vs. state machine). If you picked one over another for non-obvious reasons, flag it; we may write a small ADR before committing.
2. The **pricing table update policy**. Ship with today's prices; note in the build-notes whether you think `pricing.yaml` warrants a micro-ADR on update cadence, or whether operator-edits-the-file is enough.

Do not write the ADRs yourself — flag them for review and we'll decide together before commit.

---

## Stop conditions

Stop and ask the operator before proceeding if any of these occur:

- An ADR contradicts this prompt.
- A dependency you need isn't in the allowed list.
- A Slice A module needs a change beyond the pricing-table schema addition explicitly permitted above.
- You find the real stream-json schema differs from ADR-0002 D2's description in a way that affects parser design.
- A test reveals a real bug in Slice A code.
- You hit an asyncio deadlock or race that isn't trivially resolvable.
- The supervisor loop integration test can't be written without breaking the "no real claude" rule.

Otherwise: build it. Write the notes. Report back with a summary.
