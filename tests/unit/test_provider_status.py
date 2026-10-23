"""Provider status, disabled-first service seams, the pinned Maritime URL and the Primitive webhook helpers (Task 4)."""

import hashlib
import hmac
import inspect
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import ProviderUnavailable
from app.main import create_app
from app.providers import (
    PROVIDER_NAMES,
    ProviderStatus,
    config_status,
    mark_available,
    mark_unavailable,
    provider_statuses,
    reset_runtime_state,
)
from app.providers.maritime.client import MaritimeClient
from app.providers.maritime.models import RunOutcome, StepRecord
from app.providers.maritime.service import MaritimeService
from app.providers.primitive.client import PrimitiveClient
from app.providers.primitive.models import PrimitiveEvent, PrimitiveMemory
from app.providers.primitive.service import PrimitiveService
from app.providers.primitive.webhook import WebhookPayloadError, parse_event, verify_signature
from app.settings import Settings, get_settings

ENV_NAMES = [name.upper() for name in Settings.model_fields]

# name -> (flag field, credential field, flag env name, credential env name)
PROVIDERS = {
    "primitive": ("enable_primitive", "primitive_api_key", "ENABLE_PRIMITIVE", "PRIMITIVE_API_KEY"),
    "subq": ("enable_subq_snap", "subq_api_key", "ENABLE_SUBQ_SNAP", "SUBQ_API_KEY"),
    "maritime": ("enable_maritime", "maritime_computers_key", "ENABLE_MARITIME", "MARITIME_COMPUTERS_KEY"),
    "keiki": ("enable_keiki", "keiki_gateway_key", "ENABLE_KEIKI", "KEIKI_GATEWAY_KEY"),
    "inkbox": ("enable_inkbox", "inkbox_api_key", "ENABLE_INKBOX", "INKBOX_API_KEY"),
}
# Settings a provider needs besides its credential before it counts as configured (Keiki also needs its agent map).
EXTRA_OK: dict[str, dict[str, Any]] = {"keiki": {"keiki_agent_ids": {"alex": "agent-alex-id"}}}
KEYS = {
    "primitive_api_key": "SENTINEL-prim-key-91d2",
    "primitive_webhook_secret": "SENTINEL-prim-webhook-77ac",
    "subq_api_key": "SENTINEL-subq-key-3b18",
    "maritime_computers_key": "SENTINEL-mk-key-c5e0",
    "keiki_gateway_key": "SENTINEL-keiki-gw-5e21",
    "keiki_api_key": "SENTINEL-keiki-admin-a90c",
    "inkbox_api_key": "SENTINEL-inkbox-key-6b73",
    "inkbox_webhook_auth_token": "SENTINEL-inkbox-hook-d4f8",
    "secret_encryption_key": "SENTINEL-enc-key-0f6a",
    "app_session_secret": "SENTINEL-session-secret-8d41",
}


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


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def active_settings(**overrides: Any) -> Settings:
    """Every provider flag on with a sentinel credential."""
    flags = {flag: True for flag, *_ in PROVIDERS.values()}
    extras = {k: v for extra in EXTRA_OK.values() for k, v in extra.items()}
    return make_settings(**flags, **KEYS, **extras, **overrides)


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected provider HTTP call: {request.method} {request.url}")


# --- status -------------------------------------------------------------------------------------------------------


