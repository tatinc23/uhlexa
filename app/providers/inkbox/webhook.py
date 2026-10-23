"""Inkbox webhook verification and parsing (research section 2). Own implementation: the SDK's `verify_webhook` does not
enforce the 300 s window. Pure functions; callers pass the RAW request bytes, never a re-serialised body."""

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

from app.channels.types import InboundTurn
from app.providers.inkbox.models import InkboxEvent, InkboxPayloadError

TOLERANCE_SECONDS = 300
_SIGNATURE_PREFIX = "sha256="


class InkboxSignatureError(Exception):
    """A webhook or WebSocket handshake failed verification. `code` is stable; the text never holds a key or digest."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def verify(headers: Mapping[str, str], raw: bytes, signing_key: str, *, now: float | None = None) -> None:
    """Raise `InkboxSignatureError` unless `raw` was signed by Inkbox with `signing_key`, within 300 s either way.

    An empty key is a rejection: until a key exists Inkbox sends unsigned traffic, which must never be accepted.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    key = signing_key.removeprefix("whsec_")
    if not key:
        raise InkboxSignatureError("no_signing_key")
    request_id = lowered.get("x-inkbox-request-id", "")
    timestamp = lowered.get("x-inkbox-timestamp", "")
    signature = lowered.get("x-inkbox-signature", "")
    if not (request_id and timestamp and signature):
        raise InkboxSignatureError("missing_signature")
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise InkboxSignatureError("invalid_timestamp") from None
    if not signature.startswith(_SIGNATURE_PREFIX):
        raise InkboxSignatureError("invalid_signature")
    mac = hmac.new(key.encode(), f"{request_id}.{timestamp}.".encode() + raw, hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), signature.removeprefix(_SIGNATURE_PREFIX)):
        raise InkboxSignatureError("invalid_signature")
    current = time.time() if now is None else now
    if abs(current - sent_at) > TOLERANCE_SECONDS:
        raise InkboxSignatureError("stale_timestamp")


def parse(raw: bytes) -> InkboxEvent:
    """Parse the envelope `{id, event_type, timestamp, data}`. Raises `InkboxPayloadError` (a ValueError)."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise InkboxPayloadError("body is not JSON") from None
    if not isinstance(body, dict):
        raise InkboxPayloadError("envelope must be an object")
    event_id, event_type, data = body.get("id"), body.get("event_type"), body.get("data", {})
    if not isinstance(event_id, str) or not event_id:
        raise InkboxPayloadError("envelope has no id")
    if not isinstance(event_type, str) or not event_type:
        raise InkboxPayloadError("envelope has no event_type")
    if not isinstance(data, dict):
        raise InkboxPayloadError("envelope data must be an object")
    timestamp = body.get("timestamp")
    return InkboxEvent(event_id, event_type, timestamp if isinstance(timestamp, str) else "", data)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _inbound(
    event: InkboxEvent, handle: str, channel: str, sender: str, text: str, ctx: dict[str, Any]
) -> InboundTurn | None:
    if not sender.strip() or not text.strip():
        return None
    return InboundTurn(
        channel=channel,
        provider_event_id=event.id,
        agent_ref=handle,
        sender_ref=sender,
        text=text,
        held_back=False,
        reply_ctx={"handle": handle, **ctx},
    )


def to_inbound(event: InkboxEvent, handle: str) -> InboundTurn | None:
    """Map an inbound message event to a turn. Outbound rows, other event types and empty text give None.

    `sender_ref` is whatever Inkbox reports and is NOT yet an identity: the channel layer must resolve it to a
    binding before acting. For email, `body_truncated` in `reply_ctx` tells the caller to fetch the full body.
    """
    if event.event_type == "imessage.received":
        msg = event.data.get("message")
        if not isinstance(msg, dict) or msg.get("direction", "inbound") != "inbound":
            return None
        ctx = {
            "conversation_id": _str(msg.get("conversation_id")),
            "message_id": _str(msg.get("id")),
            "service": _str(msg.get("service")),
        }
        return _inbound(event, handle, "inkbox:imessage", _str(msg.get("remote_number")), _str(msg.get("content")), ctx)
    if event.event_type == "text.received":
        msg = event.data.get("text_message")
        if not isinstance(msg, dict) or msg.get("direction", "inbound") != "inbound":
            return None
        ctx = {"conversation_id": _str(msg.get("conversation_id")), "message_id": _str(msg.get("id"))}
        return _inbound(event, handle, "inkbox:sms", _str(msg.get("remote_phone_number")), _str(msg.get("text")), ctx)
    if event.event_type == "message.received":
        msg = event.data.get("message")
        if not isinstance(msg, dict) or msg.get("direction") != "inbound":
            return None
        ctx = {
            "message_id": _str(msg.get("id")),
            "rfc_message_id": _str(msg.get("message_id")),
            "thread_id": _str(msg.get("thread_id")),
            "subject": _str(msg.get("subject")),
            "body_truncated": bool(msg.get("body_truncated", False)),
        }
        return _inbound(event, handle, "inkbox:email", _str(msg.get("from_address")), _str(msg.get("body")), ctx)
    return None
