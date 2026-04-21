# ADR-0003: Agent Definitions, Tool Allowlists, and Handoff Contracts

**Status:** Accepted
**Date:** 2026-04-21
**Author:** Rich (with in-repo review by Claude Code)
**Supersedes:** —
**Related:** ADR-0001 (charter), ADR-0002 (orchestrator daemon internals), ADR-0004 (frontend testing & UI gate, future), ADR-0005 (parallelism, future), ADR-0006 (retrospectives & iteration, future), ADR-0007 (bootstrap & distribution, future)

## Context

ADR-0001 named a six-role agent roster; ADR-0002 specified the Python orchestrator that launches sessions. Neither pinned down how a role actually manifests at the Claude Code layer, or what an agent produces and consumes. This ADR closes that gap. It defines each agent's purpose, system prompt, tool allowlist, model assignment, and — most importantly — the file-based handoff contracts that let the outer orchestrator treat each session as an ephemeral unit of forward progress.

The guiding architectural choice is **one role per session** (Option C from pre-drafting discussion). A Claude Code session is the unit of role execution. When the Architect's work is done, its session ends and its artifacts are on disk; when the Python Supervisor picks up the task, it launches a new session for the Implementer with a fresh context. The Task tool is reserved for narrow in-session delegation (test execution, doc lookup) where context isolation genuinely helps, not for cross-role handoffs.

This choice has one consequence that reshapes the ADR-0001 roster: the "Orchestrator" role as an in-session coordinator disappears. Session lifecycle and role sequencing are the Python Supervisor's job. The agent roster shrinks from six to five: Architect, Implementer, TestWriter, Critic, Documenter. This is reconciled in §Revisions to prior ADRs below.

## Decisions

### D1. One role per session; role chosen by Supervisor at launch

The Python Supervisor (ADR-0002 D1) owns role sequencing. For each in-flight task, the Supervisor reads `status.yaml.state` and maps states to roles:

| `status.yaml.state`    | Role launched          |
| ---------------------- | ---------------------- |
| `pending`              | Architect              |
| `speccing` (in flight) | Architect              |
| `implementing`         | Implementer            |
| `testing`              | TestWriter             |
| `reviewing`            | Critic                 |
| `software-complete`    | (no session — awaits human or next task) |
| `awaiting-human`       | (no session — awaits reply) |
| `done`                 | Documenter (if scoped) |
| `blocked`              | (no session — awaits operator) |

The Supervisor constructs the `claude -p` invocation with:

- The minimal deterministic prompt from ADR-0002 D2
- `--append-system-prompt` carrying **the full contents of the role's agent file** (e.g. `.claude/agents/architect.md`)
- `CLAUDE_CODE_SUBAGENT_MODEL` set per the role's model assignment (D4 below)
- `cwd` set to the target repo root (or the worktree root, once ADR-0005 lands)

Agent files live in `.claude/agents/*.md` per Claude Code conventions, so they're also discoverable by a human running Claude Code manually in the repo. But the canonical invocation path is via the Supervisor.

### D2. The state machine transitions and what drives them

An agent does not decide what state comes next. An agent writes its artifact and updates `status.yaml` to the next canonical state for its role. The Supervisor reads the new state on the next tick and launches the next role. This keeps state transitions explicit and audit-able.

Canonical transitions:

- **Architect** writes `spec.md`, sets `status.yaml.state: implementing`. Sets `foundational: true|false`. Populates `ui_review.required` if kind is frontend or fullstack.
- **Implementer** writes `implementation.md`, sets `status.yaml.state: testing`. Attaches diff-scoped file list.
- **TestWriter** writes `tests.md`, runs the test suite locally and records pass/fail, runs mutation testing and records survival rate, sets `status.yaml.state: reviewing`.
- **Critic** writes `review.md`, sets `status.yaml.state: software-complete` (accept) or `implementing` (reject with findings, returns to Implementer) or `blocked` (unresolvable blocker). On accept for a UI task with `foundational: true`, also populates `ui_review` fields and the Supervisor notifies the operator (ADR-0001 D6).
- **Documenter** (post-merge) writes doc updates, sets `status.yaml.state: done`.

The "reject" transition — Critic sending the task back to Implementer — is bounded. The Critic may reject a task at most **twice** per batch. A third rejection sets `status.yaml.state: blocked` with `blockers: [<reason>]` and escalates to the operator. This is what keeps the Critic↔Implementer loop from becoming infinite.

### D3. Tool allowlists are scoped per role