def test_all_disabled_by_default() -> None:
    statuses = provider_statuses(make_settings())
    assert tuple(statuses) == PROVIDER_NAMES == ("primitive", "subq", "maritime", "keiki", "inkbox")
    for name, status in statuses.items():
        assert isinstance(status, ProviderStatus)
        assert (status.name, status.status, status.enabled, status.configured) == (name, "disabled", False, False)
        assert PROVIDERS[name][2] in status.detail  # names the flag that turns it on


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_flag_on_without_key_is_misconfigured(name: str) -> None:
    flag, _, flag_env, key_env = PROVIDERS[name]
    status = provider_statuses(make_settings(**{flag: True}))[name]
    assert (status.status, status.enabled, status.configured) == ("misconfigured", True, False)
    assert key_env in status.detail and flag_env in status.detail


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_blank_key_counts_as_missing(name: str, blank: str) -> None:
    flag, key, *_ = PROVIDERS[name]
    settings = make_settings(**{flag: True, key: blank}, **EXTRA_OK.get(name, {}))
    assert provider_statuses(settings)[name].status == "misconfigured"


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_flag_on_with_key_is_ok(name: str) -> None:
    flag, key, *_ = PROVIDERS[name]
    status = provider_statuses(make_settings(**{flag: True, key: KEYS[key]}, **EXTRA_OK.get(name, {})))[name]
    assert (status.status, status.enabled, status.configured) == ("ok", True, True)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_key_without_flag_stays_disabled(name: str) -> None:
    _, key, *_ = PROVIDERS[name]
    status = provider_statuses(make_settings(**{key: KEYS[key]}, **EXTRA_OK.get(name, {})))[name]
    assert (status.status, status.enabled, status.configured) == ("disabled", False, True)


def test_providers_are_independent() -> None:
    statuses = provider_statuses(make_settings(enable_subq_snap=True, subq_api_key=KEYS["subq_api_key"]))
    assert {n: s.status for n, s in statuses.items()} == {
        "primitive": "disabled",
        "subq": "ok",
        "maritime": "disabled",
        "keiki": "disabled",
        "inkbox": "disabled",
    }


def test_keiki_inkbox_in_provider_names() -> None:
    assert len(PROVIDER_NAMES) == 5
    assert PROVIDER_NAMES[-2:] == ("keiki", "inkbox")
    assert {"keiki", "inkbox"} <= set(provider_statuses(make_settings()))


def test_keiki_flag_without_gateway_key_is_misconfigured() -> None:
    status = provider_statuses(make_settings(enable_keiki=True, **EXTRA_OK["keiki"]))["keiki"]
    assert (status.status, status.enabled, status.configured) == ("misconfigured", True, False)
    assert "KEIKI_GATEWAY_KEY" in status.detail and "ENABLE_KEIKI" in status.detail


def test_keiki_flag_without_agent_ids_is_misconfigured() -> None:
    settings = make_settings(enable_keiki=True, keiki_gateway_key=KEYS["keiki_gateway_key"])
    status = provider_statuses(settings)["keiki"]
    assert (status.status, status.enabled, status.configured) == ("misconfigured", True, False)
    assert "KEIKI_AGENT_IDS" in status.detail


def test_keiki_with_key_and_agent_ids_is_ok() -> None:
    settings = make_settings(
        enable_keiki=True, keiki_gateway_key=KEYS["keiki_gateway_key"], keiki_agent_ids={"alex": "agent-1"}
    )
    assert provider_statuses(settings)["keiki"].status == "ok"


def test_inkbox_flag_without_key_is_misconfigured() -> None:
    status = provider_statuses(make_settings(enable_inkbox=True))["inkbox"]
    assert (status.status, status.enabled, status.configured) == ("misconfigured", True, False)
    assert "INKBOX_API_KEY" in status.detail and "ENABLE_INKBOX" in status.detail


def test_status_payload_has_no_new_secret_values() -> None:
    signing = {"alex-agent": "SENTINEL-signing-key-b1c7"}
    settings = active_settings(inkbox_signing_keys=signing)
    mark_unavailable("keiki", f"401 {KEYS['keiki_gateway_key']} {KEYS['keiki_api_key']}")
    mark_unavailable(
        "inkbox", f"boom {KEYS['inkbox_api_key']} {signing['alex-agent']} {KEYS['inkbox_webhook_auth_token']}"
    )
    statuses = provider_statuses(settings)
    blob = json.dumps({name: s.as_dict() for name, s in statuses.items()}) + repr(statuses) + repr(settings)
    for secret in (*KEYS.values(), *signing.values()):
        assert secret not in blob
    assert statuses["keiki"].status == "unavailable" and statuses["inkbox"].status == "unavailable"


