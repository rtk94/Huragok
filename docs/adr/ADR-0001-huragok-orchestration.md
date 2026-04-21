# ADR-0001: Huragok — Autonomous Multi-Agent Development Orchestration

**Status:** Accepted
**Date:** 2026-04-21
**Author:** Rich
**Related:** ADR-0002 (orchestrator daemon internals, pending), ADR-0003 (agent definitions, pending), ADR-0004 (frontend testing & UI gate, future), ADR-0005 (parallelism, future), ADR-0006 (retrospectives & iteration, future), ADR-0007 (bootstrap & distribution, future)

**Revision history:**
- 2026-04-21 — Initial Accepted version.
- 2026-04-21 — Renumbered downstream ADRs: inserted ADR-0002 (orchestrator daemon internals) per in-repo review; former 0002–0006 shifted to 0003–0007.
- 2026-04-21 — D3 roster reconciled with ADR-0003: Orchestrator agent collapsed into the Python Supervisor; roster shrinks to five Claude Code agents (Architect, Implementer, TestWriter, Critic, Documenter). Critic model assignment upgraded from Sonnet to Opus to reflect its final-judgment authority.

## Context

We want a system that accepts batches of software development work, executes them using a team of specialized Claude Code sub-agents, runs mostly autonomously within declared budgets (time, tokens, dollars, rate-limit windows), notifies the operator on completion or when human input is required, and supports parallel execution of duplicatable worker roles. The system must be capable of checkpoint/resume across Claude Code sessions, survive rate-limit and session-cap events without losing progress, and enforce quality gates — especially on front-end work — that an unattended agent cannot reliably enforce on its own.

Claude Code as of April 2026 ships the necessary primitives: filesystem-based subagent definitions, the Task tool for in-session delegation, agent teams for sustained-parallelism workflows, worktree isolation for conflict-free parallel execution, hooks for event-driven automation, headless mode for scripted invocation, and MCP support for external tools including Playwright for browser automation. None of these, individually or together, provides the orchestration layer this system requires. Huragok is that layer.

### Name

Huragok — the Engineers of the Halo universe. Autonomous, self-organizing biological constructs whose entire purpose is to build, maintain, and repair complex systems. They operate in swarms, specialize deeply, and are genuinely good at what they do. The name describes what the system *is* rather than warning about what it might become. Fits the existing Halo-Forerunner naming convention across the homelab (031 Exuberant Witness, 001 Shamed Instrument, 859 Static Carillon, 000 Tragic Solitude, Discovery-One).

## Decision

### D1. Two-tier architecture

Huragok is split into an **outer orchestrator** and an **inner coordinator**, with a hard boundary between them.

The outer orchestrator is a Python daemon. It owns the batch queue, budget accounting, session lifecycle management, rate-limit handling, notification dispatch, and all persistence. It invokes Claude Code in headless mode (`claude -p ... --output-format stream-json`) for each work session, captures the stream, detects session-end conditions, and decides whether to launch the next session, wait, or escalate to the operator.

The inner coordinator is a Claude Code session running in the target project's repo, reading state from versioned files on disk, dispatching work to specialist subagents via the Task tool, and writing all outputs back to disk before the session ends.

**This boundary is non-negotiable.** The orchestrator never shares in-memory state with a session. The session never assumes state continuity across invocations. Every handoff — between sessions, between agents inside a session, between the orchestrator and the operator — is a file.

**Rationale:** A single Claude Code session cannot reliably run for 12 hours. It will hit session caps, context rot, rate-limit backoffs, or transient errors. Treating each session as ephemeral and state as durable is the only design that survives those failures without human babysitting.

### D2. State lives on disk, in the repo

Every work unit is a folder at `.huragok/work/<task-id>/` containing a fixed set of files:

```
.huragok/
├── batch.yaml              # current batch: task list, acceptance criteria, priorities
├── state.yaml              # orchestrator-owned: current task, session count, budget consumed
├── decisions.md            # append-only architectural decision log written by agents
├── retrospectives/         # end-of-batch and end-of-iteration retros
└── work/
    └── task-0042/
        ├── spec.md         # written by Architect: acceptance criteria, API shape, scope,
        │                   # and foundational:true|false flag (see D6)
        ├── implementation.md  # written by Implementer(s): what was built, files touched, caveats
        ├── tests.md        # written by TestWriter: what is tested, what is not, mutation results
        ├── review.md       # written by Critic: findings, blockers, sign-off or reject
        ├── ui-review.md    # (if UI work) screenshot paths, visual-critic notes, human gate status
        └── status.yaml     # task state machine: pending | speccing | implementing | testing
                            #                     | reviewing | software-complete
                            #                     | awaiting-human | done | blocked
```

Agents read and write these files. They do not pass structured data through the Task tool's prompt string beyond a pointer to the relevant folder. This discipline is what makes checkpoint/resume trivial: any future session can read `state.yaml`, find the current task, read its folder, and continue.

**Rationale:** In-memory handoffs between agents look clean in diagrams but fail silently at scale. File-based handoffs are auditable, resumable, and survive process death.

### D3. Agent roster (summary; full definitions in ADR-0003)

**Session coordination — reading state, sequencing roles, dispatching sessions — is handled by the Python Supervisor (ADR-0002 D1). It is not itself a Claude Code agent.** The five Claude Code agents below each execute in their own session, launched by the Supervisor:

- **Architect** — produces `spec.md` from a batch entry. Single invocation per task. Sets the `foundational` flag. Opus. No write access outside the task folder. Dual-mode: backend architecture and UI/UX specification, selected by the batch entry.
- **Implementer** — consumes `spec.md`, writes code, writes `implementation.md`. Duplicatable; N parallel instances run in separate git worktrees on independent tasks. Sonnet. Full code-write access within its worktree.
- **TestWriter** — consumes `spec.md` and `implementation.md`, writes tests, writes `tests.md`. Runs mutation testing and records survival rate. Sonnet.
- **Critic** — consumes all of the above, executes tests, inspects mutation results, drives Playwright for UI flows, writes `review.md` with pass / fail / block. Authority to mark a task `software-complete` or `blocked`. Opus with read-heavy toolset; no code-write access outside review artifacts.
- **Documenter** — post-merge only. Updates user-facing docs when features reach `done`. Haiku. Scoped to docs paths.

The collapses relative to the original eight-role vision — PM into Supervisor (out-of-session), Tester + Reviewer into Critic, UX Designer into Architect mode flag — are motivated by coordination-cost concerns documented in ADR-0003.

### D4. Budget enforcement is the orchestrator's job

The orchestrator enforces four independent budgets on every run:

- **Wall-clock budget** — default 12h, configurable per batch.
- **Token budget** — from stream-json accounting, aggregated across all sessions in the batch.
- **Dollar budget** — derived from token counts and per-model pricing.
- **Rate-limit awareness** — tracks 5-hour rolling session windows and weekly caps; pauses rather than retries when near-cap.

Any budget crossing its threshold halts session launches, completes the in-flight session gracefully, writes a halt summary to `state.yaml`, and dispatches a notification. The operator decides what to do next. The orchestrator never raises its own budget.

`CLAUDE_CODE_SUBAGENT_MODEL` is set on every session launch so worker sessions default to Sonnet while the Architect and Critic sessions run on Opus. This is the single largest cost lever in the system.

### D5. Notifications are actionable, not decorative

Notification dispatch is Telegram-primary (reusing the OpenClaw integration on Discovery-One), with email fallback. Every notification carries:

- A one-line state summary (e.g. `batch 03 / 8 tasks software-complete / UI review needed`)
- A link or path to the relevant artifact (screenshot, review file, blocker log, preview URL)
- Four reply options, parsed by the orchestrator on the next poll: `continue`, `iterate`, `stop`, `escalate` (where escalate means "open a session and let the operator drive interactively")

Triggers: batch complete, foundational UI task needs checkpoint (see D6), budget threshold (80% warning, 100% halt), task blocked by Critic, unhandled error, rate-limit pause exceeding 30 minutes.

### D6. UI human-gate: deferred by default, checkpoint on foundational tasks

The UI gate is enforced at the point where human judgment actually adds value, not on every UI-touching task.

