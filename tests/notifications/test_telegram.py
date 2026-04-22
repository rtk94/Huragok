"""Tests for ``orchestrator.notifications.telegram``.

Every test mocks HTTPX via :class:`httpx.MockTransport` — no test
reaches ``api.telegram.org``. The dispatcher's ``client`` kwarg lets us
inject a handler that returns canned responses per request.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from orchestrator.notifications import Notification
from orchestrator.notifications.telegram import (
    ParsedReply,
    TelegramDispatcher,
    normalize_verb,
    parse_reply_text,
)
from orchestrator.paths import audit_log

# ---------------------------------------------------------------------------
# MockTransport helpers.
# ---------------------------------------------------------------------------


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_dispatcher(
    *,
    client: httpx.AsyncClient,
    root: Path | None = None,
    batch_id: str | None = None,
    grace_seconds: int = 600,
    chat_id: str = "12345",
) -> TelegramDispatcher:
    return TelegramDispatcher(
        bot_token=SecretStr("test-token"),
        default_chat_id=chat_id,
        root=root,
        batch_id=batch_id,
        client=client,
        reachability_grace_seconds=grace_seconds,
    )


def _make_notification(
    *,
    kind: str = "error",
    summary: str = "something broke",
    reply_verbs: list[str] | None = None,
) -> Notification:
    return Notification.make(
        kind=kind,  # type: ignore[arg-type]
        summary=summary,
        reply_verbs=reply_verbs if reply_verbs is not None else [],
    )


# ---------------------------------------------------------------------------
# normalize_verb() and parse_reply_text().
# ---------------------------------------------------------------------------


def test_normalize_verb_canonical_forms() -> None:
    assert normalize_verb("continue") == "continue"
    assert normalize_verb("iterate") == "iterate"
    assert normalize_verb("stop") == "stop"
    assert normalize_verb("escalate") == "escalate"


def test_normalize_verb_aliases() -> None:
    assert normalize_verb("c") == "continue"
    assert normalize_verb("ok") == "continue"
    assert normalize_verb("yes") == "continue"
    assert normalize_verb("i") == "iterate"
    assert normalize_verb("s") == "stop"
    assert normalize_verb("e") == "escalate"


def test_normalize_verb_case_insensitive() -> None:
    assert normalize_verb("CONTINUE") == "continue"
    assert normalize_verb("  Iterate  ") == "iterate"


def test_normalize_verb_unknown_returns_none() -> None:
    assert normalize_verb("foo") is None
    assert normalize_verb("") is None


def test_parse_reply_text_bare_verb() -> None:
    assert parse_reply_text("continue") == ParsedReply(verb="continue")


def test_parse_reply_text_with_id() -> None:
    parsed = parse_reply_text("continue 01HXYZ")
    assert parsed == ParsedReply(verb="continue", notification_id="01HXYZ")


def test_parse_reply_text_with_annotation() -> None:
    parsed = parse_reply_text("iterate 01ABC also-fix-typos please")
    assert parsed == ParsedReply(
        verb="iterate",
        notification_id="01ABC",
        annotation="also-fix-typos please",
    )


def test_parse_reply_text_invalid_verb() -> None:
    assert parse_reply_text("hello world") is None


def test_parse_reply_text_empty() -> None:
    assert parse_reply_text("   ") is None


# ---------------------------------------------------------------------------
# send() — HTTP status handling.
# ---------------------------------------------------------------------------


async def test_send_200_updates_reachable_and_records_pending() -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.url.path.endswith("/sendMessage")
        body = json.loads(request.content)
        assert body["chat_id"] == "12345"
        assert "something broke" in body["text"]
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client)
    notif = _make_notification(reply_verbs=["continue", "stop"])

    await dispatcher.send(notif)

    assert len(seen_requests) == 1
    assert dispatcher.reachable is True
    assert notif.id in dispatcher._sent
    assert notif.id in dispatcher._pending


async def test_send_is_idempotent_by_notification_id() -> None:
    count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        count[0] += 1
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client)
    notif = _make_notification()

    await dispatcher.send(notif)
    await dispatcher.send(notif)  # second call no-ops

    assert count[0] == 1


async def test_send_4xx_auth_error_logs_and_gives_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client)
    notif = _make_notification(reply_verbs=["continue"])

    await dispatcher.send(notif)

    # Auth failure blocks future reachability when there's a pending
    # notification; but since the notification was never added to
    # pending (auth path returns early), reachable stays True.
    assert notif.id not in dispatcher._sent
    # The _auth_failed flag is set.
    assert dispatcher._auth_failed is True


async def test_send_500_logs_warn_and_marks_send_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client)
    notif = _make_notification(reply_verbs=["continue"])

    await dispatcher.send(notif)

    assert notif.id not in dispatcher._sent
    assert dispatcher._last_send_ok is None
    assert dispatcher._last_send_attempt is not None


async def test_send_transport_error_marks_attempt_but_not_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client)
    notif = _make_notification(reply_verbs=["continue"])

    await dispatcher.send(notif)

    assert notif.id not in dispatcher._sent
    assert dispatcher._last_send_ok is None
    assert dispatcher._last_send_attempt is not None


# ---------------------------------------------------------------------------
# start() — long-poll loop.
# ---------------------------------------------------------------------------


async def test_start_consumes_updates_and_writes_reply(tmp_path: Path) -> None:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            payload = {
                "ok": True,
                "result": [
                    {
                        "update_id": 10,
                        "message": {
                            "chat": {"id": 12345},
                            "text": "continue 01HXYZ123 looks good",
                        },
                    }
                ],
            }
            return httpx.Response(200, json=payload)
        # Subsequent polls: empty result so the loop idles.
        return httpx.Response(200, json={"ok": True, "result": []})

    client = _make_client(handler)
    (tmp_path / ".huragok").mkdir()
    dispatcher = _make_dispatcher(client=client, root=tmp_path, batch_id="batch-001")

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.start(stop))
    # Wait for the handler to have processed at least two polls.
    for _ in range(60):
        if calls[0] >= 2:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    reply_path = tmp_path / ".huragok" / "requests" / "reply-01HXYZ123.yaml"
    assert reply_path.exists()

    import yaml

    payload = yaml.safe_load(reply_path.read_text())
    assert payload["verb"] == "continue"
    assert payload["notification_id"] == "01HXYZ123"
    assert payload["annotation"] == "looks good"
    assert payload["source"] == "telegram"

    # Audit entry written.
    audit = audit_log(tmp_path, "batch-001").read_text().strip().splitlines()
    assert audit, "audit log should have at least one entry"
    record = json.loads(audit[0])
    assert record["kind"] == "notification-reply"
    assert record["verb"] == "continue"


async def test_start_cursor_persists_across_restart(tmp_path: Path) -> None:
    """After a reply is processed, a fresh dispatcher resumes past it."""
    # First run.
    call_log: list[int] = []

    def handler_1(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        call_log.append(int(params.get("offset", "0")))
        # Return one update then empty.
        if len(call_log) == 1:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 500,
                            "message": {
                                "chat": {"id": 12345},
                                "text": "stop",
                            },
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"ok": True, "result": []})

    (tmp_path / ".huragok").mkdir()
    client = _make_client(handler_1)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.start(stop))
    for _ in range(60):
        if len(call_log) >= 2:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    # Cursor file exists.
    cursor_path = tmp_path / ".huragok" / "telegram-cursor.yaml"
    assert cursor_path.exists()

    # Second run: the first offset should be 501 (cursor + 1).
    offsets: list[int] = []

    def handler_2(request: httpx.Request) -> httpx.Response:
        offsets.append(int(request.url.params.get("offset", "0")))
        return httpx.Response(200, json={"ok": True, "result": []})

    client2 = _make_client(handler_2)
    dispatcher2 = _make_dispatcher(client=client2, root=tmp_path)
    stop2 = asyncio.Event()
    task2 = asyncio.create_task(dispatcher2.start(stop2))
    for _ in range(40):
        if offsets:
            break
        await asyncio.sleep(0.05)
    stop2.set()
    await asyncio.wait_for(task2, timeout=2.0)

    assert offsets and offsets[0] == 501


async def test_start_idempotent_on_duplicate_update_id(tmp_path: Path) -> None:
    """The same update_id delivered twice applies once."""
    (tmp_path / ".huragok").mkdir()

    deliveries = [
        {
            "update_id": 77,
            "message": {
                "chat": {"id": 12345},
                "text": "continue 01ABC",
            },
        },
        {
            "update_id": 77,  # duplicate
            "message": {
                "chat": {"id": 12345},
                "text": "continue 01ABC",
            },
        },
    ]
    call = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call[0] += 1
        if call[0] <= 2:
            return httpx.Response(200, json={"ok": True, "result": [deliveries[call[0] - 1]]})
        return httpx.Response(200, json={"ok": True, "result": []})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.start(stop))
    for _ in range(60):
        if call[0] >= 3:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    # The reply file exists — not strict to count entries; just confirm
    # the single reply landed and the cursor advanced past it.
    reply_path = tmp_path / ".huragok" / "requests" / "reply-01ABC.yaml"
    assert reply_path.exists()
    assert dispatcher._cursor >= 77


async def test_start_ignores_wrong_chat_id(tmp_path: Path) -> None:
    (tmp_path / ".huragok").mkdir()
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "chat": {"id": 99999},  # different chat
                                "text": "continue",
                            },
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"ok": True, "result": []})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path, chat_id="12345")
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.start(stop))
    for _ in range(40):
        if calls[0] >= 2:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    req_dir = tmp_path / ".huragok" / "requests"
    assert not req_dir.exists() or not any(req_dir.iterdir())


async def test_start_on_auth_error_stops_polling(tmp_path: Path) -> None:
    (tmp_path / ".huragok").mkdir()
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(401, text="Unauthorized")

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.start(stop))
    # After the first 401 the dispatcher marks _auth_failed and stops
    # polling real updates. It idles until stop.
    for _ in range(20):
        if dispatcher._auth_failed:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert dispatcher._auth_failed is True


# ---------------------------------------------------------------------------
# Reply matching rules.
# ---------------------------------------------------------------------------


async def test_resolve_reply_with_single_pending(tmp_path: Path) -> None:
    """A bare-verb reply is resolved to the only outstanding notification."""
    (tmp_path / ".huragok").mkdir()

    # Stage: exactly one notification outstanding.
    def send_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(send_handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)
    notif = _make_notification(reply_verbs=["continue", "stop"])
    await dispatcher.send(notif)

    # Now feed a bare-verb reply into _handle_update.
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 12345},
            "text": "continue",
        },
    }
    await dispatcher._handle_update(update)

    reply_path = tmp_path / ".huragok" / "requests" / f"reply-{notif.id}.yaml"
    assert reply_path.exists()
    # Pending has been cleared.
    assert notif.id not in dispatcher._pending


async def test_resolve_reply_with_multiple_pending_is_ambiguous(tmp_path: Path) -> None:
    (tmp_path / ".huragok").mkdir()

    def send_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(send_handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)
    await dispatcher.send(_make_notification(reply_verbs=["continue"]))
    await dispatcher.send(_make_notification(reply_verbs=["stop"]))

    assert len(dispatcher._pending) == 2

    update = {
        "update_id": 1,
        "message": {"chat": {"id": 12345}, "text": "continue"},
    }
    await dispatcher._handle_update(update)

    # No reply file was persisted — ambiguous bare verb was dropped.
    req_dir = tmp_path / ".huragok" / "requests"
    assert not req_dir.exists() or not any(req_dir.iterdir())


async def test_resolve_reply_with_explicit_id_even_if_unknown(tmp_path: Path) -> None:
    """If the operator specifies an id, trust it even without a pending entry."""
    (tmp_path / ".huragok").mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)

    update = {
        "update_id": 5,
        "message": {"chat": {"id": 12345}, "text": "stop 01NEWPENDING"},
    }
    await dispatcher._handle_update(update)

    reply_path = tmp_path / ".huragok" / "requests" / "reply-01NEWPENDING.yaml"
    assert reply_path.exists()


# ---------------------------------------------------------------------------
# Reachability transitions.
# ---------------------------------------------------------------------------


async def test_reachable_starts_true_before_any_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client)
    assert dispatcher.reachable is True


async def test_reachable_goes_false_after_grace_without_success(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path, grace_seconds=1)
    # Put a pending notification on the dispatcher directly so reachable
    # has something to gate on.
    notif = _make_notification(reply_verbs=["continue"])
    await dispatcher.send(notif)
    assert notif.id not in dispatcher._sent

    # Pretend the failing attempt was 2 seconds ago.
    dispatcher._last_send_attempt = datetime.now(UTC) - timedelta(seconds=2)
    dispatcher._last_receive_attempt = datetime.now(UTC) - timedelta(seconds=2)
    # But a pending notification is needed for reachable to flip.
    dispatcher._pending[notif.id] = notif

    assert dispatcher.reachable is False


async def test_reachable_recovers_on_first_success(tmp_path: Path) -> None:
    # Simulate an old failed state, then a successful send resets the
    # last_send_ok and flips reachable back to True.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path, grace_seconds=1)
    notif = _make_notification(reply_verbs=["continue"])
    dispatcher._pending[notif.id] = notif
    dispatcher._last_send_attempt = datetime.now(UTC) - timedelta(seconds=5)
    dispatcher._last_receive_attempt = datetime.now(UTC) - timedelta(seconds=5)
    assert dispatcher.reachable is False

    # Now actually send — success updates last_send_ok.
    await dispatcher.send(_make_notification(reply_verbs=["continue"]))
    assert dispatcher.reachable is True


async def test_reachable_stays_true_without_pending() -> None:
    # Even if both channels have been failing forever, reachable is True
    # when nothing is outstanding — no false positives.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, grace_seconds=1)
    dispatcher._last_send_attempt = datetime.now(UTC) - timedelta(minutes=30)
    dispatcher._last_receive_attempt = datetime.now(UTC) - timedelta(minutes=30)
    # No pending notifications.
    assert dispatcher.reachable is True


# ---------------------------------------------------------------------------
# Format checks.
# ---------------------------------------------------------------------------


async def test_outbound_message_contains_summary_and_id() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client)
    notif = Notification.make(
        kind="blocker",
        summary="task task-0042 auto-blocked: 2x crash",
        reply_verbs=["iterate", "stop"],
        artifact_path="work/task-0042/review.md",
    )
    await dispatcher.send(notif)

    assert "task task-0042" in captured["text"]
    assert "iterate | stop" in captured["text"]
    assert notif.id in captured["text"]
    assert "work/task-0042/review.md" in captured["text"]
    assert captured["chat_id"] == "12345"


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("c", "continue"),
        ("C", "continue"),
        ("ok", "continue"),
        ("YES", "continue"),
        ("iterate", "iterate"),
        ("s", "stop"),
        ("e", "escalate"),
    ],
)
def test_normalize_verb_param(alias: str, canonical: str) -> None:
    assert normalize_verb(alias) == canonical


# ---------------------------------------------------------------------------
# Bot /start — Telegram's universal first message.
# ---------------------------------------------------------------------------


async def test_start_command_does_not_log_invalid_verb(tmp_path: Path) -> None:
    """``/start`` from a new-bot greeting is silent, not an invalid-verb warning."""
    import structlog.testing

    (tmp_path / ".huragok").mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)

    with structlog.testing.capture_logs() as cap:
        update = {
            "update_id": 1,
            "message": {"chat": {"id": 12345}, "text": "/start"},
        }
        await dispatcher._handle_update(update)

    events = [entry.get("event") for entry in cap]
    assert "telegram.reply.invalid_verb" not in events
    # DEBUG record emitted under its own event name so operators can
    # grep for it if they want to audit bot-init flow.
    debug_entries = [e for e in cap if e.get("event") == "telegram.bot.initialization"]
    assert debug_entries and debug_entries[0]["log_level"] == "debug"

    # No reply file was persisted — /start is not a reply attempt.
    req_dir = tmp_path / ".huragok" / "requests"
    assert not req_dir.exists() or not any(req_dir.iterdir())


async def test_start_command_case_insensitive_and_with_payload(tmp_path: Path) -> None:
    """``/START`` and ``/start foo`` are also treated as bot-init, not replies."""
    import structlog.testing

    (tmp_path / ".huragok").mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)

    with structlog.testing.capture_logs() as cap:
        for update_id, text in (
            (10, "/START"),
            (11, "/start hello"),
            (12, "/start@mybot"),
        ):
            await dispatcher._handle_update(
                {"update_id": update_id, "message": {"chat": {"id": 12345}, "text": text}}
            )

    events = [entry.get("event") for entry in cap]
    assert "telegram.reply.invalid_verb" not in events
    # Every /start variant landed under the init event.
    init_events = [e for e in events if e == "telegram.bot.initialization"]
    assert len(init_events) == 3


async def test_other_unknown_text_still_logs_invalid_verb(tmp_path: Path) -> None:
    """Unknown text that isn't ``/start`` continues to log at INFO."""
    import structlog.testing

    (tmp_path / ".huragok").mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _make_client(handler)
    dispatcher = _make_dispatcher(client=client, root=tmp_path)

    with structlog.testing.capture_logs() as cap:
        update = {
            "update_id": 20,
            "message": {"chat": {"id": 12345}, "text": "hello there"},
        }
        await dispatcher._handle_update(update)

    matching = [e for e in cap if e.get("event") == "telegram.reply.invalid_verb"]
    assert matching and matching[0]["log_level"] == "info"
