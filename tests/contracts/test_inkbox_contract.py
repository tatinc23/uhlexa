"""Contract tests for the Inkbox channel adapter (plan Task 15; spec section 21.3 and 21.6).

All offline: the SDK is faked at the `client_factory` boundary. What is pinned down:

* webhook signature verification (own HMAC, 300 s window both ways, `whsec_` optional, case-insensitive headers) against
  a locally computed vector (docs/research/2026-10-22-inkbox-facts.md section 2: Inkbox publishes no vector);
* webhook envelope parsing and the inbound-turn mapping for iMessage, SMS and email (inbound only);
* the exact SDK calls for every send, including `place_call`, always with an idempotency key where the SDK has one;
* typed refusals (not connected, consent missing, rate limited) versus outages (`ProviderUnavailable`);
* the call WebSocket protocol as a pure object, with no framework;
* no secret ever shows up in a status, repr or exception text.
"""

import hashlib
import hmac
import json
from typing import Any

import pytest

from app.channels.types import ChannelReply, InboundTurn
from app.errors import ProviderUnavailable
from app.providers.inkbox import (
    CallSession,
    ConsentMissing,
    HandshakeRejected,
    InkboxConfig,
    InkboxEvent,
    InkboxService,
    InkboxSignatureError,
    NotConnected,
    RateLimited,
    parse,
    to_inbound,
    verify,
)

KEY = "test-signing-key-not-real"
REQUEST_ID = "11111111-2222-4333-8444-555555555555"
TS = 1764875501
VECTOR_BODY = b'{"id":"evt_test","event_type":"text.received","data":{}}'
# Locally computed vector from research section 2 (Inkbox publishes none).
VECTOR_SIG = "sha256=3eaa19b490af8a45bb010d12a78c68987f478619dc180aba3236e6ef74f767d1"

API_KEY_SENTINEL = "ApiKey_SENTINEL_api_key_value_123456"
SIGNING_SENTINEL = "whsec_SENTINEL_signing_value_654321"
BEARER_SENTINEL = "SENTINEL_bearer_token_value_777"


def sign(raw: bytes, key: str = KEY, *, request_id: str = REQUEST_ID, ts: int = TS) -> dict[str, str]:
    mac = hmac.new(key.removeprefix("whsec_").encode(), f"{request_id}.{ts}.".encode() + raw, hashlib.sha256)
    return {
        "X-Inkbox-Request-ID": request_id,
        "X-Inkbox-Timestamp": str(ts),
        "X-Inkbox-Signature": "sha256=" + mac.hexdigest(),
    }


# --- webhook signature -------------------------------------------------------------------------------------------


def test_signature_vector() -> None:
    headers = {
        "X-Inkbox-Request-ID": REQUEST_ID,
        "X-Inkbox-Timestamp": str(TS),
        "X-Inkbox-Signature": VECTOR_SIG,
    }
    verify(headers, VECTOR_BODY, KEY, now=TS)
    verify(headers, VECTOR_BODY, "whsec_" + KEY, now=TS)  # identical with the optional prefix


def test_headers_are_case_insensitive() -> None:
    headers = {k.lower(): v for k, v in sign(VECTOR_BODY).items()}
    verify(headers, VECTOR_BODY, KEY, now=TS)


def test_tampered_body_and_wrong_key_rejected() -> None:
    headers = sign(VECTOR_BODY)
    with pytest.raises(InkboxSignatureError) as bad_body:
        verify(headers, VECTOR_BODY + b" ", KEY, now=TS)
    assert bad_body.value.code == "invalid_signature"
    with pytest.raises(InkboxSignatureError) as bad_key:
        verify(headers, VECTOR_BODY, "another-key", now=TS)
    assert bad_key.value.code == "invalid_signature"


@pytest.mark.parametrize("skew", [301, -301, 3600])
def test_stale_or_future_timestamp_rejected(skew: int) -> None:
    with pytest.raises(InkboxSignatureError) as exc:
        verify(sign(VECTOR_BODY), VECTOR_BODY, KEY, now=TS + skew)
    assert exc.value.code == "stale_timestamp"