**Default behavior (deferred gate):** UI-touching tasks reach `software-complete` — all automated checks pass, screenshots captured, preview URL live — and the orchestrator moves on to the next task. The batch does not block on the operator's availability for routine UI work. When the batch finishes, the operator receives a single consolidated notification listing every task awaiting UI sign-off, with screenshots and preview URLs. The operator reviews in one focused session and replies `continue` (mark all `done`), `iterate` (open a fixup batch for listed items), or task-level annotations.

**Checkpoint exception (foundational gate):** When the Architect writes a `spec.md`, they set `foundational: true` on any task whose UI correctness will be depended upon by later tasks in the same batch — navigation rebuilds, shared layout changes, design-system additions. Foundational tasks halt the batch at `software-complete` and trigger an immediate notification. The batch does not proceed until the operator replies. This prevents compounding errors across many downstream implementations built on a broken foundation.

The Architect makes the foundational call because it is an architectural judgment about dependency coupling, not a UI-polish question.

**Rationale:** The original "gate on every UI task" design blocked batches on operator availability even when the operator would end up reviewing the whole batch anyway. The original "no gate at all" design let foundational breakage compound across eight downstream tasks before anyone looked. This policy targets the specific failure mode — propagated error through task dependencies — without paying the availability tax on routine work.

Full mechanics — screenshot conventions, the visual-critic subagent, the notification reply format, how `iterate` becomes a fixup batch — are ADR-0004.

### D7. Iteration is bounded

After a batch reaches `done` or `halted`, the orchestrator optionally invokes a Retrospective session that produces a `retrospectives/<batch-id>.md` and either a new batch file (if improvements are substantive and in-scope) or a STOP marker (if the work is complete or remaining items are out-of-scope). Maximum iteration depth per batch is a budget, default 2. The Retrospective's authority explicitly includes "no further iteration warranted"; absence of a STOP marker is not permission to loop.

### D8. Deployment topology: one daemon, runs where the work is

The orchestrator is a Python daemon that runs on whichever machine currently owns the active batch. Coordination between machines is git: the repo's `.huragok/` directory carries all durable state, and `git pull` / `git push` is the handoff protocol.

In practice:
- Interactive work on **001 Shamed Instrument** (desktop): run the daemon locally. Start it, feed it a batch, work alongside it or let it run.
- Overnight or hands-off batches on **031 Exuberant Witness**: push state from wherever the batch was prepared, `ssh 031`, start the daemon. Telegram notifications surface progress and gates.
- Claude Code sessions execute on whichever machine is running the daemon at the time.

**Rationale:** A centralized LAN-service daemon (one orchestrator on 031, clients on every workstation) is architecturally attractive but introduces a client/server protocol, an auth layer, a task queue that arbitrates between machines, and split-brain failure modes — a distributed system, with its associated maintenance burden, to solve a coordination problem that git already solves. There is also no concrete near-term workload that requires it: Phases 1–3 have exactly one batch running at a time. If concurrent cross-machine runs become a real need later, a centralized service can be added without reshaping the rest of the system, provided the daemon remains stateless between ticks and state remains in git. That constraint is inherited from D1 and D2 anyway.

### D9. Secret management

Anthropic API keys, Telegram bot credentials, and any project-specific secrets the orchestrator handles are managed in three tiers:

- **MVP:** `EnvironmentFile=` in the systemd unit. Plain-text `.env` file at `/etc/huragok/huragok.env`, mode 0600, owned by the service user. Zero new dependencies.
- **Hardening:** `LoadCredential=` in the systemd unit. Root-owned credentials under `/etc/huragok/credentials/`, systemd reads at unit start, drops privileges before exec. Service user never has raw-file read access. Natural upgrade when tier 1 feels too loose.
- **Encrypted test fixtures:** `sops` with `age` keys for secrets that should version alongside the repo (test credentials, fixture API keys). Add when a real need appears, not preemptively.

Starting tier: 1. Upgrade to 2 before exposing the orchestrator to any untrusted input source.

### D10. Repo coupling and distributability

