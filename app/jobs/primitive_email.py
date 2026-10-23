"""Email to a family member's agent through Primitive (spec section 11; addendum 21.1 flow B, Primitive as the channel).

An `email.received` webhook addressed to an inbox bound as someone's agent (`primitive_inbox`) is answered by that
person's Keiki agent, through the same bridge as texts (`channel_service.handle_inbound`). It is answered only when
Primitive says the From address is verified (`from_verified`, read back from Primitive's API, not taken from the
webhook body) AND that address is bound to the same person (`primitive_email`). Anyone else is ignored and logged.

The reply goes back on the same email thread, and the thread becomes a private Uhlexa conversation
(`transcript_provider=primitive`, `primitive_thread_id`) holding both emails: a persistent thread like any other.

At most once for the model, as for Inkbox (`channel_handlers`): `started` is committed BEFORE the gateway is called, and
`replied` keeps the reply text so a failed send is retried without a new model turn. The send carries the webhook
event id as its idempotency key, so a retry cannot email twice.
"""

import logging
import uuid
from email.utils import parseaddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.channel_principal import PRIMITIVE_EMAIL_CHANNEL, PRIMITIVE_INBOX_BINDING, UnknownSender, resolve
from app.auth.demo_auth import Principal
from app.channels.types import InboundTurn
from app.db.base import utcnow
from app.models import (
    BindingStatus,
    Conversation,
    Message,
    ProviderBinding,
    ProviderName,
    SenderType,
    TranscriptProvider,
    Visibility,
    WebhookEvent,
    WebhookStatus,
)
from app.providers.primitive.errors import PrimitiveRejection
from app.providers.primitive.models import PrimitiveMessage, PrimitiveReply
from app.services import channel_service, conversation_service, providers
from app.services.clock import tick
from app.settings import get_settings

logger = logging.getLogger(__name__)

META_KEY = "_uhlexa"  # our marker inside the stored payload; Primitive's own keys are never touched
MAX_BODY_CHARS = 5000

# Swapped by tests; resolved at call time so a monkeypatch takes effect.
make_gateway = providers.get_keiki


def has_agent_inboxes(db: Session) -> bool:
    """True when any agent has a Primitive inbox bound: only then can an email be for an agent."""
    stmt = select(ProviderBinding.id).where(
        ProviderBinding.provider == ProviderName.PRIMITIVE,
        ProviderBinding.binding_type == PRIMITIVE_INBOX_BINDING,
        ProviderBinding.status == BindingStatus.ACTIVE,
    )
    return db.scalars(stmt.limit(1)).first() is not None


def from_household(db: Session, household_id: uuid.UUID, email: PrimitiveMessage) -> bool:
    """True only for an inbound email Primitive verified, from a household member's address (their account email or a
    bound `primitive_email`). Used before an email is put into a transcript that agents read."""
    sender = parseaddr(email.sender or "")[1].strip().lower()
    if email.direction != "inbound" or not email.from_verified or "@" not in sender:
        return False
    bound = db.scalars(
        select(ProviderBinding.external_id).where(
            ProviderBinding.household_id == household_id,
            ProviderBinding.provider == ProviderName.PRIMITIVE,
            ProviderBinding.binding_type == "primitive_email",
            ProviderBinding.status == BindingStatus.ACTIVE,
        )
    )
    allowed = {a.strip().lower() for a in [*conversation_service.household_addresses(db, household_id), *bound]}
    return sender in allowed


def to_turn(event_id: str, message: PrimitiveMessage) -> InboundTurn:
    return InboundTurn(
        PRIMITIVE_EMAIL_CHANNEL,
        event_id,
        message.recipient,
        message.sender,
        message.body_text,
        sender_verified=message.from_verified,
        reply_ctx={"subject": message.subject, "email_id": message.id, "thread_id": message.thread_id},
    )


def _finish(row: WebhookEvent, status: WebhookStatus, error: str | None) -> None:
    row.status, row.error, row.processed_at = status, error, utcnow()


def _set_meta(row: WebhookEvent, **meta: Any) -> None:
    payload = dict(row.payload or {})
    payload[META_KEY] = {**(payload.get(META_KEY) or {}), **meta}
    row.payload = payload  # reassigned so the JSON column sees the change


def _thread_conversation(db: Session, principal: Principal, message: PrimitiveMessage) -> Conversation | None:
    """The Uhlexa conversation for this email thread, created (private to the owner) on the first email.

    An existing one is reused only if this flow made it for this owner: private, created by them, Primitive-backed.
    Any other conversation on the thread (another household's, or a shared one) gets None: nothing is mirrored there,
    so an email to your own agent never lands where someone else can read it."""
    conversation = db.scalars(select(Conversation).where(Conversation.primitive_thread_id == message.thread_id)).first()
    if conversation is not None:
        mine = (
            conversation.household_id == principal.household.id
            and conversation.created_by_user_id == principal.user.id
            and conversation.visibility == Visibility.PRIVATE
            and conversation.transcript_provider == TranscriptProvider.PRIMITIVE
        )
        return conversation if mine else None
    subject = " ".join((message.subject or "").split())
    conversation = conversation_service.start(db, principal, subject or "Email", f"email {subject}")
    conversation.visibility = Visibility.PRIVATE  # an email to your own agent is yours until you share it
    conversation.transcript_provider = TranscriptProvider.PRIMITIVE
    conversation.primitive_thread_id = message.thread_id
    db.flush()
    return conversation


