"""Huragok CLI.

Slice A shipped read-only inspection commands (``status``, ``tasks``,
``show``); B1 promoted ``run``, ``stop``, ``halt`` to real
implementations. B2 promotes ``submit``, ``reply``, and ``logs`` and
turns ``start`` into a doc-pointing stub that tells operators to use
the systemd unit (ADR-0002 D5).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import structlog
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from orchestrator.config import load_settings
from orchestrator.constants import (
    IMPLEMENTATION_FILE,
    REVIEW_FILE,
    SPEC_FILE,
    STATUS_FILE,
    TESTS_FILE,
    UI_REVIEW_FILE,
    WORK_DIR,
)
from orchestrator.logging_setup import configure_logging
from orchestrator.notifications.telegram import REPLY_VERB_ALIASES, normalize_verb
from orchestrator.paths import (
    HuragokNotFoundError,
    audit_log,
    batch_log,
    daemon_pid_file,
    find_huragok_root,
    huragok_dir,
    requests_dir,
    task_dir,
)
from orchestrator.state import (
    ArtifactFormatError,
    AwaitingReply,
    BatchFile,
    BudgetConsumed,
    SessionBudget,
    StateFile,
    StatusFile,
    read_artifact,
    read_batch,
    read_state,
    read_status,
    write_batch,
    write_state,
)

app = typer.Typer(
    name="huragok",
    help="Autonomous multi-agent development orchestration for Claude Code.",
    no_args_is_help=True,
)

stdout = Console()
stderr = Console(stderr=True)


# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------


def _resolve_root() -> Path:
    """Find the repo root (parent of ``.huragok/``) or exit with an error."""
    try:
        return find_huragok_root()
    except HuragokNotFoundError as exc:
        stderr.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


def _init_logging() -> None:
    """Configure structlog once per CLI invocation, per ADR-0002 D9."""
    settings = load_settings()
    configure_logging(level=settings.log_level)
    structlog.contextvars.bind_contextvars(component="cli")


def _load_batch_if_any(root: Path) -> BatchFile | None:
    """Read batch.yaml if it exists and validates; otherwise return None."""
    try:
        return read_batch(root)
    except FileNotFoundError:
        return None


def _load_task_statuses(root: Path, batch: BatchFile) -> dict[str, StatusFile]:
    """Load every status.yaml referenced by the batch, where it exists."""
    statuses: dict[str, StatusFile] = {}
    for task in batch.tasks:
        status_path = task_dir(root, task.id) / STATUS_FILE
        if not status_path.exists():
            continue
        try:
            statuses[task.id] = read_status(root, task.id)
        except ValidationError:
            # A malformed status file on one task shouldn't break the
            # whole view; surface as an unknown state instead.
            continue
    return statuses


# ---------------------------------------------------------------------------
# Formatting helpers for the status view.
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _fmt_hours(hours: float) -> str:
    return _fmt_duration(hours * 3600)


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _pct(numerator: float, denominator: float) -> int:
    if denominator <= 0:
        return 0
    return round(100 * numerator / denominator)


def _session_breakdown(root: Path, batch_id: str | None) -> tuple[int, int, int]:
    """Parse ``.huragok/audit/<batch_id>.jsonl`` into (launched, clean, retry).

    Streams the file line-by-line so long-running batches with many
    sessions don't load the whole log into memory. Unknown end_state
    values count toward ``retry`` — bucketing can be refined later
    without breaking consumers of the three-number shape.
    """
    if batch_id is None:
        return 0, 0, 0
    path = audit_log(root, batch_id)
    if not path.exists():
        return 0, 0, 0

    launched = 0
    clean = 0
    retry = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = record.get("kind")
            if kind == "session-launched":
                launched += 1
            elif kind == "session-ended":
                if record.get("end_state") == "clean":
                    clean += 1
                else:
                    retry += 1
    return launched, clean, retry


def _count_by_state(statuses: dict[str, StatusFile], total_tasks: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in statuses.values():
        counts[status.state] = counts.get(status.state, 0) + 1
    # Tasks with no status.yaml on disk are implicitly pending.
    counts["pending"] = counts.get("pending", 0) + (total_tasks - len(statuses))
    return counts


def _render_human_status(
    state: StateFile,
    batch: BatchFile | None,
    statuses: dict[str, StatusFile],
    root: Path,
) -> None:
    """Print the ADR-0002 D9 status view using rich."""
    header_label = state.batch_id or "no-batch"
    phase_fragment = state.phase
    if state.phase == "paused" and state.halted_reason:
        phase_fragment = f"paused — {state.halted_reason}"
    stdout.print(Text(f"huragok — {header_label} ({phase_fragment})", style="bold"))
    stdout.print("═" * 63)

    if batch is None:
        stdout.print("idle — no batch in flight")
        return

    consumed = state.budget_consumed
    budgets = batch.budgets

    elapsed = _fmt_duration(consumed.wall_clock_seconds)
    wall_budget = _fmt_hours(budgets.wall_clock_hours)
    wall_pct = _pct(consumed.wall_clock_seconds, budgets.wall_clock_hours * 3600)
    stdout.print(f"Elapsed:        {elapsed} / {wall_budget}    ({wall_pct}%)")

    tokens_total = consumed.tokens_input + consumed.tokens_output
    token_pct = _pct(tokens_total, budgets.max_tokens)
    stdout.print(
        f"Tokens:         {_fmt_count(tokens_total)} / {_fmt_count(budgets.max_tokens)}    "
        f"({token_pct}%)"
    )
    stdout.print(f"  input:        {_fmt_count(consumed.tokens_input)}")
    stdout.print(f"  output:       {_fmt_count(consumed.tokens_output)}")
    stdout.print(f"  cache read:   {_fmt_count(consumed.tokens_cache_read)}")
    stdout.print(f"  cache write:  {_fmt_count(consumed.tokens_cache_write)}")

    dollar_pct = _pct(consumed.dollars, budgets.max_dollars)
    stdout.print(
        f"Dollars:        ${consumed.dollars:.2f} / ${budgets.max_dollars:.2f}    "
        f"({dollar_pct}%)  (table est., not reconciled)"
    )
    stdout.print(f"Iterations:     {consumed.iterations} / {budgets.max_iterations}")
    launched, clean, retry = _session_breakdown(root, state.batch_id)
    stdout.print(f"Sessions:       {launched} launched, {clean} clean, {retry} retry")
    stdout.print()

    if state.current_task:
        stdout.print(f"Current task:   {state.current_task}")
        if state.current_agent:
            stdout.print(f"  agent:        {state.current_agent}")
        if state.session_id:
            stdout.print(f"  session:      {state.session_id}")
        stdout.print()

    counts = _count_by_state(statuses, len(batch.tasks))
    in_flight = sum(counts.get(s, 0) for s in ("speccing", "implementing", "testing", "reviewing"))
    stdout.print(
        f"Tasks:          {len(batch.tasks)} total · "
        f"{counts.get('done', 0)} done · {in_flight} in-flight · "
        f"{counts.get('pending', 0)} pending · {counts.get('blocked', 0)} blocked"
    )

    stdout.print()
    if state.awaiting_reply.notification_id:
        stdout.print(f"Pending notifications:  awaiting reply ({state.awaiting_reply.kind})")
    else:
        stdout.print("Pending notifications:  (none)")


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------


@app.command()
def status(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit state.yaml as JSON for programmatic use."),
    ] = False,
) -> None:
    """Show the orchestrator's current state."""
    root = _resolve_root()
    _init_logging()

    try:
        state = read_state(root)
    except FileNotFoundError:
        # Fresh repo that hasn't run ``huragok submit`` yet. The raw
        # FileNotFoundError traceback was unhelpful; render a friendly
        # message and exit 0 so operators aren't alarmed.
        if json_output:
            typer.echo(json.dumps({"phase": "no-batch", "batch_id": None}, indent=2))
        else:
            stdout.print(Text("huragok — no batch submitted", style="bold"))
            stdout.print("Run `huragok submit <batch.yaml>` to begin.")
        return

    if json_output:
        payload = state.model_dump(mode="json", by_alias=True)
        typer.echo(json.dumps(payload, indent=2, default=str, sort_keys=False))
        return

    batch = _load_batch_if_any(root)
    statuses = _load_task_statuses(root, batch) if batch is not None else {}
    _render_human_status(state, batch, statuses, root)