def test_get_keiki_and_get_inkbox_factories_follow_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.providers import get_inkbox, get_keiki

    assert get_keiki().status()["status"] == "disabled"
    assert get_inkbox().status().state == "disabled"
    monkeypatch.setenv("ENABLE_KEIKI", "true")
    monkeypatch.setenv("KEIKI_GATEWAY_KEY", KEYS["keiki_gateway_key"])
    monkeypatch.setenv("KEIKI_AGENT_IDS", '{"alex": "agent-1"}')
    monkeypatch.setenv("ENABLE_INKBOX", "true")
    monkeypatch.setenv("INKBOX_API_KEY", KEYS["inkbox_api_key"])
    monkeypatch.setenv("ENABLE_INKBOX_SMS", "true")
    get_settings.cache_clear()
    assert get_keiki().status()["status"] == "ok"
    inkbox = get_inkbox().status()
    assert inkbox.state == "ok" and inkbox.channels["sms"] == "ok" and inkbox.channels["email"] == "disabled"


def test_status_payload_has_no_secret_values() -> None:
    settings = active_settings()
    mark_unavailable("subq", f"HTTP 401 for key {KEYS['subq_api_key']} and {KEYS['app_session_secret']}")
    statuses = provider_statuses(settings)
    blob = json.dumps({name: s.as_dict() for name, s in statuses.items()}) + repr(statuses) + repr(settings)
    for secret in KEYS.values():
        assert secret not in blob
    assert statuses["subq"].status == "unavailable"


def test_as_dict_matches_the_spec_payload() -> None:
    payload = provider_statuses(make_settings())["subq"].as_dict()
    assert set(payload) == {"status", "enabled", "configured", "detail"}
    assert payload["status"] == "disabled" and payload["enabled"] is False


def test_mark_unavailable_overlays_ok_and_clears_on_success() -> None:
    settings = active_settings()
    mark_unavailable("maritime", "computer_limit: no free slot")
    status = provider_statuses(settings)["maritime"]
    assert status.status == "unavailable" and "computer_limit" in status.detail
    assert (status.enabled, status.configured) == (True, True)
    mark_available("maritime")
    assert provider_statuses(settings)["maritime"].status == "ok"


def test_mark_unavailable_never_hides_a_config_problem() -> None:
    mark_unavailable("primitive", "boom")
    assert provider_statuses(make_settings())["primitive"].status == "disabled"
    assert provider_statuses(make_settings(enable_primitive=True))["primitive"].status == "misconfigured"


def test_unavailable_reason_is_trimmed_and_single_line() -> None:
    mark_unavailable("subq", "line one\n" + "x" * 5000)
    detail = provider_statuses(active_settings())["subq"].detail
    assert "\n" not in detail and len(detail) < 400
    mark_unavailable("subq", "   ")
    assert provider_statuses(active_settings())["subq"].detail.strip() != ""


def test_unknown_provider_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        mark_unavailable("ring", "nope")
    with pytest.raises(ValueError):
        config_status(make_settings(), "ring")


def test_config_status_ignores_runtime_state() -> None:
    mark_unavailable("subq", "boom")
    assert config_status(active_settings(), "subq").status == "ok"


# --- GET /api/providers -------------------------------------------------------------------------------------------


def providers_client(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)  # no `with`: the lifespan (DB, seed, worker) is not needed here


def test_providers_route_is_registered_in_create_app() -> None:
    assert "get" in create_app().openapi()["paths"]["/api/providers"]


def test_providers_endpoint_flags_off() -> None:
    body = providers_client(make_settings()).get("/api/providers").json()
    assert set(body) == {"primitive", "subq", "maritime", "keiki", "inkbox"}
    assert all(v["status"] == "disabled" and v["enabled"] is False for v in body.values())
    assert all(set(v) == {"status", "enabled", "configured", "detail"} for v in body.values())


def test_providers_endpoint_flags_on() -> None:
    settings = make_settings(enable_primitive=True, enable_subq_snap=True, subq_api_key=KEYS["subq_api_key"])
    body = providers_client(settings).get("/api/providers").json()
    assert body["primitive"]["status"] == "misconfigured"
    assert body["subq"]["status"] == "ok" and body["subq"]["configured"] is True
    assert body["maritime"]["status"] == "disabled"


def test_providers_endpoint_reports_runtime_unavailable_without_secrets() -> None:
    settings = active_settings()
    mark_unavailable("primitive", f"upstream said {KEYS['primitive_api_key']} is revoked")
    response = providers_client(settings).get("/api/providers")
    assert response.status_code == 200
    assert response.json()["primitive"]["status"] == "unavailable"
    assert not any(secret in response.text for secret in KEYS.values())


