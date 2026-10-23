"""Helpers for the channel tests: Inkbox bindings, signed envelopes, and fakes for the gateway and the Inkbox SDK.

Nothing here touches the network."""

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.models import BindingStatus, ProviderBinding, ProviderName
from app.providers.keiki import GatewayReply

SIGNING_KEY = "test-signing-key-not-real"
ALEX_HANDLE, JORDAN_HANDLE, MAX_HANDLE = "parker-alex", "parker-katie", "parker-max"
ALEX_NUMBER, JORDAN_NUMBER, MAX_NUMBER = "+15551230001", "+15551230002", "+15551230003"
STRANGER_NUMBER = "+15559998888"


def bind(
    db: Session,
    principal: Principal,
    binding_type: str,
    external_id: str,
    *,
    status: BindingStatus = BindingStatus.ACTIVE,
) -> ProviderBinding:
    row = ProviderBinding(
        household_id=principal.household.id,
        user_id=principal.user.id,
        provider=ProviderName.INKBOX,
        binding_type=binding_type,
        external_id=external_id,
        status=status,
        config={"consent": {"by": "test"}},
    )
    db.add(row)
    db.commit()
    return row


def bind_family(db: Session, seeded: dict[str, Principal]) -> None:
    """Alex and Katie each own an Inkbox identity and a bound number; Max (a child) is bound too and must not work."""
    for who, handle, number in (
        ("alex", ALEX_HANDLE, ALEX_NUMBER),
        ("katie", JORDAN_HANDLE, JORDAN_NUMBER),
        ("max", MAX_HANDLE, MAX_NUMBER),
    ):
        bind(db, seeded[who], "inkbox_identity", handle)
        bind(db, seeded[who], "inkbox_imessage", number)


def imessage_envelope(
    event_id: str = "evt_1",
    *,
    sender: str = ALEX_NUMBER,
    text: str = "what is on the list?",
    conversation: str = "conv_1",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "event_type": "imessage.received",
        "timestamp": "2026-10-23T12:00:00Z",
        "data": {
            "message": {
                "id": "msg_1",
                "conversation_id": conversation,
                "direction": "inbound",
                "remote_number": sender,
                "content": text,
                "service": "imessage",
            }
        },
    }


def sign(raw: bytes, key: str = SIGNING_KEY, *, request_id: str | None = None, ts: int | None = None) -> dict[str, str]:
    request_id = request_id or str(uuid.uuid4())
    stamp = str(int(time.time()) if ts is None else ts)
    mac = hmac.new(key.removeprefix("whsec_").encode(), f"{request_id}.{stamp}.".encode() + raw, hashlib.sha256)
    return {
        "X-Inkbox-Request-ID": request_id,
        "X-Inkbox-Timestamp": stamp,
        "X-Inkbox-Signature": "sha256=" + mac.hexdigest(),
    }


def call_headers(
    call_id: str = "call_1", *, phone: str = ALEX_NUMBER, direction: str = "inbound", key: str = SIGNING_KEY
) -> dict[str, str]:
    context = json.dumps({"call_id": call_id, "phone_number": phone, "direction": direction, "contacts": []})
    return {"X-Call-Context": context, **sign(context.encode(), key)}


class FakeGateway:
    """Both gateway shapes: sync `complete` and async `stream_text`."""

    def __init__(
        self,
        reply: str = "You have bread and bananas.",
        pieces: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.reply = reply
        self.pieces = pieces if pieces is not None else ["Added ", "milk."]
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(self, agent_id: str, conversation_id: str, text: str, **kw: Any) -> GatewayReply:
        self.calls.append({"agent_id": agent_id, "conversation_id": conversation_id, "text": text, **kw})
        if self.error is not None:
            raise self.error
        return GatewayReply(self.reply, conversation_id)

    async def stream_text(self, agent_id: str, conversation_id: str, text: str, **kw: Any) -> AsyncIterator[str]:
        self.calls.append({"agent_id": agent_id, "conversation_id": conversation_id, "text": text, **kw})
        for piece in self.pieces:
            yield piece
        if self.error is not None:
            raise self.error


class SyncOnlyGateway:
    """A gateway with no `stream_text`: the call route must fall back to one blocking `complete` per turn."""

    def __init__(self, reply: str = "Sure.") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def complete(self, agent_id: str, conversation_id: str, text: str, **kw: Any) -> GatewayReply:
        self.calls.append({"agent_id": agent_id, "conversation_id": conversation_id, "text": text, **kw})
        return GatewayReply(self.reply, conversation_id)


class FakeInkbox:
    def __init__(self, remote_number: str | None = ALEX_NUMBER, send_error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.remote_number = remote_number
        self.send_error = send_error

    def _send(self, kind: str, handle: str, **kw: Any) -> str:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((kind, handle, kw))
        return f"sent_{len(self.sent)}"

    def send_imessage(self, handle: str, **kw: Any) -> str:
        return self._send("imessage", handle, **kw)

    def send_text(self, handle: str, **kw: Any) -> str:
        return self._send("sms", handle, **kw)

    def send_email(self, handle: str, **kw: Any) -> str:
        return self._send("email", handle, **kw)

    def get_message(self, handle: str, message_id: str) -> Any:
        raise AssertionError("not expected in these tests")

    def call_remote_number(self, handle: str, call_id: str) -> str | None:
        return self.remote_number
