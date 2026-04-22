"""Telegram bot dispatcher (ADR-0002 D6).

Outbound ``sendMessage``, inbound ``getUpdates`` long-poll, reply
parsing, idempotency, and 10-minute reachability tracking. Runs
entirely over ``httpx`` for both synchronous sends and the long-poll
loop so no extra client lives alongside the rest of the daemon.

No test in this repo hits ``api.telegram.org`` — the dispatcher accepts
an injected :class:`httpx.AsyncClient` (usually backed by
:class:`httpx.MockTransport`) so responses can be stubbed at the
transport layer.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml
from pydantic import SecretStr

from orchestrator.notifications.base import Notification, NotificationDispatcher
from orchestrator.state import append_audit

__all__ = [
    "REACHABILITY_GRACE_SECONDS",
    "REPLY_VERB_ALIASES",
    "TELEGRAM_API_BASE",
    "ParsedReply",
    "TelegramDispatcher",
    "normalize_verb",
    "parse_reply_text",
]


TELEGRAM_API_BASE: str = "https://api.telegram.org"
REACHABILITY_GRACE_SECONDS: int = 600  # 10 minutes — ADR-0002 D6 closing paragraph.

# Poll timeout. Telegram recommends 25-50s; 25 keeps latency predictable.
_DEFAULT_POLL_TIMEOUT_SECONDS: int = 25
_DEFAULT_SEND_TIMEOUT_SECONDS: float = 10.0

# After a transport-layer failure we back off briefly before retrying the
# poll. Short enough that a transient outage costs at most a few seconds;
# long enough that a persistent outage doesn't hammer the network.
_POLL_ERROR_BACKOFF_SECONDS: float = 5.0
_SEND_ERROR_BACKOFF_SECONDS: float = 5.0

# Canonical verbs in ADR-0002 D6 plus the aliases explicitly called out.
REPLY_VERB_ALIASES: dict[str, str] = {
    "continue": "continue",
    "c": "continue",
    "ok": "continue",
    "yes": "continue",
    "iterate": "iterate",
    "i": "iterate",
    "stop": "stop",
    "s": "stop",
    "escalate": "escalate",
    "e": "escalate",
}


# ---------------------------------------------------------------------------
# Parsed-reply dataclass (public for tests and the CLI's reply command).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedReply:
    """A parsed inbound Telegram message.

    ``verb`` is the normalised canonical form (``continue`` / ``iterate``
    / ``stop`` / ``escalate``). ``notification_id`` is optional per
    ADR-0002 D6. ``annotation`` is free-form text from the third
    whitespace-separated field onwards.
    """

    verb: str
    notification_id: str | None = None
    annotation: str | None = None


def normalize_verb(raw: str) -> str | None:
    """Normalise a verb or alias to the canonical form, or ``None`` if invalid."""
    return REPLY_VERB_ALIASES.get(raw.strip().lower())


def parse_reply_text(text: str) -> ParsedReply | None:
    """Parse an inbound message body into a :class:`ParsedReply`.

    Returns ``None`` when the message is empty or the first token is not
    a recognised verb.
    """
    parts = text.strip().split(maxsplit=2)
    if not parts:
        return None
    verb = normalize_verb(parts[0])
    if verb is None:
        return None
    notification_id: str | None = None
    annotation: str | None = None
    if len(parts) > 1:
        notification_id = parts[1].strip() or None
    if len(parts) > 2:
        annotation = parts[2].strip() or None
    return ParsedReply(verb=verb, notification_id=notification_id, annotation=annotation)


# ---------------------------------------------------------------------------
# TelegramDispatcher.
# ---------------------------------------------------------------------------


class TelegramDispatcher(NotificationDispatcher):
    """Real-network notification backend backed by the Telegram Bot API.

    Consumes notifications via :meth:`send`, writes operator replies as
    ``.huragok/requests/reply-<id>.yaml`` via :meth:`start`, and exposes
    a :attr:`reachable` property the supervisor consults before
    transitioning to ``paused`` (ADR-0002 D6 closing paragraph).
    """

    def __init__(
        self,
        bot_token: SecretStr,
        default_chat_id: str,
        *,
        poll_timeout_seconds: int = _DEFAULT_POLL_TIMEOUT_SECONDS,
        send_timeout_seconds: float = _DEFAULT_SEND_TIMEOUT_SECONDS,
        root: Path | None = None,
        batch_id: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_base: str = TELEGRAM_API_BASE,
        reachability_grace_seconds: int = REACHABILITY_GRACE_SECONDS,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = str(default_chat_id)
        self._poll_timeout_seconds = poll_timeout_seconds
        self._send_timeout_seconds = send_timeout_seconds
        self._root = root
        self._batch_id = batch_id
        self._api_base = api_base.rstrip("/")
        self._grace = reachability_grace_seconds
        # Injected client for tests (httpx.MockTransport-backed). The
        # dispatcher never tears the caller's client down; it only
        # closes the one it creates itself.
        self._client = client
        self._owns_client = client is None
        self._log = structlog.get_logger(__name__).bind(component="telegram-dispatcher")

        # Idempotency + reachability state.
        self._sent: set[str] = set()
        self._pending: dict[str, Notification] = {}
        self._cursor: int = self._load_cursor()
        # Start reachable. _last_send_ok/_last_receive_ok stay None
        # until the first success; until then we can't be "unreachable
        # for 10 minutes" since nothing's been outstanding yet. The
        # dispatcher boots healthy.
        self._last_send_ok: datetime | None = None
        self._last_receive_ok: datetime | None = None
        self._last_send_attempt: datetime | None = None
        self._last_receive_attempt: datetime | None = None
        # When True, the inbound loop has detected a permanent
        # unauthenticated error and will no longer poll.
        self._auth_failed: bool = False

    # ------------------------------------------------------------------
    # Public API.
    # ------------------------------------------------------------------

    @property
    def reachable(self) -> bool:
        """True unless an outage is confirmed per ADR-0002 D6.

        Unreachable means: we have a pending notification AND both
        ``sendMessage`` and ``getUpdates`` have been failing for at
        least 10 minutes (or never succeeded while attempts were being
        made). A transient 5xx does not by itself flip this flag.
        """
        if self._auth_failed and self._pending:
            return False
        if not self._pending:
            return True
        now = datetime.now(UTC)
        send_failing = self._is_failing_for(now, self._last_send_ok, self._last_send_attempt)
        receive_failing = self._is_failing_for(
            now, self._last_receive_ok, self._last_receive_attempt
        )
        return not (send_failing and receive_failing)

    async def send(self, notification: Notification) -> None:
        """Dispatch ``notification`` to the configured chat.

        Idempotent by ``notification.id``. A successful send updates
        :attr:`reachable` state; HTTP 4xx gives up; HTTP 5xx / transport
        failures warn and leave the message on the pending queue for
        reachability tracking but does NOT retry from inside ``send``.
        The supervisor is expected to re-issue via its own rhythm when
        the network recovers — simpler than baking another retry loop
        into the dispatcher.
        """
        if notification.id in self._sent:
            return

        body = _format_outbound(notification, default_chat_id=self._chat_id)
        now = datetime.now(UTC)
        self._last_send_attempt = now
        try:
            response = await self._do_send(body)
        except httpx.HTTPError as exc:
            self._log.warning(
                "telegram.send.transport_error",
                notification_id=notification.id,
                error=str(exc),
            )
            return

        if response.status_code == 200:
            self._on_send_success(notification)
            return
        if response.status_code in (401, 403, 404):
            self._log.error(
                "telegram.send.auth_error",
                notification_id=notification.id,
                status=response.status_code,
                body=response.text[:200],
            )
            self._auth_failed = True
            return
        if response.status_code >= 500:
            self._log.warning(
                "telegram.send.server_error",
                notification_id=notification.id,
                status=response.status_code,
                body=response.text[:200],
            )
            return
        self._log.warning(
            "telegram.send.bad_request",
            notification_id=notification.id,
            status=response.status_code,
            body=response.text[:200],
        )

    async def start(self, stop_event: asyncio.Event) -> None:
        """Long-poll ``getUpdates`` until ``stop_event`` fires.

        Each update is either (a) a recognised reply that lands as a
        ``reply-<id>.yaml`` file in ``.huragok/requests/`` and is
        appended to the audit log, or (b) an unrecognised message that
        is logged at INFO and ignored.
        """
        try:
            while not stop_event.is_set():
                if self._auth_failed:
                    # Nothing more to do — we've already logged critical
                    # when the auth failure first appeared. Wait for
                    # shutdown without burning CPU.
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                    except TimeoutError:
                        continue
                    return

                self._last_receive_attempt = datetime.now(UTC)
                try:
                    updates = await self._do_poll()
                except httpx.HTTPError as exc:
                    self._log.warning("telegram.poll.transport_error", error=str(exc))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=_POLL_ERROR_BACKOFF_SECONDS
                        )
                    continue
                except _AuthError as exc:
                    self._log.critical(
                        "telegram.poll.auth_error",
                        status=exc.status_code,
                        body=exc.body,
                    )
                    self._auth_failed = True
                    continue
                except _TransientError as exc:
                    self._log.warning(
                        "telegram.poll.server_error",
                        status=exc.status_code,
                        body=exc.body,
                    )
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=_POLL_ERROR_BACKOFF_SECONDS
                        )
                    continue

                self._last_receive_ok = datetime.now(UTC)

                for update in updates:
                    await self._handle_update(update)

                # Explicit cooperative yield so a MockTransport-backed
                # test loop — or a server replying instantly with empty
                # results — can observe an externally-set stop_event
                # without starving the event loop.
                await asyncio.sleep(0)
        finally:
            if self._owns_client and self._client is not None:
                with contextlib.suppress(Exception):
                    await self._client.aclose()
                self._client = None

    # ------------------------------------------------------------------
    # Internal: HTTP.
    # ------------------------------------------------------------------

    async def _do_send(self, payload: dict[str, Any]) -> httpx.Response:
        client = self._get_client()
        url = f"{self._api_base}/bot{self._bot_token.get_secret_value()}/sendMessage"
        return await client.post(url, json=payload, timeout=self._send_timeout_seconds)

    async def _do_poll(self) -> list[dict[str, Any]]:
        client = self._get_client()
        url = f"{self._api_base}/bot{self._bot_token.get_secret_value()}/getUpdates"
        params = {
            "offset": self._cursor + 1,
            "timeout": self._poll_timeout_seconds,
        }
        # Give HTTPX a small extra margin over the server-side timeout
        # so the server has time to respond with an empty result.
        request_timeout = float(self._poll_timeout_seconds) + 10.0
        response = await client.get(url, params=params, timeout=request_timeout)

        if response.status_code in (401, 403, 404):
            raise _AuthError(response.status_code, response.text[:200])
        if response.status_code >= 500:
            raise _TransientError(response.status_code, response.text[:200])
        if response.status_code != 200:
            # Malformed 4xx — treat as transient so we retry with a
            # backoff rather than halting the poller.
            raise _TransientError(response.status_code, response.text[:200])

        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok", False):
            return []
        data = payload.get("result")
        return data if isinstance(data, list) else []

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    # ------------------------------------------------------------------
    # Internal: bookkeeping.
    # ------------------------------------------------------------------

    def _on_send_success(self, notification: Notification) -> None:
        self._sent.add(notification.id)
        if notification.reply_verbs:
            # Only notifications that accept replies count as "pending"
            # for reachability purposes — one-shot notifications (batch
            # complete with no verbs, for instance) do not gate the
            # daemon's reachability state.
            self._pending[notification.id] = notification
        self._last_send_ok = datetime.now(UTC)
        self._log.info(
            "telegram.send.ok",
            notification_id=notification.id,
            kind=notification.kind,
        )

    def _is_failing_for(
        self,
        now: datetime,
        last_ok: datetime | None,
        last_attempt: datetime | None,
    ) -> bool:
        """Returns True when a channel has been failing for >= grace seconds.

        "Failing" means: an attempt has been made AND the most recent
        successful response is older than ``grace`` seconds (or never
        landed). Until any attempt is made on a channel, it isn't
        considered failing.
        """
        if last_attempt is None:
            # No attempt yet on this channel — can't be "failing".
            return False
        threshold = timedelta(seconds=self._grace)
        if last_ok is None:
            return (now - last_attempt) >= threshold
        return (now - last_ok) >= threshold

    async def _handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        if update_id <= self._cursor:
            return  # idempotency on retried deliveries

        # Advance and persist the cursor regardless of whether the
        # message body parses; a bad reply shouldn't wedge the poller.
        self._cursor = update_id
        self._save_cursor(update_id)

        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if isinstance(chat, dict):
            from_chat = chat.get("id")
            if from_chat is not None and str(from_chat) != self._chat_id:
                self._log.info(
                    "telegram.reply.wrong_chat",
                    update_id=update_id,
                    from_chat=from_chat,
                )
                return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        parsed = parse_reply_text(text)
        if parsed is None:
            self._log.info("telegram.reply.invalid_verb", text=text[:120])
            return

        resolved_id = self._resolve_notification_id(parsed.notification_id)
        if resolved_id is None and parsed.notification_id is None:
            self._log.info("telegram.reply.no_pending", text=text[:120])
            return
        # Note: if the user gave us an explicit notification_id we honour
        # it even if we don't know about it — the supervisor may be
        # warm-booting and not yet have rebuilt its pending queue.
        target_id = resolved_id if resolved_id is not None else parsed.notification_id
        assert target_id is not None

        await self._persist_reply(
            notification_id=target_id,
            verb=parsed.verb,
            annotation=parsed.annotation,
            source="telegram",
        )
        self._pending.pop(target_id, None)

    def _resolve_notification_id(self, explicit: str | None) -> str | None:
        """Resolve a bare-verb reply against the current pending set.

        With exactly one pending notification, match it. With more than
        one, return ``None`` — the caller logs a disambiguation
        message. With zero pending and no explicit id, also ``None``.
        """
        if explicit is not None:
            return explicit
        if len(self._pending) == 1:
            (the_id,) = self._pending
            return the_id
        if len(self._pending) > 1:
            self._log.warning(
                "telegram.reply.ambiguous",
                pending=list(self._pending.keys()),
            )
            return None
        return None

    async def _persist_reply(
        self,
        *,
        notification_id: str,
        verb: str,
        annotation: str | None,
        source: str,
    ) -> None:
        if self._root is None:
            self._log.debug(
                "telegram.reply.skipped_persist",
                reason="no root",
                notification_id=notification_id,
            )
            return
        req_dir = self._root / ".huragok" / "requests"
        req_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "notification_id": notification_id,
            "verb": verb,
            "annotation": annotation,
            "received_at": datetime.now(UTC).isoformat(),
            "source": source,
        }
        path = req_dir / f"reply-{notification_id}.yaml"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        tmp.replace(path)

        self._log.info(
            "telegram.reply.received",
            notification_id=notification_id,
            verb=verb,
            annotation=annotation,
        )

        if self._batch_id is not None:
            append_audit(
                self._root,
                self._batch_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "kind": "notification-reply",
                    "notification_id": notification_id,
                    "verb": verb,
                    "annotation": annotation,
                    "source": source,
                },
            )

    # ------------------------------------------------------------------
    # Cursor persistence.
    # ------------------------------------------------------------------

    def _cursor_path(self) -> Path | None:
        if self._root is None:
            return None
        return self._root / ".huragok" / "telegram-cursor.yaml"

    def _load_cursor(self) -> int:
        path = self._cursor_path()
        if path is None or not path.exists():
            return 0
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return 0
        cursor = data.get("cursor") if isinstance(data, dict) else None
        if isinstance(cursor, int) and cursor >= 0:
            return cursor
        return 0

    def _save_cursor(self, cursor: int) -> None:
        path = self._cursor_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cursor": cursor, "updated_at": datetime.now(UTC).isoformat()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            self._log.warning("telegram.cursor.save_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Internal exceptions.
# ---------------------------------------------------------------------------


class _AuthError(Exception):
    """Telegram returned 401/403/404 — invalid token or chat."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Telegram auth error: {status_code}")
        self.status_code = status_code
        self.body = body