`.huragok/` is committed to the target repo, with a top-level `.huragok/README.md` explaining what it is to human collaborators who encounter it in the tree. This is required by D2 (state lives in the repo) and D8 (git is the coordination substrate across machines), and makes end-to-end auditability possible — every decision, every artifact, every review is in history.

Distributability is an explicit goal: Huragok should be installable by someone who clones it from GitHub and wants to point it at their own repos. A `huragok init` scaffold generator — creates `.huragok/`, installs `.claude/agents/*.md`, appends a Huragok section to `CLAUDE.md` — is the onboarding path. Details are ADR-0007.

## Consequences

**Positive:**

- Checkpoint/resume is trivial because state is on disk and in git.
- Rate-limit and session-cap failures are non-fatal.
- Cost is bounded by explicit budgets, not by how long you forget to check.
- UI quality gate is enforced where it catches compounding errors without blocking routine work on operator availability.
- Agent roster is small enough that handoff overhead stays tractable.
- Git-as-coordination-substrate keeps multi-machine workflow simple today and leaves a clean upgrade path to a centralized service later.
- Repo-committed `.huragok/` makes the system distributable and auditable.

**Negative:**

- Two systems to maintain (Python orchestrator, Claude Code agent definitions).
- File-based state discipline requires agent prompts that consistently read/write state correctly; drift here will be the most likely failure mode in practice.
- The foundational-task UI gate means some batches will still halt awaiting operator review. This is intentional and scoped.
- Worktree-based parallelism (ADR-0005) will require careful handling of shared dependencies and integration testing.
- Tier-1 secret management is adequate but loose; the upgrade to tier 2 needs to happen before any exposure increase.

## Target project sequencing

1. **Phase 1 — MVP build.** No dogfooding possible; Huragok doesn't exist yet. Build by hand: the Python orchestrator, the six agent definitions, a minimal batch for a toy target. No parallelism. Sequential-only, single-machine, Tier-1 secrets.
2. **Phase 2 — First real run: Huragok-on-Huragok.** Use the MVP to produce ADRs 0005, 0006, 0007 (all text deliverables). Bounded blast radius. Validates the retrospective/iteration engine against a tractable domain before subjecting a real codebase to it.
3. **Phase 3 — First code project: Guituner.** Smaller scope than Argus and exposes Android as a target — surfaces whether ADR-0004 needs Android-specific branches (Playwright is desktop-web-focused) *early*, while the Huragok codebase is still malleable.
4. **Phase 4+ — Argus, then anything else.** Talos stays out until past planning.

## Open questions

*None remaining for ADR-0001.* Deployment, secrets, repo coupling, and first-target sequencing are resolved above. Further questions will surface in ADRs 0002–0007.

## Alternatives considered

**Single in-session coordinator, no external orchestrator.** Rejected. Session caps and context rot make 12-hour unattended runs inside one session unreliable, and there is no clean way to resume from a mid-session rate-limit error without external state management.

**Pure external orchestration using the Anthropic API directly, bypassing Claude Code.** Rejected. Gives up the entire Claude Code tool ecosystem — subagent dispatch, hook system, MCP servers including Playwright, worktree integration — in exchange for marginal flexibility that isn't worth the cost.

**Single generalist agent, no specialization.** Rejected. Specialist subagents with narrow tool allowlists catch handoff-shaped defects (test theater, scope creep, architecture drift) that a single generalist will not flag against itself. The cost of specialization is handoff overhead; the six-role roster is the budget we are willing to pay.

**More than six specialist roles.** Rejected. Coordination cost grows faster than specialization benefit past roughly six roles at this system size. Collapses documented above; details in ADR-0003.

**Centralized LAN-service orchestrator daemon.** Rejected for Phase 1–3. Introduces a distributed-system maintenance burden to solve a coordination problem git already handles. Left as a clean future upgrade path conditional on concrete concurrent-workload need.

**UI gate on every UI-touching task.** Rejected. Pays an availability tax on routine work without catching the actual failure mode (propagated error through task dependencies) any better than the foundational-flag design.

**No UI gate at all.** Rejected. Foundational UI breakage compounds across downstream implementations before the operator sees it. Worst case is an entire batch built atop a broken foundation.