def test_providers_endpoint_is_401_when_demo_auth_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_DEMO_AUTH", "false")
    get_settings.cache_clear()
    response = providers_client(make_settings()).get("/api/providers")
    assert response.status_code == 401 and response.json()["code"] == "unauthorized"


def test_bare_router_serves_without_the_rest_of_the_app() -> None:
    from app.api.providers import router

    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get("/api/providers").status_code == 200


# --- Primitive service (disabled-first, client seam) --------------------------------------------------------------

PRIMITIVE_CALLS: dict[str, Callable[[PrimitiveService], Any]] = {
    "get_thread": lambda s: s.get_thread("thr_1"),
    "get_message": lambda s: s.get_message("em_1"),
    "reply_to_thread": lambda s: s.reply_to_thread(
        "thr_1", "hello", sender_label="Uhlexa", allowed_senders=("mom@example.com",)
    ),
    "search_messages": lambda s: s.search_messages("milk", thread_id="thr_1", limit=5),
    "get_memory": lambda s: s.get_memory("uhlexa:thread:1:state"),
    "put_memory": lambda s: s.put_memory("uhlexa:thread:1:state", {"items": ["milk"]}),
    "delete_memory": lambda s: s.delete_memory("uhlexa:thread:1:state"),
}
calls = pytest.mark.parametrize("call", PRIMITIVE_CALLS.values(), ids=PRIMITIVE_CALLS.keys())


@calls
def test_primitive_disabled_raises_provider_unavailable(call: Callable[[PrimitiveService], Any]) -> None:
    service = PrimitiveService(make_settings(), transport=httpx.MockTransport(no_network))
    with pytest.raises(ProviderUnavailable) as caught:
        call(service)
    assert caught.value.provider == "primitive" and caught.value.status_code == 503
    assert caught.value.reason == service.status.detail and "ENABLE_PRIMITIVE" in caught.value.reason


@calls
def test_primitive_misconfigured_raises_provider_unavailable(call: Callable[[PrimitiveService], Any]) -> None:
    service = PrimitiveService(make_settings(enable_primitive=True), transport=httpx.MockTransport(no_network))
    assert service.status.status == "misconfigured"
    with pytest.raises(ProviderUnavailable) as caught:
        call(service)
    assert "PRIMITIVE_API_KEY" in caught.value.reason


def primitive_outage(failure: int | None) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A Primitive that is down: every request gets HTTP `failure`, or a connection error when it is None.

    The transport is the whole network here: nothing it does leaves the process (tests/network_guard.py enforces it)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if failure is None:
            raise httpx.ConnectError(f"connect failed for {KEYS['primitive_api_key']}", request=request)
        return httpx.Response(failure, json={"success": False, "error": {"code": "unavailable", "message": "down"}})

    return httpx.MockTransport(handler), seen


@pytest.mark.parametrize("failure", [503, None], ids=["http-503", "connection-error"])
@calls
def test_primitive_provider_error_is_unavailable_and_never_reaches_the_network(
    call: Callable[[PrimitiveService], Any], failure: int | None
) -> None:
    transport, seen = primitive_outage(failure)
    service = PrimitiveService(active_settings(), transport=transport)
    with pytest.raises(ProviderUnavailable) as caught:
        call(service)
    assert caught.value.provider == "primitive" and caught.value.status_code == 503
    assert seen and {request.url.host for request in seen} == {"api.primitive.dev"}  # asked the fake wire, only
    assert ("HTTP 503" if failure else "ConnectError") in caught.value.reason
    assert service.status.status == "unavailable" and caught.value.reason in service.status.detail
    assert KEYS["primitive_api_key"] not in caught.value.reason + service.status.detail


def test_primitive_success_clears_unavailable() -> None:
    up = False
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if not up:
            return httpx.Response(502, json={"success": False, "error": {"code": "bad_gateway"}})
        body = {"success": True, "data": {"key": "k", "value": {"a": 1}, "version": "v1"}}
        return httpx.Response(200, json=body)

    service = PrimitiveService(active_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailable):
        service.get_memory("k")
    assert service.status.status == "unavailable"
    up = True
    assert service.get_memory("k") == PrimitiveMemory("k", {"a": 1}, "v1")
    assert service.status.status == "ok"
    assert seen == ["GET /v1/memories", "GET /v1/memories"]