def test_timestamp_window_is_inclusive_300() -> None:
    verify(sign(VECTOR_BODY), VECTOR_BODY, KEY, now=TS + 300)
    verify(sign(VECTOR_BODY), VECTOR_BODY, KEY, now=TS - 300)


def test_missing_headers_rejected_when_key_present() -> None:
    for drop in ("X-Inkbox-Request-ID", "X-Inkbox-Timestamp", "X-Inkbox-Signature"):
        headers = sign(VECTOR_BODY)
        del headers[drop]
        with pytest.raises(InkboxSignatureError) as exc:
            verify(headers, VECTOR_BODY, KEY, now=TS)
        assert exc.value.code == "missing_signature"


def test_unsigned_or_malformed_signature_rejected() -> None:
    with pytest.raises(InkboxSignatureError) as exc:
        verify({}, VECTOR_BODY, KEY, now=TS)
    assert exc.value.code == "missing_signature"
    headers = sign(VECTOR_BODY)
    headers["X-Inkbox-Signature"] = headers["X-Inkbox-Signature"].removeprefix("sha256=")
    with pytest.raises(InkboxSignatureError) as bare:
        verify(headers, VECTOR_BODY, KEY, now=TS)
    assert bare.value.code == "invalid_signature"
    headers["X-Inkbox-Timestamp"] = "not-a-number"
    with pytest.raises(InkboxSignatureError) as bad_ts:
        verify(headers, VECTOR_BODY, KEY, now=TS)
    assert bad_ts.value.code == "invalid_timestamp"


def test_empty_signing_key_fails_closed() -> None:
    """No key means Inkbox sends unsigned traffic (research section 2): that must never be accepted."""
    with pytest.raises(InkboxSignatureError) as exc:
        verify(sign(VECTOR_BODY, key="x"), VECTOR_BODY, "", now=TS)
    assert exc.value.code == "no_signing_key"


def test_signature_error_text_has_no_key_or_signature() -> None:
    with pytest.raises(InkboxSignatureError) as exc:
        verify(sign(VECTOR_BODY), VECTOR_BODY, SIGNING_SENTINEL, now=TS)
    text = f"{exc.value} {exc.value!r}"
    assert SIGNING_SENTINEL not in text
    assert "sha256=" not in text


# --- parsing and inbound mapping ---------------------------------------------------------------------------------


def envelope(event_type: str, data: dict[str, Any], event_id: str = "evt_1") -> bytes:
    return json.dumps(
        {"id": event_id, "event_type": event_type, "timestamp": "2026-10-22T10:00:00.000Z", "data": data}
    ).encode()


