"""`APPROVE|DENY <code>` by text: deterministic, owner-only, flag-gated, and never seen by the model."""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.channels.types import InboundTurn
from app.db.base import utcnow
from app.errors import Forbidden
from app.models import ApprovalStatus
from app.services import approval_service, channel_service, task_service
from app.settings import Settings
from tests.helpers_channels import (
    ALEX_HANDLE,
    ALEX_NUMBER,
    JORDAN_HANDLE,
    JORDAN_NUMBER,
    FakeGateway,
    bind_family,
)
from tests.helpers_services import real_snap


@pytest.fixture
def on(settings_env: Settings) -> Settings:
    return settings_env.model_copy(
        update={"keiki_agent_ids": {"alex": "agent_alex", "katie": "agent_katie"}, "enable_channel_approvals": True}
    )


@pytest.fixture
def pending(db: Session, seeded: dict[str, Principal]):
    """Katie's $600 purchase waits for a guardian (Alex); Katie may not approve their own overspend."""
    bind_family(db, seeded)
    result = task_service.create(
        db, seeded["katie"], title="TV", kind="purchase", amount_cents=60_000, source="rest", snap=real_snap()
    )
    db.commit()
    assert result.approval is not None
    return result.approval


@pytest.fixture
def code(db: Session, seeded: dict[str, Principal], pending) -> str:
    """The one-time code Alex was given (on the Family Board) for Katie's purchase."""
    issued = approval_service.issue_text_code(db, seeded["alex"], pending.id)
    db.commit()
    return issued


def text(
    body: str,
    sender: str = ALEX_NUMBER,
    handle: str = ALEX_HANDLE,
    channel: str = "inkbox:imessage",
    service: str | None = "imessage",
) -> InboundTurn:
    ctx = {"service": service} if service else {}
    return InboundTurn(channel, "e1", handle, sender, body, reply_ctx=ctx)


def test_approve_by_text_requires_flag_adult_owner_and_code(
    db: Session, pending, code: str, on: Settings, settings_env: Settings
) -> None:
    gateway = FakeGateway()
    # flag off: not a command (goes to the model, which has no approve tool), nothing is decided
    off = settings_env.model_copy(update=on.model_dump() | {"enable_channel_approvals": False})
    channel_service.handle_inbound(db, text(f"APPROVE {code}"), gateway=gateway, settings=off)
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING
    # wrong or partial code
    assert (
        channel_service.handle_inbound(db, text("APPROVE 00000000"), gateway=gateway, settings=on)
        == channel_service.NO_MATCH_REPLY
    )
    assert (
        channel_service.handle_inbound(db, text(f"APPROVE {code[:4]}"), gateway=FakeGateway(), settings=on)
        == "You have bread and bananas."
    )
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING
    # the requester cannot decide their own overspend, even by text, and the model is not asked
    quiet = FakeGateway()
    reply = channel_service.handle_inbound(
        db, text(f"approve {code}", JORDAN_NUMBER, JORDAN_HANDLE), gateway=quiet, settings=on
    )
    assert reply is not None and reply.startswith("I couldn't do that")
    assert quiet.calls == []
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING
    # a stranger or a non-owner cannot
    assert (
        channel_service.handle_inbound(db, text(f"APPROVE {code}", JORDAN_NUMBER), gateway=quiet, settings=on) is None
    )
    # the owner with the exact code can
    assert channel_service.handle_inbound(db, text(f"Approve {code}"), gateway=quiet, settings=on) == "Approved."
    db.refresh(pending)
    assert pending.status == ApprovalStatus.APPROVED


def test_approve_text_never_reaches_gateway(db: Session, pending, code: str, on: Settings) -> None:
    gateway = FakeGateway()
    channel_service.handle_inbound(db, text(f"DENY {code}"), gateway=gateway, settings=on)
    assert gateway.calls == []
    db.refresh(pending)
    assert pending.status == ApprovalStatus.DENIED


def test_free_text_approve_everything_does_nothing(db: Session, pending, code: str, on: Settings) -> None:
    gateway = FakeGateway()
    for body in ("approve everything", "APPROVE ALL", f"please approve {code}"):
        channel_service.handle_inbound(db, text(body), gateway=gateway, settings=on)
    assert len(gateway.calls) == 3  # all went to the model, which cannot approve
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING


def test_email_cannot_approve(db: Session, pending, code: str, on: Settings) -> None:
    gateway = FakeGateway()
    assert (
        channel_service.handle_inbound(
            db, text(f"APPROVE {code}", "alex@example.com", channel="inkbox:email"), gateway=gateway, settings=on
        )
        is None
    )
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING


def test_spoofed_sms_approve_does_nothing(db: Session, pending, code: str, on: Settings) -> None:
    """SMS sender ids can be forged, so even the right owner number with the right code decides nothing."""
    gateway = FakeGateway()
    reply = channel_service.handle_inbound(
        db, text(f"APPROVE {code}", channel="inkbox:sms", service=None), gateway=gateway, settings=on
    )
    assert reply == channel_service.TEXT_NOT_VERIFIED_REPLY
    assert gateway.calls == []
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING


@pytest.mark.parametrize("service", [None, "sms", "rcs"])
def test_imessage_event_not_marked_imessage_cannot_approve(
    db: Session, pending, code: str, on: Settings, service: str | None
) -> None:
    """iMessage falls back to SMS on the wire; only an event Inkbox says is `imessage` is a verified channel."""
    reply = channel_service.handle_inbound(
        db, text(f"APPROVE {code}", service=service), gateway=FakeGateway(), settings=on
    )
    assert reply == channel_service.TEXT_NOT_VERIFIED_REPLY
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING


def test_wrong_code_does_nothing_and_the_old_derived_code_is_dead(
    db: Session, pending, code: str, on: Settings
) -> None:
    gateway = FakeGateway()
    derived = str(pending.id)[:8]  # what the old scheme accepted
    wrong = "0" * 8 if code != "0" * 8 else "1" * 8
    for guess in (wrong, derived):
        assert (
            channel_service.handle_inbound(db, text(f"APPROVE {guess}"), gateway=gateway, settings=on)
            == channel_service.NO_MATCH_REPLY
        )
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING


def test_valid_imessage_code_approves_once_and_never_twice(db: Session, pending, code: str, on: Settings) -> None:
    gateway = FakeGateway()
    assert channel_service.handle_inbound(db, text(f"APPROVE {code}"), gateway=gateway, settings=on) == "Approved."
    db.refresh(pending)
    assert pending.status == ApprovalStatus.APPROVED
    assert (
        channel_service.handle_inbound(db, text(f"APPROVE {code}"), gateway=gateway, settings=on)
        == channel_service.NO_MATCH_REPLY
    )
    assert (
        channel_service.handle_inbound(db, text(f"DENY {code}"), gateway=gateway, settings=on)
        == channel_service.NO_MATCH_REPLY
    )
    db.refresh(pending)
    assert pending.status == ApprovalStatus.APPROVED


def test_code_is_stored_hashed_and_expires(db: Session, pending, code: str, on: Settings) -> None:
    db.refresh(pending)
    assert pending.text_code_hash and code not in pending.text_code_hash
    pending.text_code_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert (
        channel_service.handle_inbound(db, text(f"APPROVE {code}"), gateway=FakeGateway(), settings=on)
        == channel_service.NO_MATCH_REPLY
    )
    db.refresh(pending)
    assert pending.status == ApprovalStatus.PENDING


def test_a_new_code_replaces_the_old_one(
    db: Session, seeded: dict[str, Principal], pending, code: str, on: Settings
) -> None:
    newer = approval_service.issue_text_code(db, seeded["alex"], pending.id)
    db.commit()
    assert (
        channel_service.handle_inbound(db, text(f"APPROVE {code}"), gateway=FakeGateway(), settings=on)
        == channel_service.NO_MATCH_REPLY
    )
    assert (
        channel_service.handle_inbound(db, text(f"APPROVE {newer}"), gateway=FakeGateway(), settings=on) == "Approved."
    )


def test_only_someone_who_may_decide_gets_a_code(db: Session, seeded: dict[str, Principal], pending) -> None:
    with pytest.raises(Forbidden):
        approval_service.issue_text_code(db, seeded["katie"], pending.id)  # the requester cannot approve their own


def test_spoken_digits_reads_digits_and_number_words() -> None:
    assert channel_service.spoken_digits("my pin is 4 8 2 1") == "4821"
    assert channel_service.spoken_digits("Four, eight, two, one.") == "4821"
    assert channel_service.spoken_digits("oh nine one zero") == "0910"
    assert channel_service.spoken_digits("hello there") == ""


def test_pin_hash_is_salted_and_verifies() -> None:
    a, b = channel_service.hash_pin("4821"), channel_service.hash_pin("4821")
    assert a != b and "4821" not in a
    assert channel_service.verify_pin(a, "4821") and not channel_service.verify_pin(a, "1234")
    assert not channel_service.verify_pin("garbage", "4821") and not channel_service.verify_pin(a, "")