def test_primitive_transport_errors_become_unavailable_without_leaking_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(self: PrimitiveClient, key: str) -> None:
        raise httpx.ConnectError(f"connect failed for {KEYS['primitive_api_key']}")

    monkeypatch.setattr(PrimitiveClient, "get_memory", boom)
    service = PrimitiveService(active_settings())
    with pytest.raises(ProviderUnavailable) as caught:
        service.get_memory("k")
    assert caught.value.reason == "ConnectError"
    assert KEYS["primitive_api_key"] not in service.status.detail


# --- Primitive webhook helpers ------------------------------------------------------------------------------------

VECTORS = [
    # Official signing vectors (primitivedotdev/sdks test-fixtures/signing/vectors.json), per the research file.
    (
        b'{"event":"email.received","email_id":"msg_123"}',
        "whsec_test_secret_key_123",
        1734567890,
        "45c9e2f95add59da39d6bdf3e617b8bfcad771a59d06bf4687d11d39af041d91",
    ),
    (b"{}", "secret", 1000000000, "10447a13c77e61914632c736653543f75b152b77a1573fc15a41baad63fc66a3"),
]
BODY = b'{"id":"evt_1","event":"email.received"}'
SECRET = "whsec_unit_test"
NOW = 1_800_000_000.0


def digest(body: bytes, secret: str, t: int | str) -> str:
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def header(t: int, *v1: str, extra: str = "") -> dict[str, str]:
    parts = [f"t={t}", *(f"v1={v}" for v in v1)]
    return {"Primitive-Signature": ",".join(parts) + extra}


@pytest.mark.parametrize(("body", "secret", "t", "expected"), VECTORS)
def test_webhook_official_vectors_verify(body: bytes, secret: str, t: int, expected: str) -> None:
    assert digest(body, secret, t) == expected
    assert verify_signature({"Primitive-Signature": f"t={t},v1={expected}"}, body, secret, now=float(t)) is True


def test_webhook_valid_signature_uses_the_clock_when_now_is_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.providers.primitive.webhook.time.time", lambda: NOW)
    assert verify_signature(header(int(NOW), digest(BODY, SECRET, int(NOW))), BODY, SECRET) is True


def test_webhook_rejects_tampered_body_and_wrong_secret() -> None:
    headers = header(int(NOW), digest(BODY, SECRET, int(NOW)))
    assert verify_signature(headers, BODY + b" ", SECRET, now=NOW) is False
    assert verify_signature(headers, BODY, "another-secret", now=NOW) is False
    assert verify_signature(headers, BODY, "", now=NOW) is False


@pytest.mark.parametrize(("skew", "valid"), [(300, True), (301, False), (-60, True), (-61, False), (0, True)])
def test_webhook_timestamp_tolerance(skew: int, valid: bool) -> None:
    t = int(NOW) - skew  # positive skew = signed in the past
    assert verify_signature(header(t, digest(BODY, SECRET, t)), BODY, SECRET, now=NOW) is valid


def test_webhook_accepts_any_matching_v1_and_ignores_unknown_parts() -> None:
    t = int(NOW)
    good = digest(BODY, SECRET, t)
    assert verify_signature(header(t, "0" * 64, good), BODY, SECRET, now=NOW) is True
    assert verify_signature({"Primitive-Signature": f" t={t} , v0=legacy , v1={good} "}, BODY, SECRET, now=NOW) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        "t=,v1=ab",
        "t=abc,v1=ab",
        "v1=ab",
        "t=1800000000",
        "t=1800000000,v1=",
        "t=1800000000,t=1800000001,v1=ab",
    ],
)
def test_webhook_rejects_malformed_headers(value: str) -> None:
    assert verify_signature({"Primitive-Signature": value}, BODY, SECRET, now=NOW) is False


