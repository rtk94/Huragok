# Huragok Phase 1 — Slice B2: Integration Surface

You are completing the Huragok Phase 1 MVP. Slice B1 shipped the daemon core: asyncio supervisor, session runners, budget tracker, and a logging-only notification stub. B2 adds the real Telegram dispatcher, the full D7 error taxonomy, the remaining CLI commands, the systemd unit, and promotes the deferred pieces of the `huragok status` view. After B2 lands, Phase 1 is complete and we're in a position to do the first real end-to-end run.

Read this entire prompt before starting. Read the ADRs and the B1 notes. If anything here contradicts an ADR, STOP and ask — the ADRs are authoritative.

---

## Read first, in this order

1. `CLAUDE.md` at repo root.
2. `docs/adr/ADR-0001-huragok-orchestration.md`.
3. `docs/adr/ADR-0002-orchestrator-daemon-internals.md` — authoritative spec. **D5 (CLI), D6 (Telegram), D7 (error taxonomy), D8 (systemd), D9 (observability)** are the directly-implemented decisions in B2.
4. `docs/adr/ADR-0003-agent-definitions.md` — for context; you are NOT editing agent files.
5. `docs/notes/slice-a-build-notes.md`.
6. `docs/notes/slice-b1-build-notes.md` — **critical**. The "What's next for B2" section is effectively the exit criteria for this slice. The "Sharp edges" section contains three items B2 addresses; the "Known issues" section's B2-plumbs-in list is the inbound work queue.
7. Skim `orchestrator/` to understand the module layout B1 left.

Do not modify any file under `orchestrator/state/`, `orchestrator/config.py`, `orchestrator/constants.py`, `orchestrator/paths.py`, `orchestrator/logging_setup.py`, `orchestrator/session/`, or `orchestrator/budget/` beyond the specific exceptions called out below. The `NotificationDispatcher` base class and `Notification` dataclass in `orchestrator/notifications/base.py` are frozen — extend via subclassing, do not reshape.

---

## Scope boundaries for B2

**In scope:**

- `orchestrator/notifications/telegram.py` — `TelegramDispatcher(NotificationDispatcher)`. Real `sendMessage`, real `getUpdates` long-poll, reply parsing, idempotency, pause-on-outage.
- `orchestrator/errors.py` — the full D7 error taxonomy: a classifier that maps session outcomes to one of the seven categories, plus a retry policy module that decides what to do per category.
- `orchestrator/session/runner.py` — **bounded changes** to emit enough signal (exit code, specific stream events, stderr fragments) that the classifier can distinguish all seven categories cleanly. The SessionResult dataclass may grow fields; do not reshape its existing ones.
- `orchestrator/supervisor/loop.py` — **bounded changes** to (a) use the new classifier and retry policy in place of the current two-dirty-ends cap, (b) persist per-task attempt counters to `status.yaml.history` instead of the in-memory dict, (c) construct a `TelegramDispatcher` when `TELEGRAM_BOT_TOKEN` is set in `.env`, falling back to `LoggingDispatcher` otherwise, (d) feed the Dispatcher's `start()` coroutine into the event loop's long-lived coroutine set.
- `orchestrator/cli.py` — promote `submit`, `reply`, `logs` to real commands. Leave `start` as a **doc-pointing stub** per the B2 scope call. Update `status` to render "N launched, M clean, K retry" by parsing the per-batch audit log.
- `scripts/systemd/huragok.service` — the unit file from ADR-0002 D8. Ships as an artifact in the repo; **do not install it** — installation is an operator step documented below.
- `docs/deployment.md` — operator-facing documentation covering install, run, stop, halt, reply, log-tail, and systemd setup. Points at ADRs for design rationale; itself is operator-facing prose.
- Tests throughout.
- `docs/notes/slice-b2-build-notes.md` — the same format as the B1 notes.

**Explicitly NOT in scope — do NOT build:**