def test_parse_envelope() -> None:
    event = parse(envelope("imessage.received", {"message": {}}, "evt_42"))
    assert event == InkboxEvent(
        id="evt_42", event_type="imessage.received", timestamp="2026-10-22T10:00:00.000Z", data={"message": {}}
    )


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"event_type":"x","data":{}}', b'{"id":"a","data":{}}'])
def test_parse_rejects_malformed(raw: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse(raw)


IMESSAGE = {
    "message": {
        "id": "m-1",
        "conversation_id": "c-1",
        "direction": "inbound",
        "remote_number": "+15551230001",
        "content": "Ask Uhlexa to add milk and eggs",
        "service": "imessage",
        "is_group": False,
    }
}


def test_iMessage_webhook_to_inbound_turn() -> None:
    turn = to_inbound(parse(envelope("imessage.received", IMESSAGE, "evt_im")), "parker-alex")
    assert turn == InboundTurn(
        channel="inkbox:imessage",
        provider_event_id="evt_im",
        agent_ref="parker-alex",
        sender_ref="+15551230001",
        text="Ask Uhlexa to add milk and eggs",
        held_back=False,
        reply_ctx={"handle": "parker-alex", "conversation_id": "c-1", "message_id": "m-1", "service": "imessage"},
    )


def test_sms_webhook_to_inbound_turn() -> None:
    data = {
        "text_message": {
            "id": "t-1",
            "direction": "inbound",
            "remote_phone_number": "+15551230002",
            "local_phone_number": "+15559990000",
            "text": "what is on Sunday groceries?",
            "type": "sms",
            "conversation_id": "c-2",
        }
    }
    turn = to_inbound(parse(envelope("text.received", data, "evt_sms")), "parker-katie")
    assert turn is not None
    assert turn.channel == "inkbox:sms"
    assert turn.sender_ref == "+15551230002"
    assert turn.text == "what is on Sunday groceries?"
    assert turn.agent_ref == "parker-katie"
    assert turn.provider_event_id == "evt_sms"
    assert turn.reply_ctx["conversation_id"] == "c-2"


EMAIL = {
    "message": {
        "id": "uuid-1",
        "message_id": "<rfc@inkboxmail.com>",
        "thread_id": "th-1",
        "from_address": "katie@example.com",
        "subject": "Groceries",
        "body": "what is left on the list?",
        "body_truncated": False,
        "direction": "inbound",
    }
}


def test_email_webhook_to_inbound_turn() -> None:
    turn = to_inbound(parse(envelope("message.received", EMAIL, "evt_mail")), "parker-katie")
    assert turn is not None
    assert turn.channel == "inkbox:email"
    assert turn.sender_ref == "katie@example.com"
    assert turn.text == "what is left on the list?"
    assert turn.reply_ctx == {
        "handle": "parker-katie",
        "message_id": "uuid-1",
        "rfc_message_id": "<rfc@inkboxmail.com>",
        "thread_id": "th-1",
        "subject": "Groceries",
        "body_truncated": False,
    }


def test_truncated_email_body_is_flagged_for_get_message() -> None:
    data = {"message": {**EMAIL["message"], "body_truncated": True}}
    turn = to_inbound(parse(envelope("message.received", data)), "h")
    assert turn is not None
    assert turn.reply_ctx["body_truncated"] is True


def test_outbound_email_not_turned_into_inbound() -> None:
    data = {"message": {**EMAIL["message"], "direction": "outbound"}}
    assert to_inbound(parse(envelope("message.received", data)), "h") is None


def test_outbound_imessage_and_sms_not_turned_into_inbound() -> None:
    out_im = {"message": {**IMESSAGE["message"], "direction": "outbound"}}
    assert to_inbound(parse(envelope("imessage.received", out_im)), "h") is None
    out_sms = {"text_message": {"direction": "outbound", "remote_phone_number": "+1", "text": "x"}}
    assert to_inbound(parse(envelope("text.received", out_sms)), "h") is None


@pytest.mark.parametrize("event_type", ["imessage.delivery_failed", "call.ended", "a2a.task.created", "text.sent"])
def test_other_event_types_are_not_inbound_turns(event_type: str) -> None:
    assert to_inbound(parse(envelope(event_type, IMESSAGE)), "h") is None


def test_empty_text_and_missing_sender_are_not_turns() -> None:
    blank = {"message": {**IMESSAGE["message"], "content": "   "}}
    assert to_inbound(parse(envelope("imessage.received", blank)), "h") is None
    no_sender = {"message": {k: v for k, v in IMESSAGE["message"].items() if k != "remote_number"}}
    assert to_inbound(parse(envelope("imessage.received", no_sender)), "h") is None


# --- service: fake SDK at the client_factory boundary --------------------------------------------------------------


class FakeApiError(Exception):
    """Duck-types `inkbox.exceptions.InkboxAPIError` (status_code, detail); the service must not import the SDK."""

    def __init__(self, status_code: int, detail: Any = "") -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class FakeIdentity:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], fail: Exception | None) -> None:
        self._log = log
        self._fail = fail
        self.calls: list[Any] = []

    def _do(self, name: str, kwargs: dict[str, Any], result: Any) -> Any:
        self._log.append((name, kwargs))
        if self._fail is not None:
            raise self._fail
        return result

    def send_imessage(self, **kw: Any) -> Any:
        return self._do("send_imessage", kw, type("R", (), {"id": "im-1"})())

    def send_text(self, **kw: Any) -> Any:
        return self._do("send_text", kw, type("R", (), {"id": "tx-1"})())

    def send_email(self, **kw: Any) -> Any:
        return self._do("send_email", kw, type("R", (), {"id": "em-1"})())

    def get_message(self, message_id: str) -> Any:
        body = type("D", (), {"id": message_id, "subject": "Groceries", "from_address": "j@example.com"})()
        body.body_text = "the full body"
        return self._do("get_message", {"message_id": message_id}, body)

    def place_call(self, **kw: Any) -> Any:
        return self._do("place_call", kw, type("C", (), {"id": "call-9", "status": "ringing"})())

    def list_calls(self, **kw: Any) -> Any:
        known = type("Call", (), {"id": "call-1", "remote_phone_number": "+15551230001"})()
        other = type("Call", (), {"id": "call-2", "remote_phone_number": "+15551230009"})()
        return self._do("list_calls", kw, [other, known])


