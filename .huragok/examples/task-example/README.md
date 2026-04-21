# Example task: `task-example`

This folder is a worked example of a complete, well-formed Huragok task. It exists so that agents launched in a fresh repo can pattern-match on format without having to infer it from the ADRs alone.

The example task is fictional: "Add a `/healthz` endpoint to a FastAPI service." It's chosen because it's small, tangible, and exercises both backend and test-writing without dragging in UI concerns. Every artifact below is the kind of thing Huragok expects to produce for every real task.

**None of this folder is executed.** It is reference-only. After the first real task completes in a Huragok-enabled repo, this folder can be deleted without consequence — it exists for cold-start calibration.

## Files

- `spec.md` — what the Architect produces.
- `implementation.md` — what the Implementer produces.
- `tests.md` — what the TestWriter produces.
- `review.md` — what the Critic produces.
- `status.yaml` — task state at the `done` terminal.

The example has no `ui-review.md` because the example task has no UI surface.
