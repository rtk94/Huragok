# Slice B2 — Integration Surface: Build Notes

**Date:** 2026-04-22
**Author:** Claude Code (Opus 4.7, 1M context)
**Scope:** Phase 1 MVP, Slice B2 (Telegram dispatcher, full D7 taxonomy,
real `submit`/`reply`/`logs`, systemd unit, operator docs).
**Spec:** the Slice B2 prompt shipped with this session.
**Reviewed against:** ADR-0001, ADR-0002, ADR-0003, the B1 notes, and
the B1 module tree.

## Summary

Phase 1 MVP is complete. Shipped in this slice:

- **`orchestrator/errors.py`** — the ADR-0002 D7 seven-category
  taxonomy, a pure-function `classify()`, a pure-function
  `decide_action()` encoding the full retry table, and
  `count_attempts()` for the history-based cap logic. No I/O.
- **`orchestrator/notifications/telegram.py`** —
  `TelegramDispatcher(NotificationDispatcher)`. Real `sendMessage`,
  real 25-second `getUpdates` long-poll, reply parsing to
  `.huragok/requests/reply-<id>.yaml`, cursor persistence at
  `.huragok/telegram-cursor.yaml`, idempotency by `update_id`, and
  the 10-minute reachability contract from ADR-0002 D6's closing
  paragraph.
- **`orchestrator/session/runner.py`** — bounded extension of
  `SessionResult` with `last_events` (ring buffer of raw stream-event
  dicts) and `last_assistant_stop_reason` so the classifier can
  distinguish context-overflow, transient-network, subprocess-crash,
  and unknown without reshaping `ResultEvent`.
- **`orchestrator/supervisor/loop.py`** — the B1 in-memory
  two-dirty-ends cap is replaced with `classify() + decide_action()`
  against history-derived attempt counters. A `build_dispatcher()`
  factory chooses `TelegramDispatcher` when a bot token is present,
  `LoggingDispatcher` otherwise. Reachability-driven transitions to /
  from `paused` are wired into the main loop. Reply files are
  applied to `state.yaml.awaiting_reply`, with `iterate` resetting
  categorised history entries and `continue` advancing past a
  failure.
- **`orchestrator/cli.py`** — `submit`, `reply`, `logs` promoted to
  real commands. `start` now prints a pointer to the systemd unit and
  exits 1. `status` parses the per-batch audit log into the
  "N launched, M clean, K retry" breakdown.
- **`orchestrator/state/schemas.py`** — one added field:
  `HistoryEntry.category: str | None = None`. Called out below as the
  one exception to the "don't touch state/" scope rule; required by
  D7's history-based retry counting.
- **`scripts/systemd/huragok.service`** — shipped as the exact unit
  from ADR-0002 D8. Not auto-installed.
- **`docs/deployment.md`** — operator-facing guide covering install,
  foreground + systemd modes, the full command surface, and
  troubleshooting.
- **`tests/`** — **256 tests passing in ~8 s**, 88 new on top of
  B1's 168. Ruff lint + format clean.

## Module-by-module notes

### `orchestrator/errors.py`

Classifier and retry-policy module. Pure functions; no I/O.

- `SessionFailureCategory` is a `StrEnum` — each member equals its
  string value, which lets audit-log `json.dumps` round-trip without
  a custom encoder.
- `ClassificationContext.from_result` builds the classifier's input
  bundle straight off a `SessionResult`; tests can also construct one
  directly for edge-case coverage without a full runner cycle.
- Classification precedence: runner end_state → mid-stream 429 →
  context-overflow markers → transient-network markers → subprocess
  crash → `UNKNOWN` fallback.
- Signal lists (`_RATE_LIMIT_MARKERS`, `_CONTEXT_OVERFLOW_MARKERS`,
  `_TRANSIENT_NETWORK_MARKERS`) are tuples kept narrow by design. I
  erred on the side of `UNKNOWN` (halt) over blind retries. A new
  signal the classifier doesn't know about fails safely.
- `_lookup_retry_after_in_event` walks nested stream-event dicts
  looking for `retry_after` / `Retry-After` style keys, falling back
  to a regex on string values. The Cost API payload shape doesn't
  apply here — this is specifically for 429 pacing hints.
