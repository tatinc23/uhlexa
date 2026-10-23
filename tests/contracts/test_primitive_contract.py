"""Contract tests for the Primitive adapter (plan Task 10; spec sections 11 and 20).

What these pin down, all offline (httpx.MockTransport; there is no Primitive key, see BLOCKERS.md):

* webhook signature verification against the 12 official signing vectors and the documented tolerance window;
* the exact method, path, query, headers and JSON body of every capability, per the OpenAPI shapes in
  docs/research/2026-10-22-primitive-facts.md (unverified against the live API until a key exists);
* `reply_to_thread` as a composition: get_thread -> newest message / latest inbound id -> POST /emails/{id}/reply;
* envelope parsing and the error map: outages become `ProviderUnavailable` (status badge `unavailable`), answers such
  as "recipient not allowed" or "memory conflict" become typed `PrimitiveRejection`s (badge stays `ok`);
* the API key is only ever sent as `Authorization: Bearer` and never appears in a URL, a reason or a status.
"""

import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.errors import ProviderUnavailable
from app.providers import provider_statuses, reset_runtime_state
from app.providers.primitive import (
    MemoryConflict,
    NotRepliable,
    PrimitiveEvent,
    PrimitiveNotFound,
    PrimitiveRejection,
    PrimitiveService,
    RecipientNotAllowed,
    ThreadChanged,
    parse_event,
    verify_signature,
)
from app.providers.primitive.client import DEFAULT_BASE_URL
from app.providers.primitive.models import (
    PrimitiveMemory,
    PrimitiveMessage,
    PrimitiveReply,
    PrimitiveThread,
    PrimitiveThreadMessage,
)
from app.settings import Settings, get_settings

KEY = "prim_SENTINEL-api-key-5d1c"
WEBHOOK_SECRET = "SENTINEL-webhook-secret-e07b"
ENV_NAMES = [name.upper() for name in Settings.model_fields]

# --- the 12 official signing vectors ------------------------------------------------------------------------------
# Source: https://github.com/primitivedotdev/sdks/blob/main/test-fixtures/signing/vectors.json (MIT licence),
# fetched 2026-10-22 and copied verbatim. Fields: raw_body, secret, timestamp (the `t=` value), expected_v1,
# expected_valid; optional now_seconds (the verifier's clock; defaults to `timestamp`), verify_secret (verify with this
# secret instead of `secret`), signature_header (use this header instead of building `t=<ts>,v1=<expected_v1>`).
VECTORS: list[dict[str, Any]] = [
    {
        "name": "simple",
        "raw_body": '{"event":"email.received","email_id":"msg_123"}',
        "secret": "whsec_test_secret_key_123",
        "timestamp": 1734567890,
        "expected_v1": "45c9e2f95add59da39d6bdf3e617b8bfcad771a59d06bf4687d11d39af041d91",
        "expected_valid": True,
    },
    {
        "name": "minimal",
        "raw_body": "{}",
        "secret": "secret",
        "timestamp": 1000000000,
        "expected_v1": "10447a13c77e61914632c736653543f75b152b77a1573fc15a41baad63fc66a3",
        "expected_valid": True,
    },
    {
        "name": "special_chars",
        "raw_body": '{"subject":"Test with \\"quotes\\" and \\n newlines","from":"test@example.com"}',
        "secret": "whsec_another_secret!@#$%",
        "timestamp": 1700000000,
        "expected_v1": "cd71c66c33d133f60a4aa83681e6fa748cb4267745f80755be152abb85617417",
        "expected_valid": True,
    },
    {
        "name": "wrong_secret_mismatch",
        "raw_body": '{"test":"data"}',
        "secret": "secret",
        "timestamp": 1734567890,
        "verify_secret": "wrong-secret",
        "expected_v1": "f747da0a13743bf5e35669aa362d26c14d128057818097909b62279f48a55a0e",
        "expected_valid": False,
        "expected_error_code": "SIGNATURE_MISMATCH",
    },
    {
        "name": "expired_timestamp",
        "raw_body": '{"event":"email.received"}',
        "secret": "secret",
        "timestamp": 1734567000,
        "now_seconds": 1734567890,
        "expected_v1": "798b9ea6c530f7316d3b2b9fc8e90857923bcb85f9a2a80628cb59d026f14a43",
        "expected_valid": False,
        "expected_error_code": "TIMESTAMP_OUT_OF_RANGE",
    },
    {
        "name": "boundary_old_valid",
        "raw_body": '{"event":"email.received"}',
        "secret": "secret",
        "timestamp": 1734567590,
        "now_seconds": 1734567890,
        "expected_v1": "dab67b1a17f919727e08cb3715d30011e7770b41df7ea8fef7a4f85d340dd2fa",
        "expected_valid": True,
    },
    {
        "name": "boundary_old_invalid",
        "raw_body": '{"event":"email.received"}',
        "secret": "secret",
        "timestamp": 1734567589,
        "now_seconds": 1734567890,
        "expected_v1": "af94511ae93a37402b9e2af32e9254779cc5440926a4fc3b27f4c32543348024",
        "expected_valid": False,
        "expected_error_code": "TIMESTAMP_OUT_OF_RANGE",
    },
    {
        "name": "future_boundary_valid",
        "raw_body": '{"event":"email.received"}',
        "secret": "secret",
        "timestamp": 1734567950,
        "now_seconds": 1734567890,
        "expected_v1": "de4c46874e28aa0acac95ac3658fc63dd093561ec80f49baae7aa35eb93a60d7",
        "expected_valid": True,
    },
    {
        "name": "future_boundary_invalid",
        "raw_body": '{"event":"email.received"}',
        "secret": "secret",
        "timestamp": 1734567951,
        "now_seconds": 1734567890,
        "expected_v1": "bb5a05745bbef90ecd2252d16b9846d460efcdac31c45f937e00ea3ad4abfbd6",
        "expected_valid": False,
        "expected_error_code": "TIMESTAMP_OUT_OF_RANGE",
    },
    {
        "name": "multiple_v1_values_with_match",
        "raw_body": '{"event":"email.received","id":"evt_1"}',
        "secret": "secret",
        "timestamp": 1734567890,
        "now_seconds": 1734567890,
        "signature_header": "t=1734567890,v1=0000000000000000000000000000000000000000000000000000000000000000,v1=cfad7c8c0ad57539146bdfb0d41856b105dd9a79e1d8d768bb46b1c29213e081",  # noqa: E501 (verbatim vector)
        "expected_v1": "cfad7c8c0ad57539146bdfb0d41856b105dd9a79e1d8d768bb46b1c29213e081",
        "expected_valid": True,
    },
    {
        "name": "whitespace_and_legacy_fields",
        "raw_body": '{"event":"email.received"}',
        "secret": "secret",
        "timestamp": 1734567890,
        "now_seconds": 1734567890,
        "signature_header": " t=1734567890 , v0=legacy , "
        "v1=da72bb671b4a86a7e9df5eabbe2bad6144211ce123f5251525aab7c4c0c636e8 ",
        "expected_v1": "da72bb671b4a86a7e9df5eabbe2bad6144211ce123f5251525aab7c4c0c636e8",
        "expected_valid": True,
    },
    {
        "name": "sent_email_docs_worked_example",
        "raw_body": '{"id":"9f1c3a52-7d4e-4b8a-9c21-5e6f7a8b9c0d","event":"sent_email.completed"}',
        "secret": "whsec_example",
        "timestamp": 1797588005,
        "now_seconds": 1797588005,
        "expected_v1": "4f93d0ceb7f6fdd4a71f4d6beb08a5624a753a376841fd4aa93c62ecaa42c9b2",
        "expected_valid": True,
    },
]