def _mirror(db: Session, conversation: Conversation, message: Message) -> None:
    now = tick()
    message.conversation_id, message.created_at = conversation.id, now
    conversation.last_message_at = now
    db.add(message)
    db.flush()


def _mirror_inbound(db: Session, conversation: Conversation, principal: Principal, email: PrimitiveMessage) -> None:
    if db.scalars(select(Message.id).where(Message.primitive_message_id == email.id)).first() is not None:
        return
    _mirror(
        db,
        conversation,
        Message(
            sender_type=SenderType.USER,
            sender_user_id=principal.user.id,
            body=(email.body_text or "")[:MAX_BODY_CHARS],
            primitive_message_id=email.id,
            source=TranscriptProvider.PRIMITIVE,
        ),
    )


def _mirror_reply(
    db: Session, conversation: Conversation, principal: Principal, text: str, sent: PrimitiveReply | None
) -> None:
    on_primitive = sent is not None and sent.sent
    _mirror(
        db,
        conversation,
        Message(
            sender_type=SenderType.AGENT,
            sender_agent_id=principal.agent.id,
            body=text[:MAX_BODY_CHARS],
            primitive_message_id=sent.id if on_primitive else None,  # type: ignore[union-attr]
            source=TranscriptProvider.PRIMITIVE if on_primitive else TranscriptProvider.LOCAL,
        ),
    )


def handle(db: Session, row: WebhookEvent, email: PrimitiveMessage, primitive: Any) -> bool:
    """Answer one inbound email if it was sent to an agent inbox. True: dealt with (the row is finished and committed).
    False: not addressed to any agent, so the caller treats it as an ordinary thread email.

    A Primitive outage while sending raises `ProviderUnavailable`: the job retries the SEND only (stage `replied`)."""
    meta = dict((row.payload or {}).get(META_KEY) or {})
    stage = meta.get("stage")
    turn = to_turn(row.external_event_id, email)
    if stage == "started":
        # The previous attempt died somewhere around the model call. Never run the agent twice for one email.
        _finish(row, WebhookStatus.FAILED, "worker stopped mid-turn; not re-run")
        db.commit()
        return True
    if stage == "replied":
        reply = str(meta.get("reply") or "")
    else:
        if email.direction != "inbound" or not email.thread_id:
            return False
        try:
            resolve(db, turn)
        except UnknownSender as exc:
            if exc.reason == "unknown_agent":
                return False  # not one of our agent inboxes
            channel_service.log_rejected(db, turn, exc)
            _finish(row, WebhookStatus.IGNORED, "sender not recognized")
            db.commit()
            return True
        _set_meta(row, stage="started")
        db.commit()  # committed first: from here on a crash means "do not re-run the model"
        reply = channel_service.handle_inbound(db, turn, gateway=make_gateway(), settings=get_settings()) or ""
        if not reply:
            _finish(row, WebhookStatus.IGNORED, "sender not recognized")
            db.commit()
            return True
        _set_meta(row, stage="replied", reply=reply)
        db.commit()

    try:
        principal = resolve(db, turn)
    except UnknownSender as exc:  # the binding was removed between the model turn and the send
        _finish(row, WebhookStatus.FAILED, f"sender no longer recognized ({exc.reason})")
        db.commit()
        return True
    conversation = _thread_conversation(db, principal, email)
    if conversation is not None:
        _mirror_inbound(db, conversation, principal, email)
    try:
        sent: PrimitiveReply | None = primitive.reply_to_email(
            email.id,  # exactly the email that was verified; Primitive sends the reply to its sender
            reply,
            sender_label=principal.agent.name,
            idempotency_key=f"uhlexa-{row.external_event_id}",
        )
    except PrimitiveRejection as exc:
        logger.warning("primitive refused the reply: %s", exc.reason)
        sent = None
        _finish(row, WebhookStatus.FAILED, f"primitive refused the reply ({exc.reason})")
    else:
        if sent.sent:
            channel_service.record_reply_sent(db, principal, turn, len(reply))
            _finish(row, WebhookStatus.PROCESSED, None)
        else:
            _finish(row, WebhookStatus.FAILED, "primitive did not send the reply")
    if conversation is not None:
        _mirror_reply(db, conversation, principal, reply, sent)
    db.commit()
    return True