class FakeInkbox:
    def __init__(self, fail: Exception | None = None) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.handles: list[str] = []
        self._fail = fail

    def get_identity(self, handle: str) -> FakeIdentity:
        self.handles.append(handle)
        return FakeIdentity(self.log, self._fail)


def make_service(
    fail: Exception | None = None, **overrides: Any
) -> tuple[InkboxService, FakeInkbox, list[InkboxConfig]]:
    base = {
        "enabled": True,
        "api_key": API_KEY_SENTINEL,
        "signing_keys": {"parker-alex": SIGNING_SENTINEL},
        "webhook_auth_token": BEARER_SENTINEL,
        "imessage": True,
        "sms": True,
        "email": True,
        "calls": True,
    }
    config = InkboxConfig(**{**base, **overrides})
    fake = FakeInkbox(fail)
    seen: list[InkboxConfig] = []

    def factory(cfg: InkboxConfig) -> FakeInkbox:
        seen.append(cfg)
        return fake

    return InkboxService(config, client_factory=factory), fake, seen


def test_send_request_shapes() -> None:
    service, fake, _ = make_service()
    assert service.send_imessage("parker-alex", to="+15551230001", text="hi", idempotency_key="k1") == "im-1"
    assert service.send_imessage("parker-alex", conversation_id="c-1", text="again", idempotency_key="k2") == "im-1"
    assert service.send_text("parker-alex", to="+15551230002", text="sms", idempotency_key="k3") == "tx-1"
    assert (
        service.send_email(
            "parker-katie",
            to=["katie@example.com"],
            subject="Re: Groceries",
            body_text="done",
            in_reply_to_message_id="uuid-1",
            idempotency_key="k4",
        )
        == "em-1"
    )
    assert fake.handles == ["parker-alex", "parker-alex", "parker-alex", "parker-katie"]
    assert fake.log == [
        (
            "send_imessage",
            {"to": "+15551230001", "conversation_id": None, "text": "hi", "idempotency_key": "k1"},
        ),
        (
            "send_imessage",
            {"to": None, "conversation_id": "c-1", "text": "again", "idempotency_key": "k2"},
        ),
        (
            "send_text",
            {"to": "+15551230002", "conversation_id": None, "text": "sms", "idempotency_key": "k3"},
        ),
        (
            "send_email",
            {
                "to": ["katie@example.com"],
                "subject": "Re: Groceries",
                "body_text": "done",
                "in_reply_to_message_id": "uuid-1",
                "idempotency_key": "k4",
            },
        ),
    ]


def test_send_requires_an_idempotency_key_and_one_target() -> None:
    service, fake, _ = make_service()
    with pytest.raises(ValueError, match="idempotency_key"):
        service.send_imessage("parker-alex", to="+15551230001", text="hi", idempotency_key="")
    with pytest.raises(ValueError, match="exactly one"):
        service.send_imessage("parker-alex", text="hi", idempotency_key="k")
    with pytest.raises(ValueError, match="exactly one"):
        service.send_text("parker-alex", to="+1", conversation_id="c", text="hi", idempotency_key="k")
    assert fake.log == []


def test_sms_text_longer_than_1600_refused_before_sending() -> None:
    service, fake, _ = make_service()
    with pytest.raises(ValueError, match="1600"):
        service.send_text("parker-alex", to="+15551230002", text="x" * 1601, idempotency_key="k")
    assert fake.log == []


