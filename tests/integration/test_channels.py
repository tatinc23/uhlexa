"""Channel bridge (plan Task 17): who may speak to an agent, what the Keiki gateway is asked, and what comes back.

Fakes only: a fake gateway and a fake Inkbox SDK service. The job handler is driven directly on stored webhook rows."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.channel_principal import UnknownSender, resolve
from app.auth.demo_auth import Principal
from app.channels.types import InboundTurn
from app.jobs import channel_handlers
from app.models import ActivityEvent, BindingStatus, ProviderName, WebhookEvent, WebhookStatus
from app.providers.inkbox import RateLimited
from app.providers.keiki import KeikiBusy, KeikiTurnFailed, conversation_id_for
from app.services import channel_service
from app.settings import Settings
from tests.helpers_channels import (
    ALEX_HANDLE,
    ALEX_NUMBER,
    JORDAN_HANDLE,
    JORDAN_NUMBER,
    MAX_HANDLE,
    MAX_NUMBER,
    STRANGER_NUMBER,
    FakeGateway,
    FakeInkbox,
    bind,
    bind_family,
    imessage_envelope,
)


@pytest.fixture
def family(db: Session, seeded: dict[str, Principal]) -> dict[str, Principal]:
    bind_family(db, seeded)
    return seeded


@pytest.fixture
def cfg(settings_env: Settings) -> Settings:
    return settings_env.model_copy(
        update={"keiki_agent_ids": {"alex": "agent_alex", "katie": "agent_katie"}, "keiki_daily_turn_cap": 200}
    )


def turn(
    sender: str = ALEX_NUMBER, handle: str = ALEX_HANDLE, text: str = "hello", channel: str = "inkbox:imessage"
) -> InboundTurn:
    return InboundTurn(channel, "evt_1", handle, sender, text, reply_ctx={"handle": handle})


def events(db: Session, event_type: str) -> list[ActivityEvent]:
    return list(db.scalars(select(ActivityEvent).where(ActivityEvent.event_type == event_type)))


# --- identity -----------------------------------------------------------------------------------------------------


def test_owner_resolves_to_their_principal(db: Session, family: dict[str, Principal]) -> None:
    principal = resolve(db, turn())
    assert principal.user.handle == "alex"
    assert principal.household.id == family["alex"].household.id


def test_unbound_sender_gets_nothing_and_logs(db: Session, family: dict[str, Principal], cfg: Settings) -> None:
    gateway = FakeGateway()
    assert channel_service.handle_inbound(db, turn(sender=STRANGER_NUMBER), gateway=gateway, settings=cfg) is None
    assert gateway.calls == []
    rejected = events(db, "channel.rejected")
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "unbound_sender"
    assert STRANGER_NUMBER not in str(rejected[0].payload) + rejected[0].summary  # a digest, never the number


def test_unknown_agent_handle_gets_nothing(db: Session, family: dict[str, Principal], cfg: Settings) -> None:
    gateway = FakeGateway()
    assert channel_service.handle_inbound(db, turn(handle="nobody"), gateway=gateway, settings=cfg) is None
    assert gateway.calls == []


def test_non_owner_household_member_cannot_use_other_agent(
    db: Session, family: dict[str, Principal], cfg: Settings
) -> None:
    """Katie is a bound adult in Alex's household, but Alex's agent acts with Alex's authority."""
    gateway = FakeGateway()
    with pytest.raises(UnknownSender) as caught:
        resolve(db, turn(sender=JORDAN_NUMBER, handle=ALEX_HANDLE))
    assert caught.value.reason == "sender_not_owner"
    assert channel_service.handle_inbound(db, turn(sender=JORDAN_NUMBER), gateway=gateway, settings=cfg) is None
    assert gateway.calls == []
    assert events(db, "channel.rejected")[0].payload["reason"] == "sender_not_owner"
    # ...but Katie can use Katie's own agent.
    assert resolve(db, turn(sender=JORDAN_NUMBER, handle=JORDAN_HANDLE)).user.handle == "katie"