@app.command()
def tasks(
    state: Annotated[
        str | None,
        typer.Option("--state", help="Filter by status.yaml.state value."),
    ] = None,
) -> None:
    """List the current batch's tasks, optionally filtered by state."""
    root = _resolve_root()
    _init_logging()

    batch = _load_batch_if_any(root)
    if batch is None or not batch.tasks:
        typer.echo("no batch in flight")
        return

    table = Table(title=f"Tasks — {batch.batch_id}", show_lines=False)
    table.add_column("ID", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Priority", justify="right", no_wrap=True)
    table.add_column("Title", overflow="fold")

    statuses = _load_task_statuses(root, batch)
    rendered = 0
    for task in batch.tasks:
        resolved_state = statuses[task.id].state if task.id in statuses else "pending"
        if state is not None and resolved_state != state:
            continue
        table.add_row(task.id, resolved_state, task.kind, str(task.priority), task.title)
        rendered += 1

    if rendered == 0:
        typer.echo("(no tasks match filter)")
        return

    stdout.print(table)


@app.command()
def show(
    task_id: Annotated[str, typer.Argument(help="Task ID to inspect.")],
    full: Annotated[
        bool,
        typer.Option("--full", help="Inline every artifact body under ## headings."),
    ] = False,
) -> None:
    """Show a task's summary; ``--full`` inlines every artifact body."""
    root = _resolve_root()
    _init_logging()

    folder = task_dir(root, task_id)
    if not folder.is_dir():
        stderr.print(f"[red]error:[/red] task not found: {task_id}")
        raise typer.Exit(1)

    status_obj: StatusFile | None = None
    status_path = folder / STATUS_FILE
    if status_path.exists():
        try:
            status_obj = read_status(root, task_id)
        except ValidationError as exc:
            stderr.print(f"[yellow]warn:[/yellow] malformed status.yaml: {exc}")

    title = _artifact_title(folder / SPEC_FILE)

    stdout.print(Text(task_id, style="bold"))
    if title is not None:
        stdout.print(f"  title:        {title}")
    if status_obj is not None:
        stdout.print(f"  state:        {status_obj.state}")
        stdout.print(f"  foundational: {str(status_obj.foundational).lower()}")
        if status_obj.blockers:
            stdout.print("  blockers:")
            for blocker in status_obj.blockers:
                stdout.print(f"    - {blocker}")
        if status_obj.ui_review.required:
            resolved = status_obj.ui_review.resolved or "pending"
            stdout.print(f"  ui_review:    required (resolved: {resolved})")

    artifact_order = (
        SPEC_FILE,
        IMPLEMENTATION_FILE,
        TESTS_FILE,
        REVIEW_FILE,
        UI_REVIEW_FILE,
    )
    present = [name for name in artifact_order if (folder / name).exists()]
    if present:
        stdout.print(f"  artifacts:    {', '.join(present)}")

    if not full:
        return

    for name in present:
        stdout.print()
        stdout.print(Text(f"## {name}", style="bold"))
        stdout.print()
        try:
            _, body = read_artifact(folder / name)
        except ArtifactFormatError as exc:
            stderr.print(f"[yellow]warn:[/yellow] {exc}")
            continue
        stdout.print(body.rstrip() or "(empty body)")


def _artifact_title(spec_path: Path) -> str | None:
    """Pull the first ``# Heading`` from a spec.md body, if present."""
    if not spec_path.exists():
        return None
    try:
        _, body = read_artifact(spec_path)
    except ArtifactFormatError:
        return None
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


# ---------------------------------------------------------------------------
# Supervisor lifecycle: run / stop / halt.
# ---------------------------------------------------------------------------


@app.command()
def run() -> None:
    """Start the orchestrator daemon in the foreground."""
    # Imported lazily so that `huragok --help` and read-only commands do not
    # pay the import cost of the asyncio supervisor stack.
    from orchestrator.supervisor.loop import run as supervisor_run

    root = _resolve_root()
    _init_logging()
    settings = load_settings()

    exit_code = asyncio.run(supervisor_run(root, settings))
    raise typer.Exit(exit_code)


@app.command()
def start() -> None:
    """Point the operator at the systemd unit; not a real background launcher.

    Per ADR-0002 D5 and the B2 scope note, the CLI deliberately does
    not fork-and-daemonise. Foreground use is ``huragok run``; long-
    running background use is ``systemctl --user start huragok.service``.
    """
    typer.echo(
        "For background deployment, install the systemd unit and run:\n"
        "\n"
        "  systemctl --user start huragok.service\n"
        "\n"
        "Installation instructions: docs/deployment.md",
        err=True,
    )
    raise typer.Exit(1)


@app.command()
def stop() -> None:
    """Gracefully stop a running orchestrator daemon.

    Sends SIGTERM to the PID recorded in ``.huragok/daemon.pid``. If no
    daemon is running, exits 0 with a friendly message — a missing
    daemon is not an error condition.
    """
    root = _resolve_root()
    _init_logging()

    pid = _read_daemon_pid(root)
    if pid is None:
        typer.echo("no daemon running")
        return
    if not _process_alive(pid):
        typer.echo(f"stale pid file (pid {pid} not running); removing")
        daemon_pid_file(root).unlink(missing_ok=True)
        return

    # Belt-and-suspenders: write a ``stop`` request marker so the loop's
    # request-file poll picks it up even if signal delivery is slow.
    req_dir = requests_dir(root)
    req_dir.mkdir(parents=True, exist_ok=True)
    (req_dir / "stop").write_text("")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"pid {pid} exited before the signal landed")
        return

    typer.echo(f"sent SIGTERM to pid {pid}")