def test_webhook_rejects_missing_header_and_never_raises_on_odd_input() -> None:
    assert verify_signature({}, BODY, SECRET, now=NOW) is False
    huge = {"Primitive-Signature": "t=" + "9" * 5000 + ",v1=ab"}
    assert verify_signature(huge, BODY, SECRET, now=NOW) is False
    assert verify_signature({"Primitive-Signature": f"t={int(NOW)},v1=éé"}, BODY, SECRET, now=NOW) is False


def test_webhook_header_lookup_is_case_insensitive() -> None:
    t = int(NOW)
    value = f"t={t},v1={digest(BODY, SECRET, t)}"
    assert verify_signature({"primitive-signature": value}, BODY, SECRET, now=NOW) is True
    assert verify_signature({"X-Primitive-Signature": value}, BODY, SECRET, now=NOW) is True


def test_parse_event_email_received() -> None:
    payload = {"id": "evt_9", "event": "email.received", "email": {"id": "em_7", "thread_id": "thr_3"}}
    event = parse_event(json.dumps(payload).encode())
    assert event == PrimitiveEvent("evt_9", "email.received", "thr_3", "em_7", payload)


def test_parse_event_tolerates_missing_email_block() -> None:
    event = parse_event(b'{"id":"evt_1","event":"sent_email.delivered"}')
    assert (event.id, event.type, event.thread_id, event.message_id) == ("evt_1", "sent_email.delivered", None, None)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'"x"',
        b"{}",
        b'{"id":"a"}',
        b'{"event":"x"}',
        b'{"id":1,"event":"x"}',
        b'{"id":"","event":"x"}',
        b"\xff",
    ],
)
def test_parse_event_rejects_malformed_bodies(raw: bytes) -> None:
    with pytest.raises(WebhookPayloadError):
        parse_event(raw)


# --- Maritime service ---------------------------------------------------------------------------------------------

MARITIME_CALLS: dict[str, Callable[[MaritimeService, uuid.UUID], Any]] = {
    "ensure_computer": lambda s, u: s.ensure_computer(u),
    "run_list_add": lambda s, u: s.run_list_add(u, "cmp_1", "https://example.test/list", ["milk"], lambda step: None),
    "request_takeover": lambda s, u: s.request_takeover(u, "cmp_1", "login required"),
    "takeover_status": lambda s, u: s.takeover_status(u, "cmp_1"),
}
maritime_calls = pytest.mark.parametrize("call", MARITIME_CALLS.values(), ids=MARITIME_CALLS.keys())


def test_maritime_pinned_url_is_stable_and_server_side() -> None:
    service = MaritimeService(make_settings())
    user_id = uuid.uuid4()
    assert (
        service.pinned_mcp_url(user_id) == service.pinned_mcp_url(user_id) == f"https://mcp.maritime.sh/mcp/u/{user_id}"
    )
    assert service.pinned_mcp_url(uuid.uuid4()) != service.pinned_mcp_url(user_id)
    # The only input is the authenticated user's id: no URL, header or free-text parameter exists to change it.
    assert list(inspect.signature(MaritimeService.pinned_mcp_url).parameters) == ["self", "user_id"]


def test_maritime_pinned_url_embeds_exactly_the_given_id() -> None:
    service = MaritimeService(make_settings())
    for _ in range(50):
        user_id = uuid.uuid4()
        url = service.pinned_mcp_url(user_id)
        assert url.startswith("https://mcp.maritime.sh/mcp/u/") and url.count(str(user_id)) == 1
        assert url.endswith(str(user_id))


def test_maritime_pinned_url_follows_the_configured_base_and_never_carries_the_key() -> None:
    settings = active_settings(maritime_mcp_url="https://maritime.example.test/mcp/")
    user_id = uuid.uuid4()
    url = MaritimeService(settings).pinned_mcp_url(user_id)
    assert url == f"https://maritime.example.test/mcp/u/{user_id}"
    assert KEYS["maritime_computers_key"] not in url


@pytest.mark.parametrize("bad", ["../../etc/passwd", "alex", str(uuid.uuid4()), None, 7])
def test_maritime_pinned_url_accepts_only_a_uuid(bad: Any) -> None:
    with pytest.raises(TypeError):
        MaritimeService(make_settings()).pinned_mcp_url(bad)


