"""Notification dispatch interface (ADR-0002 D6).

B1 shipped the abstract :class:`NotificationDispatcher` and a
no-network :class:`LoggingDispatcher`. B2 adds :class:`TelegramDispatcher`
as the real production backend; the logging dispatcher remains the
fallback when ``TELEGRAM_BOT_TOKEN`` is not configured.
"""

from orchestrator.notifications.base import (
    Notification,
    NotificationDispatcher,
)
from orchestrator.notifications.logging import LoggingDispatcher
from orchestrator.notifications.telegram import (
    ParsedReply,
    TelegramDispatcher,
    normalize_verb,
    parse_reply_text,
)

__all__ = [
    "LoggingDispatcher",
    "Notification",
    "NotificationDispatcher",
    "ParsedReply",
    "TelegramDispatcher",
    "normalize_verb",
    "parse_reply_text",
]
