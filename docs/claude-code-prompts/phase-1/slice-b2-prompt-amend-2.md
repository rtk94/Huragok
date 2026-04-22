# Huragok Phase 1 — Slice B2 Amendment 2: Smoke-Test Post-Mortem Fixes

**Amends:** `slice-b2-prompt.md` (and extends the changes introduced in `slice-b2-prompt-amend-1.md`)
**Source notes:** `docs/notes/slice-b2-build-notes.md`; first real end-to-end smoke-test run on 2026-04-22 (`smoke-001` in `~/Programming/huragok-smoke-test/`)
**Dated:** 2026-04-22

---

You are completing a set of post-mortem fixes discovered during Huragok's first real end-to-end run against Claude Code. The smoke test succeeded — five agents executed, a task transitioned to `done`, artifacts landed correctly, and the pipeline held up. But the run surfaced several real issues in the daemon's operator-facing behavior that need to be fixed before the next real run.

This amendment addresses nine items. Scope is tight but non-trivial; expect this to take longer than amendment 1.

Read this entire prompt before starting. Read the referenced files. If anything here contradicts an ADR or the existing build notes, STOP and ask.

---

## Context

The smoke test ran four Claude Code sessions (Architect → Implementer → TestWriter → Critic) against a trivial Python task. Total wall time: ~4 minutes. End state: task=`done`, four sessions ended `clean`, zero retries. The task artifacts (spec.md, implementation.md, tests.md, review.md, hello.py, test_hello.py) were all produced correctly and tests pass.

Observations from the run that motivate this amendment:

1. **Status view hides cache tokens.** Shown: `input 186 output 13.1K`. Actual state.yaml: 186 input + 13K output + **2.56M cache_read + 367K cache_write**. Cache tokens dominated real token usage and contributed most of the dollar figure, but were invisible to the operator.
2. **Batch did not transition to `complete`.** After all tasks were `done`, the daemon logged `supervisor.idle.no_pending_tasks` every few seconds forever. Phase stayed `running`. Current_task stayed pinned to the last task that completed.
3. **SIGINT-when-idle required two presses.** First Ctrl-C logged `signal.term.received`; daemon did not exit. Second Ctrl-C triggered `signal.term.escalating` (the force-kill path). No in-flight session — the "let the session finish first" logic should not apply when there is no session.
4. **Telegram `/start` logged as invalid_verb.** Not wrong, but operators will always type `/start` as their first message to a new bot. The noise pollutes structured logs.
5. **`huragok status` raises a FileNotFoundError traceback** when run in a fresh repo before `huragok submit`. The error message is opaque.
6. **Env allowlist doesn't include `CLAUDE_CODE_OAUTH_TOKEN`.** Blocker for systemd deployment under Max billing. Interactive use works because cached OAuth creds are read from `~/.claude/` via `HOME`, but systemd services may run with an isolated home.
7. **Tracker accounting was never verified against realistic Claude Code output.** The existing tests use contrived usage deltas; they don't replay multi-session runs with cache-heavy patterns. Cache dominated the real run and we should lock the accounting down with a regression test.
8. **`.env.example` implies `ANTHROPIC_API_KEY` is primary.** Per the smoke test's billing investigation, OAuth is preferable for Max subscribers — setting `ANTHROPIC_API_KEY` silently routes billing to API credits.
9. **`docs/deployment.md` doesn't document the OAuth/Max/API-key distinction or that `max_dollars` is theoretical-cost, not Max quota.**

---

## Read first

1. `CLAUDE.md` at repo root.
2. `docs/notes/slice-b2-build-notes.md` — the full B2 notes and the amendment-1 section (batch log mirror).
3. `docs/adr/ADR-0002-orchestrator-daemon-internals.md` — especially D1 (process model/signal handling), D4 (budget), D5 (CLI), D8 (systemd), D9 (observability).
4. `orchestrator/supervisor/loop.py` — understand the main loop's shape before changing it.
5. `orchestrator/supervisor/signals.py` — understand the current signal handling.
6. `orchestrator/budget/tracker.py` — understand the snapshot structure, `total_tokens()`, and `_check_thresholds`.
7. `orchestrator/cli.py` — especially `status` and `start`.
8. `orchestrator/notifications/telegram.py` — understand how reply verbs are parsed and logged.
9. `orchestrator/session/runner.py` — env scrubbing / `_INHERIT_ENV_KEYS` / `default_session_env`.
10. `orchestrator/logging_setup.py` — in case anything needs a logging-level tweak.

