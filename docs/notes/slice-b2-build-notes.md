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

## Amendment 2026-04-22: smoke-test post-mortem fixes

**Driven by:** `docs/claude-code-prompts/phase-1/slice-b2-prompt-amend-2.md`
**Motivated by:** first real end-to-end run on 2026-04-22 (`smoke-001`
in `~/Programming/huragok-smoke-test/`) — five Claude Code sessions
(Architect → Implementer → TestWriter → Critic, ~4 min wall time)
produced a `done` task and surfaced nine operator-facing issues that
this amendment closes.

Items addressed:

1. Status view — cache tokens exposed as sub-lines.
2. Batch-complete transition — daemon exits when all tasks terminal.
3. SIGINT-when-idle — single-signal exit when no in-flight session.
4. `/start` — no longer logs as `invalid_verb`.
5. `huragok status` graceful on missing state.yaml.
6. `CLAUDE_CODE_OAUTH_TOKEN` added to runner env allowlist.
7. Tracker accounting regression tests.
8. `.env.example` rewrite (OAuth-first).
9. `docs/deployment.md` updates (auth/billing; Max vs API budget
   interpretation).

### Per-item details

#### 1. Status view — cache tokens exposed as sub-lines

**Files:** `orchestrator/cli.py`, `tests/test_cli.py`.

The `Tokens:` line now renders `input`, `output`, `cache read`, and
`cache write` as four two-space-indented sub-lines. The main-line
percentage still uses `input + output` only — matching the
`total_tokens()` aggregate that `_check_thresholds` halts on, so the
displayed percent agrees with the enforcement rule. The JSON output
(`--json`) already exposes `tokens_cache_read` and
`tokens_cache_write` under the `budget_consumed` object — a direct
`model_dump` of `StateFile` — so no JSON change was required;
verified by a shell-level inspection of the current emitter before
editing.

Test added: `test_status_exposes_cache_token_sublines` seeds nonzero
cache values in `.huragok/state.yaml` and asserts the four sub-line
labels plus humanised figures appear in the rendered output.

#### 2. Batch-complete transition

**Files:** `orchestrator/supervisor/loop.py`, `tests/supervisor/test_loop.py`.

Added `_batch_is_complete(root)` which returns True when `batch.yaml`
exists, has ≥1 task, and every task's `status.yaml.state` is `done`
or `blocked` (both terminal per ADR-0002 D3). Added
`_transition_to_complete(ctx, state)` which flips the phase to
`complete`, clears `current_task`/`current_agent`/`session_id` so
operators don't see a dangling pointer in `huragok status`, writes a
`batch-complete` audit event, and dispatches a `batch-complete`
notification with no reply verbs (FYI, not a gate).

The new check sits inside the main loop's "next_task is None"
branch, so the per-iteration work is unchanged on the happy path.
The `phase` Literal in `orchestrator/state/schemas.py` already
included `complete`, so no schema migration was needed — the
micro-exception allowance for schema touches was NOT triggered.

**Tests added:**

- `test_loop_transitions_to_complete_when_all_tasks_done` — one task
  pre-seeded `done`, asserts `phase=complete`, `current_task=None`,
  and a `batch-complete` audit record.
- `test_loop_transitions_to_complete_with_blocked_task` — two tasks,
  one `done`, one `blocked`, asserts still transitions to `complete`
  (blocked is terminal).

Behaviourally this subsumes what `supervisor.idle.no_pending_tasks`
used to log forever. The pre-amendment integration test
`test_loop_exits_on_stop_request_even_without_work` — which
pre-marked its one task `done` and relied on a `stop` file to break
the loop — continues to pass because the loop now exits via
`complete` before the stop file is written; both paths return exit
code 0 and the test only checks the exit code.

#### 3. SIGINT-when-idle exits immediately

**Files:** `orchestrator/supervisor/loop.py`, `tests/supervisor/test_loop.py`.