- `huragok init` scaffold generator. This is ADR-0007 territory and belongs to Phase 2+ work.
- Parallel Implementers, worktree isolation. ADR-0005.
- The visual-critic UI review section of `huragok status` (`ui_review.required: true` rendering). ADR-0004.
- Retrospective engine, iteration cycles. ADR-0006.
- Tier-2 `LoadCredential=` secrets. ADR-0001 D9 calls this an upgrade path; B2 ships Tier-1 `EnvironmentFile=` and stops there.
- Any real `claude -p` invocations in tests. Reuse `tests/fixtures/fake-claude.sh`.
- Any real Telegram sends. All Telegram code is tested against a mocked HTTPX transport (see Testing section).
- A metrics endpoint. ADR-0002 D9 defers this.

If you find yourself reaching for any of those, stop. Scope creep into an ADR we haven't finalized is the biggest risk in this slice.

---

## Dependencies

No new runtime dependencies beyond B1's `httpx` and `uuid-v7`. Telegram's Bot API is JSON-over-HTTP; `httpx` is all we need.

For tests, consider adding `respx` (mocking library for `httpx`) as a dev dependency if it makes the Telegram tests cleaner:

```toml
[dependency-groups]
dev = [
    # ...existing...
    "respx>=0.21",
]
```

If you prefer to mock with `unittest.mock` directly, skip `respx`. Your call — whichever produces cleaner tests.

---

## Design guidance

Slice B1 set up the interfaces; B2 is mostly implementation behind those interfaces. Design rails:

**Telegram dispatcher (ADR-0002 D6).** One chat ID per daemon instance, read from `HURAGOK_TELEGRAM_DEFAULT_CHAT_ID` in settings (or overridden per-batch via `batch.yaml.notifications.telegram_chat_id` once the supervisor's batch loader surfaces it — B1 doesn't, so fall back to settings). Outbound sends include the notification ID, the summary, and the reply-verb menu; format is up to you but make it readable on mobile. Inbound long-poll runs in the dispatcher's `start()` coroutine; parse replies per D6 and write them to `.huragok/requests/reply-<id>.yaml` so the supervisor's existing request-file ingestion picks them up. Idempotency: dedupe by `update_id` from the Telegram API (you'll want a small persisted cursor at `.huragok/telegram-cursor.yaml` so reply polling doesn't re-process on daemon restart).

**Telegram reachability (ADR-0002 D6 closing paragraph).** Track the last successful `getUpdates` and the last successful `sendMessage` timestamps. If a notification has been outstanding *and* both operations have failed for more than 10 minutes, log `critical`, set `state.yaml.phase` to `paused` with `halted_reason: notification-backend-unreachable`, and stop launching sessions until Telegram recovers. Recovery (a successful send or receive) clears the pause and the daemon resumes; log at `warn` on the transition back. Do not treat transient 5xx as "unreachable" — it's sustained failure.

**Error taxonomy (ADR-0002 D7).** The classifier takes a `SessionResult` plus context (the last few stream events, stderr tail, exit code, whether the runner recorded a timeout) and returns one of the seven categories. Per-category retry policy from D7 is enforced by a pure function that takes (category, attempt count) and returns an action: `advance`, `retry_same`, `retry_fresh`, `backoff(seconds)`, `escalate`, or `halt`. The supervisor loop consults this function after each session and acts. Per-task attempt counts are now in `status.yaml.history` — each rejection or failure appends a history entry with a `category` field; counting is `sum(1 for h in history if h.category in {"session-timeout", "subprocess-crash"})`.

**Exponential backoff for transient-network.** Standard: 1s, 2s, 4s, with jitter. Three attempts per D7 table, then escalate.

**Rate-limited category.** The classifier looks for HTTP 429 signatures in stream events or stderr. `retry_after` comes from the 429 response if present; default to a conservative 60 seconds. No attempt counter for rate-limited retries (ADR-0002 D7 explicit).

**Context-overflow detection.** The agent signals this via either (a) the terminal `result` event with a `stop_reason` / `is_error` combination indicating it, or (b) an assistant event containing recognizable overflow markers in the text. The precise detection pattern is your call; err on the side of treating ambiguous signals as `unknown` rather than `context-overflow`. Halt-batch behavior either way on first context-overflow — this is a human-intervention category per D7.