# Source: test-fixtures/webhook/valid-email-received.json in the same repo (trimmed: raw MIME blob and auth block cut).
EMAIL_RECEIVED_EVENT: dict[str, Any] = {
    "id": "evt_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "event": "email.received",
    "version": "2025-12-14",
    "delivery": {"endpoint_id": "ep_xyz789", "attempt": 1, "attempted_at": "2025-12-14T12:00:00Z"},
    "email": {
        "id": "em_def456",
        "thread_id": "44444444-4444-4444-4444-444444444444",
        "received_at": "2025-12-14T11:59:50Z",
        "headers": {
            "message_id": "<abc123@example.com>",
            "subject": "Test Email",
            "from": "sender@example.com",
            "to": "recipient@domain.com",
            "date": "Sat, 14 Dec 2025 11:59:50 +0000",
        },
        "parsed": {"status": "complete", "body_text": "Hello World", "attachments": []},
    },
}

THREAD_ID = "11111111-1111-4111-8111-111111111111"
IN_1 = "22222222-2222-4222-8222-222222222221"
OUT_1 = "33333333-3333-4333-8333-333333333331"
IN_2 = "22222222-2222-4222-8222-222222222222"
MEMORY_KEY = "uhlexa:thread:7d1e:state"
STATE = {"summary": "Sunday grocery list for the Parker family.", "items": ["bread", "bananas"], "version": 2}
LABEL = "Alex's Uhlexa"
ALLOWED = ("mom@example.com",)  # the household address the thread fixtures write from


@pytest.fixture(autouse=True)
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer env var or .env can reach these tests; runtime availability starts clean."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    reset_runtime_state()
    yield
    reset_runtime_state()
    get_settings.cache_clear()


# --- helpers --------------------------------------------------------------------------------------------------------


def settings_for(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"enable_primitive": True, "primitive_api_key": KEY}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "meta": meta or {"total": None, "limit": 20, "cursor": None}}


def error_body(code: str, message: str = "nope", **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message, **extra}}


def message_row(message_id: str, direction: str, timestamp: str, sender: str = "mom@example.com") -> dict[str, Any]:
    return {
        "direction": direction,
        "id": message_id,
        "message_id": f"<{message_id}@mail>",
        "from": sender,
        "to": "uhlexa@agent.primitive.email",
        "subject": "Sunday groceries",
        "status": "delivered",
        "timestamp": timestamp,
        "fyi": False,
        "interaction_hint": "none",
    }


def thread_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": THREAD_ID,
        "subject": "Sunday groceries",
        "root_message_id": "<root@mail>",
        "message_count": 3,
        "first_message_at": "2026-10-20T10:00:00Z",
        "last_message_at": "2026-10-20T12:00:00Z",
        "created_at": "2026-10-20T10:00:00Z",
        "messages": [
            message_row(IN_1, "inbound", "2026-10-20T10:00:00Z"),
            message_row(OUT_1, "outbound", "2026-10-20T11:00:00Z", sender="uhlexa@agent.primitive.email"),
            message_row(IN_2, "inbound", "2026-10-20T12:00:00Z"),
        ],
        "latest_inbound_id": IN_2,
        "latest_outbound_id": OUT_1,
    }
    data.update(overrides)
    return data


def send_result(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "55555555-5555-4555-8555-555555555555",
        "status": "queued",
        "from": "uhlexa@agent.primitive.email",
        "queue_id": None,
        "accepted": ["mom@example.com"],
        "rejected": [],
        "client_idempotency_key": "k",
        "request_id": "req_1",
        "content_hash": "abc",
        "idempotent_replay": False,
    }
    data.update(overrides)
    return data


def memory_record(value: Any, version: str = "3", key: str = MEMORY_KEY) -> dict[str, Any]:
    return {
        "id": "66666666-6666-4666-8666-666666666666",
        "key": key,
        "scope": {"type": "org", "id": None},
        "value": value,
        "version": version,
        "created_at": "2026-10-20T10:00:00Z",
        "updated_at": "2026-10-20T10:00:00Z",
        "last_read_at": None,
        "read_count": "0",
        "write_count": "1",
        "expires_at": None,
        "created_by": None,
        "updated_by": None,
    }


Route = Callable[[httpx.Request], httpx.Response]