- `decide_action` returns a `RetryAction` with `kind` + optional
  `backoff_seconds`. `retry_fresh` / `retry_same` differ only in
  whether the caller should hand-reset prior conversation state
  (currently both result in the same supervisor behaviour — new
  session with fresh context — since ADR-0002 D7 uses `retry_fresh`
  across the board in Phase 1; `retry_same` is reserved).
- `jitter_backoff` is NOT pure (it calls `random.uniform`); it lives
  in this module because it's thematically part of the retry policy,
  but `decide_action` itself returns the base backoff and the
  caller (the supervisor) applies jitter separately so
  `decide_action` can be tested deterministically.

### `orchestrator/notifications/telegram.py`

HTTPX-backed dispatcher. Key design notes:

- `TelegramDispatcher(bot_token, default_chat_id, *, client=None, ...)`
  accepts an injected `httpx.AsyncClient`. Tests pass
  `httpx.MockTransport` for canned responses without hitting the
  network. Production constructs its own client and owns teardown.
- Reachability tracks `_last_send_ok` / `_last_receive_ok` /
  `_last_send_attempt` / `_last_receive_attempt`. `reachable` returns
  `True` unless there's a pending reply-verb-carrying notification
  AND both channels have been failing (last_ok older than the grace,
  or never succeeded post-attempt) for at least the grace period
  (default 10 minutes per ADR-0002 D6).
- The long-poll loop in `start()` uses `getUpdates?offset=cursor+1&timeout=25`.
  25 seconds is Telegram's recommended server-side long-poll timeout;
  HTTPX is given 35 s on top to cover the server reply. Each
  successful poll advances and persists the cursor; each reply is
  written as a `.huragok/requests/reply-<id>.yaml` file with an
  atomic rename.
- **Auth errors (401/403/404) stop the poll loop.** Per ADR-0002 D6
  this is a permanent failure (invalid token or invalid chat). The
  dispatcher logs critical once and then idles until shutdown — it
  does NOT retry, because a misconfigured token will fail the same
  way forever. Reachability flips to `False` if any notification is
  pending, pausing the supervisor until an operator intervenes.
- **5xx errors are transient.** The dispatcher backs off 5 seconds,
  then retries. Reachability handles the multi-minute outage case.
- **Explicit cooperative yield.** The poll loop calls
  `asyncio.sleep(0)` after each iteration. Without it, a
  `MockTransport` or an upstream returning empty instantly can starve
  the event loop; the yield gives `stop_event` a chance to be
  observed externally. Non-obvious, so called out here.
- **Cursor persistence** uses the same write-then-rename pattern as
  the state-IO module (simpler: no fsync, no stale-tmp sweep — the
  cursor file is advisory and can be regenerated from Telegram's API
  if lost).
- **Bare-verb resolution.** When a reply arrives without a
  `notification_id`, the dispatcher matches against its in-memory
  `_pending` dict (notifications it sent that carried reply verbs).
  Zero pending → logged and dropped. One pending → matched
  automatically. More than one → logged as ambiguous and dropped.
  Explicit IDs always win, even for IDs the dispatcher doesn't know
  about (warm-boot resilience).
- **Outbound message format.** Plain text with emoji-per-kind hints,
  reply-verb menu, and ID footer. MarkdownV2 was tempting but the
  escaping rules are a trap; plain text reads fine on mobile.

### `orchestrator/session/runner.py`

Two new fields on `SessionResult`:

- `last_events: list[dict[str, Any]]` — ring buffer of the last 5
  raw stream-event dicts captured in-order. Kept as raw dicts so
  unrecognised-new-fields from future Claude Code releases still
  reach the classifier.
- `last_assistant_stop_reason: str | None` — the most recent
  `stop_reason` observed on either an assistant event or the
  terminal result event. Extracted with `_extract_stop_reason` which
  looks at both flat and `message.stop_reason` locations.

The runner's own end-state classification (`clean` / `dirty` /
`timeout` / `rate-limited`) is unchanged — it continues to be the
B1 authoritative verdict, and the D7 classifier uses it as the fast
path before falling back to signal inspection.