**Escalation notifications.** Category + task ID + last session log tail + reply verbs. Reply handling: `continue` advances the task past the failure (sets state to `implementing` or the next pipeline step, with a history entry noting the operator override). `iterate` resets attempt counters for the task and retries fresh. `stop` sets `phase: halted`. `escalate` transitions the task to a special `awaiting-human` state and stops launching for that task; the operator is expected to intervene via `huragok` CLI with a live `claude` session against the repo.

**`huragok submit`.** Reads a YAML file path from argv, validates against the `BatchFile` schema (reuse Slice A's schemas), copies to `.huragok/batch.yaml` atomically. Refuses to overwrite an in-flight batch (`state.yaml.phase == running`). If no `state.yaml` is active (idle), wipes any existing `work/` directories for previous batches (or moves them to `work.archived/<batch_id>/` — your call on the archival flavor, but preserving history is better than deleting) and initializes fresh. Does not start the daemon; the operator runs `huragok run` or `systemctl --user start huragok` afterward.

**`huragok reply <verb> [notification_id] [annotation]`.** Writes `.huragok/requests/reply-<id>.yaml` with the parsed payload (verb, optional annotation, timestamp). If only one notification is outstanding and no ID is given, matches it automatically. If multiple are outstanding, errors with a list. If the daemon is running (PID file exists and process is alive), sends SIGUSR1. If not, still writes the reply file — it'll be picked up on next start.

**`huragok logs [--follow] [--level LEVEL]`.** Tails `.huragok/logs/batch-<batch_id>.jsonl` for the current batch (from `state.yaml.batch_id`). `--follow` is standard `tail -f` semantics implemented in Python (no shell-out). `--level` filters records by their `level` field. Default is the last 50 records; `--follow` streams new ones as they land. If no active batch, print "no batch in flight" and exit 0.

**`huragok status` — session breakdown line.** Parse `.huragok/audit/<batch_id>.jsonl` to count `session-started` / `session-ended` events, group by `end_state` field. Render as "N launched, M clean, K retry" in the `Sessions:` line per ADR-0002 D9. For B2 just the three labels; if new categories become interesting we can extend the display. Keep the existing "no active batch" path unchanged.

**`huragok start` stub.** One-liner per ADR-0002 D5 plus this slice's operator stance:

```
For background deployment, install the systemd unit and run:

  systemctl --user start huragok.service

Installation instructions: docs/deployment.md
```

Exit 1. This is a stub so operators who type `huragok start` get pointed at the right path; it's not trying to abstract over systemd.

**systemd unit.** Ship `scripts/systemd/huragok.service` with exactly the contents from ADR-0002 D8. Do not auto-install. `docs/deployment.md` covers the `ln -s` / `cp` step the operator performs manually.

**Deployment docs.** `docs/deployment.md` covers:
- Prereqs (Python 3.12, `uv`, Claude Code 2.1.91+, optional: Telegram bot token)
- First-time setup: `uv sync`, `.env` configuration, initial `batch.yaml` authoring
- Running foreground: `huragok submit batch.yaml && huragok run`
- Running as a systemd user service: install the unit, `systemctl --user start huragok.service`, `systemctl --user enable huragok.service` for auto-start on boot
- Stop / halt / reply / logs — one-paragraph each
- Troubleshooting: daemon pid stale (solution: `huragok stop` cleans it), Telegram bot not responding (check token, check chat ID), version check fails (upgrade Claude Code)
- Points at `docs/adr/` for design rationale and at `docs/notes/` for build history

Keep it operator-focused, not dev-focused. The reader is someone deploying Huragok, not contributing to it.

---

## Key module contracts

### `orchestrator/errors.py`

Your call on the exact shape; suggested skeleton:

```python
from enum import Enum
from dataclasses import dataclass

class SessionFailureCategory(Enum):
    CLEAN_END = "clean-end"
    RATE_LIMITED = "rate-limited"
    CONTEXT_OVERFLOW = "context-overflow"
    SESSION_TIMEOUT = "session-timeout"
    SUBPROCESS_CRASH = "subprocess-crash"
    TRANSIENT_NETWORK = "transient-network"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class RetryAction:
    kind: Literal["advance", "retry_same", "retry_fresh", "backoff", "escalate", "halt"]
    backoff_seconds: float | None = None

def classify(result: SessionResult, context: ClassificationContext) -> SessionFailureCategory:
    """Pure function; no I/O."""

def decide_action(category: SessionFailureCategory, attempt_count: int) -> RetryAction:
    """ADR-0002 D7 table encoded as decision function."""
```

`ClassificationContext` carries enough signal for the classifier: stderr tail, last few stream events, exit code, whether timeout fired. The `SessionResult` from B1 already has most of this; extend carefully if the classifier needs more, documenting what you added and why in the build notes.

### `orchestrator/notifications/telegram.py`

```python
class TelegramDispatcher(NotificationDispatcher):
    def __init__(
        self,
        bot_token: SecretStr,
        default_chat_id: str,
        *,
        poll_timeout_seconds: int = 25,
        send_timeout_seconds: float = 10.0,
        root: Path | None = None,
        batch_id: str | None = None,
        client: httpx.AsyncClient | None = None,  # inject for tests
    ): ...

    async def send(self, notification: Notification) -> None: ...
    async def start(self, stop_event: asyncio.Event) -> None: ...  # long-poll loop
```

The dispatcher tracks last-send-ok and last-receive-ok timestamps internally, surfacing a `reachable: bool` property the supervisor can consult before deciding to transition to paused.

Message format is your choice, but here's a reasonable default for the outbound:

```
🟡 Huragok — budget-threshold (80%)
Batch: batch-001 · Task: task-0042

Tokens: 4.0M / 5.0M (80%)

Reply: continue | iterate | stop | escalate
ID: 01HXYZ...
```

Keep it plain-text Markdown-light; Telegram accepts Markdown V2 but escaping is a pain. Reply parsing accepts `<verb>` alone, `<verb> <id>`, or `<verb> <id> <annotation>` as documented in ADR-0002 D6.

### Supervisor loop changes

Minimal — the loop should be mostly unchanged. The deltas:

1. Constructor path: if `settings.telegram_bot_token` is set, build a `TelegramDispatcher`; otherwise build `LoggingDispatcher` (current behavior). Add the dispatcher's `start()` coroutine to the long-lived set alongside the tracker.
2. After each session ends, call `classify(result, context)` then `decide_action(category, attempts)`. Map the returned `RetryAction` to a state-yaml transition. Replace the existing two-dirty-ends cap logic with this.
3. Attempt counts are no longer in the in-memory `attempts: dict`. Compute from `status.yaml.history` on each decision. The supervisor may still cache during a session to avoid re-reading, but on daemon restart the count is recovered from disk.
4. When the classifier returns `CONTEXT_OVERFLOW`, transition phase to halted and notify. Do not retry.
5. When the dispatcher's `reachable` property goes false for >10 minutes, transition phase to `paused`. When it goes true again, transition back to `running`. Emit audit events for both transitions.

### Session runner changes (bounded)

The runner's `SessionResult` may need to grow fields so the classifier can distinguish cases it currently can't:

- Add a field for the terminal `result` event's `stop_reason` and `is_error` fields (for context-overflow detection).
- Add a field for the last ~5 raw stream events as dicts (for classifier introspection).
- Keep existing fields unchanged.

The runner does not itself classify — that's the classifier's job, called by the supervisor after the runner returns. Keep the runner stupid and the classifier smart.

### Audit log parsing for `huragok status`

Read `.huragok/audit/<batch_id>.jsonl` line-by-line. Filter to `kind in {"session-started", "session-ended"}`. For `session-ended` events, group by the `end_state` field. Produce counts. Render inline in the status view. Unknown-end-state events (from B1 retry logic or future categories) count toward "retry" to keep the math simple — we can refine the bucketing later. Stream the file rather than loading it; audit files can grow over many sessions.

---

## Tests

Use `respx` or `unittest.mock` to mock HTTPX. Every Telegram test runs against a mock; zero tests hit api.telegram.org.

Aim for coverage of:

**Error taxonomy (`tests/test_errors.py`):**
- Every category correctly classified from representative inputs.
- `decide_action` returns expected shape for each (category, attempts) input.
- Pure-function tests: no I/O, no side effects.
- Edge cases: timeout that also had a 429 upstream, crash with malformed stream, unknown with zero-byte stderr.

**Telegram dispatcher (`tests/notifications/test_telegram.py`):**
- `send()` builds the correct outbound request (URL, headers, JSON body).
- `send()` handles 200, 4xx, 5xx differently (4xx logs error and gives up; 5xx logs warn and marks last-send-failed).
- `start()` long-poll loop consumes `getUpdates`, advances cursor, writes reply files, updates last-receive-ok.
- `start()` on 4xx (invalid token, chat not found) logs critical and exits cleanly.
- Idempotency: re-polling with the same cursor doesn't re-process old updates.
- Reply parsing: valid verbs, aliases, bare verb without ID (single-pending match), disambiguation on multiple-pending, invalid verb.
- `reachable` transitions: starts true, goes false after 10m of failures, goes true again on success.
- Cursor persistence: cursor survives restart via `telegram-cursor.yaml`.

**Supervisor integration (`tests/supervisor/test_loop.py` additions):**
- Classifier integration: a session that returns `SUBPROCESS_CRASH` triggers a retry; two crashes escalate per decide_action.
- Attempt persistence: a daemon restart mid-batch continues counting from `status.yaml.history`.
- Dispatcher reachability: mock dispatcher reports unreachable; supervisor transitions to paused; dispatcher recovers; supervisor resumes.
- Real TelegramDispatcher plumbing is tested via a fake-transport `respx` mock (not mocking the dispatcher class itself).

**CLI (`tests/test_cli.py` additions):**
- `huragok submit <valid batch.yaml>` → writes `.huragok/batch.yaml`.
- `huragok submit <invalid batch>` → exits 1 with a clear validation error.
- `huragok submit` with in-flight batch → exits 1 without overwriting.
- `huragok reply continue` with single pending → writes reply file and sends SIGUSR1 (mock the signal send).
- `huragok reply continue` with no pending → exits 0 with "no pending notifications."
- `huragok reply continue` with multiple pending → exits 1 with disambiguation.
- `huragok logs` with no batch → "no batch in flight", exits 0.
- `huragok logs --follow` → smoke test that it exits cleanly when SIGTERMed.
- `huragok status` — sessions-breakdown line renders correct counts from a synthetic audit file.
- `huragok start` exits 1 with the doc-pointer message.

**Systemd unit (`tests/test_systemd_unit.py`):**
- The shipped `scripts/systemd/huragok.service` parses cleanly (Python's `configparser` handles systemd unit syntax well enough for validation).
- Required keys are present: `Type=notify`, `ExecStart`, `Restart`, `WorkingDirectory`.

No test starts a real systemd service; we just validate the unit file shape.

---

## Conventions

Same as B1. Line length 100, py312, full type hints, docstrings, no bare except, no TODOs. Run `uv run ruff check .` and `uv run ruff format .` and `uv run pytest` before declaring done.

---

## Deliverable: `docs/notes/slice-b2-build-notes.md`

Same format as the B1 notes:

- Summary of what shipped
- Module-by-module notes, especially non-obvious design choices
- Tests: pass count, skipped tests and why
- Deviations from this prompt
- Known issues and Phase-2-boundary notes
- Any micro-ADR candidates you think we should write before committing

Particularly flag:
- Any category in the D7 classifier where you made a judgment call about how to distinguish it from adjacent categories (e.g. unknown vs. subprocess-crash edge cases).
- The Telegram message format choice if you deviated from my suggested default.
- Anything in the supervisor integration that changed the loop's control flow more than "swap in the classifier."

---

## Stop conditions

Stop and ask the operator before proceeding if:

- An ADR contradicts this prompt.
- A B1 module needs a reshape beyond the bounded changes listed here.
- A test reveals a real bug in B1 (flag the bug; don't silently fix it in B2 code).
- The Telegram Bot API's actual request/response shape differs from what ADR-0002 D6 assumes in a way that affects the dispatcher design.
- The classifier cannot distinguish two categories without heuristics you're uncomfortable shipping.
- You hit an async deadlock or race.

Otherwise: build it. Test it. Document it. Report back.
