"""Provider webhooks (spec sections 11 and 14). Verify over the raw bytes, dedupe, store, enqueue; never act inline."""

import copy
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.session import get_db
from app.errors import ProviderUnavailable, Unauthorized, ValidationFailed
from app.jobs import dispatcher
from app.models.binding import ProviderName
from app.models.webhook_event import WebhookEvent, WebhookStatus
from app.providers.primitive.webhook import WebhookPayloadError, parse_event, verify_signature
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Inline raw email is capped at 256 KiB by Primitive, plus parsed bodies and headers; anything far beyond is not theirs.
MAX_BODY_BYTES = 2 * 1024 * 1024
PRIMITIVE_SYNC_JOB = "primitive_sync"


class PayloadTooLarge(ValidationFailed):
    status_code = 413
    code = "payload_too_large"
    default_message = "Webhook body too large"


class MalformedPayload(ValidationFailed):
    status_code = 400
    code = "malformed_payload"
    default_message = "Malformed webhook body"


def _without_raw_email(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the base64 raw message: the job re-fetches by email id, and the DB shouldn't hold whole emails."""
    raw = payload.get("email", {}).get("content", {}).get("raw") if isinstance(payload.get("email"), dict) else None
    if not isinstance(raw, dict) or "data" not in raw:
        return payload
    trimmed = copy.deepcopy(payload)
    del trimmed["email"]["content"]["raw"]["data"]
    return trimmed


def _record_rejected(db: Session) -> None:
    """Count a rejected delivery in one row per UTC hour, so unauthenticated traffic can't grow the table.

    The unverified body is never stored and its id can't be trusted; the row's payload is just `{"count": n}`.
    """
    bucket = f"rejected:{utcnow():%Y%m%d%H}"
    for _ in range(2):
        row = db.scalar(
            select(WebhookEvent).where(
                WebhookEvent.provider == ProviderName.PRIMITIVE, WebhookEvent.external_event_id == bucket
            )
        )
        if row is not None:
            row.payload = {"count": int(row.payload.get("count", 0)) + 1}
        else:
            db.add(
                WebhookEvent(
                    provider=ProviderName.PRIMITIVE,
                    external_event_id=bucket,
                    event_type="unverified",
                    signature_valid=False,
                    payload={"count": 1},
                    status=WebhookStatus.IGNORED,
                    error="invalid signature",
                )
            )
        try:
            db.commit()
            return
        except IntegrityError:
            db.rollback()  # another rejection created this hour's row first; count into it instead
    logger.warning("primitive webhook: could not record a rejected delivery")


async def _read_capped(request: Request) -> bytes:
    """Read the body in chunks and stop as soon as it passes the cap, whatever Content-Length claims."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise PayloadTooLarge()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise PayloadTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


def _store_and_enqueue(db: Session, event_id: str, event_type: str, payload: dict[str, Any]) -> str:
    existing = select(WebhookEvent.id).where(
        WebhookEvent.provider == ProviderName.PRIMITIVE, WebhookEvent.external_event_id == event_id
    )
    if db.scalar(existing) is not None:
        return "duplicate"
    event = WebhookEvent(
        provider=ProviderName.PRIMITIVE,
        external_event_id=event_id,
        event_type=event_type[:128],
        signature_valid=True,
        payload=_without_raw_email(payload),
    )
    db.add(event)
    try:
        db.flush()
        dispatcher.enqueue(db, PRIMITIVE_SYNC_JOB, {"webhook_event_id": str(event.id)})  # wakes the worker on commit
        db.commit()
    except IntegrityError:
        db.rollback()  # a concurrent redelivery won the unique (provider, external_event_id) race
        return "duplicate"
    return "accepted"


@router.post("/primitive")
async def primitive_webhook(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    settings = get_settings()
    if not settings.enable_primitive or not settings.primitive_webhook_secret:
        raise ProviderUnavailable("primitive", "webhook not configured")
    raw = await _read_capped(request)
    if not verify_signature(request.headers, raw, settings.primitive_webhook_secret):
        await run_in_threadpool(_record_rejected, db)
        raise Unauthorized("Invalid webhook signature", code="invalid_signature")
    try:
        event = parse_event(raw)
    except WebhookPayloadError:
        raise MalformedPayload() from None
    status = await run_in_threadpool(_store_and_enqueue, db, event.id, event.type, event.payload)
    return JSONResponse({"status": status})
