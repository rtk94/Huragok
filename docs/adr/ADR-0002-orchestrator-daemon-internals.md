# ADR-0002: Orchestrator Daemon Internals

**Status:** Accepted
**Date:** 2026-04-21
**Author:** Rich (with in-repo review by Claude Code)
**Supersedes:** —
**Related:** ADR-0001 (charter), ADR-0003 (agent definitions, pending), ADR-0004 (frontend testing & UI gate, future), ADR-0005 (parallelism, future), ADR-0006 (retrospectives & iteration, future), ADR-0007 (bootstrap & distribution, future)

## Context

ADR-0001 established the two-tier architecture (outer orchestrator, inner coordinator), the file-based state discipline, the six-role agent roster, four budget dimensions, the UI gate policy, and the deployment topology. It stopped short of orchestrator implementation details by design.

This ADR fills that gap. It pins the nine interlocking decisions that determine how the orchestrator daemon is built before any `orchestrator/` code is written. These decisions interact: the process model dictates how the session pipeline is structured, the session pipeline dictates what budget accounting can observe, observability constrains what error taxonomy can distinguish. Treating them separately invites retrofit churn. Treating them together is ADR-0002.

The scope is intentionally narrow: this ADR does **not** define the agent contracts (that's ADR-0003), the UI gate mechanics (ADR-0004), or the retrospective engine (ADR-0006). It defines the substrate those higher-level features will plug into.

## Decisions

### D1. Process model: asyncio supervisor with subprocess session runners

The orchestrator is a single Python process running an `asyncio` event loop. One daemon per active batch; no multi-tenant host. The daemon is composed of three long-lived coroutines — **Supervisor**, **Budget Tracker**, and **Notification Dispatcher** — plus short-lived **Session Runner** coroutines spawned one-per-session.

- **Supervisor** owns the state machine. It reads `state.yaml` at startup, picks the next action (launch session / pause / halt / await reply), and orchestrates the other components. One at a time. There is no concurrency of sessions in Phase 1 (ADR-0001 defers parallelism to ADR-0005).
- **Session Runner** is a short-lived coroutine that spawns `claude -p ... --output-format stream-json` as an `asyncio.subprocess`, pumps its stdout line-by-line through a parser, emits events to the other components, and exits when the subprocess exits.
- **Budget Tracker** owns per-batch accounting (tokens, dollars, wall-clock, rate-limit windows). It receives events from the Session Runner and emits threshold signals to the Supervisor.
- **Notification Dispatcher** owns Telegram I/O: outbound sends, inbound polling, reply parsing. It feeds parsed replies to the Supervisor.

Components communicate via `asyncio.Queue` instances, not shared mutable state. All persistence is through the state module (D3), never directly via component-owned file writes.

**SIGTERM handling:** the Supervisor installs a signal handler that sets a `shutting_down` event. On the next loop tick each component checks the event, finishes its in-flight unit of work (the Session Runner finishes parsing the current stream-json line, the Budget Tracker commits the current accounting delta, the Dispatcher finishes the current send), writes a `halted: operator-sigterm` marker to `state.yaml`, and exits cleanly. A second SIGTERM is treated as SIGKILL and skips graceful shutdown. The state on disk must be recoverable from a `kill -9` at any instant — this is a constraint on D3's atomic-write protocol.

**Why not threads:** subprocess I/O is the dominant workload and `asyncio` handles it without locks. A thread pool buys nothing.

**Why not a supervisor/worker split across processes:** premature. One process is operationally simpler, debuggable with a single `py-spy dump`, and the Phase 1 workload is sequential. If parallel Implementers (ADR-0005) need worker processes later, the supervisor/worker split becomes a refactor with a clear motivation, not speculation.

### D2. Session invocation pipeline

Each session is a single `claude -p "<prompt>" --output-format stream-json --append-system-prompt "<orchestrator-context>"` invocation in a subprocess, with `cwd` set to the target repo root.

The prompt passed to `-p` is minimal and deterministic:

```
Read .huragok/state.yaml. Execute the Orchestrator agent per .claude/agents/orchestrator.md. Return when the current task reaches a terminal in-session state (implementing-complete, testing-complete, reviewing-complete, or blocked). Do not exceed the session budget in state.yaml.
```

The prompt is constant across sessions in a batch; per-session variance lives in `state.yaml`. This keeps the command line auditable and lets the orchestrator diff invocations cheaply.

**Stream-json parsing:** each stdout line is a JSON object. The parser dispatches on the `type` field. Known types for Phase 1: `system` (session init, ignored after sanity check), `assistant` (main-model turns; token counts extracted), `user` (tool results; inspected for `is_error` to surface subprocess-level failures), `result` (final session summary; authoritative token and cost totals for budget reconciliation). Unknown types are logged at INFO and ignored — the stream format is expected to evolve.

**Session-end detection:** the subprocess exiting with code 0 and a `result` event received is a **clean end**. Subprocess exit with non-zero code, or EOF without a `result` event, is a **dirty end** and triggers D7 error handling. The distinction matters for retry policy.

**Timeouts:** each session has a hard wall-clock timeout, default 45 minutes, configurable per batch. Exceeding it sends SIGTERM to the subprocess, waits up to 30 seconds, then SIGKILL. The Supervisor records this as a `timeout` dirty end. 45 minutes is the heuristic upper bound on what a sanely-scoped single session should need; it's a smell, not a ceiling, and tripping it surfaces an ADR-0003 agent-prompt problem more often than a legitimate workload.

**Session budget handoff:** the Supervisor writes a per-session budget into `state.yaml` before launch (remaining tokens, remaining dollars, session timeout). The Orchestrator agent is expected to respect it. Actual enforcement is still the outer orchestrator's job (D4) — the in-session number is advisory, to give the agent headroom awareness for scoping its work.

**Environment:** `CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-4-6` is exported before spawn so worker subagents default to Sonnet while the Orchestrator agent runs on Opus (see ADR-0001 D4). `ANTHROPIC_API_KEY` is passed through from the orchestrator's systemd-provided env. No other env vars leak to the subprocess by default; the Supervisor uses a scrubbed env dict.

**Claude Code version requirement:** minimum `@anthropic-ai/claude-code` version is **`>=2.1.91`** (early April 2026). Below this, stream-json evolution produces parsing drift we don't plan to accommodate. On daemon startup, the Supervisor runs `claude --version`, parses the reported version, and refuses to start if it's below the minimum. The minimum version is a constant in `orchestrator/constants.py` and is reviewed per Huragok release, not per session.

### D3. State schemas and atomic write protocol

Every schema in `.huragok/` is defined as a **Pydantic v2 model** in `orchestrator/state/schemas.py`. Pydantic is the single source of truth: YAML files validate against the model on every read, and writes serialize from the model with a deterministic dumper. Schema drift between the daemon and in-session agents is caught at read time, not silently tolerated.

#### `.huragok/state.yaml`

```yaml
version: 1                          # schema version; bump on breaking changes
phase: idle                         # idle | running | paused | halted | complete
batch_id: null                      # str | null
current_task: null                  # task-id | null
current_agent: null                 # orchestrator | architect | implementer | testwriter | critic | documenter | null
session_count: 0                    # int — sessions launched this batch
session_id: null                    # uuid of in-flight session or last-completed
started_at: null                    # ISO-8601 UTC
last_checkpoint: null               # ISO-8601 UTC
halted_reason: null                 # str | null — populated on halt
budget_consumed:
  wall_clock_seconds: 0
  tokens_input: 0
  tokens_output: 0
  tokens_cache_read: 0
  tokens_cache_write: 0
  dollars: 0.0
  iterations: 0
session_budget:                     # advisory budget written BEFORE session launch
  remaining_tokens: null
  remaining_dollars: null
  timeout_seconds: null
pending_notifications: []           # queue of unsent notifications (Dispatcher drains)
awaiting_reply:                     # set when a notification needs operator action
  notification_id: null
  sent_at: null
  kind: null                        # foundational-gate | budget-threshold | blocker | batch-complete | error | rate-limit
  deadline: null                    # optional soft deadline
```

#### `.huragok/batch.yaml`

```yaml
version: 1
batch_id: batch-001
created: 2026-04-21T09:00:00Z
description: "One-line human-readable summary"
budgets:
  wall_clock_hours: 12
  max_tokens: 5_000_000
  max_dollars: 50.00
  max_iterations: 2
  session_timeout_minutes: 45
notifications:
  telegram_chat_id: null              # null → use daemon default
  warn_threshold_pct: 80              # notify at 80% of any budget
tasks:
  - id: task-0001
    title: "Add user preferences endpoint"
    kind: backend                     # backend | frontend | fullstack | docs
    priority: 1
    acceptance_criteria:
      - "GET /api/preferences returns 200 with current user's prefs"
    depends_on: []
    foundational: false               # set by Architect; default false (ADR-0001 D6)
```

#### `.huragok/work/<task-id>/status.yaml`

```yaml
version: 1
task_id: task-0001
state: pending                      # pending | speccing | implementing | testing | reviewing | software-complete | awaiting-human | done | blocked
foundational: false
history:                            # append-only state transitions
  - at: 2026-04-21T09:02:11Z
    from: pending
    to: speccing
    by: orchestrator
    session_id: 01HXYZ...
blockers: []                        # populated by Critic on block
ui_review:                          # populated for UI-touching tasks
  required: false
  screenshots: []                   # relative paths under task folder
  preview_url: null
  resolved: null                    # null | approved | rejected
```

The content files (`spec.md`, `implementation.md`, `tests.md`, `review.md`, `ui-review.md`) are free-form markdown with required top-of-file frontmatter:

```
---
task_id: task-0001
author_agent: architect
written_at: 2026-04-21T09:15:00Z
session_id: 01HXYZ...
---
```

Frontmatter is machine-readable and validated; body is markdown for humans and agents alike.

#### Atomic writes

All writes to `.huragok/` files use write-to-temp-and-rename on the same filesystem:

1. Write payload to `.huragok/<path>.tmp.<pid>.<uuid>`
2. `fsync` the temp file.
3. `rename` to the target path. (POSIX guarantees this is atomic within a filesystem.)
4. `fsync` the containing directory.

A `kill -9` at any step either leaves the pre-write file intact (step 1 or 2 incomplete) or completes the rename (step 3 done). Partial writes are never observable by readers. Stale `.tmp.*` files are cleaned up by the Supervisor at startup.

`decisions.md` and per-batch audit files (D9) are strictly append-only. Writers open with `O_APPEND` and write a single block per commit. No truncation, no rewrites.

#### Schema versioning

Every schema file carries a `version: 1` field. Loaders check the version and refuse to run against a newer version than they know. Breaking changes bump the integer and ship with a one-shot migration script in `scripts/migrations/`. No silent upgrades.

### D4. Budget accounting mechanics

Budgets are evaluated on **every stream event** that carries token or cost information, not at session end. Threshold crossings are observable mid-session and can trigger early termination.

#### Token accounting

Per-session, reconciled at session end. During a session, the Budget Tracker maintains a running delta keyed by `session_id`. Each `assistant` or `result` event's `usage` block contributes to the delta. The canonical per-session total is the `usage` block of the terminal `result` event; the running delta is a live estimate and is overwritten by the `result` total on clean session end.

#### Dollar accounting: two sources, two latencies

Dollar accounting is hybrid by design. Live estimates come from a local pricing table; authoritative reconciliation comes from Anthropic's Cost API when available.

**Live estimation** (always on): the local pricing table at `orchestrator/pricing.yaml` is used to compute dollars from token counts as each stream event arrives. This is what budget thresholds are evaluated against in real time. Stale pricing produces stale live estimates, but never delays enforcement.

```yaml
version: 1
updated: 2026-04-21
models:
  claude-opus-4-7:
    input_per_mtok: 15.00
    output_per_mtok: 75.00
    cache_read_per_mtok: 1.50
    cache_write_per_mtok: 18.75
  claude-sonnet-4-6:
    input_per_mtok: 3.00
    output_per_mtok: 15.00
    cache_read_per_mtok: 0.30
    cache_write_per_mtok: 3.75
  claude-haiku-4-5-20251001:
    input_per_mtok: 1.00
    output_per_mtok: 5.00
    cache_read_per_mtok: 0.10
    cache_write_per_mtok: 1.25
```

Pricing is versioned and updated manually. The daemon refuses to start if any model referenced in an active session is missing from the table.

**Authoritative reconciliation** (optional): if the operator provides an Admin API key (`ANTHROPIC_ADMIN_API_KEY`, separate from the standard `ANTHROPIC_API_KEY`), the Budget Tracker queries `/v1/organizations/cost_report` at session end and batch end, with a ~5-minute delay tolerance. Returned costs supersede the table-derived estimates in `state.yaml.budget_consumed.dollars` and are written to the audit log as a `cost-reconciliation` event. The Cost API's 5-minute lag is why reconciliation is not the real-time path, but reconciled totals are strictly more accurate than local-table totals — and catch pricing changes automatically.

Without an Admin API key the daemon runs fine on table-only estimates. Operators who want authoritative dollar figures (e.g. for chargebacks or tight budgets) provision the Admin key; others accept table drift as a known limitation. The operator decision is made once per install, not per batch.

#### Wall-clock budget

A monotonic clock timer started at batch start. Pauses (rate-limit, awaiting-reply) do not stop the clock — 12h of wall time is 12h of wall time, regardless of what the daemon is doing. This is the strictest of the four budgets and usually the first to trip.

#### Rate-limit awareness

The Budget Tracker tracks two windows: the 5-hour rolling session-usage window and the weekly cap. It maintains a persistent counter at `.huragok/rate-limit-log.yaml`, appending a timestamped entry on every session launch.

**Log truncation:** on daemon startup, entries older than **7 days** are dropped from `rate-limit-log.yaml`. Older entries serve no rate-limit purpose (both relevant windows are shorter) and archival is handled by the audit stream (D9). The file stays bounded at roughly one week of session-launch records, under 50 lines in typical operation. Session-launch history for purposes other than rate-limit tracking — retrospectives, debugging, accounting — is persisted in the per-batch audit log, not here.

Before each launch the Supervisor queries the Tracker: "am I safe to launch a session now?" The Tracker replies with `ok | warn | defer <seconds>`. `warn` sends a Telegram notification; `defer` pauses the daemon until the window opens. This counter is approximate — Anthropic's actual rate limits are authoritative — but it prevents the daemon from hammering the API into a hard 429 wall.

#### Threshold semantics

- **80% of any budget** → notify the operator, continue running.
- **100% of any budget** → halt session launches; complete the in-flight session (do not SIGTERM it); write halt summary; notify operator.
- **Session budget overshoot** (an individual session exceeding its advisory budget by >25%) → log a WARN, do not intervene, let the session finish. This catches runaway agents without killing productive work.

Budget overshoots are telemetry for agent-prompt tuning, not a normal failure mode.

### D5. CLI surface and daemon-state interaction

The `huragok` CLI is the operator's interface. It is built with **Typer**. Command shape:

```
huragok submit <batch.yaml>        # queue a batch for execution
huragok run [--batch <id>]         # start the daemon in the current repo (foreground)
huragok start                      # start as a background daemon (systemd in prod)
huragok stop                       # graceful shutdown of a running daemon
huragok status                     # show current state (reads .huragok/state.yaml)
huragok halt                       # signal a running daemon to halt after in-flight session
huragok reply <verb> [args]        # send a reply to a pending notification (continue|iterate|stop|escalate)
huragok logs [--follow] [--level]  # tail the current batch log
huragok tasks [--state <state>]    # list tasks in current batch, optionally filtered
huragok show <task-id>             # print status + artifacts for a task (summary by default, --full for inline dump)
```

**Daemon-CLI communication is via the filesystem plus signals, not a socket.** Rationale: everything the CLI needs to *show* is already in `.huragok/state.yaml` and `.huragok/work/`. The only CLI → daemon *commands* are stop, halt, and reply — all of which are adequately served by writing a request file and sending a signal:

- `huragok stop` / `halt` → writes `.huragok/requests/stop` or `.huragok/requests/halt`, sends `SIGUSR1` to the daemon PID (read from `.huragok/daemon.pid`). Supervisor checks the requests dir on the next loop tick.
- `huragok reply <verb>` → writes `.huragok/requests/reply-<notification_id>.yaml` with the parsed reply; Dispatcher picks it up on its next poll.

This avoids a daemon-local socket, its auth story, and its lifecycle. The PID file is created on daemon start and removed atomically on clean shutdown (with stale-PID detection on startup via `/proc/<pid>` check).

`huragok run` starts the daemon in the foreground — this is the mode used during development and interactive sessions on 001 Shamed Instrument. `huragok start` is a convenience that invokes the systemd unit (D8). The daemon itself is the same binary.

The CLI is importable as `orchestrator.cli:main` and registered in `pyproject.toml`'s `[project.scripts]`.

### D6. Telegram reply ingestion: polling, not webhooks

The Notification Dispatcher runs a long-polling loop against `getUpdates` with a 25-second timeout. One chat ID per daemon instance (the `telegram_chat_id` in batch.yaml, or daemon-default from env). Incoming messages are parsed as:

```
<verb> [notification_id] [free-form annotation]
```

Valid verbs are the four from ADR-0001 D5 (`continue`, `iterate`, `stop`, `escalate`), plus convenience aliases (`c`, `i`, `s`, `e`, and `ok`/`yes` as synonyms for `continue`). A reply without a `notification_id` is matched against the single pending notification if exactly one is outstanding; otherwise the Dispatcher responds with a disambiguation prompt listing outstanding notification IDs.

Replies are written to `.huragok/requests/reply-<id>.yaml` and the Supervisor picks them up on the next tick.

**Why polling, not webhooks:** webhooks require an externally-reachable HTTPS endpoint with a valid cert. 031 Exuberant Witness is behind double NAT (per the existing OpenWrt DDNS setup for rknepp.com); 001 Shamed Instrument is not publicly reachable at all. Long-polling works from anywhere with outbound HTTPS. The latency cost (up to 25s between send and reply) is tolerable for human-in-the-loop gates.

**Bot identity:** Huragok uses a dedicated Telegram bot separate from `@rtk_hal_9000_bot` (OpenClaw's). One bot per system keeps authorization scopes clean and avoids cross-contamination between OpenClaw's general-purpose channel and Huragok's batch-specific notifications. Bot token is in `.env` (Tier-1 secrets per ADR-0001 D9).

**Idempotency:** each outbound notification carries a `notification_id` (UUIDv7, time-ordered). Replies reference the ID, and the Dispatcher deduplicates — a reply received twice (e.g. operator retry over flaky connection) applies once. This matters because the polling loop can briefly double-deliver on Telegram API retries.

**Telegram unreachable handling:** if Telegram is unreachable (network failure, API outage, authentication failure) for more than 10 minutes while a notification is outstanding, the Dispatcher logs at `critical` and the Supervisor transitions `state.yaml.phase` to `paused` with `halted_reason: notification-backend-unreachable`. The daemon does not launch further sessions until Telegram recovers or the operator intervenes. `huragok status` surfaces the paused state and the reason, so the operator discovers the problem by checking status (manually or via a scheduled cron).

There is no email fallback in Phase 1. A second notification backend is future work under a dedicated ADR; the Dispatcher's interface is designed to accept additional backends (Matrix, Discord, generic webhook) without reshaping D6's contract.

### D7. Error taxonomy and retry policy

Every session-level failure is classified into one of seven categories. The taxonomy is explicit because the retry behavior diverges meaningfully across them.

| Category | Detection | Retry policy |
|---|---|---|
| **clean-end** | exit 0 + `result` event | Not a failure. Advance state machine. |
| **rate-limited** | API 429 in stream, or pre-check from Tracker | Defer per `retry-after`, then relaunch same session. No attempt counter. |
| **context-overflow** | agent reports running out of context | Halt batch. Human intervention required; this is an ADR-0003 scoping issue. |
| **session-timeout** | wall-clock timeout triggered SIGTERM | Relaunch fresh session, increment attempt counter (max 2 per task). |
| **subprocess-crash** | non-zero exit code, no `result` event | Relaunch fresh session, max 2 attempts. Log the tail of stderr. |
| **transient-network** | connection reset / DNS / TLS transient | Exponential backoff, 3 attempts, then escalate to operator. |
| **unknown** | anything that doesn't match above | Log full context, halt batch, notify operator. Do not retry blind. |

Attempt counters are per-task, not per-session: a task that has seen two session-timeout retries and then a subprocess-crash is halted, not retried a third time. The counters live in `status.yaml.history`.

**Escalation** sends a Telegram notification with category, task ID, session log tail, and reply verbs. The operator can `continue` (advance despite the failure — rare, usually wrong), `iterate` (reset task state and try a fresh session with no attempt history), `stop` (halt the batch), or `escalate` (drop the operator into an interactive Claude Code session at the point of failure).

### D8. Systemd unit and process lifecycle

The orchestrator runs as a systemd **user service** on 031 Exuberant Witness (and optionally on 001 Shamed Instrument). Unit file is shipped in the repo at `scripts/systemd/huragok.service` and installed via the `huragok init` scaffolder (ADR-0007).

```ini
[Unit]
Description=Huragok — autonomous multi-agent development orchestration
Documentation=https://github.com/rtk94/huragok/blob/main/docs/adr/ADR-0001-huragok-orchestration.md
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
WorkingDirectory=%h/huragok-runtime
EnvironmentFile=%h/.config/huragok/huragok.env
ExecStart=%h/.local/bin/uv run huragok run
Restart=on-failure
RestartSec=30s
TimeoutStartSec=60s
TimeoutStopSec=120s
KillMode=mixed
KillSignal=SIGTERM

# Tier 1 secrets (ADR-0001 D9). Upgrade to LoadCredential for Tier 2.
# Filesystem hardening — target repo lives under %h; no system writes.
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/huragok-runtime
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

**Notes on the unit:**
- `Type=notify` requires the daemon to call `sd_notify(READY=1)` after startup. Python integration via `systemd-python` or a minimal socket write — likely the latter to avoid a C-extension dependency.
- `%h/huragok-runtime` is a symlink-or-directory the operator sets up per batch. It's distinct from the repo root because a user might run batches against several repos; the unit is batch-agnostic.
- `Restart=on-failure` + 30s delay means a daemon crash is automatically recovered, but a batch halt (exit 0 with halted state) is not re-run — the operator decides.
- Hardening (`ProtectSystem`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`) matches the posture OpenClaw uses. No CAP_NET_ADMIN or similar is needed; this is a pure-userspace service.

**User service, not system service:** the daemon only needs the operator's home directory and the operator's Claude Code installation. Running as a system service would require duplicating API key secrets into root-owned credential storage and coordinating user/root permissions on `.huragok/` files. User-service keeps everything in one UID's namespace.

**PID tracking:** the daemon writes its own PID to `.huragok/daemon.pid` on startup and removes it on clean exit. This is belt-and-suspenders alongside `systemctl --user status`; the PID file is what `huragok stop` and `huragok halt` read.

### D9. Observability: structured logs, a status view, and a per-batch audit trail

Three layers:

**1. Structured logs.** All daemon log output is JSON Lines, emitted to stdout (captured by journald under systemd) and optionally mirrored to a rotating file at `.huragok/logs/batch-<batch_id>.jsonl`. Log library is **structlog**, configured with a single JSON renderer. Every record carries:

```json
{
  "ts": "2026-04-21T14:03:22.117Z",
  "level": "info",
  "component": "session-runner",
  "batch_id": "batch-001",
  "task_id": "task-0042",
  "session_id": "01HXYZ...",
  "event": "session.stream.assistant",
  "msg": "...",
  "...additional fields..."
}
```

Five log levels used: `debug` (off by default; enable with `LOG_LEVEL=debug`), `info` (everyday events), `warn` (budget warnings, retry attempts, schema oddities), `error` (session failures, classification hits in D7), `critical` (unrecoverable state, halt conditions).

Batch log files rotate naturally: one per batch, bounded by batch lifetime. No daemon-side rotation policy is needed.

**2. Live status view.** `huragok status` renders the state file plus derived info:

```
huragok — batch-001 (running)
═══════════════════════════════════════════════════════════════
Elapsed:        3h 47m / 12h 00m    (31%)
Tokens:         1.82M / 5.00M       (36%)  input 1.12M  output 0.70M
Dollars:        $18.92 / $50.00     (38%)  (table est., not reconciled)
Iterations:     0 / 2
Sessions:       7 launched, 6 clean, 1 retry

Current task:   task-0042 (implementing)
  agent:        implementer
  started:      2026-04-21T13:42:11Z  (21m ago)
  session:      01HXYZ8JKQ...

Tasks:          12 total · 6 done · 1 in-flight · 5 pending · 0 blocked

Pending notifications:  (none)
```

When the phase is `paused`, the header shows the reason (e.g. `paused — notification backend unreachable`). The dollar figure annotates whether it is reconciled (Admin API key present) or a table estimate. The same info is available as `huragok status --json` for programmatic consumption.

**3. Per-batch audit trail.** Every state transition (`.huragok/state.yaml` change, `status.yaml` state field change, agent artifact write, budget reconciliation) emits an audit event to **`.huragok/audit/<batch_id>.jsonl`** (per-batch file, not a cumulative one), append-only. The file is the authoritative record of "what happened in this batch" — logs can be rotated away; audit is forever.

Each event:

```json
{
  "ts": "...",
  "kind": "status-transition",
  "task_id": "task-0042",
  "from": "implementing",
  "to": "testing",
  "agent": "orchestrator",
  "session_id": "01HXYZ..."
}
```

Per-batch audit files (rather than a single cumulative `audit.jsonl`) keep each file bounded to the size of one batch, make cold-storage archival of finished batches trivial (compress the file, move it), and let the Retrospective session (ADR-0006) read a single file as its raw material. Audit writes go through the same atomic-append protocol as `decisions.md`.

**No metrics endpoint in Phase 1.** A `/metrics` Prometheus endpoint is tempting but adds a web framework dependency and a port to manage for zero Phase 1 value — there's no dashboard consuming it yet. If a real consumer appears (a homelab Grafana, say), it gets its own ADR.

## Consequences

**Positive:**

- Single-process asyncio is operationally simple, debuggable with standard tools, and has no premature distribution complexity.
- Pydantic-first schemas catch drift between daemon and agents at read time.
- Atomic write protocol survives SIGKILL at any instant — required for checkpoint/resume under rate-limit pauses.
- Filesystem-based CLI↔daemon communication means no socket lifecycle, no auth layer, and `huragok status` works even when the daemon is down (it reads state directly).
- Polling-based Telegram integration works behind NAT and requires no inbound network config.
- Explicit error taxonomy replaces "retry a few times and hope" with classifier-driven behavior.
- Structured logs + per-batch audit log separate volatile from durable observability correctly, and the per-batch audit file makes cold-storage archival trivial.
- Cost API reconciliation path means dollar totals can be authoritative without requiring perfect local pricing data.

**Negative:**

- Nine interlocking decisions in one ADR is a lot to absorb. The ADR will need revision as implementation surfaces contradictions.
- Polling incurs up to 25s reply latency. Acceptable for gates; would not scale to chatty interactive use.
- User-service systemd means Huragok runs as the operator's UID, not a dedicated `huragok` user. Blast-radius of a compromise is bounded only by filesystem permissions under `$HOME`.
- No metrics endpoint means ad-hoc investigation of running batches relies on tailing logs and reading status output. Fine today, a gap someday.
- Local pricing table still needs manual updates when operator chooses not to provision an Admin API key. Operators who skip reconciliation accept table drift as a known limitation.
- No email fallback for Telegram outages. The daemon pauses instead of routing to an alternative channel; the operator discovers the paused state by checking `huragok status`. Acceptable for Phase 1; revisit when a second backend becomes easy.

## Alternatives considered

**Threading instead of asyncio.** Rejected. All orchestrator work is subprocess + network I/O; asyncio handles it without locks. Threads buy nothing and add synchronization complexity.

**Supervisor + worker processes.** Rejected for Phase 1. Single-process is simpler; worker separation is a clear, motivated refactor when ADR-0005 introduces parallelism.

**Unix-socket RPC between CLI and daemon.** Rejected. State is already on disk; the incremental commands (stop, halt, reply) are adequately served by request files + SIGUSR1. Avoiding a socket eliminates an entire class of auth, lifecycle, and testing concerns.

**Telegram webhooks.** Rejected. Requires externally-reachable HTTPS; 031 is behind double NAT, 001 is not public. Polling is not elegant but it works everywhere.

**Per-event atomic state writes vs. batched.** Went with per-event (every state.yaml mutation is a full atomic rewrite). Batched would be faster but reopens the SIGKILL-mid-batch failure mode that D3 exists to eliminate.

**Prometheus `/metrics` endpoint.** Deferred. No Phase 1 consumer. Adds a port and a web dependency. Trivial to add later if a real need materializes.

**System service vs. user service.** Chose user service. Keeps secrets and filesystem permissions in one UID's namespace; avoids duplicating API key storage under root.

**Table-only dollar accounting, no Cost API.** Rejected. Stale pricing table produces stale dollar totals forever. Cost API reconciliation at session/batch end catches drift automatically and is strictly more accurate. Making reconciliation optional (Admin key gated) means operators who don't want to provision an extra key still get a working system, just with known drift.

**Single cumulative audit log.** Rejected. `.huragok/audit.jsonl` as one file grows without bound, mixes finished and in-flight batches in one stream, and has no natural archival seam. Per-batch files bound size by batch lifetime, make archival trivial, and give the Retrospective session a single self-contained file to read.

**Email fallback for Telegram outages in Phase 1.** Rejected. SMTP configuration is enough work to warrant deferring until genuinely needed. Pausing the daemon on Telegram failure and surfacing the pause in `huragok status` is a cheaper, sufficient signal path; a proper second-backend story can happen under its own ADR when warranted.

**Webhook-ingested replies from a non-Telegram channel (Matrix, Discord).** Out of scope for Phase 1. Telegram is already plumbed via OpenClaw experience; the Dispatcher interface is designed to accept additional backends later without reshaping D6.

**`sd_notify` via C extension vs. raw socket.** Leaning raw socket (a few lines of stdlib code) to avoid pulling `systemd-python`. Decision deferred to implementation; either is acceptable and the choice doesn't affect the architecture.

**Pinning an exact Claude Code version.** Rejected. Claude Code ships 30+ versions a month; pinning exactly is pointless churn. A minimum version (`>=2.1.91`) with a startup check is sufficient and survives the release cadence.

## Open questions

1. **`huragok show <task-id>` summary fields.** Summary-by-default is locked in (see D5). The exact fields rendered in the summary — spec title, state, last-agent, timestamps, blockers — will be refined during CLI implementation. Resolves at first code PR for `show`.
