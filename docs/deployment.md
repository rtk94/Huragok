# Deploying Huragok

Operator-facing guide for installing, running, and managing the
Huragok orchestrator daemon. This document is intentionally narrow:
it covers what you need to get the daemon running against a target
repo and tell it when to stop. Design rationale lives in the ADRs
(`docs/adr/ADR-0001` through `ADR-0003`); implementation history lives
in `docs/notes/slice-*-build-notes.md`.

## Prerequisites

- **Python 3.12** — pinned in `.python-version`.
- **`uv`** — package manager. Install from <https://docs.astral.sh/uv/>.
- **Claude Code `>= 2.1.91`** — `npm install -g @anthropic-ai/claude-code`
  or equivalent. The daemon refuses to start below this minimum
  (ADR-0002 D2).
- **Claude Code authentication.** Either a Max subscription (cached
  OAuth credentials from `claude login`, or a long-lived token from
  `claude setup-token`) **or** an Anthropic API key
  (`ANTHROPIC_API_KEY`) for pay-as-you-go billing. See *Authentication
  and billing* below for the tradeoffs.
- **Optional: an Anthropic Admin API key** (`ANTHROPIC_ADMIN_API_KEY`)
  for authoritative dollar reconciliation via the Cost API. Without
  one the daemon uses the shipped pricing table for dollar estimates
  (ADR-0002 D4).
- **Optional: a Telegram bot** if you want operator-in-the-loop
  notifications. Without a bot token the daemon falls back to the
  no-network `LoggingDispatcher`; batches still run, but you won't
  get notified when one halts or hits a budget threshold.

## Authentication and billing

Huragok launches Claude Code as a subprocess, so whatever mechanism
Claude Code uses to authenticate is what the daemon uses too. There
are two supported routes, and they route billing to different accounts:

### Option A — Max subscription via OAuth (recommended for Max users)

Run `claude login` once on the machine that will host the daemon.
Claude Code caches OAuth credentials under `~/.claude/.credentials.json`.
The session runner inherits `HOME` from its parent process, so
`claude -p` finds those credentials automatically and sessions bill
against your Max subscription.

Interactive runs (`huragok run` from your own shell) work with cached
creds alone — no env var needed.

For **systemd deployment**, the unit file's sandboxing (`ProtectHome=read-only`,
`PrivateTmp=true`, and so on — see the unit file below) can isolate
the daemon from your user home. In that case, cached OAuth creds are
not reachable. Generate a long-lived token with:

```bash
claude setup-token
```

Copy the printed `sk-ant-oat01-...` value into your `EnvironmentFile`:

