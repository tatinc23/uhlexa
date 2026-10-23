"""Typed shapes the adapter returns or consumes. Wire details: docs/research/2026-10-22-inkbox-facts.md section 2."""

from dataclasses import dataclass
from typing import Any

# Channel -> the webhook event type that carries an inbound message on it.
INBOUND_EVENT_TYPES = ("imessage.received", "text.received", "message.received")


class InkboxPayloadError(ValueError):
    """The webhook body is not a usable Inkbox envelope."""


@dataclass(frozen=True)
class InkboxEvent:
    id: str  # envelope id: stable across retries and replays, so it is the dedupe key
    event_type: str
    timestamp: str
    data: dict[str, Any]


@dataclass(frozen=True)
class EmailBody:
    message_id: str
    subject: str
    from_address: str
    body_text: str


@dataclass(frozen=True)
class PlacedCall:
    call_id: str
    status: str