def test_get_message_returns_full_body() -> None:
    service, fake, _ = make_service()
    detail = service.get_message("parker-katie", "uuid-1")
    assert (detail.message_id, detail.subject, detail.from_address, detail.body_text) == (
        "uuid-1",
        "Groceries",
        "j@example.com",
        "the full body",
    )
    assert fake.log == [("get_message", {"message_id": "uuid-1"})]


def test_call_remote_number_matches_call_id_via_list_calls() -> None:
    service, fake, _ = make_service()
    assert service.call_remote_number("parker-alex", "call-1") == "+15551230001"
    assert service.call_remote_number("parker-alex", "no-such-call") is None
    assert fake.log[0][0] == "list_calls"


def test_place_call_request_shape() -> None:
    service, fake, _ = make_service()
    placed = service.place_call("parker-alex", "+15551230001", "wss://uhlexa.example.com/ws/phone/parker-alex")
    assert (placed.call_id, placed.status) == ("call-9", "ringing")
    assert fake.handles == ["parker-alex"]
    assert fake.log == [
        (
            "place_call",
            {
                "to_number": "+15551230001",
                "client_websocket_url": "wss://uhlexa.example.com/ws/phone/parker-alex",
                "mode": "client_websocket",
            },
        )
    ]


@pytest.mark.parametrize(
    ("number", "url"),
    [
        ("5551230001", "wss://uhlexa.example.com/ws"),  # not E.164
        ("+15551230001", "ws://uhlexa.example.com/ws"),  # not TLS
        ("+15551230001", "https://uhlexa.example.com/ws"),  # not a websocket URL
        ("+15551230001", ""),
    ],
)
def test_place_call_refuses_bad_number_or_url_before_calling(number: str, url: str) -> None:
    service, fake, _ = make_service()
    with pytest.raises(ValueError):  # noqa: PT011
        service.place_call("parker-alex", number, url)
    assert fake.log == []


def test_place_call_refused_when_calls_disabled() -> None:
    service, fake, seen = make_service(calls=False)
    with pytest.raises(ProviderUnavailable) as exc:
        service.place_call("parker-alex", "+15551230001", "wss://uhlexa.example.com/ws/phone/parker-alex")
    assert exc.value.provider == "inkbox"
    assert "calls" in exc.value.reason
    assert fake.log == []
    assert seen == []  # not even a client was built


def test_disabled_channel_and_disabled_provider_refuse_sends() -> None:
    service, fake, _ = make_service(sms=False)
    with pytest.raises(ProviderUnavailable, match="sms"):
        service.send_text("parker-alex", to="+1", text="x", idempotency_key="k")
    off, _, _ = make_service(enabled=False)
    with pytest.raises(ProviderUnavailable):
        off.send_imessage("parker-alex", to="+1", text="x", idempotency_key="k")
    missing_key, _, _ = make_service(api_key="")
    with pytest.raises(ProviderUnavailable, match="not set"):
        missing_key.send_email("h", to=["a@b.c"], subject="s", body_text="b", idempotency_key="k")
    assert fake.log == []


def test_not_connected_is_typed_not_outage() -> None:
    cases = [(404, "not found"), (409, {"code": "imessage_connection_expired"}), (422, "recipient_not_e164")]
    for status, detail in cases:
        service, _, _ = make_service(FakeApiError(status, detail))
        with pytest.raises(NotConnected) as exc:
            service.send_imessage("parker-alex", to="+15551230001", text="hi", idempotency_key="k")
        assert isinstance(exc.value, ProviderUnavailable)  # callers that fall back on it stay safe
        assert exc.value.provider_answered is True  # the provider replied, so the badge must not flip
        assert service.status().state == "ok"


def test_sms_consent_missing_is_typed() -> None:
    for detail in ("recipient_not_opted_in", {"code": "recipient_opted_out"}):
        service, _, _ = make_service(FakeApiError(403, detail))
        with pytest.raises(ConsentMissing) as exc:
            service.send_text("parker-alex", to="+15551230002", text="hi", idempotency_key="k")
        assert exc.value.provider_answered is True


