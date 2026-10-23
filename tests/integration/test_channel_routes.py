"""Inkbox webhook route and call WebSocket (plan Task 18). Fakes only; signatures are computed locally."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocketDisconnect

from app.api import channels
from app.auth.demo_auth import Principal
from app.jobs import channel_handlers
from app.jobs.worker import run_once
from app.models import ActivityEvent, Job, JobStatus, WebhookEvent, WebhookStatus
from app.settings import Settings, get_settings
from tests.helpers_channels import (
    ALEX_HANDLE,
    ALEX_NUMBER,
    JORDAN_NUMBER,
    SIGNING_KEY,
    STRANGER_NUMBER,
    FakeGateway,
    FakeInkbox,
    SyncOnlyGateway,
    bind_family,
    call_headers,
    imessage_envelope,
    sign,
)

URL = f"/webhooks/inkbox/{ALEX_HANDLE}"
BEARER_SENTINEL = "SENTINEL_bearer_token_value_777"
API_KEY_SENTINEL = "ApiKey_SENTINEL_api_key_value_123456"


@pytest.fixture(autouse=True)
def _clean_call_registry() -> Iterator[None]:
    channels.reset_call_registry()
    yield
    channels.reset_call_registry()


@pytest.fixture
def inkbox_on(settings_env: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ENABLE_INKBOX", "true")
    monkeypatch.setenv("ENABLE_INKBOX_IMESSAGE", "true")
    monkeypatch.setenv("ENABLE_INKBOX_SMS", "true")
    monkeypatch.setenv("ENABLE_INKBOX_CALLS", "true")
    monkeypatch.setenv("INKBOX_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("INKBOX_SIGNING_KEYS", json.dumps({ALEX_HANDLE: SIGNING_KEY}))
    monkeypatch.setenv("ENABLE_KEIKI", "true")
    monkeypatch.setenv("KEIKI_AGENT_IDS", json.dumps({"alex": "agent_alex", "katie": "agent_katie"}))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def family(db: Session, seeded: dict[str, Principal]) -> dict[str, Principal]:
    bind_family(db, seeded)
    return seeded


def post(
    client: TestClient, envelope: dict[str, Any], *, headers: dict[str, str] | None = None, key: str = SIGNING_KEY
) -> Any:
    raw = json.dumps(envelope).encode()
    return client.post(URL, content=raw, headers=sign(raw, key) if headers is None else headers)


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


# --- webhook ------------------------------------------------------------------------------------------------------


def test_inkbox_webhook_unsigned_rejected(client: TestClient, db: Session, family: dict, inkbox_on: None) -> None:
    assert post(client, imessage_envelope(), headers={}).status_code == 401
    assert post(client, imessage_envelope(), key="wrong-key").status_code == 401
    assert post(client, imessage_envelope(), headers=sign(b"{}", ts=1)).status_code == 401  # stale and wrong body
    assert count(db, Job) == 0
    rows = db.scalars(select(WebhookEvent)).all()
    assert len(rows) == 1 and rows[0].signature_valid is False and rows[0].payload == {"count": 3}


def test_inkbox_webhook_no_signing_key_configured_rejects(
    client: TestClient, family: dict, inkbox_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INKBOX_SIGNING_KEYS", "{}")
    get_settings.cache_clear()
    assert post(client, imessage_envelope()).status_code == 401


def test_inkbox_webhook_unknown_handle_404_and_disabled_503(
    client: TestClient, family: dict, settings_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert post(client, imessage_envelope()).status_code == 503  # Inkbox disabled
    monkeypatch.setenv("ENABLE_INKBOX", "true")
    get_settings.cache_clear()
    raw = json.dumps(imessage_envelope()).encode()
    assert client.post("/webhooks/inkbox/nobody", content=raw, headers=sign(raw)).status_code == 404
    get_settings.cache_clear()


def test_inkbox_replay_deduped(client: TestClient, db: Session, family: dict, inkbox_on: None) -> None:
    assert post(client, imessage_envelope()).json() == {"status": "accepted"}
    assert post(client, imessage_envelope()).json() == {"status": "duplicate"}
    assert count(db, WebhookEvent) == 1
    job = db.scalars(select(Job)).one()
    assert (job.kind, job.status) == ("inkbox_event", JobStatus.QUEUED)
    assert set(job.payload) == {"webhook_event_id", "handle"}  # ids only


def test_non_message_event_is_stored_but_not_enqueued(
    client: TestClient, db: Session, family: dict, inkbox_on: None
) -> None:
    assert post(client, {"id": "evt_c", "event_type": "call.ended", "data": {}}).json() == {"status": "ignored"}
    assert count(db, Job) == 0
    assert db.scalars(select(WebhookEvent)).one().status == WebhookStatus.IGNORED


def test_disabled_channel_is_ignored(
    client: TestClient, db: Session, family: dict, inkbox_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_INKBOX_IMESSAGE", "false")
    get_settings.cache_clear()
    assert post(client, imessage_envelope()).json() == {"status": "ignored"}
    assert count(db, Job) == 0


def test_optional_bearer_second_factor(
    client: TestClient, family: dict, inkbox_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INKBOX_WEBHOOK_AUTH_TOKEN", BEARER_SENTINEL)
    get_settings.cache_clear()
    raw = json.dumps(imessage_envelope()).encode()
    assert client.post(URL, content=raw, headers=sign(raw)).status_code == 401
    good = {**sign(raw), "Authorization": f"Bearer {BEARER_SENTINEL}"}
    assert client.post(URL, content=raw, headers=good).status_code == 200


def test_malformed_signed_body_400(client: TestClient, family: dict, inkbox_on: None) -> None:
    raw = b'{"nope": true}'
    assert client.post(URL, content=raw, headers=sign(raw)).status_code == 400


def test_inkbox_imessage_end_to_end_with_fake_sdk_and_fake_gateway(
    client: TestClient,
    db: Session,
    session_factory: sessionmaker[Session],
    family: dict,
    inkbox_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, inkbox = FakeGateway(reply="Bread and bananas."), FakeInkbox()
    monkeypatch.setattr(channel_handlers, "make_gateway", lambda: gateway)
    monkeypatch.setattr(channel_handlers, "make_inkbox", lambda: inkbox)
    assert post(client, imessage_envelope("evt_e2e")).status_code == 200
    assert run_once(session_factory) == 1
    assert gateway.calls[0]["agent_id"] == "agent_alex"
    assert inkbox.sent == [
        ("imessage", ALEX_HANDLE, {"text": "Bread and bananas.", "idempotency_key": "evt_e2e", "to": ALEX_NUMBER})
    ]
    db.expire_all()
    assert db.scalars(select(WebhookEvent)).one().status == WebhookStatus.PROCESSED
    assert db.scalars(select(Job)).one().status == JobStatus.SUCCEEDED


# --- call WebSocket -----------------------------------------------------------------------------------------------


@pytest.fixture
def ws_wired(client: TestClient, db: Session, family: dict, inkbox_on: None) -> dict[str, Any]:
    wiring: dict[str, Any] = {"gateway": FakeGateway(), "inkbox": FakeInkbox()}

    @contextmanager
    def scope() -> Iterator[Session]:
        yield db

    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[channels.get_gateway] = lambda: wiring["gateway"]
    overrides[channels.get_inkbox_service] = lambda: wiring["inkbox"]
    overrides[channels.get_session_scope] = lambda: scope
    wiring["client"] = client
    return wiring


def connect(client: TestClient, headers: dict[str, str], handle: str = ALEX_HANDLE) -> Any:
    return client.websocket_connect(f"/ws/phone/{handle}", headers=headers)


def say(ws: Any, text: str, *, turn_id: int = 1) -> list[dict[str, Any]]:
    """Send a final transcript and collect events up to the turn's `done`."""
    ws.send_json({"event": "transcript", "is_final": True, "text": text, "turn_id": turn_id})
    out: list[dict[str, Any]] = []
    while True:
        event = ws.receive_json()
        out.append(event)
        if event.get("done") is True:
            return out