Each agent's `.claude/agents/*.md` file declares an explicit `tools:` frontmatter that Claude Code honors. Narrow allowlists are the mechanism for enforcing role discipline — the Architect literally cannot write code because it doesn't have `Write` or `Edit` for paths outside its task folder.

The allowlists below name Claude Code's current built-in tools. Anything not listed is not available.

| Role         | Read | Grep | Glob | Bash | Write | Edit | WebFetch | WebSearch | Task | Notes                                                         |
| ------------ | :--: | :--: | :--: | :--: | :---: | :--: | :------: | :-------: | :--: | ------------------------------------------------------------- |
| Architect    |  ✓   |  ✓   |  ✓   |  —   |  ▲    |  —   |    ✓     |     ✓     |  —   | ▲ Write restricted to `.huragok/work/<task-id>/spec.md` only. |
| Implementer  |  ✓   |  ✓   |  ✓   |  ✓   |   ✓   |  ✓   |    —     |     —     |  ✓   | Bash restricted; see agent file for allowlist. Task used for narrow in-session delegation. |
| TestWriter   |  ✓   |  ✓   |  ✓   |  ✓   |   ✓   |  ✓   |    —     |     —     |  ✓   | Write scoped to test paths and `.huragok/work/<task-id>/tests.md`. |
| Critic       |  ✓   |  ✓   |  ✓   |  ✓   |   ▲   |  —   |    —     |     —     |  ✓   | ▲ Write restricted to `.huragok/work/<task-id>/review.md` and `ui-review.md`. Bash for running tests only. |
| Documenter   |  ✓   |  ✓   |  ✓   |  —   |   ✓   |  ✓   |    —     |     —     |  —   | Write scoped to docs paths only.                              |

Path restrictions on `Write`/`Edit` are enforced by the agent's system prompt (a Claude Code agent file can describe scope, but path-level enforcement is by convention and prompt discipline). A future hardening step, if agent drift becomes a real problem, is implementing a pre-tool-use hook (ADR-0007) that rejects writes outside declared paths. For Phase 1 we rely on the prompt plus post-hoc audit.

Bash allowlists for Implementer, TestWriter, and Critic are specified in their individual agent files rather than enumerated here, because they depend on target-project tooling (Argus's `uv run pytest` differs from Guituner's `./gradlew test`).

### D4. Model assignments

| Role        | Model                    | Rationale                                                                 |
| ----------- | ------------------------ | ------------------------------------------------------------------------- |
| Architect   | `claude-opus-4-7`        | Upfront architectural judgment benefits most from the strongest reasoning. |
| Implementer | `claude-sonnet-4-6`      | Bulk coding work at Sonnet cost is the ADR-0001 D4 cost lever.            |
| TestWriter  | `claude-sonnet-4-6`      | Test writing is Sonnet-appropriate; mutation analysis is summarization.   |
| Critic      | `claude-opus-4-7`        | Final judgment authority warrants the strongest model. Small session.     |
| Documenter  | `claude-haiku-4-5-20251001` | Doc writing is Haiku-appropriate; post-merge, low-stakes.              |

The `CLAUDE_CODE_SUBAGENT_MODEL` env var (ADR-0002 D2) is set to the role's model at launch. Within a session, any Task-tool subagents inherit that default unless the parent agent overrides it explicitly in the Task invocation — which no agent file currently does.

Opus for Architect and Critic is deliberate: they bookend the task (design and judgment), and mistakes at those bookends compound through the middle. Sonnet for the middle three agents is also deliberate: Implementer and TestWriter do the bulk of the token consumption, and the cost gap between Opus-everywhere and Sonnet-middle-three is ~5x across a batch.

### D5. Handoff contracts: what each artifact must contain

An agent's job ends when its artifact is on disk *and* meets the contract. Contracts are specified here and enforced by the next agent in the chain — the Implementer refuses to start work if `spec.md` lacks acceptance criteria, the Critic refuses to review if `tests.md` doesn't report mutation results, etc. Contract violations emit a `handoff-rejected` event to the audit log and transition the task back to the violating role.

All content files carry the frontmatter schema from ADR-0002 D3:

```
---
task_id: task-0001
author_agent: architect
written_at: 2026-04-21T09:15:00Z
session_id: 01HXYZ...
---
```

#### `spec.md` contract (Architect's output)

Required sections:

- **Problem statement** — 1–3 sentences. What is this task changing and why?
- **Acceptance criteria** — 3–10 bullet points, each individually testable. Copied from `batch.yaml` if provided; refined or added to by the Architect.
- **Scope** — explicit in-scope and out-of-scope lists. Prevents Implementer scope creep.
- **Interface / API shape** — for backend tasks, the endpoint shape, request/response schemas, error modes. For frontend tasks, the component boundary, props, and state contract. For fullstack, both.
- **UI surface** (if kind is frontend/fullstack) — screens affected, key interactions, foundational status and rationale. Populates `status.yaml.ui_review.required` and `foundational`.
- **Dependencies** — references to other tasks (`depends_on`), libraries to add, environment requirements.
- **Open questions** — things the Architect could not decide without human input. Non-empty open questions set `status.yaml.state: blocked` instead of advancing.

#### `implementation.md` contract (Implementer's output)

- **Summary** — one paragraph of what was built.
- **Files touched** — list of paths, each annotated `+N / -N` line count or `new file`.
- **Approach notes** — material choices the Implementer made that weren't explicit in `spec.md`. These become the Critic's review material.
- **Deviations from spec** — an explicit list. Empty is the goal, but deviations happen; documenting them is non-negotiable.
- **Caveats / known limitations** — things that work but shouldn't be relied on, or things that need cleanup in a later task.

#### `tests.md` contract (TestWriter's output)

- **Test inventory** — list of test files added or modified, with one-line descriptions.
- **Coverage summary** — what `spec.md` acceptance criteria each test covers. Every acceptance criterion must map to at least one test, or the criterion is flagged as un-tested and the Critic treats that as a rejection cause.
- **Run results** — current pass/fail for the task's tests. Failing tests here must be justified (e.g. "expected to fail, pending backend task-0044") or the task returns to Implementer.
- **Mutation testing results** — survival rate from `mutmut` (Python) or the per-language equivalent. A survival rate above 30% is flagged in the Critic's handoff as "weak tests."
- **Coverage gaps** — explicit list of acceptance criteria the TestWriter could not test and why.

#### `review.md` contract (Critic's output)

- **Verdict** — `accept` | `reject` | `block`.
- **Findings** — numbered list. Each finding has a severity (`blocker` | `major` | `minor` | `nit`) and a remediation suggestion. Blockers force a `reject` or `block` verdict.
- **Test execution** — Critic re-runs the full test suite and reports the actual result here, not trusting `tests.md`'s self-report.
- **Mutation review** — if TestWriter flagged a survival rate >30%, Critic assesses whether it's a real weakness or an artifact (e.g. mutations on logged strings that don't affect behavior).
- **UI review** (if applicable) — screenshot paths, observations, whether the task satisfies `ui_review.required`. Drives the foundational-gate notification (ADR-0001 D6).
- **Ship recommendation** — if accept, one line on whether this is safe to merge or has caveats.

#### `ui-review.md` contract (Critic's output, UI tasks only)

Populated when `status.yaml.ui_review.required: true`. Details are ADR-0004's scope; for Phase 1 this file captures screenshot paths, preview URL, a short visual-critic summary, and a `foundational` restatement from `spec.md`. The operator's reply verb resolves `ui_review.resolved`.

### D6. Worked example

A fleshed-out example of the complete artifact set for one hypothetical task is shipped at `.huragok/examples/task-example/` in the repo (added in the same commit as this ADR and the agent files). Agents are expected to read the example on their first invocation in a new repo to calibrate on format. After the first few real tasks in a repo, the example becomes redundant and can be ignored or deleted; it exists for the cold-start case.

### D7. The "no Orchestrator agent" rule and what replaces it

ADR-0001 D3 originally listed Orchestrator as the sixth role. Under Option C, there is no Orchestrator agent file. The Supervisor (Python) handles everything the Orchestrator agent was slated for:

- Reading `state.yaml` and picking the next role
- Launching sessions with the right model and system prompt
- Advancing task state after a session ends
- Driving notifications on state transitions that warrant them

This is reconciled in §Revisions to prior ADRs. There is deliberately no `.claude/agents/orchestrator.md` file.

## Consequences

**Positive:**

- Each role sees a fresh context. Context-overflow failures (ADR-0002 D7) become rare because no single session has to carry spec → implement → test → review.
- Handoff contracts are explicit; contract violations are machine-detectable.
- Tool allowlists enforce role discipline at the Claude Code level, not just by prompt convention.
- Model assignments give us the 5x cost lever from Sonnet-middle-three.
- Checkpoint/resume (the core reason for ADR-0001 D1) works at every role boundary, which is the natural granularity.