def test_429_is_rate_limited_and_other_errors_are_outages() -> None:
    service, _, _ = make_service(FakeApiError(429, "slow down"))
    with pytest.raises(RateLimited) as limited:
        service.send_text("parker-alex", to="+1", text="hi", idempotency_key="k")
    assert limited.value.provider_answered is True

    for failure in (FakeApiError(500, "boom"), FakeApiError(403, "forbidden"), ConnectionError("down"), TimeoutError()):
        service, _, _ = make_service(failure)
        with pytest.raises(ProviderUnavailable) as exc:
            service.send_email("h", to=["a@b.c"], subject="s", body_text="b", idempotency_key="k")
        assert not isinstance(exc.value, (NotConnected, ConsentMissing, RateLimited))
        assert service.status().state == "unavailable"


def test_outage_clears_on_next_success() -> None:
    service, fake, _ = make_service(FakeApiError(500, "boom"))
    with pytest.raises(ProviderUnavailable):
        service.send_text("h", to="+1", text="x", idempotency_key="k")
    assert service.status().state == "unavailable"
    fake._fail = None
    service.send_text("h", to="+1", text="x", idempotency_key="k2")
    assert service.status().state == "ok"


def test_sdk_error_detail_never_reaches_exception_text() -> None:
    leak = FakeApiError(500, f"upstream echoed {API_KEY_SENTINEL} and {SIGNING_SENTINEL}")
    service, _, _ = make_service(leak)
    with pytest.raises(ProviderUnavailable) as exc:
        service.send_text("h", to="+1", text="x", idempotency_key="k")
    text = f"{exc.value} {exc.value!r} {exc.value.reason} {exc.value.body()}"
    assert API_KEY_SENTINEL not in text
    assert SIGNING_SENTINEL not in text
    assert API_KEY_SENTINEL not in service.status().detail


def test_default_factory_imports_sdk_lazily_and_builds_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    captured: dict[str, Any] = {}

    class StubInkbox:
        def __init__(self, api_key: str, *, base_url: str) -> None:
            captured.update(api_key=api_key, base_url=base_url)

        def get_identity(self, handle: str) -> FakeIdentity:
            return FakeIdentity([], None)

    monkeypatch.setitem(sys.modules, "inkbox", type(sys)("inkbox"))
    sys.modules["inkbox"].Inkbox = StubInkbox  # type: ignore[attr-defined]
    service = InkboxService(InkboxConfig(enabled=True, api_key="k-1", email=True))
    service.send_email("h", to=["a@b.c"], subject="s", body_text="b", idempotency_key="k")
    assert captured == {"api_key": "k-1", "base_url": "https://inkbox.ai"}


# --- status ----------------------------------------------------------------------------------------------------


def test_status_per_channel_flags() -> None:
    service, _, _ = make_service(sms=False, calls=False)
    status = service.status()
    assert status.state == "ok"
    assert status.channels == {"imessage": "ok", "sms": "disabled", "email": "ok", "calls": "disabled"}
    assert status.as_dict()["channels"]["sms"] == "disabled"


def test_status_disabled_misconfigured_ok() -> None:
    assert make_service(enabled=False)[0].status().state == "disabled"
    assert make_service(enabled=False)[0].status().channels == {
        "imessage": "disabled",
        "sms": "disabled",
        "email": "disabled",
        "calls": "disabled",
    }
    missing = make_service(api_key="  ")[0].status()
    assert missing.state == "misconfigured"
    assert "INKBOX_API_KEY" in missing.detail
    assert make_service()[0].status().state == "ok"


def test_status_misconfigured_for_plain_http_base_url() -> None:
    assert make_service(base_url="http://inkbox.example.com")[0].status().state == "misconfigured"
    assert make_service(base_url="http://localhost:8080")[0].status().state == "ok"


def test_status_calls_need_a_signing_key() -> None:
    status = make_service(signing_keys={})[0].status()
    assert status.channels["calls"] == "misconfigured"
    assert status.channels["imessage"] == "ok"


def test_secrets_never_in_status() -> None:
    service, _, _ = make_service()
    config = service._config  # noqa: SLF001
    for rendered in (repr(config), str(config), str(service.status()), json.dumps(service.status().as_dict())):
        for secret in (API_KEY_SENTINEL, SIGNING_SENTINEL, BEARER_SENTINEL):
            assert secret not in rendered


