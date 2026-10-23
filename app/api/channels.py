"""Inkbox channel routes (addendum 21.5): the signed webhook and the phone-call WebSocket.

Both carry their own authentication (Inkbox signatures over the raw bytes), so the public-tunnel gate leaves
`/webhooks/*` and `/ws/phone/*` open. Neither route does any work inline: the webhook stores, dedupes and enqueues; the
call socket hands every final transcript to the owner's Keiki agent and speaks what comes back.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.api.webhooks import MalformedPayload, _read_capped
from app.auth.channel_principal import UnknownSender, identity_owner, resolve
from app.channels.types import InboundTurn
from app.db.base import utcnow
from app.db.session import get_db, session_scope
from app.errors import NotFound, ProviderUnavailable, Unauthorized
from app.jobs import dispatcher
from app.jobs.channel_handlers import JOB_KIND
from app.models import ProviderName, WebhookEvent, WebhookStatus
from app.providers.inkbox import (
    CallSession,
    HandshakeRejected,
    InkboxPayloadError,
    InkboxSignatureError,
    parse,
    to_inbound,
)
from app.providers.inkbox import verify as verify_signature
from app.services import channel_service, providers
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["channels"])

# Which settings switch gates each inbound webhook family.
_CHANNEL_FLAG = {
    "inkbox:imessage": "enable_inkbox_imessage",
    "inkbox:sms": "enable_inkbox_sms",
    "inkbox:email": "enable_inkbox_email",
}
POLICY_VIOLATION = 1008
REFUSAL_SPEECH = "Sorry, I can only talk with the person this assistant belongs to. Goodbye."
UNAVAILABLE_SPEECH = "Sorry, I can't take calls right now. Goodbye."


# --- dependencies (tests override these) ------------------------------------------------------------------------


def get_gateway() -> Any:
    return providers.get_keiki()


def get_inkbox_service() -> Any:
    return providers.get_inkbox()


def get_session_scope() -> Callable[[], AbstractContextManager[Session]]:
    """A factory for short-lived sessions: the call socket never holds one across a whole call."""
    return session_scope


# --- webhook ------------------------------------------------------------------------------------------------------


def _record_rejected(db: Session, handle: str) -> None:
    """One counter row per handle per UTC hour, so unauthenticated traffic can't grow the table. No body is stored."""
    bucket = f"rejected:{handle}:{utcnow():%Y%m%d%H}"[:255]
    for _ in range(2):
        row = db.scalar(
            select(WebhookEvent).where(
                WebhookEvent.provider == ProviderName.INKBOX, WebhookEvent.external_event_id == bucket
            )
        )
        if row is not None:
            row.payload = {"count": int(row.payload.get("count", 0)) + 1}
        else:
            db.add(
                WebhookEvent(
                    provider=ProviderName.INKBOX,
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
            db.rollback()
    logger.warning("inkbox webhook: could not record a rejected delivery")


def _known_handle(db: Session, handle: str) -> Any:
    binding = identity_owner(db, handle)
    return None if binding is None else binding.household_id


def _store_and_enqueue(db: Session, settings: Settings, handle: str, household_id: Any, raw: bytes) -> str:
    try:
        event = parse(raw)
    except InkboxPayloadError:
        raise MalformedPayload() from None
    exists = select(WebhookEvent.id).where(
        WebhookEvent.provider == ProviderName.INKBOX, WebhookEvent.external_event_id == event.id
    )
    if db.scalar(exists) is not None:
        return "duplicate"
    turn = to_inbound(event, handle)
    enabled = turn is not None and bool(getattr(settings, _CHANNEL_FLAG.get(turn.channel, ""), False))
    row = WebhookEvent(
        provider=ProviderName.INKBOX,
        external_event_id=event.id,
        event_type=event.event_type[:128],
        signature_valid=True,
        payload=json.loads(raw) if enabled else {"id": event.id, "event_type": event.event_type},
        status=WebhookStatus.RECEIVED if enabled else WebhookStatus.IGNORED,
        error=None if enabled else "not an inbound message on an enabled channel",
        processed_at=None if enabled else utcnow(),
    )
    db.add(row)
    try:
        db.flush()
        if enabled:
            dispatcher.enqueue(
                db, JOB_KIND, {"webhook_event_id": str(row.id), "handle": handle}, household_id=household_id
            )
        db.commit()
    except IntegrityError:
        db.rollback()  # a concurrent redelivery won the unique (provider, external_event_id) race
        return "duplicate"
    return "accepted" if enabled else "ignored"


@router.post("/webhooks/inkbox/{handle}")
async def inkbox_webhook(
    handle: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> JSONResponse:
    if not settings.enable_inkbox:
        raise ProviderUnavailable("inkbox", "webhook not configured")
    household_id = await run_in_threadpool(_known_handle, db, handle)
    if household_id is None:
        raise NotFound("Unknown Inkbox identity")
    raw = await _read_capped(request)
    token = settings.inkbox_webhook_auth_token
    if token:
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip().encode(), token.encode()):
            await run_in_threadpool(_record_rejected, db, handle)
            raise Unauthorized("Invalid webhook credentials", code="invalid_signature")
    try:
        verify_signature(request.headers, raw, settings.inkbox_signing_keys.get(handle, ""))
    except InkboxSignatureError as exc:
        await run_in_threadpool(_record_rejected, db, handle)
        raise Unauthorized("Invalid webhook signature", code="invalid_signature") from exc
    status = await run_in_threadpool(_store_and_enqueue, db, settings, handle, household_id, raw)
    return JSONResponse({"status": status})


# --- call WebSocket -------------------------------------------------------------------------------------------------

REPLAY_MEMORY_S = 900.0  # a signature is good for 300 s either side of its timestamp; keep well past that
_calls_lock = threading.Lock()
_seen_calls: dict[str, float] = {}  # call_id -> last time it was accepted or ended
_live_calls: set[str] = set()


def claim_call(call_id: str, *, now: float | None = None) -> bool:
    """Reserve `call_id` for one session. False for an id that is live or was accepted within the replay window."""
    moment = time.monotonic() if now is None else now
    with _calls_lock:
        for old in [c for c, at in _seen_calls.items() if c not in _live_calls and moment - at > REPLAY_MEMORY_S]:
            del _seen_calls[old]
        if call_id in _live_calls or call_id in _seen_calls:
            return False
        _seen_calls[call_id] = moment
        _live_calls.add(call_id)
        return True


def release_call(call_id: str) -> None:
    with _calls_lock:
        _live_calls.discard(call_id)
        _seen_calls[call_id] = time.monotonic()  # the replay window runs from the end of the call too


def reset_call_registry() -> None:
    with _calls_lock:
        _seen_calls.clear()
        _live_calls.clear()


def call_greeting(agent_name: str, *, ask: bool = True) -> str:
    """What the agent says first on an inbound call: who she is, that she is an AI, and that the call is transcribed."""
    return f"Hi, this is {agent_name}, an AI assistant. This call is transcribed." + (" How can I help?" if ask else "")


def _call_context(headers: dict[str, str]) -> dict[str, Any]:
    try:
        parsed = json.loads(headers.get("x-call-context", ""))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _caller_number(inkbox: Any, handle: str, call_id: str) -> str | None:
    """Who is on the line: the remote number from Inkbox's call record, and nothing else. The signed `X-Call-Context`
    is not a source (undocumented which side `phone_number` is). The record can lag the handshake, so retry briefly."""
    for attempt in range(3):
        try:
            remote = inkbox.call_remote_number(handle, call_id)
        except Exception as exc:
            logger.warning("call record lookup failed: %s", type(exc).__name__)
            remote = None
        if remote:
            return remote
        if attempt < 2:
            time.sleep(0.3)
    return None


def _identify_caller(
    sessions: Callable[[], AbstractContextManager[Session]],
    inkbox: Any,
    settings: Settings,
    handle: str,
    call_id: str,
    context: dict[str, Any],
) -> tuple[Any, str | None, Any]:
    """(owner Principal, None, PIN binding id or None) when a bound owner is calling, else (None, reason, None).
    Inkbox's call record has no verified-caller signal (SDK 0.7.20), so a binding that holds a PIN makes the caller
    say it. Runs in a worker thread."""
    if context.get("direction") == "outbound":
        return None, "outbound_not_supported", None  # no `contact.call_placed` intent flow yet: hang up
    number = _caller_number(inkbox, handle, call_id) or ""
    turn = InboundTurn("inkbox:call", call_id, handle, number, "")
    with sessions() as db:
        try:
            principal = resolve(db, turn)
        except UnknownSender as exc:
            channel_service.log_rejected(db, turn, exc)
            db.commit()
            return None, exc.reason, None
        if not (getattr(settings, "keiki_agent_ids", None) or {}).get(principal.user.handle):
            return None, "no_agent_id", None
        channel_service.record_inbound(db, principal, turn, call_id=call_id)
        binding = channel_service.pin_binding(db, principal, number)
        if binding is not None and channel_service.pin_locked(binding):
            db.commit()
            return None, "pin_locked", None  # too many wrong PINs on earlier calls: only an admin re-bind unlocks
        db.commit()
        return principal, None, None if binding is None else binding.id
    return None, "no_caller_number", None


async def _speak_and_hang_up(ws: WebSocket, session: CallSession, speech: str) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json({"event": "text", "delta": speech, "done": False, "turn_id": 0})
        await ws.send_json({"event": "text", "done": True, "turn_id": 0})
        await ws.send_json(session.hangup())


class _EndOfTurn:
    pass


_END = _EndOfTurn()


def _pin_attempt(
    sessions: Callable[[], AbstractContextManager[Session]], binding_id: Any
) -> Callable[[str], tuple[bool, bool]]:
    def attempt(digits: str) -> tuple[bool, bool]:
        with sessions() as db:
            result = channel_service.attempt_pin(db, binding_id, digits)
            db.commit()
            return result

    return attempt


async def _speak(ws: WebSocket, speech: str) -> None:
    await ws.send_json({"event": "text", "delta": speech, "done": False, "turn_id": 0})
    await ws.send_json({"event": "text", "done": True, "turn_id": 0})


async def _turn_worker(
    ws: WebSocket,
    session: CallSession,
    queue: "asyncio.Queue[dict[str, Any]]",
    gate: channel_service.PinGate | None = None,
) -> None:
    """Answer final transcripts one at a time, sending each piece as the brain produces it. A caller who fails the
    PIN gate is hung up on after the turn that locked it."""
    while True:
        event = await queue.get()
        pieces = session.stream_event(event)
        while True:
            out = await run_in_threadpool(next, pieces, _END)
            if out is _END:
                break
            await ws.send_json(out)
        if gate is not None and gate.locked:
            await ws.send_json(session.hangup())
            return


@router.websocket("/ws/phone/{handle}")
async def phone_socket(
    ws: WebSocket,
    handle: str,
    settings: Settings = Depends(get_settings),
    sessions: Callable[[], AbstractContextManager[Session]] = Depends(get_session_scope),
    gateway: Any = Depends(get_gateway),
    inkbox: Any = Depends(get_inkbox_service),
) -> None:
    signing_key = settings.inkbox_signing_keys.get(handle, "")
    if not (settings.enable_inkbox and settings.enable_inkbox_calls and signing_key):
        await ws.close(code=POLICY_VIOLATION)
        return
    loop = asyncio.get_running_loop()
    chosen: dict[str, Any] = {"brain": lambda _text: ["Sorry, I can't answer right now."]}
    session = CallSession(handle, signing_key, lambda text: chosen["brain"](text))
    headers = {k.lower(): v for k, v in ws.headers.items()}
    try:
        response_headers = session.accept(headers)
    except HandshakeRejected as exc:
        logger.warning("call handshake rejected: %s", exc.code)
        await ws.close(code=exc.close_code)
        return
    call_id = session.call_id
    if not call_id or not claim_call(call_id):
        logger.warning("call handshake rejected: replayed or concurrent call id")
        await ws.close(code=POLICY_VIOLATION)
        return
    worker: asyncio.Task[None] | None = None
    try:
        known = await run_in_threadpool(lambda: _with_session(sessions, lambda db: _known_handle(db, handle)))
        if known is None:
            logger.warning("call for an unknown identity handle")
            await ws.close(code=POLICY_VIOLATION)
            return
        principal, refusal, pin_binding_id = await run_in_threadpool(
            _identify_caller, sessions, inkbox, settings, handle, call_id, _call_context(headers)
        )
        await ws.accept(headers=[(k.lower().encode(), v.encode()) for k, v in response_headers.items()])
        if principal is None:
            logger.info("call refused: %s", refusal)
            await _speak_and_hang_up(ws, session, UNAVAILABLE_SPEECH if refusal == "no_agent_id" else REFUSAL_SPEECH)
            await _close(ws)
            return
        agent_id = settings.keiki_agent_ids[principal.user.handle]
        chosen["brain"] = channel_service.call_brain_for(gateway, agent_id, str(principal.user.id), settings, loop)
        gate = None if pin_binding_id is None else channel_service.PinGate(_pin_attempt(sessions, pin_binding_id))
        if gate is not None:
            chosen["brain"] = gate.wrap(chosen["brain"])
            await _speak(ws, f"{call_greeting(principal.agent.name, ask=False)} {channel_service.PIN_PROMPT}")
        else:
            session.greeting = call_greeting(principal.agent.name)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        worker = asyncio.create_task(_turn_worker(ws, session, queue, gate))
        await _receive_loop(ws, session, queue, worker)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("call socket ended with an error: %s", type(exc).__name__)
    finally:
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(BaseException):
                await worker
        session.hangup()  # stops any running turn and closes the gateway stream
        release_call(call_id)
        await _close(ws)


def _with_session(sessions: Callable[[], AbstractContextManager[Session]], fn: Callable[[Session], Any]) -> Any:
    with sessions() as db:
        return fn(db)


async def _receive_loop(
    ws: WebSocket, session: CallSession, queue: "asyncio.Queue[dict[str, Any]]", worker: "asyncio.Task[None]"
) -> None:
    while not session.ended and not worker.done():
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        raw = message.get("text")
        if raw is None and message.get("bytes") is not None:
            raw = message["bytes"].decode("utf-8", "replace")
        try:
            event = json.loads(raw or "")
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "transcript":
            if event.get("is_final") is True:
                queue.put_nowait(event)
            continue
        for out in session.stream_event(event):  # barge_in -> clear, stop -> ends the session; both are instant
            await ws.send_json(out)


async def _close(ws: WebSocket) -> None:
    if ws.application_state == WebSocketState.CONNECTED:
        with contextlib.suppress(Exception):
            await ws.close()