def test_children_never_resolve(db: Session, family: dict[str, Principal]) -> None:
    with pytest.raises(UnknownSender) as caught:
        resolve(db, turn(sender=MAX_NUMBER, handle=MAX_HANDLE))
    assert caught.value.reason == "child"


def test_inactive_binding_does_not_resolve(db: Session, family: dict[str, Principal]) -> None:
    bind(db, family["alex"], "inkbox_phone", "+15550001111", status=BindingStatus.INACTIVE)
    with pytest.raises(UnknownSender):
        resolve(db, turn(sender="+15550001111"))


def test_unverified_email_sender_rejected(db: Session, family: dict[str, Principal], cfg: Settings) -> None:
    """An email From address is forgeable and Inkbox exposes no verified-sender signal: email fails closed, even from
    a bound address, and nothing is sent."""
    bind(db, family["alex"], "inkbox_email", "alex@example.com")
    email = InboundTurn(
        "inkbox:email", "e1", ALEX_HANDLE, "alex@example.com", "Please add milk", reply_ctx={"subject": "Groceries"}
    )
    with pytest.raises(UnknownSender) as caught:
        resolve(db, email)
    assert caught.value.reason == "unverified_sender"
    gateway = FakeGateway()
    assert channel_service.handle_inbound(db, email, gateway=gateway, settings=cfg) is None
    assert gateway.calls == []
    assert events(db, "channel.rejected")[0].payload["reason"] == "unverified_sender"
    with pytest.raises(UnknownSender):  # nor does an email address vouch on a text channel's phone binding
        resolve(db, turn(sender="alex@example.com", channel="inkbox:sms"))


# --- the bridge ---------------------------------------------------------------------------------------------------


def test_inkbox_text_calls_gateway_with_stable_conversation_id_and_replies(
    db: Session, family: dict[str, Principal], cfg: Settings
) -> None:
    gateway = FakeGateway(reply="You have bread and bananas.")
    first = channel_service.handle_inbound(db, turn(text="what is on the list?"), gateway=gateway, settings=cfg)
    second = channel_service.handle_inbound(db, turn(text="and now?"), gateway=gateway, settings=cfg)
    assert first == second == "You have bread and bananas."
    expected = conversation_id_for(str(family["alex"].user.id), "imsg")
    assert [c["conversation_id"] for c in gateway.calls] == [expected, expected]
    assert gateway.calls[0]["agent_id"] == "agent_alex"
    assert gateway.calls[0]["text"] == "what is on the list?"
    assert "Alex" in gateway.calls[0]["system"]
    # Another channel is another conversation; another person is another conversation.
    assert conversation_id_for(str(family["alex"].user.id), "sms") != expected
    assert len(events(db, "channel.inbound")) == 2


def test_gateway_error_gives_fixed_reply(db: Session, family: dict[str, Principal], cfg: Settings) -> None:
    failing = FakeGateway(error=KeikiTurnFailed("the turn failed", 500))
    reply = channel_service.handle_inbound(db, turn(), gateway=failing, settings=cfg)
    assert reply == channel_service.ERROR_REPLY
    assert events(db, "channel.error")[0].payload["error"] == "KeikiTurnFailed"
    busy = FakeGateway(error=KeikiBusy("busy", 409))
    assert channel_service.handle_inbound(db, turn(), gateway=busy, settings=cfg) == channel_service.BUSY_REPLY
    crashing = FakeGateway(error=RuntimeError("boom with secret-ish detail"))
    assert channel_service.handle_inbound(db, turn(), gateway=crashing, settings=cfg) == channel_service.ERROR_REPLY
    assert "secret-ish" not in str([e.payload for e in events(db, "channel.error")])


def test_reply_is_capped_at_1000_chars(db: Session, family: dict[str, Principal], cfg: Settings) -> None:
    reply = channel_service.handle_inbound(db, turn(), gateway=FakeGateway(reply="x" * 4000), settings=cfg)
    assert reply is not None
    assert len(reply) == channel_service.MAX_REPLY_CHARS