def test_maritime_service_repr_does_not_expose_the_key() -> None:
    service = MaritimeService(active_settings())
    assert KEYS["maritime_computers_key"] not in repr(service)


@maritime_calls
def test_maritime_disabled_raises_without_building_a_client(call: Callable[[MaritimeService, uuid.UUID], Any]) -> None:
    built: list[str] = []
    service = MaritimeService(make_settings(), client_factory=lambda url: built.append(url))  # type: ignore[arg-type,return-value]
    with pytest.raises(ProviderUnavailable) as caught:
        call(service, uuid.uuid4())
    assert caught.value.provider == "maritime" and "ENABLE_MARITIME" in caught.value.reason
    assert built == []


@maritime_calls
def test_maritime_misconfigured_raises(call: Callable[[MaritimeService, uuid.UUID], Any]) -> None:
    service = MaritimeService(make_settings(enable_maritime=True))
    with pytest.raises(ProviderUnavailable) as caught:
        call(service, uuid.uuid4())
    assert "MARITIME_COMPUTERS_KEY" in caught.value.reason


class UnreachableMaritime(httpx2.AsyncBaseTransport):
    """Stands in for the network: nothing is reachable, and nothing leaves the process."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("unreachable")


@maritime_calls
def test_maritime_unreachable_server_is_honestly_unavailable(call: Callable[[MaritimeService, uuid.UUID], Any]) -> None:
    # Task 11 wired the real client: a flag + key with no reachable server must not fake a computer or a run.
    settings = active_settings()
    service = MaritimeService(
        settings, client_factory=lambda url: MaritimeClient(settings, url, transport=UnreachableMaritime())
    )
    with pytest.raises(ProviderUnavailable) as caught:
        call(service, uuid.uuid4())
    assert caught.value.provider == "maritime" and "connection failed" in caught.value.reason
    assert service.status.status == "unavailable"


class FakeMaritimeClient(MaritimeClient):
    def __init__(self, pinned_url: str) -> None:
        self.pinned_url = pinned_url
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def get_computer(self) -> str:
        self.calls.append(("get_computer", ()))
        return "cmp_fake"

    def run_list_add(self, computer_id: str, target_url: str, items: list[str], on_step: Callable[[StepRecord], None]):
        self.calls.append(("run_list_add", (computer_id, target_url, tuple(items))))
        on_step(StepRecord(index=0, action="navigate", ok=True, note="opened", screenshot_ref=None))
        return RunOutcome("succeeded", True, detail="done")

    def request_takeover(self, computer_id: str, reason: str) -> str:
        self.calls.append(("request_takeover", (computer_id, reason)))
        return "https://takeover.example.test/secret-link"

    def takeover_status(self, computer_id: str):
        self.calls.append(("takeover_status", (computer_id,)))
        return "human"


def test_maritime_active_service_drives_the_injected_client_with_the_pinned_url() -> None:
    clients: list[FakeMaritimeClient] = []

    def factory(url: str) -> FakeMaritimeClient:
        clients.append(FakeMaritimeClient(url))
        return clients[-1]

    service = MaritimeService(active_settings(), client_factory=factory)
    user_id = uuid.uuid4()
    steps: list[StepRecord] = []

    assert service.ensure_computer(user_id) == "cmp_fake"
    outcome = service.run_list_add(user_id, "cmp_fake", "https://example.test/list", ["milk", "eggs"], steps.append)
    assert (outcome.status, outcome.verified) == ("succeeded", True) and [s.action for s in steps] == ["navigate"]
    assert service.request_takeover(user_id, "cmp_fake", "login") == "https://takeover.example.test/secret-link"
    assert service.takeover_status(user_id, "cmp_fake") == "human"
    assert {c.pinned_url for c in clients} == {service.pinned_mcp_url(user_id)}
    assert service.status.status == "ok"


def test_maritime_models_hide_secrets_from_repr() -> None:
    outcome = RunOutcome(
        "needs_takeover", False, "https://takeover.example.test/secret-link", b"\x89PNG-bytes", "login wall"
    )
    assert "secret-link" not in repr(outcome) and "PNG-bytes" not in repr(outcome)
    assert RunOutcome("failed", False).takeover_url is None