def test_ws_handshake_bad_signature_closes_1008(ws_wired: dict) -> None:
    client = ws_wired["client"]
    for headers in ({}, call_headers(key="wrong-key"), {"X-Call-Context": "{}"}):
        with pytest.raises(WebSocketDisconnect) as caught, connect(client, headers):
            pass
        assert caught.value.code == 1008


def test_ws_unknown_handle_or_calls_disabled_closes_1008(ws_wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    client = ws_wired["client"]
    with pytest.raises(WebSocketDisconnect) as caught, connect(client, call_headers(), handle="stranger"):
        pass
    assert caught.value.code == 1008
    monkeypatch.setenv("ENABLE_INKBOX_CALLS", "false")
    get_settings.cache_clear()
    with pytest.raises(WebSocketDisconnect) as off, connect(client, call_headers("call_off")):
        pass
    assert off.value.code == 1008


def test_ws_inbound_call_final_transcript_gets_agent_reply(ws_wired: dict, db: Session) -> None:
    gateway = ws_wired["gateway"]
    with connect(ws_wired["client"], call_headers("call_ok")) as ws:
        ws.send_json({"event": "transcript", "is_final": False, "text": "add mi", "turn_id": 1})  # interim: no answer
        events = say(ws, "add milk")
    assert [e.get("delta") for e in events if e.get("delta")] == ["Added ", "milk."]
    assert events[-1] == {"event": "text", "done": True, "turn_id": 1}
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["agent_id"] == "agent_alex"
    assert gateway.calls[0]["text"] == "add milk"
    assert gateway.calls[0]["conversation_id"].endswith("-call")
    assert (
        db.scalars(select(ActivityEvent).where(ActivityEvent.event_type == "channel.inbound")).one().payload["call_id"]
        == "call_ok"
    )


def test_ws_gateway_without_streaming_still_answers(ws_wired: dict) -> None:
    ws_wired["gateway"] = SyncOnlyGateway("Sure thing.")
    with connect(ws_wired["client"], call_headers("call_sync")) as ws:
        events = say(ws, "hello")
    assert [e["delta"] for e in events if e.get("delta")] == ["Sure thing."]


def test_ws_unbound_caller_is_refused_and_hung_up(ws_wired: dict, db: Session) -> None:
    ws_wired["inkbox"] = FakeInkbox(remote_number=STRANGER_NUMBER)
    with connect(ws_wired["client"], call_headers("call_bad", phone=STRANGER_NUMBER)) as ws:
        events = [ws.receive_json() for _ in range(3)]
    assert events[0]["event"] == "text" and "can only talk" in events[0]["delta"]
    assert events[-1] == {"event": "stop", "reason": "agent_hang_up"}
    assert ws_wired["gateway"].calls == []
    assert (
        db.scalars(select(ActivityEvent).where(ActivityEvent.event_type == "channel.rejected")).one().payload["reason"]
        == "unbound_sender"
    )


def test_ws_household_member_who_is_not_the_owner_is_refused(ws_wired: dict) -> None:
    ws_wired["inkbox"] = FakeInkbox(remote_number=JORDAN_NUMBER)
    with connect(ws_wired["client"], call_headers("call_katie", phone=JORDAN_NUMBER)) as ws:
        assert ws.receive_json()["event"] == "text"
    assert ws_wired["gateway"].calls == []


def test_ws_caller_id_comes_only_from_the_call_record(ws_wired: dict) -> None:
    """The signed context's `phone_number` is not trusted: a record that names a stranger wins, and no record fails."""
    for remote in (STRANGER_NUMBER, None):
        ws_wired["inkbox"] = FakeInkbox(remote_number=remote)
        with connect(ws_wired["client"], call_headers(f"call_ctx_{remote}", phone=ALEX_NUMBER)) as ws:
            assert "can only talk" in ws.receive_json()["delta"]
        channels.reset_call_registry()
    assert ws_wired["gateway"].calls == []


PIN = "4821"


def _bind_pin(db: Session, number: str = ALEX_NUMBER, pin: str = PIN) -> None:
    from app.models import ProviderBinding
    from app.services import channel_service

    row = db.scalars(select(ProviderBinding).where(ProviderBinding.external_id == number)).first()
    row.config = {**row.config, "pin_hash": channel_service.hash_pin(pin)}
    db.commit()


def _heard(ws: Any) -> str:
    return "".join(e.get("delta", "") for e in _drain(ws))


def _drain(ws: Any) -> list[dict[str, Any]]:
    out = []
    while True:
        event = ws.receive_json()
        out.append(event)
        if event.get("done") is True or event.get("event") == "stop":
            return out


def test_ws_pin_set_caller_is_asked_and_wrong_pin_never_reaches_the_agent(ws_wired: dict, db: Session) -> None:
    _bind_pin(db)
    with connect(ws_wired["client"], call_headers("call_pin1")) as ws:
        assert "pin" in _heard(ws).lower()  # asked up front
        ws.send_json({"event": "transcript", "is_final": True, "text": "add milk please", "turn_id": 1})
        assert "pin" in _heard(ws).lower()  # not a PIN: asked again, and the agent never saw it
    assert ws_wired["gateway"].calls == []


def test_ws_three_wrong_pins_hang_up(ws_wired: dict, db: Session) -> None:
    _bind_pin(db)
    with connect(ws_wired["client"], call_headers("call_pin2")) as ws:
        _drain(ws)
        for n in range(3):
            ws.send_json({"event": "transcript", "is_final": True, "text": "1 1 1 1", "turn_id": n + 1})
            _drain(ws)
        assert ws.receive_json() == {"event": "stop", "reason": "agent_hang_up"}
    assert ws_wired["gateway"].calls == []


@pytest.mark.parametrize("spoken", ["4821", "four eight two one", "my pin is 4 8 2 1"])
def test_ws_correct_pin_passes_then_the_agent_answers(ws_wired: dict, db: Session, spoken: str) -> None:
    _bind_pin(db)
    with connect(ws_wired["client"], call_headers("call_pin3")) as ws:
        _drain(ws)
        ws.send_json({"event": "transcript", "is_final": True, "text": spoken, "turn_id": 1})
        _drain(ws)
        events = say(ws, "add milk", turn_id=2)
    assert [e.get("delta") for e in events if e.get("delta")] == ["Added ", "milk."]
    assert [c["text"] for c in ws_wired["gateway"].calls] == ["add milk"]  # the PIN never went to the model


def _pin_config(db: Session) -> dict:
    from app.models import ProviderBinding

    db.expire_all()
    return db.scalars(select(ProviderBinding).where(ProviderBinding.external_id == ALEX_NUMBER)).first().config


def _wrong_pins(client: TestClient, call_id: str, times: int) -> None:
    with connect(client, call_headers(call_id)) as ws:
        _drain(ws)
        for n in range(times):
            ws.send_json({"event": "transcript", "is_final": True, "text": "1 1 1 1", "turn_id": n + 1})
            _drain(ws)


def test_ws_failed_pins_add_up_across_calls_and_lock_the_binding(ws_wired: dict, db: Session) -> None:
    _bind_pin(db)
    _wrong_pins(ws_wired["client"], "call_lock1", 3)  # hung up after 3
    _wrong_pins(ws_wired["client"], "call_lock2", 2)  # 5 in total: locked
    assert _pin_config(db)["pin_failures"] == 5
    # even the right PIN on a fresh call is refused now; the agent is never reached
    with connect(ws_wired["client"], call_headers("call_lock3")) as ws:
        assert "can only talk" in ws.receive_json()["delta"]
    assert ws_wired["gateway"].calls == []
    kinds = [e.event_type for e in db.scalars(select(ActivityEvent).order_by(ActivityEvent.created_at))]
    assert kinds.count("channel.pin_failed") == 4 and kinds.count("channel.pin_locked") == 1


def test_a_right_pin_never_clears_a_lock_in_place(db: Session, family: dict) -> None:
    from app.models import ProviderBinding
    from app.services import channel_service

    row = db.scalars(select(ProviderBinding).where(ProviderBinding.external_id == ALEX_NUMBER)).first()
    row.config = {**row.config, "pin_hash": channel_service.hash_pin(PIN)}
    db.commit()
    for _ in range(channel_service.PIN_MAX_FAILURES):
        channel_service.attempt_pin(db, row.id, "1111")
    ok, locked = channel_service.attempt_pin(db, row.id, PIN)  # the right PIN, but the lock is already on
    assert (ok, locked) == (False, True)
    assert channel_service.pin_locked(row)


def test_old_style_lock_without_an_end_time_stays_locked(db: Session, family: dict) -> None:
    from app.models import ProviderBinding
    from app.services import channel_service

    row = db.scalars(select(ProviderBinding).where(ProviderBinding.external_id == ALEX_NUMBER)).first()
    row.config = {**row.config, "pin_hash": channel_service.hash_pin(PIN), "pin_failures": 5}
    db.commit()
    assert channel_service.pin_locked(row)
    assert channel_service.attempt_pin(db, row.id, PIN) == (False, True)


def test_pin_lock_expires_and_a_right_pin_then_works(db: Session, family: dict) -> None:
    from datetime import timedelta

    from app.db.base import utcnow
    from app.models import ProviderBinding
    from app.services import channel_service

    row = db.scalars(select(ProviderBinding).where(ProviderBinding.external_id == ALEX_NUMBER)).first()
    row.config = {**row.config, "pin_hash": channel_service.hash_pin(PIN)}
    db.commit()
    for _ in range(channel_service.PIN_MAX_FAILURES):
        channel_service.attempt_pin(db, row.id, "1111")
    row.config = {**row.config, "pin_locked_until": (utcnow() - timedelta(seconds=1)).isoformat()}
    db.commit()
    assert not channel_service.pin_locked(row)
    assert channel_service.attempt_pin(db, row.id, PIN) == (True, False)
    assert "pin_failures" not in row.config and "pin_locked_until" not in row.config


def test_ws_right_pin_resets_the_failure_count(ws_wired: dict, db: Session) -> None:
    _bind_pin(db)
    _wrong_pins(ws_wired["client"], "call_reset1", 2)
    assert _pin_config(db)["pin_failures"] == 2
    with connect(ws_wired["client"], call_headers("call_reset2")) as ws:
        _drain(ws)
        ws.send_json({"event": "transcript", "is_final": True, "text": PIN, "turn_id": 1})
        _drain(ws)
    assert _pin_config(db).get("pin_failures", 0) == 0


def test_ws_without_a_pin_set_caller_id_alone_still_works(ws_wired: dict) -> None:
    with connect(ws_wired["client"], call_headers("call_nopin")) as ws:
        events = say(ws, "add milk")
    assert [e.get("delta") for e in events if e.get("delta")] == ["Added ", "milk."]


def test_ws_outbound_direction_without_intent_hangs_up(ws_wired: dict) -> None:
    with connect(ws_wired["client"], call_headers("call_out", direction="outbound")) as ws:
        events = [ws.receive_json() for _ in range(3)]
    assert events[-1]["event"] == "stop"
    assert ws_wired["gateway"].calls == []


def test_ws_barge_in_sends_clear(ws_wired: dict) -> None:
    with connect(ws_wired["client"], call_headers("call_barge")) as ws:
        ws.send_json({"event": "barge_in"})
        assert ws.receive_json() == {"event": "clear"}


def test_ws_replayed_handshake_same_call_id_rejected(ws_wired: dict) -> None:
    headers = call_headers("call_replay")
    with connect(ws_wired["client"], headers) as ws:
        say(ws, "hi")
    with pytest.raises(WebSocketDisconnect) as caught, connect(ws_wired["client"], headers):
        pass
    assert caught.value.code == 1008
    # a fresh signature over the same call id is a replay of the call, not a new call
    with pytest.raises(WebSocketDisconnect) as again, connect(ws_wired["client"], call_headers("call_replay")):
        pass
    assert again.value.code == 1008
    assert len(ws_wired["gateway"].calls) == 1


def test_ws_second_concurrent_session_same_call_id_rejected(ws_wired: dict) -> None:
    client = ws_wired["client"]
    with connect(client, call_headers("call_dup")) as first:
        with pytest.raises(WebSocketDisconnect) as caught, connect(client, call_headers("call_dup")):
            pass
        assert caught.value.code == 1008
        assert [e.get("delta") for e in say(first, "still here") if e.get("delta")] == [
            "Added ",
            "milk.",
        ]  # first lives


def test_call_registry_forgets_after_the_replay_window() -> None:
    assert channels.claim_call("c1", now=0.0)
    assert not channels.claim_call("c1", now=1.0)
    channels.release_call("c1")
    assert not channels.claim_call("c1", now=10.0)
    assert channels.claim_call("c2", now=channels.REPLAY_MEMORY_S * 10)  # prunes old, unrelated entries


# --- secrets ------------------------------------------------------------------------------------------------------


def test_no_secret_in_any_response_or_log(
    ws_wired: dict, db: Session, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INKBOX_WEBHOOK_AUTH_TOKEN", BEARER_SENTINEL)
    get_settings.cache_clear()
    client: TestClient = ws_wired["client"]
    caplog.set_level(logging.DEBUG)
    raw = json.dumps(imessage_envelope()).encode()
    texts = []
    for headers in (
        {},
        sign(raw),
        {**sign(raw, "wrong"), "Authorization": f"Bearer {BEARER_SENTINEL}"},
        {**sign(raw), "Authorization": "Bearer nope"},
        {**sign(raw), "Authorization": f"Bearer {BEARER_SENTINEL}"},
    ):
        texts.append(client.post(URL, content=raw, headers=headers).text)
    texts.append(client.post("/webhooks/inkbox/nobody", content=raw, headers=sign(raw)).text)
    for headers in (call_headers(key="wrong"), call_headers("call_s")):
        try:
            with connect(client, headers) as ws:
                say(ws, "hello")
        except WebSocketDisconnect:
            pass
    everything = (
        "\n".join(texts)
        + "\n"
        + caplog.text
        + json.dumps(client.get("/api/providers", headers={"X-Uhlexa-User": "alex"}).json())
    )
    for secret in (SIGNING_KEY, BEARER_SENTINEL, API_KEY_SENTINEL):
        assert secret not in everything
    stored = json.dumps([(e.payload, e.summary) for e in db.scalars(select(ActivityEvent))])
    for secret in (SIGNING_KEY, BEARER_SENTINEL, API_KEY_SENTINEL):
        assert secret not in stored
