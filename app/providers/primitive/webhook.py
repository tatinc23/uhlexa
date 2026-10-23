"""Primitive webhook verification and parsing (stdlib only; no network).

Scheme (docs/research/2026-10-22-primitive-facts.md, checked against the 12 official signing vectors):
header `Primitive-Signature: t=<unix seconds>,v1=<lowercase hex>[,v1=...]`; the signed string is `"<t>.<raw body>"`;
HMAC-SHA256 keyed with the account webhook secret used as a plain UTF-8 string (NOT base64); a delivery is accepted if
any `v1` matches in constant time and `t` is at most 300 s old or 60 s in the future.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

from app.providers.primitive.models import PrimitiveEvent

SIGNATURE_HEADERS = ("primitive-signature", "x-primitive-signature")
MAX_AGE_SECONDS = 300
MAX_FUTURE_SECONDS = 60
_MAX_TIMESTAMP_DIGITS = 12


class WebhookPayloadError(ValueError):
    """The body is not a usable Primitive event."""


def _signature_header(headers: Mapping[str, str]) -> str | None:
    lowered = {key.lower(): value for key, value in headers.items()}
    return next((lowered[name] for name in SIGNATURE_HEADERS if name in lowered), None)


def _parse_header(value: str) -> tuple[str, list[str]] | None:
    """(timestamp text exactly as signed, every v1 candidate), or None when the header is unusable."""
    timestamps: list[str] = []
    candidates: list[str] = []
    for part in value.split(","):
        key, separator, item = part.strip().partition("=")
        if not separator:
            continue
        key, item = key.strip(), item.strip()
        if key == "t":
            timestamps.append(item)
        elif key == "v1" and item:
            candidates.append(item)
    if len(timestamps) != 1 or not candidates:
        return None
    timestamp = timestamps[0]
    if not (timestamp.isascii() and timestamp.isdigit()) or len(timestamp) > _MAX_TIMESTAMP_DIGITS:
        return None
    return timestamp, candidates


def verify_signature(headers: Mapping[str, str], raw_body: bytes, secret: str, *, now: float | None = None) -> bool:
    """True only for a fresh, correctly signed delivery. Never raises; every doubt is a rejection."""
    if not secret:
        return False
    header = _signature_header(headers)
    parsed = _parse_header(header) if header is not None else None
    if parsed is None:
        return False
    timestamp, candidates = parsed
    age = (time.time() if now is None else now) - int(timestamp)
    if age > MAX_AGE_SECONDS or -age > MAX_FUTURE_SECONDS:
        return False
    signed = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest().encode()
    return any(hmac.compare_digest(expected, candidate.encode("utf-8")) for candidate in candidates)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_event(raw_body: bytes) -> PrimitiveEvent:
    """Read the event id and type (and the email/thread ids when present). Call only after `verify_signature`."""
    try:
        payload = json.loads(raw_body)
    except (ValueError, RecursionError) as exc:
        raise WebhookPayloadError("webhook body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WebhookPayloadError("webhook body is not a JSON object")
    event_id, event_type = _text(payload.get("id")), _text(payload.get("event"))
    if event_id is None or event_type is None:
        raise WebhookPayloadError("webhook body has no event id or type")
    email = payload.get("email")
    email = email if isinstance(email, dict) else {}
    return PrimitiveEvent(event_id, event_type, _text(email.get("thread_id")), _text(email.get("id")), payload)