def test_daily_cap_fails_closed_without_gateway_call(db: Session, family: dict[str, Principal], cfg: Settings) -> None:
    capped = cfg.model_copy(update={"keiki_daily_turn_cap": 1})
    gateway = FakeGateway()
    assert channel_service.handle_inbound(db, turn(), gateway=gateway, settings=capped) == "You have bread and bananas."
    assert channel_service.handle_inbound(db, turn(), gateway=gateway, settings=capped) == channel_service.CAP_REPLY
    assert len(gateway.calls) == 1


def test_missing_agent_id_is_a_fixed_reply_not_an_error(
    db: Session, family: dict[str, Principal], cfg: Settings
) -> None:
    bare = cfg.model_copy(update={"keiki_agent_ids": {}})
    gateway = FakeGateway()
    assert (
        channel_service.handle_inbound(db, turn(), gateway=gateway, settings=bare) == channel_service.NOT_SET_UP_REPLY
    )
    assert gateway.calls == []


# --- the job handler ----------------------------------------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, cfg: Settings) -> tuple[FakeGateway, FakeInkbox]:
    gateway, inkbox = FakeGateway(), FakeInkbox()
    monkeypatch.setattr(channel_handlers, "make_gateway", lambda: gateway)
    monkeypatch.setattr(channel_handlers, "make_inkbox", lambda: inkbox)
    monkeypatch.setattr(channel_handlers, "get_settings", lambda: cfg)
    return gateway, inkbox


def store(db: Session, envelope: dict) -> dict:
    row = WebhookEvent(
        provider=ProviderName.INKBOX,
        external_event_id=envelope["id"],
        event_type=envelope["event_type"],
        signature_valid=True,
        payload=envelope,
    )
    db.add(row)
    db.commit()
    return {"webhook_event_id": str(row.id), "handle": ALEX_HANDLE}


def test_job_replies_on_the_same_channel_with_envelope_id_as_idempotency_key(
    db: Session, family: dict[str, Principal], wired: tuple[FakeGateway, FakeInkbox]
) -> None:
    gateway, inkbox = wired
    payload = store(db, imessage_envelope("evt_77"))
    channel_handlers.handle_inkbox_event(db, payload)
    assert inkbox.sent == [
        (
            "imessage",
            ALEX_HANDLE,
            {"text": "You have bread and bananas.", "idempotency_key": "evt_77", "to": ALEX_NUMBER},
        )
    ]
    row = db.scalars(select(WebhookEvent)).one()
    assert row.status == WebhookStatus.PROCESSED
    assert len(gateway.calls) == 1
    assert len(events(db, "channel.reply_sent")) == 1


def test_job_sends_nothing_for_an_unknown_sender(
    db: Session, family: dict[str, Principal], wired: tuple[FakeGateway, FakeInkbox]
) -> None:
    gateway, inkbox = wired
    channel_handlers.handle_inkbox_event(db, store(db, imessage_envelope(sender=STRANGER_NUMBER)))
    assert inkbox.sent == []
    assert gateway.calls == []
    assert db.scalars(select(WebhookEvent)).one().status == WebhookStatus.IGNORED
    assert len(events(db, "channel.rejected")) == 1


def test_job_never_reruns_the_model_after_it_started(
    db: Session, family: dict[str, Principal], wired: tuple[FakeGateway, FakeInkbox]
) -> None:
    gateway, inkbox = wired
    payload = store(db, imessage_envelope())
    row = db.scalars(select(WebhookEvent)).one()
    row.payload = {**row.payload, channel_handlers.META_KEY: {"stage": "started"}}
    db.commit()
    channel_handlers.handle_inkbox_event(db, payload)
    assert gateway.calls == []
    assert inkbox.sent == []
    assert db.scalars(select(WebhookEvent)).one().status == WebhookStatus.FAILED