class _TransientError(Exception):
    """Telegram returned 5xx or an unexpected 4xx — retry later."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Telegram transient error: {status_code}")
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Outbound message formatting.
# ---------------------------------------------------------------------------


_KIND_EMOJI: dict[str, str] = {
    "budget-threshold": "🟡",
    "blocker": "🔴",
    "error": "❌",
    "foundational-gate": "🖼️",
    "batch-complete": "✅",
    "rate-limit": "⏳",
}


def _format_outbound(notification: Notification, *, default_chat_id: str) -> dict[str, Any]:
    """Render a :class:`Notification` as the JSON body of sendMessage.

    Plain text (not MarkdownV2) to sidestep Telegram's escaping quirks;
    the cosmetic touches (emoji kind hint, reply menu, id footer) are
    enough for the operator reading on mobile without needing formatting.

    A per-notification ``metadata['chat_id']`` override wins over the
    dispatcher's default — shipped so a future batch-scoped override
    doesn't need to reshape the dispatcher.
    """
    emoji = _KIND_EMOJI.get(notification.kind, "📨")
    lines = [
        f"{emoji} Huragok — {notification.kind}",
        "",
        notification.summary,
    ]
    if notification.artifact_path:
        lines.extend(["", f"Artifact: {notification.artifact_path}"])
    if notification.reply_verbs:
        lines.extend(
            [
                "",
                "Reply: " + " | ".join(notification.reply_verbs),
            ]
        )
    lines.extend(["", f"ID: {notification.id}"])

    override = notification.metadata.get("chat_id") if notification.metadata else None
    chat_id = override if isinstance(override, str) and override else default_chat_id

    return {
        "chat_id": chat_id,
        "text": "\n".join(lines),
        "disable_notification": False,
    }