```dotenv
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

The runner passes this through on every session subprocess.

### Option B — API credits via `ANTHROPIC_API_KEY` (pay-as-you-go)

Set `ANTHROPIC_API_KEY=sk-ant-api-...` in the environment. Billing
routes through your Anthropic Console account's API credit balance,
NOT your Max subscription.

### Precedence and a warning

When both an API key and OAuth credentials are present, **the API key
wins** per Claude Code's auth precedence. Setting `ANTHROPIC_API_KEY`
on a Max-subscribed machine silently costs real money — work that
would otherwise be covered by your Max quota now hits API credits. Do
not set both unless that's what you want.

Empirically verified on Claude Code 2.1.117 (smoke-test run on
2026-04-22): `claude -p` with cached OAuth creds and no
`ANTHROPIC_API_KEY` in env routes billing to the Max subscription.
This contradicts at least one GitHub issue thread predating the fix;
the current behaviour is the working one.

## Budget interpretation for Max vs. API

`batch.yaml` exposes `max_dollars` as a budget cap. What that figure
*means* depends on your billing route:

### On API credits (Option B)

`max_dollars` is **theoretical API cost** computed from the local
pricing table at `orchestrator/pricing.yaml`. It corresponds roughly
one-to-one with real dollars off your API balance. The 100% halt
threshold (ADR-0002 D4) is a useful safety net.

### On Max billing (Option A)

`max_dollars` is a **counterfactual figure** — what the same session
would have cost against API credits. Actual Max usage is measured by
Anthropic in rate-limit windows (5-hour session and weekly message
caps), not in dollars. The dollar figure remains useful as a
"work intensity" proxy, but it does not track your Max quota.

**Cache tokens dominate the dollar estimate.** Claude Code
aggressively caches system prompts, agent files, and project context.
A small-looking task routinely produces several dollars of theoretical
API cost through cache reads and writes (the 2026-04-22 smoke-001 run
burned ~$6.67 of theoretical cost on a ~4-minute trivial Python task).
`huragok status` now surfaces `cache read` and `cache write` as
sub-lines under `Tokens:` so the cache footprint is visible.

Suggested practice on Max: set `max_dollars` 5–10x what you would for
API billing, or treat it as an emergency safety net rather than a
primary gate. The strict budgets that matter on Max are
`wall_clock_hours` and `max_iterations`.

## First-time setup

### 1. Clone and sync

```bash
git clone https://github.com/rtk94/huragok.git
cd huragok
uv sync
```

`uv sync` installs the runtime and dev dependencies listed in
`pyproject.toml`. The console script `huragok` is registered at
`.venv/bin/huragok` and at `~/.local/bin/huragok` if you install the
wheel separately.

### 2. Configure secrets

Huragok follows Tier-1 secret management from ADR-0001 D9: plain-text
environment files loaded via systemd's `EnvironmentFile=` or via a
repo-local `.env`. Create the file the systemd unit expects:

```bash
mkdir -p ~/.config/huragok
install -m 0600 /dev/null ~/.config/huragok/huragok.env
```

Populate it based on your billing route (see *Authentication and
billing* above for the tradeoffs):

```dotenv
# ~/.config/huragok/huragok.env

# --- Authentication: pick ONE path ----------------------------------------
#
# Option A — Max subscription. For interactive foreground runs, cached
# OAuth creds from `claude login` are enough and no env var is needed.
# For systemd where ~/.claude/ may be unreachable, generate a long-
# lived token with `claude setup-token` and uncomment:
#
# REPLACE WITH YOUR ACTUAL OAUTH TOKEN
# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
#
# Option B — API credits. Setting this on a Max machine silently
# routes billing to API credits; do not combine with Option A unless
# you specifically want API billing.
#
# REPLACE WITH YOUR ACTUAL API KEY
# ANTHROPIC_API_KEY=sk-ant-api03-...

# Optional. If set, the daemon queries the Anthropic Cost API at
# session and batch end to reconcile dollar totals.
# REPLACE WITH YOUR ACTUAL ADMIN KEY
# ANTHROPIC_ADMIN_API_KEY=sk-ant-admin-...

# Optional but recommended. Without these the daemon logs instead of
# sending Telegram notifications.
# REPLACE WITH YOUR ACTUAL BOT TOKEN
# TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
# REPLACE WITH THE CHAT ID YOU WANT NOTIFICATIONS SENT TO
# HURAGOK_TELEGRAM_DEFAULT_CHAT_ID=123456789

# Optional daemon-level overrides.
# HURAGOK_LOG_LEVEL=info
```

For foreground development, the same variables in a repo-local
`.env` file (next to `pyproject.toml`) work identically and are
loaded by `pydantic-settings`. Never commit the `.env` file.

### 3. Author your first batch

Create `.huragok/batch.yaml` in the target repo. A minimal example:

```yaml
version: 1
batch_id: batch-001
created: 2026-04-21T09:00:00Z
description: "Example batch"
budgets:
  wall_clock_hours: 12
  max_tokens: 5_000_000
  max_dollars: 50.00
  max_iterations: 2
  session_timeout_minutes: 45
notifications:
  telegram_chat_id: null          # null uses the daemon default
  warn_threshold_pct: 80