class Wire:
    """A scripted Primitive: routes keyed by (METHOD, path), each a response or a list consumed in order."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Any] = dict(routes or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(599, json=error_body("test_unrouted", f"{request.method} {request.url.path}"))
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if callable(route):
            return route(request)
        return httpx.Response(route.status_code, headers=route.headers, content=route.content)

    def service(self, **setting_overrides: Any) -> PrimitiveService:
        return PrimitiveService(settings_for(**setting_overrides), transport=httpx.MockTransport(self.handler))

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def ok(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=envelope(data))


def fail(status: int, code: str, message: str = "nope", headers: dict[str, str] | None = None, **extra: Any):
    return httpx.Response(status, json=error_body(code, message, **extra), headers=headers)


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected provider HTTP call: {request.method} {request.url}")


# === 1. webhook signature verification ==============================================================================


def sign(body: bytes, secret: str, timestamp: int) -> str:
    return hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def vector_headers(case: dict[str, Any]) -> dict[str, str]:
    header = case.get("signature_header") or f"t={case['timestamp']},v1={case['expected_v1']}"
    return {"Primitive-Signature": header}


def test_the_twelve_embedded_vectors_are_the_official_set() -> None:
    assert len(VECTORS) == 12
    assert len({case["name"] for case in VECTORS}) == 12


@pytest.mark.parametrize("case", VECTORS, ids=[case["name"] for case in VECTORS])
def test_vectors_recompute_with_stdlib_hmac(case: dict[str, Any]) -> None:
    """The documented algorithm (HMAC-SHA256 hex over "<t>.<raw body>", secret as a UTF-8 string) reproduces them."""
    assert sign(case["raw_body"].encode(), case["secret"], case["timestamp"]) == case["expected_v1"]


@pytest.mark.parametrize("case", VECTORS, ids=[case["name"] for case in VECTORS])
def test_webhook_signature_official_vectors(case: dict[str, Any]) -> None:
    secret = case.get("verify_secret", case["secret"])
    now = float(case.get("now_seconds", case["timestamp"]))
    result = verify_signature(vector_headers(case), case["raw_body"].encode(), secret, now=now)
    assert result is case["expected_valid"], case["name"]


def test_webhook_signature_valid_vector() -> None:
    simple = next(case for case in VECTORS if case["name"] == "simple")
    headers = {"Primitive-Signature": f"t={simple['timestamp']},v1={simple['expected_v1']}"}
    assert verify_signature(headers, simple["raw_body"].encode(), simple["secret"], now=float(simple["timestamp"]))


def test_webhook_signature_rejects_tampered_body() -> None:
    simple = next(case for case in VECTORS if case["name"] == "simple")
    headers = vector_headers(simple)
    tampered = simple["raw_body"].replace("msg_123", "msg_124").encode()
    assert verify_signature(headers, tampered, simple["secret"], now=float(simple["timestamp"])) is False
    assert verify_signature(headers, simple["raw_body"].encode() + b" ", simple["secret"], now=1734567890.0) is False


def test_webhook_signature_rejects_wrong_secret() -> None:
    simple = next(case for case in VECTORS if case["name"] == "simple")
    assert (
        verify_signature(vector_headers(simple), simple["raw_body"].encode(), "not-the-secret", now=1734567890.0)
        is False
    )
    assert verify_signature(vector_headers(simple), simple["raw_body"].encode(), "", now=1734567890.0) is False


def test_webhook_signature_rejects_stale_and_future_timestamps() -> None:
    body = b'{"event":"email.received"}'
    old = 1734567890 - 301
    future = 1734567890 + 61
    assert (
        verify_signature({"Primitive-Signature": f"t={old},v1={sign(body, 's', old)}"}, body, "s", now=1734567890.0)
        is False
    )
    assert (
        verify_signature(
            {"Primitive-Signature": f"t={future},v1={sign(body, 's', future)}"}, body, "s", now=1734567890.0
        )
        is False
    )
    # A correct signature does not rescue a replayed old delivery, and the unsigned timestamp cannot be swapped in.
    fresh = 1734567890
    swapped = f"t={fresh},v1={sign(body, 's', old)}"
    assert verify_signature({"Primitive-Signature": swapped}, body, "s", now=float(fresh)) is False


def test_webhook_signature_header_name_is_case_insensitive_and_has_an_alias() -> None:
    simple = next(case for case in VECTORS if case["name"] == "simple")
    value = vector_headers(simple)["Primitive-Signature"]
    body, secret, now = simple["raw_body"].encode(), simple["secret"], float(simple["timestamp"])
    assert verify_signature({"primitive-signature": value}, body, secret, now=now) is True
    assert verify_signature({"X-Primitive-Signature": value}, body, secret, now=now) is True
    assert verify_signature({"Content-Type": "application/json"}, body, secret, now=now) is False


@pytest.mark.parametrize(
    "header",
    ["", "garbage", "t=abc,v1=00", "v1=00", "t=1734567890", "t=1734567890,t=1734567890,v1=00", "t=-5,v1=00"],
)
def test_webhook_signature_malformed_headers_never_raise(header: str) -> None:
    assert verify_signature({"Primitive-Signature": header}, b"{}", "secret", now=1734567890.0) is False


def test_parse_event_reads_the_official_email_received_payload() -> None:
    raw = json.dumps(EMAIL_RECEIVED_EVENT, separators=(",", ":")).encode()
    secret, timestamp = WEBHOOK_SECRET, 1734567890
    headers = {
        "Primitive-Signature": f"t={timestamp},v1={sign(raw, secret, timestamp)}",
        "X-Webhook-Event": "email.received",
    }
    assert verify_signature(headers, raw, secret, now=float(timestamp)) is True
    event = parse_event(raw)
    assert event == PrimitiveEvent(
        id=EMAIL_RECEIVED_EVENT["id"],
        type="email.received",
        thread_id="44444444-4444-4444-4444-444444444444",
        message_id="em_def456",
        payload=EMAIL_RECEIVED_EVENT,
    )


# === 2. request shapes: one test per documented capability ==========================================================


def test_endpoints_match_docs_get_thread() -> None:
    wire = Wire({("GET", f"/v1/threads/{THREAD_ID}"): ok(thread_data())})
    thread = wire.service().get_thread(THREAD_ID)
    assert wire.calls == [("GET", f"/v1/threads/{THREAD_ID}")]
    request = wire.requests[0]
    assert request.url.host == "api.primitive.dev" and request.url.scheme == "https"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Accept"] == "application/json"
    assert thread == PrimitiveThread(
        id=THREAD_ID,
        subject="Sunday groceries",
        message_count=3,
        latest_inbound_id=IN_2,
        latest_outbound_id=OUT_1,
        messages=(
            PrimitiveThreadMessage(IN_1, "inbound", "mom@example.com", "Sunday groceries", "2026-10-20T10:00:00Z"),
            PrimitiveThreadMessage(
                OUT_1, "outbound", "uhlexa@agent.primitive.email", "Sunday groceries", "2026-10-20T11:00:00Z"
            ),
            PrimitiveThreadMessage(IN_2, "inbound", "mom@example.com", "Sunday groceries", "2026-10-20T12:00:00Z"),
        ),
    )


def test_get_thread_derives_latest_ids_when_the_server_omits_them() -> None:
    # The OpenAPI file in the SDK repo has no latest_inbound_id / latest_outbound_id; the live one does. Accept both.
    data = thread_data()
    del data["latest_inbound_id"], data["latest_outbound_id"]
    thread = Wire({("GET", f"/v1/threads/{THREAD_ID}"): ok(data)}).service().get_thread(THREAD_ID)
    assert (thread.latest_inbound_id, thread.latest_outbound_id) == (IN_2, OUT_1)


def test_get_thread_tolerates_null_fields_and_unknown_directions() -> None:
    data = thread_data(subject=None, message_count=2)
    data["messages"] = [
        {"direction": "inbound", "id": IN_1, "from": None, "subject": None, "timestamp": None},
        {"direction": "sideways", "id": "x"},
    ]
    del data["latest_inbound_id"], data["latest_outbound_id"]
    thread = Wire({("GET", f"/v1/threads/{THREAD_ID}"): ok(data)}).service().get_thread(THREAD_ID)
    assert thread.subject == "" and thread.message_count == 2
    assert thread.messages == (PrimitiveThreadMessage(IN_1, "inbound", "", "", ""),)
    assert (thread.latest_inbound_id, thread.latest_outbound_id) == (IN_1, None)


def test_ids_are_path_quoted_so_they_cannot_escape_their_segment() -> None:
    wire = Wire()
    service = wire.service()
    with pytest.raises(ProviderUnavailable):
        service.get_thread("../../account/webhook-secret")
    assert wire.requests[0].url.raw_path == b"/v1/threads/..%2F..%2Faccount%2Fwebhook-secret"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201, "a\nb"])
def test_blank_or_hostile_ids_are_refused_before_any_http_call(bad: str) -> None:
    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(no_network))
    with pytest.raises(PrimitiveRejection):
        service.get_thread(bad)
    with pytest.raises(PrimitiveRejection):
        service.get_message(bad)


def inbound_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": IN_2,
        "message_id": "<in2@mail>",
        "sender": "mom@example.com",
        "recipient": "uhlexa@agent.primitive.email",
        "from_email": "mom@example.com",
        "to_email": "uhlexa@agent.primitive.email",
        "subject": "Re: Sunday groceries",
        "body_text": "Please add milk.",
        "body_html": "<p>Please add milk.</p>",
        "thread_id": THREAD_ID,
        "received_at": "2026-10-20T12:00:00Z",
        "created_at": "2026-10-20T12:00:01Z",
    }
    row.update(overrides)
    return row


def sent_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": OUT_1,
        "from_address": "uhlexa@agent.primitive.email",
        "to_address": "mom@example.com",
        "subject": "Re: Sunday groceries",
        "body_text": "Added bread.",
        "thread_id": THREAD_ID,
        "created_at": "2026-10-20T11:00:00Z",
        "status": "delivered",
    }
    row.update(overrides)
    return row


def test_endpoints_match_docs_get_message_inbound() -> None:
    wire = Wire({("GET", f"/v1/emails/{IN_2}"): ok(inbound_row())})
    message = wire.service().get_message(IN_2)
    assert wire.calls == [("GET", f"/v1/emails/{IN_2}")]
    assert message == PrimitiveMessage(
        id=IN_2,
        direction="inbound",
        thread_id=THREAD_ID,
        sender="mom@example.com",
        subject="Re: Sunday groceries",
        body_text="Please add milk.",
        timestamp="2026-10-20T12:00:00Z",
        recipient="uhlexa@agent.primitive.email",
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"from_verified": True, "from_header_email": "Mom <MOM@example.com>"}, True),
        ({"from_verified": "true", "from_header_email": "mom@example.com"}, False),  # not a real boolean
        ({"from_verified": True}, False),  # no header From to tie the check to
        ({"from_verified": True, "from_header_email": "attacker@evil.test"}, False),  # header and envelope differ
        ({"from_verified": None, "from_header_email": "mom@example.com"}, False),
    ],
)
def test_inbound_message_is_verified_only_for_one_consistent_address(overrides: dict[str, Any], expected: bool) -> None:
    wire = Wire({("GET", f"/v1/emails/{IN_2}"): ok(inbound_row(**overrides))})
    assert wire.service().get_message(IN_2).from_verified is expected


def test_reply_to_email_answers_exactly_that_email_with_the_key() -> None:
    wire = Wire(
        {
            ("POST", f"/v1/emails/{IN_2}/reply"): ok(
                {"id": "out_9", "status": "queued", "accepted": ["mom@example.com"], "rejected": []}
            )
        }
    )
    reply = wire.service().reply_to_email(IN_2, "Done.", sender_label="Alex's Uhlexa", idempotency_key="uhlexa-evt_1")
    assert wire.calls == [("POST", f"/v1/emails/{IN_2}/reply")]
    assert reply.sent and reply.id == "out_9"


def test_endpoints_match_docs_get_message_outbound_falls_back_to_sent_emails() -> None:
    wire = Wire(
        {
            ("GET", f"/v1/emails/{OUT_1}"): fail(404, "not_found"),
            ("GET", f"/v1/sent-emails/{OUT_1}"): ok(sent_row()),
        }
    )
    service = wire.service()
    message = service.get_message(OUT_1)
    assert wire.calls == [("GET", f"/v1/emails/{OUT_1}"), ("GET", f"/v1/sent-emails/{OUT_1}")]
    assert message == PrimitiveMessage(
        id=OUT_1,
        direction="outbound",
        thread_id=THREAD_ID,
        sender="uhlexa@agent.primitive.email",
        subject="Re: Sunday groceries",
        body_text="Added bread.",
        timestamp="2026-10-20T11:00:00Z",
    )
    assert service.status.status == "ok"


def test_get_message_missing_everywhere_is_not_found_and_does_not_flip_the_badge() -> None:
    wire = Wire(
        {
            ("GET", f"/v1/emails/{IN_1}"): fail(404, "not_found"),
            ("GET", f"/v1/sent-emails/{IN_1}"): fail(404, "not_found"),
        }
    )
    service = wire.service()
    with pytest.raises(PrimitiveNotFound):
        service.get_message(IN_1)
    assert service.status.status == "ok"


def test_get_message_does_not_probe_sent_emails_after_a_non_404_error() -> None:
    wire = Wire({("GET", f"/v1/emails/{IN_1}"): fail(401, "unauthorized")})
    with pytest.raises(ProviderUnavailable):
        wire.service().get_message(IN_1)
    assert wire.calls == [("GET", f"/v1/emails/{IN_1}")]


def test_get_message_null_body_becomes_empty_text() -> None:
    wire = Wire({("GET", f"/v1/emails/{IN_2}"): ok(inbound_row(body_text=None, subject=None))})
    message = wire.service().get_message(IN_2)
    assert (message.body_text, message.subject) == ("", "")


def test_get_message_caps_an_enormous_body() -> None:
    wire = Wire({("GET", f"/v1/emails/{IN_2}"): ok(inbound_row(body_text="x" * 500_000))})
    assert len(wire.service().get_message(IN_2).body_text) <= 60_000


def test_endpoints_match_docs_search_messages() -> None:
    results = [
        {**inbound_row(), "direction": "inbound", "attachment_count": 0, "from_known_address": True},
        {
            **sent_row(),
            "id": OUT_1,
            "direction": "outbound",
            "sender": "uhlexa@agent.primitive.email",
            "received_at": "2026-10-20T11:00:00Z",
            "attachment_count": 0,
            "from_known_address": False,
        },
        {"id": "ignored", "direction": "mystery"},
    ]
    wire = Wire(
        {("GET", "/v1/emails/search"): httpx.Response(200, json=envelope(results, total=2, limit=5, cursor=None))}
    )
    found = wire.service().search_messages("milk -eggs", thread_id=THREAD_ID, limit=5)
    assert wire.calls == [("GET", "/v1/emails/search")]
    params = wire.requests[0].url.params
    assert params["q"] == "milk -eggs"
    assert params["thread_id"] == THREAD_ID
    assert params["direction"] == "any"
    assert params["limit"] == "5"
    assert params["include"] == "body_text"
    assert [m.id for m in found] == [IN_2, OUT_1]
    assert [m.direction for m in found] == ["inbound", "outbound"]
    assert found[0].body_text == "Please add milk."


def test_search_without_a_thread_omits_thread_id_and_clamps_limit() -> None:
    wire = Wire({("GET", "/v1/emails/search"): ok([])})
    service = wire.service()
    assert service.search_messages("milk", limit=500) == []
    assert service.search_messages("milk", limit=0) == []
    first, second = (request.url.params for request in wire.requests)
    assert "thread_id" not in first and first["limit"] == "100"
    assert second["limit"] == "1"


def test_search_with_a_blank_query_is_refused_before_any_http_call() -> None:
    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(no_network))
    with pytest.raises(PrimitiveRejection):
        service.search_messages("   ")


def test_endpoints_match_docs_get_memory() -> None:
    wire = Wire({("GET", "/v1/memories"): ok(memory_record(STATE, version="3"))})
    memory = wire.service().get_memory(MEMORY_KEY)
    assert wire.calls == [("GET", "/v1/memories")]
    assert wire.requests[0].url.params["key"] == MEMORY_KEY
    assert set(wire.requests[0].url.params.keys()) == {"key"}  # no scope params: the documented curl shape
    assert memory == PrimitiveMemory(MEMORY_KEY, STATE, "3")


def test_get_memory_missing_key_is_none_and_not_an_outage() -> None:
    wire = Wire({("GET", "/v1/memories"): fail(404, "not_found", "no such key")})
    service = wire.service()
    assert service.get_memory(MEMORY_KEY) is None
    assert service.status.status == "ok"


def test_get_memory_numeric_version_is_kept_as_text() -> None:
    wire = Wire({("GET", "/v1/memories"): ok(memory_record(STATE, version=7))})  # type: ignore[arg-type]
    assert wire.service().get_memory(MEMORY_KEY).version == "7"


def test_get_memory_that_is_not_a_json_object_is_refused_not_guessed() -> None:
    wire = Wire({("GET", "/v1/memories"): ok(memory_record(["not", "an", "object"]))})
    with pytest.raises(ProviderUnavailable):
        wire.service().get_memory(MEMORY_KEY)


def test_endpoints_match_docs_put_memory() -> None:
    wire = Wire({("PUT", "/v1/memories"): ok(memory_record(STATE, version="1"), status=201)})
    memory = wire.service().put_memory(MEMORY_KEY, STATE)
    assert wire.calls == [("PUT", "/v1/memories")]
    assert wire.body() == {"key": MEMORY_KEY, "value": STATE}
    assert wire.requests[0].headers["Content-Type"] == "application/json"
    assert memory == PrimitiveMemory(MEMORY_KEY, STATE, "1")


def test_put_memory_with_if_version_is_compare_and_set() -> None:
    wire = Wire({("PUT", "/v1/memories"): ok(memory_record(STATE, version="4"))})
    memory = wire.service().put_memory(MEMORY_KEY, STATE, if_version="3")
    assert wire.body() == {"key": MEMORY_KEY, "value": STATE, "if_version": "3"}
    assert memory.version == "4"


def test_put_memory_stale_version_is_a_typed_conflict_and_not_an_outage() -> None:
    wire = Wire({("PUT", "/v1/memories"): fail(409, "memory_conflict", "version mismatch")})
    service = wire.service()
    with pytest.raises(MemoryConflict) as caught:
        service.put_memory(MEMORY_KEY, STATE, if_version="1")
    assert isinstance(caught.value, ProviderUnavailable) and caught.value.provider == "primitive"
    assert caught.value.primitive_code == "memory_conflict" and caught.value.http_status == 409
    assert service.status.status == "ok"


def test_put_memory_response_without_a_version_is_refused() -> None:
    record = memory_record(STATE)
    del record["version"]
    with pytest.raises(ProviderUnavailable):
        Wire({("PUT", "/v1/memories"): ok(record)}).service().put_memory(MEMORY_KEY, STATE)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("", {"a": 1}),
        ("k" * 513, {"a": 1}),
        ("é" * 257, {"a": 1}),  # 514 UTF-8 bytes: the limit is bytes, not characters
        (MEMORY_KEY, {"blob": "x" * 70_000}),
        (MEMORY_KEY, {"unserialisable": object()}),
    ],
    ids=["empty-key", "key-513-bytes", "key-514-bytes-2-byte-chars", "value-over-64KiB", "value-not-json"],
)
def test_memory_limits_are_enforced_locally(key: str, value: dict) -> None:
    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(no_network))
    with pytest.raises(PrimitiveRejection):
        service.put_memory(key, value)


def test_memory_key_at_the_limit_is_accepted() -> None:
    wire = Wire({("PUT", "/v1/memories"): ok(memory_record({}, key="é" * 256))})
    assert wire.service().put_memory("é" * 256, {}).key == "é" * 256


def test_endpoints_match_docs_delete_memory() -> None:
    wire = Wire({("DELETE", "/v1/memories"): ok({"deleted": True, "key": MEMORY_KEY, "scope": {"type": "org"}})})
    service = wire.service()
    assert service.delete_memory(MEMORY_KEY) is True
    assert wire.calls == [("DELETE", "/v1/memories")]
    assert wire.requests[0].url.params["key"] == MEMORY_KEY
    assert not wire.requests[0].content


def test_delete_memory_that_did_not_exist_is_false() -> None:
    wire = Wire({("DELETE", "/v1/memories"): [ok({"deleted": False, "key": MEMORY_KEY}), fail(404, "not_found")]})
    service = wire.service()
    assert service.delete_memory(MEMORY_KEY) is False
    assert service.delete_memory(MEMORY_KEY) is False
    assert service.status.status == "ok"


# === 3. reply_to_thread: a composition, not an endpoint =============================================================


def reply_wire(thread: dict[str, Any] | None = None, reply: Any = None) -> Wire:
    return Wire(
        {
            ("GET", f"/v1/threads/{THREAD_ID}"): ok(thread or thread_data()),
            ("POST", f"/v1/emails/{(thread or thread_data())['latest_inbound_id']}/reply"): reply or ok(send_result()),
        }
    )


def test_endpoints_match_docs_reply_to_thread_is_get_thread_then_reply_to_latest_inbound() -> None:
    wire = reply_wire()
    reply = wire.service().reply_to_thread(
        THREAD_ID, "Added milk and eggs.", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="msg-1"
    )
    assert wire.calls == [("GET", f"/v1/threads/{THREAD_ID}"), ("POST", f"/v1/emails/{IN_2}/reply")]
    post = wire.requests[1]
    assert post.headers["Idempotency-Key"] == "msg-1"
    assert post.headers["Authorization"] == f"Bearer {KEY}"
    assert wire.body() == {
        "body_text": f"Added milk and eggs.\n\n-- {LABEL}",
        "if_latest_message_id": IN_2,
    }
    assert reply == PrimitiveReply(
        id="55555555-5555-4555-8555-555555555555",
        status="queued",
        accepted=("mom@example.com",),
        rejected=(),
        idempotent_replay=False,
    )
    assert reply.sent is True


def test_reply_guard_is_the_newest_message_even_when_it_is_our_own_earlier_reply() -> None:
    # Thread ends with our outbound message: replying to the latest INBOUND email must still be guarded by the
    # newest message overall, or the guard would 409 on every second reply.
    data = thread_data(message_count=2, latest_inbound_id=IN_1, latest_outbound_id=OUT_1)
    data["messages"] = data["messages"][:2]
    wire = reply_wire(data)
    wire.service().reply_to_thread(
        THREAD_ID, "Done.", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="msg-2"
    )
    assert wire.calls[1] == ("POST", f"/v1/emails/{IN_1}/reply")
    assert wire.body()["if_latest_message_id"] == OUT_1


def test_reply_request_carries_no_recipient_subject_or_header_overrides() -> None:
    wire = reply_wire()
    wire.service().reply_to_thread(
        THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="msg-3"
    )
    assert set(wire.body()) == {"body_text", "if_latest_message_id"}


def test_reply_without_a_key_derives_a_stable_per_content_idempotency_key() -> None:
    first, second, other = reply_wire(), reply_wire(), reply_wire()
    first.service().reply_to_thread(THREAD_ID, "Added milk.", allowed_senders=ALLOWED, sender_label=LABEL)
    second.service().reply_to_thread(THREAD_ID, "Added milk.", allowed_senders=ALLOWED, sender_label=LABEL)
    other.service().reply_to_thread(THREAD_ID, "Added eggs.", allowed_senders=ALLOWED, sender_label=LABEL)
    keys = [w.requests[1].headers["Idempotency-Key"] for w in (first, second, other)]
    assert keys[0] == keys[1] != keys[2]  # a keyless reply is silently dropped by Primitive, so a key is always sent
    assert keys[0].startswith("uhlexa-reply-") and len(keys[0]) <= 255


@pytest.mark.parametrize("bad_key", ["", "   ", "k" * 256, "café", "line\nbreak"])
def test_reply_refuses_an_unusable_idempotency_key_before_any_http_call(bad_key: str) -> None:
    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(no_network))
    with pytest.raises(PrimitiveRejection):
        service.reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key=bad_key)


@pytest.mark.parametrize("body", ["", "   \n  ", "x" * 262_200])
def test_reply_refuses_an_empty_or_oversized_body_before_any_http_call(body: str) -> None:
    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(no_network))
    with pytest.raises(PrimitiveRejection):
        service.reply_to_thread(THREAD_ID, body, allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="k")


def test_reply_sender_label_is_flattened_and_may_be_empty() -> None:
    wire = reply_wire()
    wire.service().reply_to_thread(
        THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label="Alex's\nUhlexa\r\n  agent", idempotency_key="msg-4"
    )
    assert wire.body()["body_text"] == "Hi\n\n-- Alex's Uhlexa agent"
    blank = reply_wire()
    blank.service().reply_to_thread(
        THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label="  ", idempotency_key="msg-5"
    )
    assert blank.body()["body_text"] == "Hi"


def test_reply_works_when_the_server_omits_latest_ids() -> None:
    data = thread_data()
    del data["latest_inbound_id"], data["latest_outbound_id"]
    wire = Wire(
        {
            ("GET", f"/v1/threads/{THREAD_ID}"): ok(data),
            ("POST", f"/v1/emails/{IN_2}/reply"): ok(send_result()),
        }
    )
    wire.service().reply_to_thread(
        THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="msg-6"
    )
    assert wire.calls[1] == ("POST", f"/v1/emails/{IN_2}/reply")


def test_reply_to_a_thread_with_no_inbound_email_is_not_repliable_and_sends_nothing() -> None:
    data = thread_data(message_count=1, latest_inbound_id=None, latest_outbound_id=OUT_1)
    data["messages"] = [message_row(OUT_1, "outbound", "2026-10-20T11:00:00Z")]
    wire = Wire({("GET", f"/v1/threads/{THREAD_ID}"): ok(data)})
    service = wire.service()
    with pytest.raises(NotRepliable):
        service.reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="msg-7")
    assert wire.calls == [("GET", f"/v1/threads/{THREAD_ID}")]
    assert service.status.status == "ok"


def test_reply_to_a_missing_thread_is_not_found_and_sends_nothing() -> None:
    wire = Wire({("GET", f"/v1/threads/{THREAD_ID}"): fail(404, "not_found")})
    with pytest.raises(PrimitiveNotFound):
        wire.service().reply_to_thread(
            THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="msg-8"
        )
    assert wire.calls == [("GET", f"/v1/threads/{THREAD_ID}")]


def test_reply_gate_denied_in_a_200_is_never_reported_as_sent() -> None:
    denied = ok(send_result(status="gate_denied", accepted=[], rejected=["mom@example.com"]))
    reply = (
        reply_wire(reply=denied)
        .service()
        .reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="m")
    )
    assert reply.rejected == ("mom@example.com",) and reply.accepted == ()
    assert reply.status == "gate_denied" and reply.sent is False


def test_reply_keyless_parent_already_replied_is_never_reported_as_sent() -> None:
    dropped = ok(send_result(idempotent_replay=True, dedup_reason="parent_already_replied"))
    reply = (
        reply_wire(reply=dropped)
        .service()
        .reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="m")
    )
    assert reply.idempotent_replay is True and reply.dedup_reason == "parent_already_replied" and reply.sent is False


def test_reply_replay_of_our_own_key_is_the_original_send() -> None:
    replayed = ok(send_result(idempotent_replay=True, dedup_reason="content_hash_match"))
    reply = (
        reply_wire(reply=replayed)
        .service()
        .reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="m")
    )
    assert reply.idempotent_replay is True and reply.sent is True


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),
        ({"status": "submitted_to_agent"}, True),
        ({"status": "delivered"}, True),
        ({"status": "deferred"}, True),
        ({"status": "gate_denied"}, False),
        ({"status": "agent_failed"}, False),
        ({"status": "bounced"}, False),
        ({"status": "canceled"}, False),
        ({"status": "unknown"}, False),
        ({"accepted": []}, False),
        ({"rejected": ["dad@example.com"]}, False),  # accepted for one recipient, refused for another: not a clean send
    ],
    ids=lambda value: str(value),
)
def test_reply_sent_is_true_only_for_a_clean_accepted_send(overrides: dict[str, Any], expected: bool) -> None:
    reply = (
        reply_wire(reply=ok(send_result(**overrides)))
        .service()
        .reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="m")
    )
    assert reply.sent is expected


def test_reply_recipient_gate_is_a_typed_refusal_and_not_an_outage() -> None:
    refusal = fail(
        403, "recipient_not_allowed", "gated", gates=["send_to_known_addresses", {"gate": "send_to_any_domain"}]
    )
    wire = reply_wire(reply=refusal)
    service = wire.service()
    with pytest.raises(RecipientNotAllowed) as caught:
        service.reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="m")
    assert caught.value.gates == ("send_to_known_addresses", "send_to_any_domain")
    assert caught.value.primitive_code == "recipient_not_allowed" and caught.value.http_status == 403
    assert service.status.status == "ok"


def test_reply_when_a_newer_message_arrived_is_thread_changed_and_not_an_outage() -> None:
    newer = "77777777-7777-4777-8777-777777777777"
    conflict = fail(409, "latest_message_mismatch", "newer message", details={"latest_message_id": newer})
    service = reply_wire(reply=conflict).service()
    with pytest.raises(ThreadChanged) as caught:
        service.reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="m")
    assert caught.value.latest_message_id == newer and caught.value.http_status == 409
    assert service.status.status == "ok"


def test_reply_to_an_inbound_that_cannot_be_replied_to_is_not_repliable() -> None:
    service = reply_wire(reply=fail(422, "inbound_not_repliable", "automated sender")).service()
    with pytest.raises(NotRepliable):
        service.reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="m")
    assert service.status.status == "ok"


def test_reply_key_reuse_with_a_different_request_is_a_rejection_with_its_code() -> None:
    service = reply_wire(reply=fail(409, "idempotency_key_reused", "payload differs")).service()
    with pytest.raises(PrimitiveRejection) as caught:
        service.reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="m")
    assert caught.value.primitive_code == "idempotency_key_reused"


# === 4. envelope parsing and the error map ==========================================================================

OUTAGES = [
    pytest.param(401, "unauthorized", None, id="401-bad-key"),
    pytest.param(402, "developer_usage_credit_exhausted", None, id="402-credit-exhausted"),
    pytest.param(403, "forbidden", None, id="403-forbidden"),
    pytest.param(429, "rate_limited", {"Retry-After": "7"}, id="429-rate-limited"),
    pytest.param(500, "internal_error", None, id="500"),
    pytest.param(503, "service_unavailable", None, id="503"),
]


@pytest.mark.parametrize(("status", "code", "headers"), OUTAGES)
def test_primitive_provider_error_is_unavailable(status: int, code: str, headers: dict[str, str] | None) -> None:
    wire = Wire({("GET", "/v1/memories"): fail(status, code, f"upstream echoed {KEY} here", headers)})
    service = wire.service()
    with pytest.raises(ProviderUnavailable) as caught:
        service.get_memory(MEMORY_KEY)
    assert not isinstance(caught.value, PrimitiveRejection)
    assert caught.value.provider == "primitive" and caught.value.status_code == 503
    assert str(status) in caught.value.reason
    assert KEY not in caught.value.reason
    status_after = service.status
    assert status_after.status == "unavailable" and KEY not in status_after.detail
    assert str(status) in status_after.detail


def test_rate_limit_reason_includes_retry_after() -> None:
    wire = Wire({("GET", "/v1/memories"): fail(429, "rate_limited", headers={"Retry-After": "7"})})
    with pytest.raises(ProviderUnavailable) as caught:
        wire.service().get_memory(MEMORY_KEY)
    assert "rate limited" in caught.value.reason.lower() and "7" in caught.value.reason


@pytest.mark.parametrize("retry_after", ["9" * 5000, "-1", "soon", "99999"])
def test_a_hostile_retry_after_header_is_ignored_not_fatal(retry_after: str) -> None:
    wire = Wire({("GET", "/v1/memories"): fail(429, "rate_limited", headers={"Retry-After": retry_after})})
    with pytest.raises(ProviderUnavailable) as caught:
        wire.service().get_memory(MEMORY_KEY)
    assert "rate limited" in caught.value.reason and "retry after" not in caught.value.reason


def test_credit_exhausted_reason_says_so() -> None:
    wire = Wire({("GET", "/v1/memories"): fail(402, "developer_usage_credit_exhausted")})
    with pytest.raises(ProviderUnavailable) as caught:
        wire.service().get_memory(MEMORY_KEY)
    assert "credit" in caught.value.reason.lower()


@pytest.mark.parametrize(
    "boom",
    [
        httpx.ReadTimeout(f"timed out talking to {KEY}"),
        httpx.ConnectError(f"refused for {KEY}"),
        httpx.ReadError("eof"),
    ],
    ids=["timeout", "connect", "read"],
)
def test_transport_failures_are_unavailable_without_leaking_the_message(boom: Exception) -> None:
    def raise_it(request: httpx.Request) -> httpx.Response:
        raise boom

    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(raise_it))
    with pytest.raises(ProviderUnavailable) as caught:
        service.get_thread(THREAD_ID)
    assert KEY not in caught.value.reason and caught.value.reason
    assert service.status.status == "unavailable" and KEY not in service.status.detail


def test_a_success_clears_unavailable() -> None:
    wire = Wire({("GET", "/v1/memories"): [fail(500, "internal_error"), ok(memory_record(STATE))]})
    service = wire.service()
    with pytest.raises(ProviderUnavailable):
        service.get_memory(MEMORY_KEY)
    assert service.status.status == "unavailable"
    assert service.get_memory(MEMORY_KEY) is not None
    assert service.status.status == "ok"


def test_a_typed_refusal_clears_a_stale_unavailable_because_the_provider_answered() -> None:
    wire = Wire({("GET", f"/v1/threads/{THREAD_ID}"): [fail(500, "internal_error"), fail(404, "not_found")]})
    service = wire.service()
    with pytest.raises(ProviderUnavailable):
        service.get_thread(THREAD_ID)
    assert service.status.status == "unavailable"
    with pytest.raises(PrimitiveNotFound):
        service.get_thread(THREAD_ID)
    assert service.status.status == "ok"


def test_a_local_refusal_leaves_the_badge_alone() -> None:
    """Nothing was sent, so a bad argument is no evidence either way about whether Primitive is reachable."""
    wire = Wire({("GET", "/v1/memories"): fail(500, "internal_error")})
    service = wire.service()
    with pytest.raises(ProviderUnavailable):
        service.get_memory(MEMORY_KEY)
    assert service.status.status == "unavailable"
    with pytest.raises(PrimitiveRejection) as caught:
        service.search_messages("   ")
    assert caught.value.http_status is None and caught.value.provider_answered is False
    assert service.status.status == "unavailable"
    assert wire.calls == [("GET", "/v1/memories")]


def test_non_json_error_pages_are_unavailable_with_the_status() -> None:
    page = httpx.Response(502, text="<html>Bad gateway</html>", headers={"Content-Type": "text/html"})
    service = Wire({("GET", "/v1/memories"): page}).service()
    with pytest.raises(ProviderUnavailable) as caught:
        service.get_memory(MEMORY_KEY)
    assert "502" in caught.value.reason and "<html>" not in caught.value.reason


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json at all", headers={"Content-Type": "text/html"}),
        httpx.Response(200, json=["not", "an", "envelope"]),
        httpx.Response(200, json={"success": False, "error": {"code": "internal_error", "message": "x"}}),
        httpx.Response(200, json={"success": True}),
        httpx.Response(200, json={"success": True, "data": "a string"}),
    ],
    ids=["html", "list", "success-false", "no-data", "data-not-object"],
)
def test_malformed_success_responses_fail_closed(response: httpx.Response) -> None:
    service = Wire({("GET", f"/v1/threads/{THREAD_ID}"): response}).service()
    with pytest.raises(ProviderUnavailable):
        service.get_thread(THREAD_ID)
    assert service.status.status == "unavailable"


def test_malformed_thread_data_fails_closed() -> None:
    service = Wire({("GET", f"/v1/threads/{THREAD_ID}"): ok({"subject": "no id here", "messages": []})}).service()
    with pytest.raises(ProviderUnavailable):
        service.get_thread(THREAD_ID)


def test_redirects_are_not_followed_so_the_key_cannot_leave_the_host() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailable):
        service.get_memory(MEMORY_KEY)
    assert len(hops) == 1


@pytest.mark.parametrize("status", [400, 422])
def test_other_validation_errors_are_typed_rejections_with_a_sanitised_reason(status: int) -> None:
    wire = Wire({("GET", "/v1/memories"): fail(status, "validation_error", f"bad {KEY}\nline two " + "y" * 500)})
    service = wire.service()
    with pytest.raises(PrimitiveRejection) as caught:
        service.get_memory(MEMORY_KEY)
    assert caught.value.primitive_code == "validation_error" and caught.value.http_status == status
    assert KEY not in caught.value.reason and "\n" not in caught.value.reason and len(caught.value.reason) <= 300
    assert service.status.status == "ok"


def test_error_codes_from_the_server_are_sanitised() -> None:
    wire = Wire({("GET", "/v1/memories"): fail(400, "weird code <script>alert(1)</script>")})
    with pytest.raises(PrimitiveRejection) as caught:
        wire.service().get_memory(MEMORY_KEY)
    assert "<script>" not in caught.value.reason and "<script>" not in caught.value.primitive_code


# === 5. configuration and credentials ===============================================================================


def test_base_url_comes_from_settings_with_a_documented_default() -> None:
    assert DEFAULT_BASE_URL == "https://api.primitive.dev/v1"
    custom = Wire({("GET", "/api/v9/memories"): ok(memory_record(STATE))})
    custom.service(primitive_api_base_url="https://primitive.example.test/api/v9/").get_memory(MEMORY_KEY)
    assert str(custom.requests[0].url).startswith("https://primitive.example.test/api/v9/memories")
    blank = Wire({("GET", "/v1/memories"): ok(memory_record(STATE))})
    blank.service(primitive_api_base_url="   ").get_memory(MEMORY_KEY)
    assert blank.requests[0].url.host == "api.primitive.dev"


@pytest.mark.parametrize("url", ["http://api.primitive.dev/v1", "ftp://x/v1", "not a url", "api.primitive.dev/v1"])
def test_a_base_url_that_would_send_the_key_in_clear_text_is_refused(url: str) -> None:
    service = PrimitiveService(settings_for(primitive_api_base_url=url), transport=httpx.MockTransport(no_network))
    with pytest.raises(ProviderUnavailable) as caught:
        service.get_memory(MEMORY_KEY)
    assert KEY not in caught.value.reason


def test_plain_http_is_allowed_only_for_loopback() -> None:
    wire = Wire({("GET", "/v1/memories"): ok(memory_record(STATE))})
    wire.service(primitive_api_base_url="http://127.0.0.1:8099/v1").get_memory(MEMORY_KEY)
    assert wire.requests[0].url.host == "127.0.0.1"


def test_every_capability_sends_the_key_only_as_a_bearer_header() -> None:
    def anything(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return ok(send_result())
        if request.url.path == f"/v1/threads/{THREAD_ID}":
            return ok(thread_data())
        if request.url.path == "/v1/emails/search":
            return ok([])
        if request.url.path == "/v1/memories":
            return ok({"deleted": True, "key": MEMORY_KEY} if request.method == "DELETE" else memory_record(STATE))
        return ok(inbound_row())

    seen: list[httpx.Request] = []

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return anything(request)

    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(spy))
    service.get_thread(THREAD_ID)
    service.get_message(IN_2)
    service.reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="k")
    service.search_messages("milk", thread_id=THREAD_ID)
    service.get_memory(MEMORY_KEY)
    service.put_memory(MEMORY_KEY, STATE)
    service.delete_memory(MEMORY_KEY)
    assert len(seen) >= 8
    for request in seen:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert KEY not in str(request.url) and KEY.encode() not in request.content
        assert request.url.scheme == "https"


def test_primitive_status_endpoint_view_carries_no_key_after_failures() -> None:
    settings = settings_for(primitive_webhook_secret=WEBHOOK_SECRET)
    wire = Wire({("GET", "/v1/memories"): fail(401, "unauthorized", f"key {KEY} rejected")})
    service = PrimitiveService(settings, transport=httpx.MockTransport(wire.handler))
    with pytest.raises(ProviderUnavailable):
        service.get_memory(MEMORY_KEY)
    rendered = json.dumps({name: status.as_dict() for name, status in provider_statuses(settings).items()})
    assert KEY not in rendered and WEBHOOK_SECRET not in rendered
    assert json.loads(rendered)["primitive"]["status"] == "unavailable"


# === 6. capabilities Primitive does not document ====================================================================


def test_undocumented_capability_raises_unavailable() -> None:
    """Primitive has no thread-creation endpoint (a thread exists once an email is sent or received)."""
    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(no_network))
    with pytest.raises(ProviderUnavailable) as caught:
        service.create_thread("Sunday groceries")
    assert caught.value.provider == "primitive" and caught.value.reason.startswith("not documented")
    assert not isinstance(caught.value, PrimitiveRejection)
    assert service.status.status == "ok"  # asking for something undocumented is not an outage


def test_undocumented_capability_is_unavailable_even_when_the_provider_is_off() -> None:
    service = PrimitiveService(Settings(_env_file=None), transport=httpx.MockTransport(no_network))
    with pytest.raises(ProviderUnavailable) as caught:
        service.create_thread("Sunday groceries")
    assert caught.value.reason.startswith("not documented")
    assert service.status.status == "disabled"


def test_disabled_and_misconfigured_never_reach_the_network() -> None:
    off = PrimitiveService(Settings(_env_file=None), transport=httpx.MockTransport(no_network))
    keyless = PrimitiveService(
        Settings(_env_file=None, enable_primitive=True), transport=httpx.MockTransport(no_network)
    )
    for service, fragment in ((off, "ENABLE_PRIMITIVE"), (keyless, "PRIMITIVE_API_KEY")):
        for call in (
            lambda s: s.get_thread(THREAD_ID),
            lambda s: s.get_message(IN_1),
            lambda s: s.reply_to_thread(
                THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL, idempotency_key="k"
            ),
            lambda s: s.search_messages("milk"),
            lambda s: s.get_memory(MEMORY_KEY),
            lambda s: s.put_memory(MEMORY_KEY, STATE),
            lambda s: s.delete_memory(MEMORY_KEY),
        ):
            with pytest.raises(ProviderUnavailable) as caught:
                call(service)
            assert fragment in caught.value.reason


# === reply recipient allowlist (security review 2026-10-22) ==========================================================


def test_reply_skips_an_outsider_and_answers_the_newest_household_email() -> None:
    data = thread_data()
    data["messages"][2] = message_row(IN_2, "inbound", "2026-10-20T12:00:00Z", sender="stranger@evil.example")
    wire = Wire(
        {
            ("GET", f"/v1/threads/{THREAD_ID}"): ok(data),
            ("POST", f"/v1/emails/{IN_1}/reply"): ok(send_result()),
        }
    )
    wire.service().reply_to_thread(THREAD_ID, "Added milk.", allowed_senders=ALLOWED, sender_label=LABEL)
    assert wire.calls[1] == ("POST", f"/v1/emails/{IN_1}/reply")
    assert wire.body()["if_latest_message_id"] == IN_2  # still guarded on the newest message overall


def test_reply_refuses_when_no_inbound_email_is_from_the_household() -> None:
    data = thread_data()
    for i, row in enumerate(data["messages"]):
        if row["direction"] == "inbound":
            data["messages"][i] = {**row, "from": "stranger@evil.example"}
    wire = Wire({("GET", f"/v1/threads/{THREAD_ID}"): ok(data)})
    with pytest.raises(NotRepliable) as caught:
        wire.service().reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL)
    assert caught.value.primitive_code == "sender_not_allowed"
    assert wire.calls == [("GET", f"/v1/threads/{THREAD_ID}")]  # nothing was sent


@pytest.mark.parametrize("allowed", [(), ("",), ("not an address",)])
def test_reply_with_no_usable_allowlist_refuses_before_any_http_call(allowed: tuple[str, ...]) -> None:
    service = PrimitiveService(settings_for(), transport=httpx.MockTransport(no_network))
    with pytest.raises(NotRepliable) as caught:
        service.reply_to_thread(THREAD_ID, "Hi", allowed_senders=allowed, sender_label=LABEL)
    assert caught.value.primitive_code == "sender_not_allowed"


def test_reply_sender_match_ignores_case_and_display_name() -> None:
    data = thread_data()
    data["messages"][2] = message_row(IN_2, "inbound", "2026-10-20T12:00:00Z", sender="Mom <MOM@Example.COM>")
    wire = reply_wire(data)
    wire.service().reply_to_thread(THREAD_ID, "Hi", allowed_senders=(" mom@example.com ",), sender_label=LABEL)
    assert wire.calls[1] == ("POST", f"/v1/emails/{IN_2}/reply")


@pytest.mark.parametrize(("sender", "sends"), [("mom@example.com", True), ("stranger@evil.example", False)])
def test_reply_checks_the_sender_itself_when_the_thread_view_lists_no_rows(sender: str, sends: bool) -> None:
    data = thread_data(messages=[])
    routes: dict[tuple[str, str], Any] = {
        ("GET", f"/v1/threads/{THREAD_ID}"): ok(data),
        ("GET", f"/v1/emails/{IN_2}"): ok({"id": IN_2, "from_email": sender, "subject": "s", "body_text": "b"}),
        ("POST", f"/v1/emails/{IN_2}/reply"): ok(send_result()),
    }
    wire = Wire(routes)
    if sends:
        wire.service().reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL)
        assert wire.calls[-1] == ("POST", f"/v1/emails/{IN_2}/reply")
    else:
        with pytest.raises(NotRepliable):
            wire.service().reply_to_thread(THREAD_ID, "Hi", allowed_senders=ALLOWED, sender_label=LABEL)
        assert ("POST", f"/v1/emails/{IN_2}/reply") not in wire.calls
