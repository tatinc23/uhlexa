import hashlib
import hmac
import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.webhooks import MAX_BODY_BYTES
from app.models.binding import ProviderName
from app.models.job import Job, JobStatus
from app.models.webhook_event import WebhookEvent, WebhookStatus
from app.settings import Settings, get_settings

SECRET = "test-webhook-secret"
URL = "/webhooks/primitive"


@pytest.fixture
def primitive_on(settings_env: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ENABLE_PRIMITIVE", "true")
    monkeypatch.setenv("PRIMITIVE_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _event(event_id: str = "evt_1", raw_data: str | None = None) -> bytes:
    email: dict = {"id": "em_1", "thread_id": "th_1", "headers": {"subject": "Sunday groceries"}}
    if raw_data is not None:
        email["content"] = {"raw": {"included": True, "data": raw_data}}
    return json.dumps({"id": event_id, "event": "email.received", "email": email}).encode()


def _signed(body: bytes, *, secret: str = SECRET, ts: int | None = None) -> dict[str, str]:
    t = str(int(time.time()) if ts is None else ts)
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {
        "Primitive-Signature": f"t={t},v1={sig}",
        "X-Webhook-Event": "email.received",
        "Content-Type": "application/json",
    }


def _count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_valid_delivery_stores_event_and_enqueues_one_job(client: TestClient, db: Session, primitive_on: None) -> None:
    body = _event()
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    event = db.scalars(select(WebhookEvent)).one()
    assert (event.provider, event.external_event_id, event.event_type) == (
        ProviderName.PRIMITIVE,
        "evt_1",
        "email.received",
    )
    assert event.signature_valid is True
    assert event.status == WebhookStatus.RECEIVED
    assert event.payload["email"]["thread_id"] == "th_1"
    job = db.scalars(select(Job)).one()
    assert (job.kind, job.status) == ("primitive_sync", JobStatus.QUEUED)
    assert job.payload == {"webhook_event_id": str(event.id)}


def test_redelivery_is_deduped(client: TestClient, db: Session, primitive_on: None) -> None:
    body = _event()
    assert client.post(URL, content=body, headers=_signed(body)).json() == {"status": "accepted"}
    again = client.post(URL, content=body, headers=_signed(body))
    assert again.status_code == 200
    assert again.json() == {"status": "duplicate"}
    assert _count(db, WebhookEvent) == 1
    assert _count(db, Job) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Primitive-Signature": "t=1,v1=deadbeef"},
        "wrong_secret",
        "stale",
        "tampered",
    ],
)
def test_bad_signature_401_logged_without_payload_and_no_job(
    client: TestClient, db: Session, primitive_on: None, headers: dict[str, str] | str
) -> None:
    body = _event()
    sent = body
    if headers == "wrong_secret":
        headers = _signed(body, secret="not-the-secret")
    elif headers == "stale":
        headers = _signed(body, ts=int(time.time()) - 3600)
    elif headers == "tampered":
        headers = _signed(body)
        sent = body.replace(b"Sunday", b"Monday")
    response = client.post(URL, content=sent, headers=headers)
    assert response.status_code == 401
    event = db.scalars(select(WebhookEvent)).one()
    assert event.signature_valid is False
    assert event.payload == {"count": 1}
    assert _count(db, Job) == 0


def test_rejections_collapse_into_one_counter_row(client: TestClient, db: Session, primitive_on: None) -> None:
    body = _event()
    for _ in range(25):
        assert client.post(URL, content=body, headers={"Primitive-Signature": "t=1,v1=00"}).status_code == 401
    rows = db.scalars(select(WebhookEvent)).all()
    assert len(rows) == 1
    assert rows[0].payload == {"count": 25}
    assert rows[0].external_event_id.startswith("rejected:")


def test_chunked_body_without_content_length_is_capped(client: TestClient, db: Session, primitive_on: None) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(3):
            yield b"x" * (MAX_BODY_BYTES // 2)

    assert client.post(URL, content=chunks(), headers={"Primitive-Signature": "t=1,v1=00"}).status_code == 413
    assert _count(db, WebhookEvent) == 0


def test_signed_but_malformed_body_400_no_job(client: TestClient, db: Session, primitive_on: None) -> None:
    body = json.dumps({"event": "email.received"}).encode()
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 400
    assert _count(db, Job) == 0


def test_disabled_or_no_secret_is_503_and_stores_nothing(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _event()
    assert client.post(URL, content=body, headers=_signed(body)).status_code == 503
    monkeypatch.setenv("ENABLE_PRIMITIVE", "true")
    get_settings.cache_clear()
    assert client.post(URL, content=body, headers=_signed(body)).status_code == 503
    assert _count(db, WebhookEvent) == 0
    assert _count(db, Job) == 0


def test_oversized_body_413(client: TestClient, db: Session, primitive_on: None) -> None:
    body = b"x" * (MAX_BODY_BYTES + 1)
    assert client.post(URL, content=body, headers=_signed(body)).status_code == 413
    assert _count(db, WebhookEvent) == 0


def test_raw_email_bytes_are_not_stored(client: TestClient, db: Session, primitive_on: None) -> None:
    body = _event(raw_data="UmVjZWl2ZWQ6IGZyb20gc2VjcmV0")
    assert client.post(URL, content=body, headers=_signed(body)).status_code == 200
    event = db.scalars(select(WebhookEvent)).one()
    assert "data" not in event.payload["email"]["content"]["raw"]
    assert event.payload["email"]["content"]["raw"]["included"] is True


def test_secret_never_in_any_response(client: TestClient, primitive_on: None) -> None:
    body = _event()
    for headers in (_signed(body), {}, _signed(body, secret="nope")):
        assert SECRET not in client.post(URL, content=body, headers=headers).text