**Negative:**

- More session launches per task (5–6 vs. 1), with startup cost on each. Mitigated by the fact that each session does substantial work; session-spawn latency is a rounding error on agent runtime.
- Agent-prompt discipline is a moving target. The first few batches will surface prompt issues we can't predict from the armchair. Expect to iterate the agent files.
- Path-scoped Write enforcement is prompt-only for Phase 1. A determined or confused agent could write outside its scope. Audit log catches it post-hoc; hook-based enforcement is future work.
- Mutation testing adds real time to the TestWriter phase. Configurable but on by default.
- The Critic-reject-twice-then-block policy can frustrate on genuinely hard tasks. Operator `iterate` reply resets the counter.

## Revisions to prior ADRs

### ADR-0001

Two edits to reconcile the roster with D1/D7 of this ADR:

1. **D3 (Agent roster).** The list of six agents becomes five. The bullet for Orchestrator is removed. A brief note is added above the bullet list: *"Session coordination — reading state, sequencing roles, dispatching sessions — is handled by the Python Supervisor (ADR-0002 D1). It is not itself a Claude Code agent."*
2. **Last sentence of D3.** The parenthetical "PM into Orchestrator" collapses to "PM into Supervisor (out-of-session)." The rest of the collapses sentence is unchanged.

These are reconciliation edits, not reversals. The ADR-0001 architecture is unchanged; it just gets more precise about which component does what. ADR-0001 will receive a corresponding `Revision history` entry.

## Alternatives considered

**Option A — Orchestrator-as-subagent, all roles inside one session.** Rejected. Context accumulates across roles; single-session budgets blow for substantive tasks; checkpoint/resume semantics become "resume from wherever in the pipeline," which is the exact thing ADR-0001 D1 was designed against.

**Option B — distinct session per role, no Task usage.** Rejected. Surrenders Task entirely, which forfeits real wins on test execution (run in Task, summary returns to Critic) and narrow doc lookups. Option C is Option B plus Task-for-narrow-helpers.

**Collapsing TestWriter into Implementer.** Rejected. Implementer incentives bias toward "tests that make my code look good." Separate agent with its own context and its own handoff contract catches test theater. This is the same reasoning that kept Critic separate from both.

**Collapsing Critic into TestWriter.** Rejected for the same reason in reverse. TestWriter decides what to test; Critic decides whether the task is done. Separate authorities catch different defects.

**Documenter on every task vs. post-merge only.** Went with post-merge only, on tasks that reach `done`. Documenting software that might still be rejected wastes cycles. Documenter pass happens as a lightweight Haiku session at end-of-batch or on-demand.

**An explicit "Scoping" pre-Architect phase.** Considered and rejected for Phase 1. The Architect is expected to do the scoping in `spec.md`. If that turns out to overload the Architect in practice, a dedicated Scoper role is a clean addition and doesn't invalidate anything here.

**Enforcing tool-path scope via pre-tool-use hooks.** Deferred to Phase 2 or ADR-0007. Prompt-based scope plus audit-log review is sufficient for Phase 1 and cheaper to iterate on.

**Requiring mutation testing on every task.** Kept, but configurable. The cost is real but the failure mode it catches — tests-that-pass-nothing — is the exact failure that autonomous agents produce most often. Escaping mutation testing would surrender the most important quality signal we have.

## Open questions

1. **Critic re-running the full test suite vs. the task-scoped subset.** Full is safer (catches interactions); task-scoped is faster. Default to full in Phase 1; revisit if runtimes become a budget problem on larger target projects.

2. **Documenter scope on non-code-facing features.** For a backend refactor that changes nothing user-visible, does the Documenter run? Current answer: only if the task's `kind` is `docs` or the `spec.md` explicitly lists doc impact. A pure internal refactor skips the Documenter. This will need refinement once we have real examples.

3. **How the Architect decides `foundational: true|false`.** Current guidance in the agent file is "a task whose UI correctness will be depended upon by later tasks in the same batch." In practice this is a judgment call. May need sharpening with examples once ADR-0004 lands.

4. **Agent-file versioning.** If an agent file changes mid-batch (operator edits `architect.md` while a batch is running), does the next Architect session use the old or new version? Current behavior: whatever's on disk at session launch. Safer behavior: pin agent-file hashes into `batch.yaml` at batch start and reuse across all sessions in that batch. Defer; surface in implementation.