tasks:
  - id: task-0001
    title: "Add /healthz endpoint"
    kind: backend
    priority: 1
    acceptance_criteria:
      - "GET /healthz returns 200 with JSON {\"status\":\"ok\"}"
    depends_on: []
    foundational: false
```

Then submit it:

```bash
huragok submit .huragok/batch.yaml
```

`submit` validates the file against the `BatchFile` schema, archives
any previous batch's `.huragok/work/` directory to
`.huragok/work.archived/<old-batch-id>/`, and writes a fresh
`state.yaml` pointing at the new batch. It does **not** start the
daemon.

## Running the daemon

Huragok runs in exactly one mode per invocation: foreground
(`huragok run`) or managed by `systemd --user` (via the shipped unit
file). Both invoke the same binary.

### Foreground

```bash
huragok run
```

Blocks the terminal. Structured logs go to stdout. SIGTERM (`Ctrl-C`
twice — the first one is caught as a graceful-shutdown request)
drains the in-flight session and exits cleanly. Good for development
and for interactive runs on your workstation.

### As a systemd user service

Install the shipped unit file:

```bash
install -D scripts/systemd/huragok.service \
  ~/.config/systemd/user/huragok.service
systemctl --user daemon-reload
```

Choose or create a runtime directory. The unit's `WorkingDirectory`
is `%h/huragok-runtime`; point it at whichever repo you want the
daemon to run against:

```bash
ln -s /path/to/my-target-repo ~/huragok-runtime
```

Then start the service:

```bash
systemctl --user start huragok.service
journalctl --user -u huragok.service -f     # live logs
```

Enable at boot if you want the daemon to come up automatically:

```bash
systemctl --user enable huragok.service
```

The unit uses `Restart=on-failure` with a 30-second cooldown. A clean
halt (batch complete, `huragok halt`, or budget exhaustion) exits 0
and is NOT restarted; a crash is.

> **Note:** `huragok start` is not a background launcher. Running it
> prints a pointer to this document and exits 1. ADR-0002 D5's position
> is that the daemon doesn't try to fork-and-daemonise itself — we
> delegate that to systemd.

## Managing a running batch

### Stop

```bash
huragok stop
```

Reads the daemon PID from `.huragok/daemon.pid`, writes a `stop`
marker to `.huragok/requests/`, and sends SIGTERM. The in-flight
session (if any) is allowed to finish naturally and the daemon exits
cleanly. A second SIGTERM escalates to immediate termination.

### Halt (after in-flight session)

```bash
huragok halt
```

Writes `.huragok/requests/halt` and sends SIGUSR1. The daemon
completes the current session, transitions `state.yaml.phase` to
`halted`, and exits 0. Prefer this over `stop` when you want the
session's artifacts on disk before quitting.

### Reply to a pending notification

```bash
huragok reply continue                    # single pending
huragok reply iterate 01HXYZ              # explicit notification id
huragok reply escalate 01HXYZ "see slack" # with operator annotation
```

Verbs: `continue`, `iterate`, `stop`, `escalate`, plus aliases
`c`, `i`, `s`, `e`, `ok`, `yes`. The reply is written to
`.huragok/requests/reply-<id>.yaml` and (if the daemon is live)
signalled via SIGUSR1 for the next-tick pickup.

### Tail the batch log

```bash
huragok logs                          # last 50 records
huragok logs --follow                 # stream new records
huragok logs --level error            # filter by structlog level
```

`huragok logs` reads `.huragok/logs/batch-<batch_id>.jsonl`. Under
systemd, the same content is available via `journalctl --user -u
huragok.service`.

> **Note:** The daemon writes its structured logs to stdout by design
> (ADR-0002 D9). To capture them to the batch log file for
> `huragok logs` to read, redirect stdout when running in the
> foreground:
>
> ```bash
> mkdir -p .huragok/logs
> huragok run >> .huragok/logs/batch-$(yq -r .batch_id .huragok/batch.yaml).jsonl
> ```
>
> Under systemd, use `StandardOutput=append:%h/huragok-runtime/.huragok/logs/batch-<id>.jsonl`
> or pipe `journalctl` if you prefer journald as the source of truth.
> A proper log-file sink inside the daemon is scoped for a later
> slice; journalctl + redirect covers operators in the meantime.

### Inspect state without the daemon

All read-only commands work whether the daemon is running or not —
they read state directly from `.huragok/`:

```bash
huragok status              # budget + session + task summary
huragok status --json       # machine-readable variant
huragok tasks               # task list
huragok tasks --state done  # filter by status
huragok show task-0001      # per-task summary
huragok show task-0001 --full  # inline every artifact body
```

## Troubleshooting

### "no daemon running" but I thought I started one

`huragok stop` reads `.huragok/daemon.pid`. If the PID file is
missing, the daemon isn't running as far as Huragok is concerned.
Under systemd check `systemctl --user status huragok.service`; the
unit writes its PID into the file on startup and removes it on clean
exit.

If you see "stale pid file", the daemon was killed by SIGKILL or
similar and couldn't clean up. `huragok stop` deletes the stale PID
file for you; the next `huragok run` will write a fresh one.

### Telegram bot isn't responding

Check, in order:

1. `TELEGRAM_BOT_TOKEN` is set and non-empty in the environment file
   the daemon is reading.
2. `HURAGOK_TELEGRAM_DEFAULT_CHAT_ID` matches a chat the bot is a
   member of.
3. The bot has been started (`/start` in the target chat) so Telegram
   lets it send messages.
4. The daemon's phase is not `paused` with reason
   `notification-backend-unreachable` — that means Huragok has given
   up on Telegram because 10+ minutes of sends and polls failed.
   Fix the network issue (or the bot token) and the daemon will
   auto-resume.

### Version check rejected

```
supervisor.version.rejected  claude version 2.0.99 is below minimum 2.1.91
```

Upgrade Claude Code: `npm install -g @anthropic-ai/claude-code@latest`
(or your equivalent). ADR-0002 D2 explains the floor.

### "pricing.yaml missing model X"

The daemon validates every role's configured model against
`orchestrator/pricing.yaml` at startup. If a new model is referenced
but not priced, add it to the file manually and restart (ADR-0002
D4).

### Batch halted with an unexpected reason

`huragok status` shows the halt reason. Common reasons:

- `budget-exceeded` — a token / dollar / wall-clock cap tripped.
  Raise the budget in `batch.yaml` or accept the halt.
- `halt-context-overflow` — an agent ran out of context. This is a
  scoping issue; break the affected task into smaller pieces and
  resubmit. See ADR-0002 D7.
- `halt-unknown` — the classifier couldn't categorise a failure.
  Check `.huragok/logs/batch-<id>.jsonl` for the session that
  triggered the halt; file a bug if the signal is recognisable.
- `notification-backend-unreachable` — see the Telegram
  troubleshooting section above. Resolves automatically once the
  backend recovers.

### The daemon is running but nothing's happening

Check `huragok status`. Common idle causes:

- Every task is `done`, `blocked`, or `awaiting-human`. Nothing to
  launch. `huragok tasks` to see the list.
- `awaiting_reply` is set — the daemon is waiting for an operator
  reply before proceeding. `huragok reply <verb>` to unblock.
- Rate-limit pre-flight is deferring launches. Log record
  `supervisor.rate_limit.defer` shows for how long.

## Where to read next

- **Design rationale** — `docs/adr/ADR-0001`, `ADR-0002`, `ADR-0003`.
- **Implementation history** — `docs/notes/slice-a-build-notes.md`,
  `slice-b1-build-notes.md`, `slice-b2-build-notes.md`.
- **Agent definitions** — `.claude/agents/*.md` (those are the agents
  Huragok deploys into target projects, not the agents operating on
  Huragok itself).