@app.command()
def halt() -> None:
    """Halt a running batch after the in-flight session finishes.

    Writes ``.huragok/requests/halt`` and sends SIGUSR1 so the daemon
    picks up the request on the next tick. The current session continues
    to completion; no new sessions launch after it.
    """
    root = _resolve_root()
    _init_logging()

    req_dir = requests_dir(root)
    req_dir.mkdir(parents=True, exist_ok=True)
    halt_path = req_dir / "halt"
    halt_path.write_text("")

    pid = _read_daemon_pid(root)
    if pid is None or not _process_alive(pid):
        typer.echo("halt request written; no live daemon to signal")
        return
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        typer.echo("halt request written; daemon exited before signal")
        return
    typer.echo(f"halt request written; signalled pid {pid}")


# ---------------------------------------------------------------------------
# Still-stubbed Slice-B commands (promoted in B2).
# ---------------------------------------------------------------------------


@app.command()
def reply(
    verb: Annotated[str, typer.Argument(help="Reply verb: continue | iterate | stop | escalate.")],
    notification_id: Annotated[
        str | None,
        typer.Argument(help="Notification to reply to; omit if only one is outstanding."),
    ] = None,
    annotation: Annotated[
        str | None,
        typer.Argument(help="Free-form annotation accompanying the reply."),
    ] = None,
) -> None:
    """Reply to a pending notification on behalf of the operator."""
    root = _resolve_root()
    _init_logging()

    normalized = normalize_verb(verb)
    if normalized is None:
        stderr.print(
            f"[red]error:[/red] unknown verb {verb!r}; "
            f"valid: {', '.join(sorted(set(REPLY_VERB_ALIASES.values())))}"
        )
        raise typer.Exit(1)

    # Determine which notification this reply targets. B2's state.yaml
    # models one outstanding reply via ``awaiting_reply``; that is the
    # canonical source for single-pending matching.
    try:
        state = read_state(root)
    except FileNotFoundError:
        state = None  # No live daemon / no state file yet.

    pending_id = state.awaiting_reply.notification_id if state is not None else None

    if notification_id is None:
        if pending_id is None:
            typer.echo("no pending notifications")
            return
        notification_id = pending_id

    # Persist the reply file atomically.
    req_dir = requests_dir(root)
    req_dir.mkdir(parents=True, exist_ok=True)
    reply_path = req_dir / f"reply-{notification_id}.yaml"
    payload: dict[str, object] = {
        "notification_id": notification_id,
        "verb": normalized,
        "annotation": annotation,
        "received_at": datetime.now(UTC).isoformat(),
        "source": "cli",
    }
    tmp = reply_path.with_suffix(reply_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    tmp.replace(reply_path)

    # Signal the daemon if one is running; otherwise the reply file
    # sits on disk until the next `huragok run` start.
    pid = _read_daemon_pid(root)
    if pid is not None and _process_alive(pid):
        try:
            os.kill(pid, signal.SIGUSR1)
        except ProcessLookupError:
            typer.echo(f"reply written; pid {pid} exited before the signal landed")
            return
        typer.echo(f"reply {normalized} written; signalled pid {pid}")
    else:
        typer.echo(f"reply {normalized} written; no live daemon to signal")


@app.command()
def submit(
    batch_path: Annotated[Path, typer.Argument(help="Path to a batch.yaml file.")],
) -> None:
    """Queue a batch for the daemon to run.

    Validates against :class:`BatchFile`, refuses to overwrite an
    in-flight batch, archives any previous batch's ``work/`` folder to
    ``work.archived/<previous-batch-id>/``, and writes a fresh
    ``state.yaml`` pointing at the new batch. Does NOT start the daemon
    — that is ``huragok run`` or ``systemctl --user start
    huragok.service`` (see ADR-0002 D8).
    """
    root = _resolve_root()
    _init_logging()

    if not batch_path.exists():
        stderr.print(f"[red]error:[/red] batch file not found: {batch_path}")
        raise typer.Exit(1)

    # Validate against the schema before touching anything on disk.
    try:
        raw = yaml.safe_load(batch_path.read_text(encoding="utf-8"))
        batch = BatchFile.model_validate(raw)
    except yaml.YAMLError as exc:
        stderr.print(f"[red]error:[/red] could not parse {batch_path}: {exc}")
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        stderr.print(f"[red]error:[/red] invalid batch file: {exc}")
        raise typer.Exit(1) from exc

    # Refuse to overwrite an in-flight batch.
    existing_batch_id: str | None = None
    try:
        current = read_state(root)
    except FileNotFoundError:
        current = None
    if current is not None:
        existing_batch_id = current.batch_id
        if current.phase == "running":
            stderr.print(
                "[red]error:[/red] a batch is currently running "
                f"({current.batch_id}); stop or halt it before submitting a new one"
            )
            raise typer.Exit(1)

    # Archive the previous work/ directory if it has content. Preserve
    # history rather than deleting — easier to audit, cheap to keep.
    huragok = huragok_dir(root)
    work = huragok / WORK_DIR
    if work.is_dir() and any(work.iterdir()):
        archive_name = existing_batch_id or f"pre-{batch.batch_id}"
        archive_root = huragok / "work.archived" / archive_name
        archive_root.parent.mkdir(parents=True, exist_ok=True)
        # If an earlier submit already archived this batch id, stamp the
        # old archive with a timestamp so nothing is silently overwritten.
        if archive_root.exists():
            archive_root = archive_root.parent / (
                archive_root.name + "-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            )
        shutil.move(str(work), str(archive_root))
    work.mkdir(parents=True, exist_ok=True)

    # Atomically write the new batch.yaml.
    write_batch(root, batch)

    # Fresh state.yaml pointing at the new batch, at phase=idle so the
    # daemon picks it up on next start. ``started_at`` stays ``None``
    # until the daemon marks the batch start.
    state = StateFile(
        version=1,
        phase="idle",
        batch_id=batch.batch_id,
        current_task=None,
        current_agent=None,
        session_count=0,
        session_id=None,
        started_at=None,
        last_checkpoint=None,
        halted_reason=None,
        budget_consumed=BudgetConsumed(),
        session_budget=SessionBudget(),
        pending_notifications=[],
        awaiting_reply=AwaitingReply(),
    )
    write_state(root, state)

    typer.echo(f"submitted {batch.batch_id} with {len(batch.tasks)} task(s)")


_LOG_LEVEL_ORDER: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "critical": 50,
}


@app.command()
def logs(
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Tail the log.")] = False,
    level: Annotated[
        str | None,
        typer.Option("--level", help="Minimum log level to include (debug/info/warn/error)."),
    ] = None,
) -> None:
    """Tail the current batch's structured log.

    Reads ``.huragok/logs/batch-<id>.jsonl`` for the batch recorded in
    ``state.yaml.batch_id``. When ``--follow`` is set, streams new
    records until SIGTERM. With ``--level``, filters by the structlog
    ``level`` field.
    """
    root = _resolve_root()
    _init_logging()

    try:
        state = read_state(root)
    except FileNotFoundError:
        typer.echo("no batch in flight")
        return
    if state.batch_id is None:
        typer.echo("no batch in flight")
        return

    min_level_value: int | None = None
    if level is not None:
        normalized = level.strip().lower()
        min_level_value = _LOG_LEVEL_ORDER.get(normalized)
        if min_level_value is None:
            stderr.print(f"[red]error:[/red] unknown log level {level!r}")
            raise typer.Exit(1)

    log_path = batch_log(root, state.batch_id)
    if not log_path.exists():
        typer.echo(f"no batch log on disk yet ({log_path})")
        return

    _tail_batch_log(log_path, follow=follow, min_level_value=min_level_value)


def _tail_batch_log(path: Path, *, follow: bool, min_level_value: int | None) -> None:
    """Python-native ``tail -f`` for a single JSONL file.

    Emits the last 50 records by default; with ``follow=True``, also
    streams anything appended after opening. SIGTERM / KeyboardInterrupt
    exits cleanly with code 0.
    """
    lines = _last_n_lines(path, 50)
    for line in lines:
        _emit_log_line(line, min_level_value)

    if not follow:
        return

    try:
        with open(path, encoding="utf-8") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                _emit_log_line(line, min_level_value)
    except (KeyboardInterrupt, SystemExit):
        return


def _last_n_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` non-empty lines of a text file.

    Streams from the end of the file in 4KB chunks to avoid loading
    large audit / log files into memory. Falls back to whole-file read
    for small inputs.
    """
    block = 4096
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size <= block:
            fh.seek(0)
            return [line.decode("utf-8", errors="replace") for line in fh.readlines()[-n:]]
        pos = size
        lines_found: list[bytes] = []
        buffer = b""
        while pos > 0 and len(lines_found) < n + 1:
            read_size = min(block, pos)
            pos -= read_size
            fh.seek(pos)
            chunk = fh.read(read_size)
            buffer = chunk + buffer
            parts = buffer.split(b"\n")
            buffer = parts[0]
            for part in reversed(parts[1:]):
                lines_found.append(part)
                if len(lines_found) >= n + 1:
                    break
        if pos == 0 and buffer:
            lines_found.append(buffer)
        decoded = [line.decode("utf-8", errors="replace") for line in reversed(lines_found)]
        filtered = [line for line in decoded if line.strip()]
        return filtered[-n:]


def _emit_log_line(line: str, min_level_value: int | None) -> None:
    """Print a log line, filtering by level if configured."""
    stripped = line.rstrip("\n")
    if not stripped:
        return
    if min_level_value is not None:
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            # Unparseable line — let it through so operators can see
            # non-JSON errors captured in the file.
            typer.echo(stripped)
            return
        record_level = record.get("level", "info").lower()
        level_value = _LOG_LEVEL_ORDER.get(record_level, 20)
        if level_value < min_level_value:
            return
    typer.echo(stripped)


# ---------------------------------------------------------------------------
# CLI internals shared by run / stop / halt.
# ---------------------------------------------------------------------------


def _read_daemon_pid(root: Path) -> int | None:
    """Return the pid recorded in the daemon pid file, or None if absent."""
    pid_path = daemon_pid_file(root)
    try:
        raw = pid_path.read_text().strip()
    except FileNotFoundError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _process_alive(pid: int) -> bool:
    """Return True if ``pid`` is a live process owned by anyone on this host."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
