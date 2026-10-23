"""Email to an agent through Primitive: who gets answered, what the Keiki gateway is asked, and what is kept.

Fakes only (FakePrimitive, FakeGateway). The `primitive_sync` handler is driven directly on stored webhook rows."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import ProviderUnavailable
from app.jobs import primitive_email, worker
from app.models import (
    ActivityEvent,
    BindingStatus,
    Conversation,
    ProviderBinding,
    ProviderName,
    SenderType,
    TranscriptProvider,
    Visibility,
    WebhookEvent,
    WebhookStatus,
)
from app.providers.primitive.models import PrimitiveMessage
from app.settings import Settings
from tests.helpers_channels import FakeGateway
from tests.helpers_services import FakePrimitive

ALEX_INBOX = "alex@agentmail.tatinc.us"
ALEX_EMAIL = "ken@example.com"
JORDAN_EMAIL = "katie@example.com"


def bind(db: Session, principal: Principal, binding_type: str, external_id: str) -> None:
    db.add(
        ProviderBinding(
            household_id=principal.household.id,
            user_id=principal.user.id,
            agent_id=principal.agent.id,
            provider=ProviderName.PRIMITIVE,
            binding_type=binding_type,
            external_id=external_id,
            status=BindingStatus.ACTIVE,
            config={"consent": "test"},
        )
    )
    db.commit()


@pytest.fixture
def family(db: Session, seeded: dict[str, Principal]) -> dict[str, Principal]:
    bind(db, seeded["alex"], "primitive_inbox", ALEX_INBOX)
    bind(db, seeded["alex"], "primitive_email", ALEX_EMAIL)
    bind(db, seeded["katie"], "primitive_email", JORDAN_EMAIL)
    return seeded


@pytest.fixture
def cfg(settings_env: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    cfg = settings_env.model_copy(update={"keiki_agent_ids": {"alex": "agent_alex"}, "keiki_daily_turn_cap": 200})
    monkeypatch.setattr(primitive_email, "get_settings", lambda: cfg)
    return cfg


def email(
    sender: str = ALEX_EMAIL, *, verified: bool = True, to: str = ALEX_INBOX, email_id: str = "em_1"
) -> PrimitiveMessage:
    return PrimitiveMessage(
        email_id, "inbound", "thr_1", sender, "Sunday", "What's on the grocery list?", "2026-10-23",
        recipient=to, from_verified=verified,
    )  # fmt: skip


def run(
    db: Session, monkeypatch: pytest.MonkeyPatch, message: PrimitiveMessage, *, gateway=None, primitive=None
) -> tuple[WebhookEvent, FakeGateway, FakePrimitive]:
    gateway = gateway or FakeGateway(reply="Milk, eggs and bread.")
    primitive = primitive or FakePrimitive(messages={message.id: message})
    monkeypatch.setattr(primitive_email, "make_gateway", lambda: gateway)
    monkeypatch.setattr(worker, "make_primitive", lambda: primitive)
    row = db.scalars(select(WebhookEvent).where(WebhookEvent.external_event_id == "evt_1")).first()
    if row is None:
        row = WebhookEvent(
            provider=ProviderName.PRIMITIVE,
            external_event_id="evt_1",
            event_type="email.received",
            signature_valid=True,
            payload={"id": "evt_1", "event": "email.received", "email": {"id": message.id, "thread_id": "thr_1"}},
        )
        db.add(row)
        db.commit()
    worker.handle_primitive_sync(db, {"webhook_event_id": str(row.id)})
    db.commit()
    db.refresh(row)
    return row, gateway, primitive


def rejected(db: Session) -> list[ActivityEvent]:
    return list(db.scalars(select(ActivityEvent).where(ActivityEvent.event_type == "channel.rejected")))


def test_verified_owner_email_is_answered_on_the_same_thread(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, gateway, primitive = run(db, monkeypatch, email())
    assert row.status == WebhookStatus.PROCESSED
    assert [c["agent_id"] for c in gateway.calls] == ["agent_alex"]
    assert gateway.calls[0]["text"].startswith("Subject: Sunday")
    assert gateway.calls[0]["conversation_id"].endswith("-mail")
    assert [(e, b) for e, b, _ in primitive.email_replies] == [("em_1", "Milk, eggs and bread.")]  # that email only
    assert primitive.replies == []  # never a thread-wide search for something to answer
    assert primitive.idempotency_keys == ["uhlexa-evt_1"]


def test_the_email_thread_becomes_a_private_conversation_with_both_emails(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(db, monkeypatch, email())
    conversation = db.scalars(select(Conversation).where(Conversation.primitive_thread_id == "thr_1")).one()
    assert conversation.visibility == Visibility.PRIVATE
    assert conversation.transcript_provider == TranscriptProvider.PRIMITIVE
    assert conversation.created_by_user_id == family["alex"].user.id
    kinds = [(m.sender_type, m.source, m.primitive_message_id) for m in conversation.messages]
    assert kinds == [
        (SenderType.USER, TranscriptProvider.PRIMITIVE, "em_1"),
        (SenderType.AGENT, TranscriptProvider.PRIMITIVE, "prim-msg-1"),
    ]


def test_unverified_from_address_is_never_answered(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, gateway, primitive = run(db, monkeypatch, email(verified=False))
    assert row.status == WebhookStatus.IGNORED
    assert gateway.calls == [] and primitive.email_replies == []
    assert [e.payload["reason"] for e in rejected(db)] == ["unverified_sender"]
    assert ALEX_EMAIL not in str(rejected(db)[0].payload)


def test_another_household_member_cannot_use_someone_elses_agent(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, gateway, primitive = run(db, monkeypatch, email(JORDAN_EMAIL))
    assert row.status == WebhookStatus.IGNORED
    assert gateway.calls == [] and primitive.email_replies == []
    assert [e.payload["reason"] for e in rejected(db)] == ["sender_not_owner"]


def test_a_stranger_is_ignored(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, gateway, _ = run(db, monkeypatch, email("someone@else.com"))
    assert row.status == WebhookStatus.IGNORED and gateway.calls == []
    assert [e.payload["reason"] for e in rejected(db)] == ["unbound_sender"]


def test_email_to_an_address_that_is_no_agent_falls_back_to_thread_rules(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, gateway, _ = run(db, monkeypatch, email(to="someone@agentmail.tatinc.us"))
    assert row.status == WebhookStatus.IGNORED and "no conversation" in (row.error or "")
    assert gateway.calls == [] and rejected(db) == []


def test_never_reruns_the_model_after_it_started(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = email()
    row = WebhookEvent(
        provider=ProviderName.PRIMITIVE,
        external_event_id="evt_1",
        event_type="email.received",
        signature_valid=True,
        payload={"id": "evt_1", "event": "email.received", "email": {"id": "em_1", "thread_id": "thr_1"},
                 "_uhlexa": {"stage": "started"}},
    )  # fmt: skip
    db.add(row)
    db.commit()
    row, gateway, primitive = run(db, monkeypatch, message)
    assert row.status == WebhookStatus.FAILED and gateway.calls == [] and primitive.replies == []


def test_a_send_outage_retries_the_send_without_a_new_model_turn(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = email()

    class DownOnReply(FakePrimitive):
        def reply_to_email(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise ProviderUnavailable("primitive", "fake outage")

    with pytest.raises(ProviderUnavailable):
        run(db, monkeypatch, message, primitive=DownOnReply(messages={"em_1": message}))
    db.rollback()
    row = db.scalars(select(WebhookEvent).where(WebhookEvent.external_event_id == "evt_1")).one()
    assert row.payload["_uhlexa"]["stage"] == "replied"
    gateway = FakeGateway(reply="should not be asked")
    row, gateway, primitive = run(db, monkeypatch, message, gateway=gateway)
    assert row.status == WebhookStatus.PROCESSED and gateway.calls == []
    assert [b for _, b, _ in primitive.email_replies] == ["Milk, eggs and bread."]


def test_a_shared_conversation_on_the_thread_is_never_written_to(
    db: Session, family: dict[str, Principal], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A household conversation already linked to this thread: the private email exchange is not mirrored into it."""
    shared = db.scalars(select(Conversation).order_by(Conversation.created_at)).first()
    assert shared is not None and shared.visibility == Visibility.HOUSEHOLD
    shared.transcript_provider, shared.primitive_thread_id = TranscriptProvider.PRIMITIVE, "thr_1"
    db.commit()
    before = len(shared.messages)
    row, _, primitive = run(db, monkeypatch, email())
    assert row.status == WebhookStatus.PROCESSED and len(primitive.email_replies) == 1
    db.refresh(shared)
    assert len(shared.messages) == before