The main loop already exited within one tick of `shutting_down`
firing — every idle-sleep site goes through `sleep_or_shutdown`. The
latent drag was in `run_supervisor`'s finally block, which did
`await asyncio.gather(tracker_task, dispatcher_task,
return_exceptions=True)`. A `TelegramDispatcher.start()` in
mid-`getUpdates` is blocked on a 25-second long-poll request that
does not observe the stop event until the HTTP call completes. Under
a real Telegram-enabled smoke run that added up to 25s of apparent
"daemon won't exit" time after the first Ctrl-C, which is what
prompted the operator to SIGINT again.

Introduced `_shutdown_background_tasks(tasks, grace_seconds)` plus a
`DEFAULT_SHUTDOWN_GRACE_SECONDS = 1.0` constant. The finally block
gathers with a 1s timeout, then cancels stragglers. The tracker's
drain loop completes in milliseconds in practice and the
`LoggingDispatcher` returns immediately on stop, so the only
coroutine this cancel can actually hit is the Telegram long-poll —
which is the motivating case.

Signal handler itself is unchanged: `_handle_term` already sets
`shutting_down` on the first signal and escalates via `os._exit` on
the second, matching ADR-0002 D1.

**Test added:** `test_shutdown_cancels_blocked_dispatcher_within_grace`
— uses a `_WedgedDispatcher` whose `start()` sleeps 30s regardless
of the stop event. Deletes `batch.yaml` so the loop sits in the
"waiting for submit" idle path. Writes `stop` after 150ms, asserts
the whole `run_supervisor` returns within 2.5s (generous envelope
for 1s grace + startup jitter).

The "SIGINT mid-session lets the session finish" behaviour is
preserved implicitly: `run_session` doesn't observe the stop event,
and the main loop is blocked in `await _launch_session(...)` for the
session's lifetime, so the finally block can't fire until the
session returns.

#### 4. `/start` no longer logs as `invalid_verb`

**Files:** `orchestrator/notifications/telegram.py`, `tests/notifications/test_telegram.py`.

Added `_is_bot_start_command(text)` which matches `/start`,
`/start@botname`, `/start foo`, and case-insensitive variants. The
`_handle_update` path calls it before falling back to the existing
`invalid_verb` log; on a match it emits a `telegram.bot.initialization`
DEBUG record (so operators can still grep for bot-init flow) rather
than the noisy INFO record. No outbound reply is sent — that's a UX
flourish for a later amendment.

**Tests added (3):**

- `test_start_command_does_not_log_invalid_verb` — bare `/start`,
  verifies no `invalid_verb` record and a DEBUG `bot.initialization`
  is present.
- `test_start_command_case_insensitive_and_with_payload` — `/START`,
  `/start hello`, and `/start@mybot` all take the bot-init path.
- `test_other_unknown_text_still_logs_invalid_verb` — non-`/start`
  unknown text preserves the prior INFO log.

Tests use `structlog.testing.capture_logs()` to intercept records;
structlog is wired to `WriteLoggerFactory` (not stdlib logging), so
pytest's `caplog` doesn't see the daemon's output.

#### 5. `huragok status` graceful on missing state.yaml

**Files:** `orchestrator/cli.py`, `tests/test_cli.py`.

`status` now catches `FileNotFoundError` on `read_state(root)` and
renders a friendly two-line message pointing at `huragok submit`.
The `--json` variant returns
`{"phase": "no-batch", "batch_id": null}` rather than a Python
traceback. Exit code is 0 in both cases.

`huragok tasks` already handles this path cleanly via
`_load_batch_if_any` returning None; verified that path prints
`no batch in flight` and exits 0 in a fresh `.huragok/`.

**Tests added (3):**

- `test_status_fresh_huragok_with_no_state_yaml_is_friendly`.
- `test_status_json_fresh_huragok_with_no_state_yaml`.
- `test_tasks_fresh_huragok_with_no_batch_is_friendly` — characterises
  the pre-existing behaviour so a regression would trip it.

#### 6. `CLAUDE_CODE_OAUTH_TOKEN` in runner env allowlist