The spawn-failure path (the `FileNotFoundError` branch in
`run_session`) continues to use `_make_result` with default values
for the new fields — empty `last_events`, `None` stop reason — which
the classifier resolves correctly to `SUBPROCESS_CRASH`.

### `orchestrator/state/schemas.py`

Added one optional field: `HistoryEntry.category: str | None = None`.
This is the exception to the "don't touch `orchestrator/state/`"
rule. Rationale:

- ADR-0002 D7's history-based retry counting requires a per-entry
  category field. The prompt explicitly codes `sum(1 for h in history
  if h.category in {"session-timeout", "subprocess-crash"})`.
- `Optional[str]` with default `None` is strictly additive: existing
  status.yaml files parse unchanged (the field is absent), and new
  writes include it. No migration required.
- No other schema changes.

If the schema boundary is considered hard, alternative was encoding
the category in `by` (e.g. `"supervisor:subprocess-crash"`). I
rejected it as a lexical hack that would make the retry-counting
function brittle.

### `orchestrator/supervisor/loop.py`

Largest module in the slice. Deltas from B1:

- **Classifier integration.** `_post_session` now builds a
  `ClassificationContext`, calls `classify()`, counts prior attempts
  from `status.yaml.history`, calls `decide_action()`, and dispatches
  on the returned `RetryAction`. The B1 in-memory `attempts` dict is
  still on `SupervisorContext` but is now a non-authoritative cache
  — counts are recomputed from history on every session end.
- **Per-retry-family attempt counts.** `SESSION_TIMEOUT` and
  `SUBPROCESS_CRASH` share a cross-counted cap (per D7). 
  `TRANSIENT_NETWORK` has its own cap. `_attempts_for_category`
  picks the right counter.
- **Action dispatch** — `advance` (happy path), `retry_fresh`
  (append history, let the next tick pick it up), `backoff` (append
  history, `sleep_or_shutdown` for the computed seconds),
  `escalate` (transition to `awaiting-human`, send notification,
  record awaiting_reply), `halt` (transition to `blocked`, send
  notification, flip batch phase to halted).
  - Transient-network backoff applies jitter at the supervisor
    (`jitter_backoff(delay)`) so `decide_action` stays pure.
- **Reachability → phase reconciliation.** `_reconcile_reachability`
  runs at the top of each main-loop iteration. When the dispatcher's
  `reachable` property is False and phase is idle/running, transition
  to `paused` with `halted_reason=notification-backend-unreachable`.
  When reachable returns to True, transition back to `running`.
  Audit events emitted for both transitions. The main loop skips
  session launches while paused for this specific reason.
- **Reply-file application.** `_apply_drained_requests` processes
  the reply files that `process_request_files` has already drained.
  Matching reply → clears `awaiting_reply`, applies verb-specific
  side effects:
  - `stop` sets the shutting-down signal.
  - `iterate` truncates categorised history rows for the task and
    resets its state to `implementing`.
  - `continue` returns the task from `awaiting-human` / `blocked` to
    `implementing` with an operator-override history entry.
  - `escalate` is a no-op at supervisor level (the task is already
    in `awaiting-human` if the escalation was automatic).
- **`build_dispatcher()`** factory. Picks `TelegramDispatcher` when
  both `TELEGRAM_BOT_TOKEN` and `HURAGOK_TELEGRAM_DEFAULT_CHAT_ID`
  are configured; otherwise `LoggingDispatcher`. Logs a warning if
  the token is set but the chat id isn't, so operators catch the
  common misconfig quickly.
- **`run_supervisor(..., dispatcher=...)`** — new kwarg lets tests
  inject a dispatcher, which both keeps the existing B1 tests
  running against the no-network LoggingDispatcher and lets the
  reachability integration test inject a fake that can flip at will.

Control-flow change beyond "swap in the classifier": the main loop
now has a third pre-flight gate (reachability) before the
budget-and-rate-limit gates. I judged this the natural location —
before ever asking "is there a task to launch?" the question is
"should I be launching anything at all right now?", and the answer
pivots on both budget and dispatcher reachability. This is called
out here so the control-flow reshape doesn't hide in the diff.

### `orchestrator/cli.py`