# --- call protocol --------------------------------------------------------------------------------------------


def call_context(call_id: str = "call-1") -> str:
    return json.dumps({"call_id": call_id, "phone_number": "+15551230001", "direction": "inbound", "contacts": []})


def signed_call_headers(context: str, key: str = KEY, ts: int = TS) -> dict[str, str]:
    return {**sign(context.encode(), key, ts=ts), "X-Call-Context": context}


def make_session(brain: Any, now: float = TS) -> CallSession:
    return CallSession("parker-alex", KEY, brain, clock=lambda: now)


def accepted_session(brain: Any) -> CallSession:
    session = make_session(brain)
    session.accept(signed_call_headers(call_context()))
    return session


def test_handshake_signs_x_call_context_value() -> None:
    context = call_context()
    session = make_session(lambda text: "ok")
    response = session.accept(signed_call_headers(context))
    assert response == {"X-Use-Inkbox-Speech-To-Text": "true", "X-Use-Inkbox-Text-To-Speech": "true"}
    assert session.call_id == "call-1"


def test_handshake_rejects_signature_over_different_context() -> None:
    headers = signed_call_headers(call_context("call-1"))
    headers["X-Call-Context"] = call_context("call-OTHER")  # signed one value, sent another
    with pytest.raises(HandshakeRejected) as exc:
        make_session(lambda t: "x").accept(headers)
    assert exc.value.code == "invalid_signature"
    assert exc.value.close_code == 1008


@pytest.mark.parametrize("break_it", ["wrong_key", "stale", "unsigned", "no_context", "oversize", "no_key", "bad_json"])
def test_handshake_rejections_close_1008(break_it: str) -> None:
    context = call_context()
    headers = signed_call_headers(context)
    key = KEY
    now = TS
    if break_it == "wrong_key":
        headers = signed_call_headers(context, key="someone-elses-key")
    elif break_it == "stale":
        now = TS + 1000
    elif break_it == "unsigned":
        headers = {"X-Call-Context": context}
    elif break_it == "no_context":
        del headers["X-Call-Context"]
    elif break_it == "oversize":
        big = json.dumps({"call_id": "c", "contacts": ["x" * 9000]})
        headers = signed_call_headers(big)
    elif break_it == "no_key":
        key = ""
    elif break_it == "bad_json":
        headers = signed_call_headers("{not json")
    session = CallSession("parker-alex", key, lambda t: "x", clock=lambda: now)
    with pytest.raises(HandshakeRejected) as exc:
        session.accept(headers)
    assert exc.value.close_code == 1008


def test_events_before_accept_are_ignored() -> None:
    session = make_session(lambda t: "should not run")
    assert session.on_event({"event": "transcript", "text": "hi", "is_final": True, "turn_id": "t1"}) == []


def final(text: str, turn_id: str = "turn_01") -> dict[str, Any]:
    return {"event": "transcript", "text": text, "is_final": True, "turn_id": turn_id}


def test_call_final_transcript_streams_text_then_done() -> None:
    heard: list[str] = []

    def brain(text: str) -> list[str]:
        heard.append(text)
        return ["Milk, ", "eggs ", "and bread."]

    session = accepted_session(brain)
    out = session.on_event(final("what is left on the list"))
    assert heard == ["what is left on the list"]
    assert out == [
        {"event": "text", "delta": "Milk, ", "done": False, "turn_id": "turn_01"},
        {"event": "text", "delta": "eggs ", "done": False, "turn_id": "turn_01"},
        {"event": "text", "delta": "and bread.", "done": False, "turn_id": "turn_01"},
        {"event": "text", "done": True, "turn_id": "turn_01"},
    ]


def test_brain_may_return_a_plain_string() -> None:
    out = accepted_session(lambda text: "Milk and eggs.").on_event(final("list?"))
    assert out == [
        {"event": "text", "delta": "Milk and eggs.", "done": False, "turn_id": "turn_01"},
        {"event": "text", "done": True, "turn_id": "turn_01"},
    ]