# --- scripts/channels_bind.py -------------------------------------------------------------------------------------

SPEC = importlib.util.spec_from_file_location(
    "channels_bind", Path(__file__).parents[2] / "scripts" / "channels_bind.py"
)
channels_bind = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(channels_bind)


def test_bind_script_binds_primitive_inbox_and_address(db: Session, seeded: dict[str, Principal]) -> None:
    assert "dry run" in channels_bind.bind(db, "email-inbox", "alex", ALEX_INBOX.upper(), consent=None, apply=False)
    channels_bind.bind(db, "email-inbox", "alex", ALEX_INBOX, consent=None, apply=True)
    channels_bind.bind(db, "email", "alex", ALEX_EMAIL, consent="Ken, test", apply=True)
    rows = db.scalars(select(ProviderBinding).where(ProviderBinding.provider == ProviderName.PRIMITIVE)).all()
    assert sorted((r.binding_type, r.external_id) for r in rows) == [
        ("primitive_email", ALEX_EMAIL),
        ("primitive_inbox", ALEX_INBOX),
    ]


@pytest.mark.parametrize(
    ("kind", "handle", "value", "consent"),
    [
        ("email", "alex", ALEX_EMAIL, None),  # no consent
        ("email", "alex", "not-an-address", "ok"),
        ("email-inbox", "max", "max@agentmail.tatinc.us", None),  # a child
    ],
)
def test_bind_script_refuses(
    db: Session, seeded: dict[str, Principal], kind: str, handle: str, value: str, consent: str | None
) -> None:
    with pytest.raises(SystemExit):
        channels_bind.bind(db, kind, handle, value, consent=consent, apply=True)