Do NOT modify `orchestrator/state/`, `orchestrator/session/stream.py`, `orchestrator/budget/pricing.py`, `orchestrator/budget/rate_limit.py`, or `orchestrator/errors.py`. Bounded changes to `orchestrator/session/runner.py` are permitted for item 6 only; other runner changes are out of scope.

---

## Scope: nine items

### Item 1 — Status view: expose cache tokens and other dimensions

File: `orchestrator/cli.py` (the `status` command's rendering logic).

Current output (example from the smoke test):

```
huragok — smoke-001 (running)
═══════════════════════════════════════════════════════════════
Elapsed:        0h 04m / 2h 00m    (4%)
Tokens:         13.3K / 2.00M    (1%)  input 186  output 13.1K
Dollars:        $6.67 / $10.00    (67%)  (table est., not reconciled)
Iterations:     0 / 2
Sessions:       4 launched, 4 clean, 0 retry
```

Target output:

```
huragok — smoke-001 (running)
═══════════════════════════════════════════════════════════════
Elapsed:        0h 04m / 2h 00m    (4%)
Tokens:         13.3K / 2.00M    (1%)
  input:        186
  output:       13.1K
  cache read:   2.56M
  cache write:  367K
Dollars:        $6.67 / $10.00    (67%)  (table est., not reconciled)
Iterations:     0 / 2
Sessions:       4 launched, 4 clean, 0 retry
```

Rules:

- The main `Tokens:` line percentage stays computed against `input + output` only (matching what `total_tokens()` returns and what `_check_thresholds` halts on). **Do not change tracker halting behavior in this amendment.** The display and the enforcement must agree.
- Sub-lines are indented two spaces, left-aligned, with labels `input:`, `output:`, `cache read:`, `cache write:` in that order. Use the same humanized-number formatter as today's view.
- Apply the same treatment to the JSON output (`--json`) if the current JSON omits cache fields — expose `tokens_cache_read` and `tokens_cache_write` as top-level keys in the rendered JSON. Check what's currently emitted before adding.
- Tests: add a CLI test in `tests/test_cli.py` that constructs a fixture state.yaml with nonzero cache values and asserts the rendered output includes the four sub-lines.

### Item 2 — Batch-complete transition

Files: `orchestrator/supervisor/loop.py` primarily; possibly `orchestrator/state/schemas.py` if `TaskState`/`Phase` need a new literal (it probably doesn't — `complete` may already exist).

Current behavior: after the last task transitions to `done`, the supervisor logs `supervisor.idle.no_pending_tasks` in a loop and never exits. Phase stays `running` forever.

Target behavior:

- At the top of each main-loop iteration, after reading state.yaml, if:
  - no in-flight session (current_task and current_agent are clear or the last session has ended), AND
  - all tasks in `batch.yaml` are in a terminal state (`done`, `blocked`, or other terminals as defined),
- then: transition phase to `complete`, write an audit event (`kind: "batch-complete"`), emit a notification (via the dispatcher — can be LoggingDispatcher if no Telegram), and cleanly exit the main loop. The daemon returns 0.

Check `orchestrator/state/schemas.py` for the existing `phase` Literal. `complete` should be a valid value; if it isn't, it needs to be added — treat that as a micro-exception to the "don't touch state/" rule, with the same justification as amendment 1's `HistoryEntry.category` change: additive, backward-compatible, required by the behavior the ADR already specifies.

ADR-0001 references "Sequential batch execution" as a Phase 1 goal; an eternally-running daemon after all work is done violates the spirit of that goal.

Tests: supervisor integration test — seed a batch with one task already `done`, run the loop, assert it exits cleanly with phase=`complete`. Also test: partial-completion (one task done, one blocked) → still transitions to `complete` (blocked is terminal).

### Item 3 — SIGINT-when-idle exits immediately

Files: `orchestrator/supervisor/signals.py`, `orchestrator/supervisor/loop.py`.

Current behavior: first SIGINT sets `shutting_down`; the main loop reads it on the next iteration. But when the daemon is in the "no pending tasks" idle loop, it's not doing anything that needs to finish, so the "graceful" path is just an artificial delay before Ctrl-C takes effect.

Target behavior:

- If there is no in-flight session at the moment SIGINT/SIGTERM arrives, the signal handler should cause the loop to exit within one tick (≤1 second).
- If there IS an in-flight session, preserve the current behavior: let the session finish, then exit.
- The second-signal escalation path (`os._exit(128 + SIGTERM)`) stays as-is — it's the correct hatch for "I really meant stop now" even during active work.

This likely composes naturally with item 2: once batch-complete transitioning is in place, the "idle forever" state should effectively vanish. The SIGINT-in-idle case still matters during inter-batch gaps where the daemon is waiting for `huragok submit`.

Tests: signals test that asserts SIGINT delivered to an idle loop causes the loop to exit within one tick; signals test that SIGINT delivered mid-session lets the session finish first (should already exist from B1 — verify).

### Item 4 — `/start` shouldn't pollute logs

File: `orchestrator/notifications/telegram.py`.

Current behavior: receiving `/start` (or any non-verb text) logs `telegram.reply.invalid_verb` at INFO level. `/start` is special because Telegram itself suggests it as the first message to every new bot, and it is not a reply to anything.

Target behavior:

- Recognize `/start` (case-insensitive, with or without following text) as a bot-initialization command, not a reply attempt. Suppress the `invalid_verb` log for this specific input, OR log it at DEBUG instead of INFO.
- Do NOT send a response to `/start` in this amendment. (If we wanted to, a nice touch would be responding with "Huragok bot ready." But that's a UX nicety that can go in a later amendment; this amendment is about not polluting logs.)
- Other unknown text (not a valid verb, not `/start`) continues to log `invalid_verb` at INFO as before.

Tests: telegram test asserting `/start` does not produce an `invalid_verb` INFO log, and does produce either nothing or a `bot.initialization` DEBUG log.

### Item 5 — `huragok status` graceful on missing state.yaml

File: `orchestrator/cli.py`.

Current behavior: running `huragok status` in a repo where `.huragok/state.yaml` doesn't exist produces:

```
error: [Errno 2] No such file or directory: '/path/to/.huragok/state.yaml'
```

Target behavior: render a friendly message and exit 0:

```
huragok — no batch submitted
Run `huragok submit <batch.yaml>` to begin.
```

Detection: if `state_file(root)` doesn't exist, render the alt message; otherwise proceed as today. Do NOT create the file as a side effect of running `status`.

Same treatment for `huragok tasks` if it has the same failure mode — check it and mirror.

Tests: CLI test asserting `huragok status` in a fresh `.huragok/` directory (no state.yaml, no batch.yaml) prints the friendly message and exits 0.

### Item 6 — `CLAUDE_CODE_OAUTH_TOKEN` in runner env allowlist

File: `orchestrator/session/runner.py` — specifically `default_session_env()`.

Current behavior:

```python
if "ANTHROPIC_API_KEY" in parent:
    scrubbed["ANTHROPIC_API_KEY"] = parent["ANTHROPIC_API_KEY"]
```

Target behavior: parallel conditional for the OAuth token:

```python
if "CLAUDE_CODE_OAUTH_TOKEN" in parent:
    scrubbed["CLAUDE_CODE_OAUTH_TOKEN"] = parent["CLAUDE_CODE_OAUTH_TOKEN"]
```

Add it alongside the existing `ANTHROPIC_API_KEY` block. No allowlist changes — both remain conditional pass-throughs. The order of the two blocks shouldn't matter; put `CLAUDE_CODE_OAUTH_TOKEN` second (below the existing API key block) for clarity.

Tests: runner test asserting that when both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` are in the parent env, both are forwarded to the scrubbed env; when only one is set, only one is forwarded; when neither is set, neither is in the scrubbed env.

Do NOT update `orchestrator/config.py` to add a `claude_code_oauth_token` field on `HuragokSettings`. The runner reads directly from `os.environ` for these credentials, and adding it to Settings would imply the CLI is involved — it isn't. Keep the change purely in the runner.

### Item 7 — Tracker accounting regression tests

File: `tests/budget/test_tracker.py`.

Add tests that replay realistic Claude Code usage sequences through `BudgetTracker` and assert final state:

- **Test A — "smoke-001 replay"**: construct a `BudgetTracker`, feed it a sequence matching the 2026-04-22 smoke run (four sessions, Opus + Sonnet + Sonnet + Opus, with cache-heavy usage patterns). After draining, assert `budget_consumed` values (input ≈ 186, output ≈ 13K, cache_read ≈ 2.5M, cache_write ≈ 370K) match within a reasonable tolerance (±5%). Assert `dollars` is computed using the shipped pricing table. The fixture sequence can be fabricated with realistic numbers; it doesn't need to be literal bytes from the audit log.
- **Test B — per-event deltas sum correctly**: feed a small sequence of known usage blocks (assistant events with known input/output/cache counts) and assert the tracker's final state matches the arithmetic sum of the deltas. This guards against the class of bug where event dispatch mishandles cache fields.
- **Test C — no double-counting of result vs. assistant usage**: a session typically emits assistant events with incremental usage throughout, followed by a `result` event with cumulative totals. Verify the tracker's design for this (check the current behavior in `_apply_event_usage` — the runner notes mention the terminal result's usage is authoritative, so assistant events and result events are both applied, which risks double-counting if both carry totals). If this IS a bug, FLAG it — do not silently change the tracker's logic. Add a test that documents the current behavior either way, and note any concerns in the build notes.

Item 7 is the one that might surface a real bug. Treat the tests as diagnostic first. If tests pass cleanly against today's tracker with the realistic sequence, good — we've locked down the accounting. If they reveal an actual accounting error, FLAG it and stop before silently rewriting tracker logic. We'd then have a follow-up amendment.

### Item 8 — `.env.example` OAuth-first rewrite

File: `.env.example`.

Reorganize to reflect the current best practice:

```
# Huragok runtime configuration
# ...existing header comment...

# --- Authentication (choose one) -------------------------------------------
#
# Option A (recommended for Claude Max subscribers): use cached OAuth
# credentials from `claude login`. No env var required for the daemon — it
# inherits HOME and the session runner finds ~/.claude/.credentials.json
# on its own.
#
# For systemd deployment or isolated environments where ~/.claude/ is not
# accessible, generate a long-lived token with `claude setup-token` and set:
#
#   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
#
# Option B (for API-billed users): set an API key. This routes billing
# through pay-as-you-go API credits on your Console account, NOT your Max
# subscription. Per the Claude Code docs, when both this variable and OAuth
# credentials are present, the API key wins.
#
#   ANTHROPIC_API_KEY=sk-ant-api-...
#
# WARNING: Do not set both simultaneously unless you specifically want
# API billing. Setting ANTHROPIC_API_KEY on a Max-subscribed machine
# silently costs money that would otherwise be covered by Max.

# Optional: Admin API key for Cost API reconciliation (ADR-0002 D4).
# Not required; daemon runs on table-only estimates without it.
# ANTHROPIC_ADMIN_API_KEY=sk-ant-admin-...

# --- Telegram notifications (optional) -------------------------------------
# ...existing sections...

# --- Huragok-specific settings ---------------------------------------------
# ...existing sections...
```

Keep existing comments where they still apply. Don't strip useful context just to match the template above — the template is a rough shape.

No test needed; `.env.example` isn't loaded by pydantic-settings directly.

### Item 9 — `docs/deployment.md` updates

File: `docs/deployment.md`.

Add two new sections (or extend existing ones if natural):

**Section: Authentication and billing**

Explain:
- Two billing routes: OAuth (Max subscription) and API key (pay-as-you-go).
- OAuth is preferred for Max subscribers; cached creds from `claude login` are sufficient for interactive/desktop use; `claude setup-token` + `CLAUDE_CODE_OAUTH_TOKEN` is needed for systemd under isolated home.
- When both are present, API key wins. Warn accordingly.
- Empirically verified on Claude Code 2.1.117 (smoke-test 2026-04-22): `claude -p` with cached OAuth creds and no `ANTHROPIC_API_KEY` in env routes billing to Max subscription. This contradicts at least one GitHub issue thread predating the fix; current behavior is the working one.

**Section: Budget interpretation for Max vs API**

Explain:
- `max_dollars` is **theoretical API cost** computed from the local pricing table. On API billing, it corresponds roughly to real dollars spent.
- On Max billing, the dollar figure is counterfactual — actual Max consumption is measured in rate-limit windows (5-hour and weekly caps on message/session counts). The dollar figure remains useful as a "work intensity" proxy but does not track your Max quota.
- Cache tokens (`cache_read`, `cache_write`) dominate the dollar estimate for small Claude Code tasks because Claude Code aggressively caches system prompts, agent files, and project context. Operators should budget `max_dollars` with this in mind — a small-looking task can cost several dollars of theoretical API usage due to cache.
- Suggest Max users set `max_dollars` 5-10x higher than they'd set for API billing, or treat the cap as a safety net rather than a primary gate.

Tone: operator-facing, not dev-facing. Reference the ADRs only for design rationale.

No tests.

---

## Bundling and commit strategy

All nine items land in one amendment. The build notes gain a single new section at the top (`## Amendment 2026-04-22: smoke-test post-mortem fixes`) with `**Driven by:**` pointing at this prompt's filename. Nest item-by-item details under that heading.

At the end of your run, all tests pass via `uv run pytest`. No `ruff` violations. No new dependencies.

---

## Deliverable: build notes section

Append to `docs/notes/slice-b2-build-notes.md` — a new heading immediately after the existing amendment-1 section:

```
## Amendment 2026-04-22: smoke-test post-mortem fixes

**Driven by:** `docs/claude-code-prompts/phase-1/slice-b2-prompt-amend-2.md`
**Motivated by:** first real end-to-end run on 2026-04-22 (`smoke-001` in `~/Programming/huragok-smoke-test/`) — see the summary in that run's audit log.

Items addressed:

1. Status view — cache tokens exposed as sub-lines.
2. Batch-complete transition — daemon exits when all tasks terminal.
3. SIGINT-when-idle — single-signal exit when no in-flight session.
4. `/start` — no longer logs as `invalid_verb`.
5. `huragok status` / `tasks` graceful on missing state.yaml.
6. `CLAUDE_CODE_OAUTH_TOKEN` added to runner env allowlist.
7. Tracker accounting regression tests.
8. `.env.example` rewrite (OAuth-first).
9. `docs/deployment.md` updates (auth/billing; Max vs API budget interpretation).

Per-item details:
```

Fill in each item's per-item section with:
- What changed (file-level summary).
- Any non-obvious design choice and its rationale.
- Test coverage added.
- Any deviations from this prompt.

For item 7 specifically: if the diagnostic tests surfaced a real bug, FLAG IT in a prominent callout and stop before attempting to fix tracker logic. The bug fix would be a separate follow-up.

---

## Stop conditions

Stop and ask the operator before proceeding if:

- Adding `complete` to the state.yaml `phase` Literal breaks existing tests beyond trivial adjustments.
- Item 7's tracker regression tests surface a real accounting bug — stop, flag, don't silently fix.
- Signal handling changes cause any existing B1/B2 test to fail.
- The batch-complete transition creates an edge case with the reply-file / awaiting-human state that isn't obviously resolvable.
- Any item turns out to be materially larger than scoped above.

Otherwise: execute the nine items, write the build notes, report back.