def test_interim_transcript_ignored() -> None:
    calls: list[str] = []
    session = accepted_session(lambda text: calls.append(text) or "x")
    interim = {"event": "transcript", "text": "what is", "is_final": False, "turn_id": "turn_01"}
    assert session.on_event(interim) == []
    assert session.on_event({**final("  "), "text": "   "}) == []  # blank final: nothing to answer
    assert calls == []


def test_barge_in_cancels_and_sends_clear() -> None:
    closed: list[bool] = []

    def brain(text: str) -> Any:
        def gen() -> Any:
            try:
                yield "one "
                yield "two "
                yield "three"
            finally:
                closed.append(True)

        return gen()

    session = accepted_session(brain)
    stream = session.stream_event(final("read me the list"))
    assert next(stream) == {"event": "text", "delta": "one ", "done": False, "turn_id": "turn_01"}
    barge = {"event": "barge_in", "trigger": "interim_transcript", "text": "stop", "tts_interrupted": True}
    assert session.on_event({**barge, "turn_id": "turn_02"}) == [{"event": "clear"}]
    assert list(stream) == []  # generation stopped: no more deltas and no `done`
    assert closed == [True]  # the brain's generator was closed, not abandoned


def test_barge_in_with_nothing_in_flight_still_clears() -> None:
    session = accepted_session(lambda t: "x")
    assert session.on_event({"event": "barge_in", "trigger": "cancel_turn", "turn_id": "t9"}) == [{"event": "clear"}]


def test_next_turn_after_barge_in_answers_normally() -> None:
    session = accepted_session(lambda text: f"echo {text}")
    session.on_event({"event": "barge_in", "turn_id": "turn_02"})
    out = session.on_event(final("again", "turn_02"))
    assert out[0]["delta"] == "echo again"
    assert out[-1] == {"event": "text", "done": True, "turn_id": "turn_02"}


def test_unknown_event_ignored() -> None:
    session = accepted_session(lambda t: "x")
    assert session.on_event({"event": "mystery", "foo": 1}) == []
    assert session.on_event({"no_event_key": True}) == []
    assert session.on_event({"event": "start", "stream_id": "abc", "start": {}}) == []


def test_stop_ends_session_and_later_transcripts_are_ignored() -> None:
    session = accepted_session(lambda t: "x")
    assert session.on_event({"event": "stop", "reason": "remote"}) == []
    assert session.ended is True
    assert session.on_event(final("hello?")) == []


def test_brain_failure_speaks_fixed_apology_without_leaking() -> None:
    def brain(text: str) -> str:
        raise RuntimeError(f"db password is {API_KEY_SENTINEL}")

    out = accepted_session(brain).on_event(final("hi"))
    assert out[-1] == {"event": "text", "done": True, "turn_id": "turn_01"}
    assert API_KEY_SENTINEL not in json.dumps(out)
    assert any(e.get("delta") for e in out)


def test_hangup_event_shape() -> None:
    assert accepted_session(lambda t: "x").hangup() == {"event": "stop", "reason": "agent_hang_up"}


# --- shared channel types --------------------------------------------------------------------------------------


def test_channel_reply_defaults() -> None:
    assert ChannelReply("hi").approval_hint is None
    turn = InboundTurn("inkbox:call", "e1", "h", "+1", "hello")
    assert turn.held_back is False
    assert turn.reply_ctx == {}


def test_greeting_is_spoken_once_on_start_and_never_without_one() -> None:
    session = accepted_session(lambda t: "x")
    session.greeting = "Hi, this is Uhlexa, an AI assistant. This call is transcribed. How can I help?"
    first = session.on_event({"event": "start", "stream_id": "abc", "start": {}})
    assert [e.get("delta") for e in first] == [session.greeting, None]
    assert first[-1]["done"] is True
    assert session.on_event({"event": "start", "stream_id": "abc", "start": {}}) == []  # once only
    assert session.on_event(final("hello", "t1"))[0]["delta"] == "x"  # the greeting does not disturb a turn


def test_call_greeting_names_the_agent_says_ai_and_says_transcribed() -> None:
    from app.api.channels import call_greeting

    text = call_greeting("Jordy")
    assert "Jordy" in text and "AI assistant" in text and "transcribed" in text