**File:** `orchestrator/session/runner.py`, `tests/session/test_runner.py`.

Added a parallel conditional to the existing `ANTHROPIC_API_KEY`
pass-through in `default_session_env()`: when
`CLAUDE_CODE_OAUTH_TOKEN` is set on the parent, forward it. Order:
API key block first, OAuth block second. The runner doesn't enforce
precedence between the two — that's Claude Code's own auth logic.

Deliberately did NOT add a `claude_code_oauth_token` field on
`HuragokSettings`: the runner reads directly from `os.environ`, and
adding it to the settings type would imply CLI involvement that
doesn't exist.

**Test added:** `test_scrubbed_env_forwards_claude_code_oauth_token`
— three cases (neither set → neither forwarded; OAuth alone → OAuth
forwarded, API key absent; both set → both forwarded).

#### 7. Tracker accounting regression tests

**File:** `tests/budget/test_tracker.py`. No tracker-source changes.

Added three tests:

- **`test_tracker_replays_smoke_001_shape`** — four sessions in
  order (Opus / Sonnet / Sonnet / Opus) with per-turn deltas summing
  to input ≈ 186, output ≈ 13.1K, cache_read ≈ 2.56M, cache_write ≈
  367K. Asserts the tracker's final snapshot equals the arithmetic
  sum of deltas (exact equality, not ±5% — the tracker is integer
  arithmetic) and that the dollar figure lands in $4–$15, bracketing
  the smoke run's observed $6.67.
- **`test_tracker_per_event_deltas_sum_correctly`** — minimal
  three-event replay; guards the "one of the four columns silently
  dropped" class of regression.
- **`test_tracker_applies_both_assistant_and_result_event_usage`**
  — CHARACTERISATION test. See flag below.

##### 🚩 FLAG — latent double-counting risk in `_on_stream_event`

The existing tracker code in `BudgetTracker._on_stream_event`:

```python
if isinstance(stream_event, AssistantEvent):
    self._apply_event_usage(ctx, stream_event.usage, stream_event.model)
elif isinstance(stream_event, ResultEvent):
    # Result event's usage is authoritative for the session —
    # when we see it, subtract any running delta for this session
    # and replace with the authoritative totals. Implementation
    # note: B1 accumulates straight through because sessions are
    # strictly sequential; we only reset on session-ended below.
    self._apply_event_usage(ctx, stream_event.usage, stream_event.model)
```

The comment says "subtract and replace"; the code simply adds. Today
this is harmless because Claude Code's `result` events typically
carry empty `usage` blocks, and the smoke-001 replay test passes
with that shape. `test_tracker_applies_both_assistant_and_result_event_usage`
constructs a scenario where the result event carries non-empty
`usage` identical to an assistant event and asserts the current
behaviour: tokens double. **This is a latent bug, not a current one,
and per the amendment-2 stop condition I did NOT change tracker
logic to fix it.** A follow-up amendment should decide whether
result usage should supersede (per the comment) or be additive (per
the code), and update the tracker accordingly. The characterisation
test will flip sign on that change and document the decision.

Per the prompt's direction to flag rather than silently fix: no
fix applied here.

#### 8. `.env.example` rewrite

**File:** `.env.example`.

Reorganised into four sections: Authentication (choose one) →
Admin key (optional) → Telegram (optional) → Huragok-specific. The
Authentication section spells out Option A (Max via OAuth, cached
creds interactively, `CLAUDE_CODE_OAUTH_TOKEN` under systemd) and
Option B (`ANTHROPIC_API_KEY` for pay-as-you-go), plus the
API-key-wins precedence warning. No tests required —
`.env.example` is reference-only; `pydantic-settings` loads `.env`
(not `.env.example`).

#### 9. `docs/deployment.md` updates

**File:** `docs/deployment.md`.

Added two new sections between *Prerequisites* and *First-time
setup*:

- **Authentication and billing** — Options A/B, the
  systemd-isolated-home `setup-token` case, the precedence-and-warning
  paragraph, and the 2026-04-22 smoke-test empirical note on cached
  OAuth routing to Max.
- **Budget interpretation for Max vs. API** — `max_dollars` as real
  dollars on API billing, counterfactual on Max; the cache-dominates
  note; the suggestion to set `max_dollars` 5–10x higher on Max or
  treat it as a safety net.

Also rewrote the *Configure secrets* subsection to present OAuth
Option A as the default path, with API key as the alternative, and
removed the implication that `ANTHROPIC_API_KEY` is required.

### Deviations from the prompt

1. **Item 3 scope.** The prompt listed
   `orchestrator/supervisor/signals.py` and
   `orchestrator/supervisor/loop.py`. The actual fix is in
   `loop.py`'s finally block (bounded-wait-then-cancel for the
   tracker and dispatcher tasks). `signals.py` needed no change —
   the signal handler already sets `shutting_down` synchronously and
   the main loop already exits within one tick on that event. The
   operator-facing "did not exit" behaviour came from the Telegram
   dispatcher's in-flight poll, not from the signal path.

2. **Item 5 scope.** The prompt asked for the same treatment on
   `huragok tasks` if it shared the failure mode. It doesn't —
   `tasks` already uses `_load_batch_if_any` which swallows
   `FileNotFoundError`. Kept the test
   (`test_tasks_fresh_huragok_with_no_batch_is_friendly`) as a
   characterisation so a future regression in that path trips a
   failing test.

3. **Item 7 produced a flag.** See the boxed section above:
   `_on_stream_event` applies result-event usage additively rather
   than as a supersede, contradicting its own comment. Scope says
   "flag, don't silently fix"; I flagged.

4. **`/start` response (Item 4).** The prompt explicitly deferred
   sending a response to `/start` ("nice touch, but later
   amendment"). Not shipped; the DEBUG `bot.initialization` record is
   the only observable effect.

### Tests

Before this amendment: 264 passed.
After this amendment: **278 passed** (added 14 across five files).

| File                                           | Added |
| ---------------------------------------------- | ----- |
| `tests/test_cli.py`                            | +4    |
| `tests/supervisor/test_loop.py`                | +3    |
| `tests/notifications/test_telegram.py`         | +3    |
| `tests/session/test_runner.py`                 | +1    |
| `tests/budget/test_tracker.py`                 | +3    |
| **Total**                                      | **+14** |

- `uv run pytest` → all green.
- `uv run ruff check .` → clean.
- `uv run ruff format --check .` → clean.
- No new runtime dependencies.

### Sharp edges introduced

- **Shutdown grace is 1 second**, constant at module scope
  (`DEFAULT_SHUTDOWN_GRACE_SECONDS`). Coroutines that legitimately
  need longer than 1s to drain would be truncated by the cancel. The
  tracker's drain loop is bounded by `event_queue` size (microseconds
  in practice), and the Telegram dispatcher's own drain is trivial
  once its in-flight poll is cancelled. Flagged here so any future
  long-running coroutine added to `run_supervisor` knows about the
  1s ceiling.

- **`_is_bot_start_command` only matches `/start`.** Other slash
  commands (`/help`, `/stop`, etc.) still land in `invalid_verb`.
  That's deliberate: only `/start` is universally surfaced by
  Telegram as the "Start" button in every new bot chat. If a future
  amendment adds in-bot commands, the allowlist grows there.

- **OAuth pass-through is conditional and quiet.** If the operator
  sets neither `ANTHROPIC_API_KEY` nor `CLAUDE_CODE_OAUTH_TOKEN`
  and has no cached `~/.claude/` creds (e.g., systemd with sandboxed
  home), sessions will fail inside Claude Code's auth logic — not
  with a Huragok-level precheck. The existing `claude --version`
  startup check runs `claude` without auth, which succeeds, so
  Huragok boots even when subsequent sessions will fail on auth. An
  operator-facing auth-health check is a candidate for a future
  amendment; it's not in scope here.
