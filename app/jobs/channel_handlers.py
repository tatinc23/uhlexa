"""Job handler for `inkbox_event`: a verified Inkbox webhook -> the owner's Keiki agent -> a reply on the same channel.

At most once for the model: the `webhook_events` row carries a stage marker. `started` is committed BEFORE the gateway
is called; if the worker dies after that, the retry sees `started` and gives up instead of running the agent (which may
have created tasks) a second time. Once the reply text exists it is saved as `replied`, so a failed send is retried
without a new model turn. The send carries the envelope id as its idempotency key, so a retry cannot double-send.
"""

import dataclasses
import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.channels.types import InboundTurn
from app.db.base import utcnow
from app.models import Job, WebhookEvent, WebhookStatus
from app.providers.inkbox import InkboxPayloadError, InkboxRefusal, parse, to_inbound
from app.services import channel_service, providers
from app.settings import get_settings

logger = logging.getLogger(__name__)

JOB_KIND = "inkbox_event"
META_KEY = "_uhlexa"  # our marker inside the stored envelope; Inkbox's own keys are never touched

# Swapped by tests; resolved at call time so a monkeypatch takes effect.
make_gateway = providers.get_keiki
make_inkbox = providers.get_inkbox


def _finish(row: WebhookEvent, status: WebhookStatus, error: str | None) -> None:
    row.status, row.error, row.processed_at = status, error, utcnow()


def _set_meta(row: WebhookEvent, **meta: Any) -> None:
    payload = dict(row.payload or {})
    payload[META_KEY] = {**(payload.get(META_KEY) or {}), **meta}
    row.payload = payload  # reassigned so the JSON column sees the change


def _full_email_turn(turn: InboundTurn, inkbox: Any) -> InboundTurn:
    """The webhook may carry a truncated body; fetch the whole message (also marks it read)."""
    if not turn.reply_ctx.get("body_truncated") or not turn.reply_ctx.get("message_id"):
        return turn
    body = inkbox.get_message(turn.agent_ref, str(turn.reply_ctx["message_id"]))
    return dataclasses.replace(turn, text=body.body_text or turn.text)


def send_reply(inkbox: Any, turn: InboundTurn, text: str, idempotency_key: str) -> None:
    """Send `text` on the channel the turn arrived on, from the agent that was addressed."""
    ctx = turn.reply_ctx
    handle = turn.agent_ref
    # Always the address that was authorized, never a conversation id: a conversation could be (or become) a group.
    target: dict[str, Any] = {"to": turn.sender_ref}
    if turn.channel == "inkbox:imessage":
        inkbox.send_imessage(handle, text=text, idempotency_key=idempotency_key, **target)
    elif turn.channel == "inkbox:sms":
        inkbox.send_text(handle, text=text, idempotency_key=idempotency_key, **target)
    elif turn.channel == "inkbox:email":
        subject = str(ctx.get("subject") or "").strip()
        inkbox.send_email(
            handle,
            to=[turn.sender_ref],
            subject=subject if subject.lower().startswith("re:") else f"Re: {subject or 'your message'}",
            body_text=text,
            idempotency_key=idempotency_key,
            in_reply_to_message_id=str(ctx.get("message_id") or "") or None,
        )
    else:
        raise ValueError(f"cannot reply on {turn.channel}")


def _is_group(event: Any) -> bool:
    """True for a group conversation: a reply must never land in one. Fields read from the installed SDK's webhook
    types: iMessage `message.is_group`; SMS `data.recipient_phone_number` (null for 1:1) or several `recipients`."""
    data = event.data
    msg = data.get("message") if isinstance(data.get("message"), dict) else {}
    if msg.get("is_group"):
        return True
    text_msg = data.get("text_message") if isinstance(data.get("text_message"), dict) else {}
    recipients = text_msg.get("recipients")
    return bool(data.get("recipient_phone_number")) or (isinstance(recipients, list) and len(recipients) > 1)


def handle_inkbox_event(db: Session, payload: dict[str, Any]) -> None:
    try:
        row = db.get(WebhookEvent, uuid.UUID(str(payload["webhook_event_id"])))
    except (KeyError, ValueError):
        logger.warning("inkbox_event job without a valid webhook_event_id; dropping it")
        return
    handle = str(payload.get("handle") or "")
    if row is None or row.status != WebhookStatus.RECEIVED:
        return
    meta = dict((row.payload or {}).get(META_KEY) or {})
    stage = meta.get("stage")
    if stage == "started":
        # The previous attempt died somewhere around the model call. Never run the agent twice for one message.
        _finish(row, WebhookStatus.FAILED, "worker stopped mid-turn; not re-run")
        return
    try:
        event = parse(json.dumps(row.payload).encode())
    except InkboxPayloadError as exc:
        _finish(row, WebhookStatus.IGNORED, f"unusable payload: {exc}")
        return
    turn = to_inbound(event, handle)
    if turn is None:
        _finish(row, WebhookStatus.IGNORED, "not an inbound message")
        return
    if _is_group(event):
        _finish(row, WebhookStatus.IGNORED, "group conversation")
        return
    inkbox = make_inkbox()

    if stage == "replied":
        reply = str(meta.get("reply") or "")
    else:
        try:
            turn = _full_email_turn(turn, inkbox)
        except Exception as exc:  # fall back to the truncated body rather than lose the message
            logger.warning("could not fetch the full email body: %s", type(exc).__name__)
        _set_meta(row, stage="started")
        db.commit()  # committed first: from here on a crash means "do not re-run the model"
        reply = channel_service.handle_inbound(db, turn, gateway=make_gateway(), settings=get_settings()) or ""
        if not reply:  # unknown sender: nothing is sent, the rejection is already on the activity log
            _finish(row, WebhookStatus.IGNORED, "sender not recognized")
            db.commit()
            return
        _set_meta(row, stage="replied", reply=reply)
        db.commit()

    try:
        send_reply(inkbox, turn, reply, event.id)
    except InkboxRefusal as exc:
        logger.warning("inkbox refused the reply: %s", type(exc).__name__)
        _finish(row, WebhookStatus.FAILED, f"inkbox refused the reply ({exc.reason})")
        db.commit()
        return
    principal = _principal_for(db, turn)
    if principal is not None:
        channel_service.record_reply_sent(db, principal, turn, len(reply))
    _finish(row, WebhookStatus.PROCESSED, None)
    db.commit()


def _principal_for(db: Session, turn: InboundTurn) -> Any:
    from app.auth.channel_principal import UnknownSender, resolve

    try:
        return resolve(db, turn)
    except UnknownSender:
        return None


def abandon_inkbox_event(db: Session, job: Job) -> None:
    """The job gave up (or its lease expired at the attempt limit): the message is not left looking pending."""
    try:
        row = db.get(WebhookEvent, uuid.UUID(str((job.payload or {})["webhook_event_id"])))
    except (KeyError, ValueError):
        return
    if row is not None and row.status == WebhookStatus.RECEIVED:
        _finish(row, WebhookStatus.FAILED, job.last_error)
