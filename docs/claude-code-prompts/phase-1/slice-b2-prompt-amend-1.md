# Huragok Phase 1 — Slice B2 Amendment 1: Batch Log File Mirroring

**Amends:** `slice-b2-prompt.md`
**Source notes:** `docs/notes/slice-b2-build-notes.md` (see Deviation 5)
**Dated:** 2026-04-22

---

You are completing one small deferred item from Slice B2 before the Phase 1 commit. This is an amendment, not a new slice. Scope is deliberately narrow: wire the daemon to mirror structured logs to a per-batch JSONL file so `huragok logs` works out of the box.

Read this prompt completely, then read the files listed. The scope limits are firm — do not expand the change.

---

## Context

Slice B2 shipped `huragok logs` as a working CLI command that tails `.huragok/logs/batch-<batch_id>.jsonl`. Slice B2 also deferred the daemon-side logic that writes that file, because wiring the sink required touching `orchestrator/logging_setup.py`, which was on B2's frozen-file list. The deferral was explicit and disclosed in the B2 build notes ("Deviation 5: Batch log file mirror").

This amendment unfreezes `orchestrator/logging_setup.py`, adds the sink, and wires it in at daemon startup so `huragok logs` works end-to-end. That's it.

---

## Read first

1. `CLAUDE.md` at the repo root.
2. `docs/adr/ADR-0002-orchestrator-daemon-internals.md` — **D9 (observability)** is the specific decision this amendment completes.
3. `docs/notes/slice-b2-build-notes.md` — specifically "Deviation 5" and the "Sharp edges" note about `logging_setup.py` being structlog-cache-hostile.
4. `orchestrator/logging_setup.py` — understand what's already there before changing it.
5. `orchestrator/supervisor/loop.py` — specifically `run()` (the entry called by the CLI). This is where the sink gets wired.
6. `orchestrator/cli.py` — specifically the `logs` command, so you can confirm what path it's tailing.
7. `orchestrator/paths.py` — `batch_log(root, batch_id)` is the canonical path helper.

---

## Scope

**In scope:**

- Extend `configure_logging()` signature with an optional file-sink parameter.
- Add a structlog processor or sink that writes the same JSON-rendered records to a file path when the parameter is set.
- Wire the sink in the supervisor's `run()` at the point where the batch ID becomes known (after reading `state.yaml`). If no batch is active yet, the sink is not installed; when a batch starts, install it. On batch end, close it cleanly.
- Tests for the new behavior.

**NOT in scope:**

- Log rotation, size limits, or cleanup. One file per batch, grows monotonically; deferred.
- Journald integration beyond what systemd already captures from stdout.
- Any change to the `huragok logs` CLI itself — it already reads the right path.
- Any change to the JSON shape of log records.
- Any change to the stdout path — stdout continues to emit the same records.
- Any change to `orchestrator/cli.py`, `orchestrator/supervisor/loop.py` beyond the single wiring call at batch-start.
- Any other deferred items from B2 notes.

If you find yourself reaching for any of those, stop.

---

## Technical notes

**structlog's caching.** The B2 build notes flagged this: "a tee writer in the supervisor won't work cleanly because structlog caches the stdout reference at configure time." The fix is to design `configure_logging()` so file-sink installation is part of the configure call, not a post-hoc addition. Specifically: the function accepts the file path (or None) and configures the processor chain accordingly before structlog caches anything.

**Approach.** structlog's standard pattern for this is a processor that calls through to multiple sinks. The cleanest version is to use structlog's `WriteLogger` for stdout and a separate `WriteLogger` wrapping an open file handle, composed via a custom processor at the end of the chain. Alternatively, the final processor can render to JSON once and write to both sinks directly. Pick whichever composes more cleanly — both produce the same result.

**File opening mode.** Open with `"a"` (append) so restarts don't clobber. The file lives for the life of the batch; close it when the supervisor exits.

**Encoding.** UTF-8. Line-buffered (`buffering=1` on `open()`) so `tail -f` sees records promptly.

**Directory creation.** The `.huragok/logs/` directory may not exist on first batch. Create it if missing via `Path.mkdir(parents=True, exist_ok=True)`.

**Error handling.** A disk-full or permission error on the log file should NOT crash the daemon. Log a WARN to stdout-only and keep running without the file sink. The daemon's core work doesn't depend on the log file existing.

**What `configure_logging` should look like after this change.** Something shaped like:

```python
def configure_logging(
    level: str = "info",
    json_output: bool = True,
    file_path: Path | None = None,  # new
) -> None:
    """..."""
```

The supervisor's `run()` — currently calls `configure_logging(level=...)` — calls it again with the batch log path once the batch ID is known. Re-configuring is safe (structlog allows it); this is the simplest way to install the sink without reshaping the startup sequence.

If re-configuring breaks any of the existing B1/B2 logging tests (it shouldn't, but verify), the alternative is a dedicated `add_file_sink(path)` function that appends to the existing processor chain. Your judgment; document the choice in the build notes.

---

## Tests

Add to `tests/test_logging_setup.py` (create it if it doesn't exist):

- `configure_logging` with no file path matches the current behavior — stdout-only.
- `configure_logging` with a file path: a log call emits to both stdout and the file; both records are JSON-parseable; both contain the same fields.
- Disk-full / permission error: patch `open()` to raise, confirm daemon keeps logging to stdout and a WARN is emitted about the file sink failure.
- File path directory creation: file path under a missing parent dir gets its parent auto-created.
- Re-configuring (or the equivalent `add_file_sink` path) does not duplicate stdout records.

Add to `tests/supervisor/test_loop.py` or a new `tests/supervisor/test_log_wiring.py`:

- The supervisor, given a batch, creates `.huragok/logs/batch-<batch_id>.jsonl` and writes at least one record to it during a one-iteration integration run.

All tests must pass via `uv run pytest`. No `ruff` violations. No new dependencies.

---

## Deliverable

A short addition to `docs/notes/slice-b2-build-notes.md` under a new heading:

```
## Amendment 2026-04-22: batch log file mirror

**Driven by:** `docs/claude-code-prompts/phase-1/slice-b2-prompt-amend-1.md`
```

Include in that section:

- The `configure_logging` signature change.
- The wiring location.
- Which approach was taken (re-configure vs. add_file_sink) and why.
- Any test deviations.

Do NOT create a separate build-notes file for this amendment. It's part of B2, just completed later. The heading's `Driven by:` line is the explicit link to the prompt that produced this change — this convention applies to all amendments going forward.

---

## Stop conditions

Stop and ask if:

- Extending `configure_logging` breaks any existing test beyond an easy fix.
- structlog's processor chain requires a reshape beyond the scope above.
- The supervisor's startup sequence can't be modified at the point you need without touching other modules.
- A test reveals a real bug in existing code.

Otherwise: make the change, test it, append the notes section, and report back.