def test_job_resends_a_saved_reply_without_a_new_model_turn(
    db: Session, family: dict[str, Principal], wired: tuple[FakeGateway, FakeInkbox]
) -> None:
    gateway, inkbox = wired
    payload = store(db, imessage_envelope("evt_9"))
    row = db.scalars(select(WebhookEvent)).one()
    row.payload = {**row.payload, channel_handlers.META_KEY: {"stage": "replied", "reply": "saved reply"}}
    db.commit()
    channel_handlers.handle_inkbox_event(db, payload)
    assert gateway.calls == []
    assert inkbox.sent[0][2]["text"] == "saved reply"
    assert inkbox.sent[0][2]["idempotency_key"] == "evt_9"


def test_inkbox_refusal_is_final_and_not_retried(
    db: Session, family: dict[str, Principal], monkeypatch: pytest.MonkeyPatch, cfg: Settings
) -> None:
    monkeypatch.setattr(channel_handlers, "make_gateway", FakeGateway)
    monkeypatch.setattr(
        channel_handlers, "make_inkbox", lambda: FakeInkbox(send_error=RateLimited("rate limited by Inkbox"))
    )
    monkeypatch.setattr(channel_handlers, "get_settings", lambda: cfg)
    channel_handlers.handle_inkbox_event(db, store(db, imessage_envelope()))  # no exception: the job must not retry
    assert db.scalars(select(WebhookEvent)).one().status == WebhookStatus.FAILED


def test_sms_and_email_reply_shapes(db: Session, family: dict[str, Principal]) -> None:
    inkbox = FakeInkbox()
    sms = InboundTurn("inkbox:sms", "e", ALEX_HANDLE, ALEX_NUMBER, "hi", reply_ctx={"conversation_id": ""})
    channel_handlers.send_reply(inkbox, sms, "ok", "e")
    mail = InboundTurn(
        "inkbox:email", "e2", ALEX_HANDLE, "a@example.com", "hi", reply_ctx={"subject": "Plan", "message_id": "m1"}
    )
    channel_handlers.send_reply(inkbox, mail, "ok", "e2")
    assert inkbox.sent[0] == ("sms", ALEX_HANDLE, {"text": "ok", "idempotency_key": "e", "to": ALEX_NUMBER})
    assert inkbox.sent[1][2]["subject"] == "Re: Plan"
    assert inkbox.sent[1][2]["in_reply_to_message_id"] == "m1"


def test_reply_goes_to_checked_sender_not_conversation(
    db: Session, family: dict[str, Principal], wired: tuple[FakeGateway, FakeInkbox]
) -> None:
    _, inkbox = wired
    channel_handlers.handle_inkbox_event(db, store(db, imessage_envelope(conversation="conv_with_someone_else")))
    kwargs = inkbox.sent[0][2]
    assert kwargs["to"] == ALEX_NUMBER
    assert "conversation_id" not in kwargs
    sms = InboundTurn("inkbox:sms", "e", ALEX_HANDLE, ALEX_NUMBER, "hi", reply_ctx={"conversation_id": "conv_x"})
    channel_handlers.send_reply(inkbox, sms, "ok", "e")
    assert inkbox.sent[1][2] == {"text": "ok", "idempotency_key": "e", "to": ALEX_NUMBER}


def test_group_conversation_turn_ignored(
    db: Session, family: dict[str, Principal], wired: tuple[FakeGateway, FakeInkbox]
) -> None:
    gateway, inkbox = wired
    group = imessage_envelope("evt_g")
    group["data"]["message"]["is_group"] = True
    channel_handlers.handle_inkbox_event(db, store(db, group))
    sms_group = {
        "id": "evt_s",
        "event_type": "text.received",
        "data": {
            "text_message": {
                "direction": "inbound",
                "remote_phone_number": ALEX_NUMBER,
                "text": "hi",
                "conversation_id": "c",
            },
            "recipient_phone_number": "+15550002222",
        },
    }
    channel_handlers.handle_inkbox_event(db, store(db, sms_group))
    assert gateway.calls == [] and inkbox.sent == []
    rows = db.scalars(select(WebhookEvent)).all()
    assert [r.status for r in rows] == [WebhookStatus.IGNORED, WebhookStatus.IGNORED]
