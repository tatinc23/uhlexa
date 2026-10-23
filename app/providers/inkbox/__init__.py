"""Inkbox (inkbox.ai): real iMessage, SMS, email and phone-call channels for the family agents. Never authorization."""

from app.providers.inkbox.calls import CallSession, HandshakeRejected
from app.providers.inkbox.config import InkboxConfig
from app.providers.inkbox.models import EmailBody, InkboxEvent, InkboxPayloadError, PlacedCall
from app.providers.inkbox.service import (
    ConsentMissing,
    InkboxRefusal,
    InkboxService,
    InkboxStatus,
    NotConnected,
    RateLimited,
)
from app.providers.inkbox.webhook import InkboxSignatureError, parse, to_inbound, verify

__all__ = [
    "CallSession",
    "ConsentMissing",
    "EmailBody",
    "HandshakeRejected",
    "InkboxConfig",
    "InkboxEvent",
    "InkboxPayloadError",
    "InkboxRefusal",
    "InkboxService",
    "InkboxSignatureError",
    "InkboxStatus",
    "NotConnected",
    "PlacedCall",
    "RateLimited",
    "parse",
    "to_inbound",
    "verify",
]