- `status` — now renders the `"N launched, M clean, K retry"` line
  from parsing `.huragok/audit/<batch_id>.jsonl`. Streams the file
  line-by-line so long-running batches don't inflate memory.
- `submit` — validates a batch against `BatchFile`, refuses to
  overwrite an in-flight batch, archives any previous work directory
  to `.huragok/work.archived/<previous-batch-id>/` (or
  `pre-<new-batch-id>` if we can't identify the previous), writes
  the new `batch.yaml`, and resets `state.yaml` to phase=idle. Does
  not start the daemon.
- `reply` — normalises the verb via the Telegram module's alias
  table (reuses `REPLY_VERB_ALIASES` and `normalize_verb`), writes a
  reply file, sends SIGUSR1 to a live daemon. Matches against
  `state.awaiting_reply` for single-pending resolution.
- `logs` — Python-native `tail [-f]` of
  `.huragok/logs/batch-<id>.jsonl`. The block-based `_last_n_lines`
  helper reads in 4 KB chunks from the end of the file so it scales
  to large logs. `--level` filters by structlog record level.
  Gracefully tells the operator "no batch log on disk yet" when the
  file is missing — see Known Issues for why this happens in B2.
- `start` — now a doc pointer. One stderr message, exit 1.

### `scripts/systemd/huragok.service`

Verbatim from ADR-0002 D8. Ships as an artifact; installation is a
manual `install -D` step documented in `docs/deployment.md`.

### `tests/`

- `tests/test_errors.py` — 31 tests. Every classifier branch, every
  `decide_action` branch, attempt counting, jitter behaviour, purity
  guard, Retry-After extraction.
- `tests/notifications/test_telegram.py` — 34 tests. Verb
  normalisation + aliases, reply parsing edge cases, send()
  status-code dispatch (200 / 4xx auth / 5xx transient / transport
  error), start() long-poll happy path, cursor persistence across
  restart, idempotency on duplicate `update_id`, wrong-chat
  filtering, auth-error termination, bare-verb resolution rules,
  reachability transitions, message format.
- `tests/supervisor/test_loop.py` — 6 tests total (3 from B1,
  rewritten for the D7 flow, plus 3 new). The rewrite: "two dirty
  ends block" became "crash-cap escalates after 3 attempts"
  (subprocess-crash per D7 retries twice then escalates). The 3
  new: audit-log category + action recording, reachability-driven
  pause/resume, history-based retry counting surviving a seeded
  prior entry.
- `tests/test_cli.py` — 31 tests (up from 17). submit valid /
  invalid / missing / refuses-running / archives-work; reply
  no-pending / single-pending / explicit-id / alias-accepted /
  unknown-verb-rejected / signals-daemon; logs no-batch /
  no-file-yet / tails-records / level-filter / unknown-level; status
  breakdown line; start doc pointer.
- `tests/test_systemd_unit.py` — 6 tests parsing the unit file via
  `configparser` and asserting ADR-0002 D8's required keys. No
  systemctl invocation.

## Tests

**256 passed, 0 failed, 0 skipped, ~8 seconds.**

| Module                                    | Tests |
| ----------------------------------------- | ----- |
| (Slice A + B1 carried forward)            | 168   |
| tests/test_errors.py                      | 31    |
| tests/notifications/test_telegram.py      | 34    |
| tests/supervisor/test_loop.py (B2 adds)   | +3    |
| tests/test_cli.py (B2 adds)               | +14   |
| tests/test_systemd_unit.py                | 6     |
| **Total**                                 | **256** |

- `uv run ruff check .` → `All checks passed!`
- `uv run ruff format --check .` → clean.
- No tests skipped. No tests hit the real Anthropic API or real
  Telegram API. All HTTP mocked via `httpx.MockTransport`.

### Tests that exercise slower paths

- `test_loop_escalates_after_crash_cap` and
  `test_crash_audit_records_category_and_action` launch
  fake-claude in crash mode repeatedly until the task escalates;
  ~3–5 seconds each depending on scheduling.
- `test_attempt_count_survives_restart` seeds history then drives
  one more crash; similar cost.
- `test_reachability_transitions_to_paused_and_recovers` sets
  `reachable=False` before starting the loop, then flips it; uses
  0.05s poll intervals so completes in < 1 s on a healthy machine.

## Deviations from the prompt

1. **`HistoryEntry.category` schema extension.** The prompt freezes
   `orchestrator/state/` except for bounded exceptions. Adding the
   field is the only way to implement D7's history-based retry
   counting as the prompt specifies. I judged this an implicit
   exception; it's additive (optional `str | None`, default `None`)
   and old status files continue to parse. Called out above.
2. **Cooperative `asyncio.sleep(0)` after each poll iteration in
   `TelegramDispatcher.start`.** Necessary to avoid starving the
   event loop when `getUpdates` returns an empty result list
   instantly (as `MockTransport` does in tests, and as a hostile
   Telegram server could do in production). Not in the prompt; I
   judged it a bug fix for the long-poll loop as designed.
3. **Test-only `dispatcher` kwarg on `run_supervisor`.** The prompt
   didn't explicitly ask for injectability, but without it the B2
   tests either have to hit the real Telegram API (no) or rely on
   a dispatcher constructed from env, which the pre-existing
   `_clear_settings_cache` fixture doesn't isolate from the local
   `.env`. Adding a kwarg is cheaper than reshaping the fixture
   tower. Defaults to `None` (build from settings as before) and
   has no production effect.
4. **`_isolate_external_env` autouse fixture.** Related to the
   above. Unsets `TELEGRAM_BOT_TOKEN` / `HURAGOK_TELEGRAM_DEFAULT_CHAT_ID`
   / `ANTHROPIC_*` and disables `env_file` discovery on
   `HuragokSettings` so tests don't pick up operator-local
   secrets. Needed because my development machine had a live `.env`
   that caused the B1 supervisor integration test to try a real
   Telegram long-poll.
5. **Batch log file mirror.** `huragok logs` reads
   `.huragok/logs/batch-<id>.jsonl`. B2 does NOT install a
   duplicate-to-file structlog sink inside the daemon — that would
   require touching `orchestrator/logging_setup.py`, which is on the
   frozen-file list. Instead, `docs/deployment.md` documents two
   operator-level options: redirect stdout from `huragok run` to the
   batch log file, or use `journalctl --user -u huragok.service`.
   The CLI gracefully reports "no batch log on disk yet" when the
   file is missing so the command isn't actively broken. Flagged for
   a follow-up slice when the frozen-file rule lifts.
6. **`_stub()` helper removed.** B1 shipped it as a placeholder for
   the still-unimplemented Slice-B commands. With all commands
   promoted in B2, it has zero callers and was deleted; the
   parametrized "Slice-B stubs exit 1" test in `test_cli.py` was
   rewritten as per-command tests.

## Known issues and Phase-2-boundary notes

### B2 limitations

- **Batch log file isn't populated by the daemon itself.** See
  deviation 5 above. Operators who want `huragok logs` to work out
  of the box need to redirect stdout explicitly (documented in
  `docs/deployment.md`). Under systemd this is trivial via
  `StandardOutput=append:...`.
- **Reply-verb edge cases.** When an operator replies `continue` on
  an escalation, the supervisor resets the task to `implementing`
  unconditionally. For some escalations (e.g. critic-reject
  escalations) this may be the wrong target state — the right one is
  "whichever state the pipeline would next drive". Phase 2 can
  refine the mapping; for now `implementing` is a sane default that
  keeps the pipeline moving.
- **Cursor file persists forever.** Each Telegram restart reuses the
  cursor; the file never rotates. Not a real concern for Phase 1
  (one-off cursor = one `getUpdates` offset integer) but flagged if
  someone future-proofs the dispatcher.
- **No Markdown formatting on Telegram messages.** Plain text only.
  Phase 2 feature if someone wants richer layouts.

### Sharp edges

- **`TelegramDispatcher.start()`'s auth-error path.** On 401/403/404
  the loop marks `_auth_failed=True` and idles until shutdown. The
  dispatcher never recovers without a daemon restart. This is the
  right call (tokens don't fix themselves) but it means an operator
  who fixes the `.env` has to bounce the daemon. Documented in the
  deployment troubleshooting section.
- **`huragok reply continue`'s state-machine reversal.** Returns
  tasks in `awaiting-human` or `blocked` back to `implementing`,
  adding an `operator-override` history entry with
  `category="operator-override"`. That category is not in the
  D7-counted set so it doesn't affect future retry caps — intentional.
- **`_apply_drained_requests` walks `state.awaiting_reply` against
  every reply.** Multiple replies with different IDs all get
  processed, but only one can match the current `awaiting_reply`
  (the schema supports a single outstanding notification). Extra
  replies are audited but not otherwise applied. If the schema ever
  grows to multi-pending, this helper needs revisiting.
- **The frozen-file list and `logging_setup.py`.** See deviation 5.
  When we do plumb a file sink, a tee writer in the supervisor won't
  work cleanly because structlog caches the stdout reference at
  configure time. A future slice should revise `configure_logging()`
  to accept a file-sink parameter.

### Micro-ADR candidates flagged for review

Per the prompt's closing prompt:

1. **`HistoryEntry.category` as first-class schema field vs.
   lexical hack in `by`.** I went with the schema change. Could use
   a sentence of rationale in an ADR-0002 revision entry (or a tiny
   micro-ADR) to pin the decision.
2. **Classifier judgment calls.** Three non-trivial:
   - `max_tokens` stop_reason is NOT `CONTEXT_OVERFLOW` by default;
     it becomes overflow only when accompanied by `result.is_error`.
     This is conservative on purpose: `max_tokens` most often means
     the response was truncated by the model's output limit, not
     that the context is full. The line is fuzzy; I leaned on
     `is_error` as the disambiguator.
   - `timeout` beats `rate-limited` on conflict. A session that
     timed out with a 429 upstream is classified as SESSION_TIMEOUT
     (retry fresh) rather than RATE_LIMITED (backoff same). The
     runner's signal is more authoritative than a mid-stream hint.
   - `UNKNOWN` catches the "exit 0 but no result event" shape. This
     is ambiguous enough that halting is safer than any retry rule.
3. **Reachability grace period.** The 10-minute default is locked in
   from ADR-0002 D6, but it's a judgment call and the dispatcher
   accepts a constructor override. If real-world outages make this
   feel wrong, revisit.
4. **Telegram message format.** I chose plain text with emoji hints
   and a reply-verb menu. Not clearly the prompt's default, but the
   prompt said "readable on mobile" is the bar — reads fine, low
   escaping risk.
5. **`_apply_drained_requests` verb semantics.** `continue` resets
   to `implementing`, `iterate` truncates categorised history. These
   are reasonable default mappings but not formally specified in
   ADR-0002 or ADR-0003. A short ADR or an ADR-0002 revision would
   pin the semantics before Phase 2 operators start depending on
   them.

## Conventions & tooling

- `uv run ruff check .` → **All checks passed!**
- `uv run ruff format --check .` → all files already formatted.
- `uv sync` completes with no warnings.
- Full type hints on every public function / class / dataclass. No
  `Any` without a rationale comment.
- No `# type: ignore` except the existing B1 ones in
  `budget/tracker.py` (PricingTable annotation dodge to avoid an
  import cycle) and the supervisor's `current_agent` assignment
  (role → Literal coercion); both are carried forward unchanged.
- No TODOs in code.

## Phase 1 closes here

Slice B2 completes the Phase 1 MVP. All ADR-0001 goals met:

1. ✓ Python orchestrator daemon with systemd unit.
2. ✓ Agent definitions reconciled (ADR-0003; not edited in this slice).
3. ✓ Telegram notification integration.
4. ✓ Tier-1 secret management (`EnvironmentFile=`).
5. ✓ Sequential batch execution.
6. ✓ Budget enforcement across wall-clock / tokens / dollars / rate
   limits.
7. ✓ Checkpoint/resume across sessions.

Explicit Phase-1 non-goals deferred: parallel Implementers (ADR-0005),
worktree orchestration (ADR-0005), Playwright integration / UI gate
mechanics (ADR-0004), `huragok init` scaffold (ADR-0007),
retrospective engine (ADR-0006). Each of those has a clean extension
seam in the B2 module tree — the interfaces hold.

## Amendment 2026-04-22: batch log file mirror

**Driven by:** `docs/claude-code-prompts/phase-1/slice-b2-prompt-amend-1.md`

Completes Deviation 5 from the original B2 notes: the daemon now
mirrors structured log records to `.huragok/logs/batch-<batch_id>.jsonl`
directly, so `huragok logs` works out of the box without operators
redirecting stdout themselves. ADR-0002 D9 is now honoured in full.

### `configure_logging` signature change

```python
def configure_logging(
    level: str = "info",
    json_output: bool = True,
    file_path: Path | None = None,  # NEW
) -> None: ...
```

When `file_path` is supplied, a `_FileTeeProcessor` is appended to the
structlog processor chain. It sits after the JSON renderer, writes the
already-rendered string (plus newline) to a UTF-8, line-buffered,
append-mode handle, and returns the string unchanged so the stdout
`WriteLoggerFactory` still emits it. Parent directory is auto-created
via `Path.mkdir(parents=True, exist_ok=True)`. `OSError` on open is
caught, a WARN (`logging.file_sink.open_failed`) is emitted on stdout,
and the daemon continues with stdout-only logging — the file sink is
strictly additive. Subsequent write failures on the handle (disk full,
EPIPE, etc.) close the handle silently; stdout keeps flowing.

Module-scope `_ACTIVE_FILE_SINK` tracks the currently-installed tee so
a re-configure closes the prior handle cleanly. A new public function
`close_file_sink()` exposes the teardown path for the supervisor's
`finally` block.

### Wiring location

`orchestrator/supervisor/loop.py::run_supervisor` — the single new
wiring call sits immediately after `batch_id = _peek_batch_id(root)`,
before the dispatcher is constructed. A corresponding
`close_file_sink()` lives in the existing `finally` block so the file
descriptor is released even on abnormal exit.

The prompt named `run()` as the expected wiring site; `run_supervisor`
is the function that actually reads state.yaml and computes `batch_id`,
and all existing supervisor integration tests enter the daemon through
it. Duplicating the read or moving it to `run()` would have been
either a code-smell or a larger refactor than the "single wiring call"
the prompt scoped. Test coverage is identical either way — all
supervisor paths flow through `run_supervisor`.

### Re-configure vs. `add_file_sink`: re-configure chosen

`configure_logging` is idempotent-ish: call it once from the CLI at
startup (stdout-only), then again from the supervisor once the batch
id is known (with the file path). `structlog.configure` replaces the
processor chain wholesale, so there is no duplication hazard. The one
subtlety: `cache_logger_on_first_use` is now set to **False** so
loggers bound prior to re-configuration pick up the new chain on their
next log call. The daemon makes low-tens of log calls per second at
peak; the cache-miss cost is negligible.

No separate `add_file_sink` function was needed.

### Tests

No deviations from the prompt's list. Added:

- `tests/test_logging_setup.py` — 7 tests covering the stdout-only
  path, mirror behaviour, missing-parent auto-create, open-failure
  fallback + WARN, re-configure not duplicating stdout records,
  re-configure switching files cleanly, and `close_file_sink()`
  flushing / releasing the handle.
- `tests/supervisor/test_log_wiring.py` — one integration test
  running a fake-claude clean cycle and asserting
  `.huragok/logs/batch-001.jsonl` exists and holds well-formed
  structlog JSON records with the expected startup / session events.

**Totals after amendment:** 264 passed (was 256); ruff lint + format
clean; no new dependencies.

### Sharp edges introduced

- `cache_logger_on_first_use=False` is now the permanent default for
  this repo. Any future code that relies on structlog caching (there
  is none today) would need to reset that choice — flagged here so a
  grep for `cache_logger_on_first_use` lands on this note.
- The `_FileTeeProcessor`'s `close()` is idempotent and tolerates
  double-close, but it does NOT flush before closing — line buffering
  plus the `\n` written per record means the OS has already flushed.
  If someone later disables line buffering, revisit.
- The prior B2 deviation note about redirecting stdout under systemd
  (`StandardOutput=append:...`) is now redundant for the batch log
  path, but still useful for operators who want stdout captured
  *somewhere* when running outside systemd. `docs/deployment.md` was
  not edited in this amendment — it's slightly stale on this point,
  but not wrong.
