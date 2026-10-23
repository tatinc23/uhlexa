"""The Inkbox -> Keiki bridge (addendum 21.1 flows B and C, 21.5, 21.8).

An inbound text, email or call turn is answered by the OWNER's Keiki agent through the gateway; Uhlexa only decides who
may speak to it and carries the words. Rules that hold here:

* the sender must be bound to the owner of the addressed agent (`channel_principal.resolve`), else nothing is sent;
* the model never approves anything: this module has no approve path and the Keiki agent has no approve tool;
* a gateway or provider failure becomes a FIXED reply plus an activity event, never an exception to the transport;
* activity payloads hold ids, channel and counts only: never message text, keys or numbers.
"""

import asyncio
import concurrent.futures
import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.channel_principal import UnknownSender, resolve
from app.auth.demo_auth import Principal
from app.channels.types import InboundTurn
from app.db.base import utcnow
from app.errors import ServiceError
from app.models import ActivityEvent, BindingStatus, ProviderBinding, ProviderName
from app.providers.keiki import KeikiBusy, KeikiError, conversation_id_for
from app.services import activity_service, approval_service
from app.voice.brain import VoiceBrain

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 1000
MAX_INBOUND_CHARS = 5000
MAX_REJECTIONS_LOGGED_PER_DAY = 100

# Fixed replies: the only text Uhlexa itself ever sends (no model words, no data, no links).
BUSY_REPLY = "I'm still working on your last message. Give me a moment and try again."
ERROR_REPLY = "Sorry, I couldn't reach my brain just now. Please try again in a bit."
NOT_SET_UP_REPLY = "I'm not set up to answer yet. Please check with whoever runs this demo."
CAP_REPLY = "I've hit today's message limit. Try again tomorrow."
NO_MATCH_REPLY = "I couldn't find a pending approval with that code."
TEXT_NOT_VERIFIED_REPLY = "I can only take approvals by iMessage. Please approve on the Family Board or from iMessage."
EMPTY_REPLY = "I don't have anything to add to that."
CALL_SYSTEM_LINE = "Voice: answer in one or two short spoken sentences. No lists, links, emoji or markdown."

_CHANNEL_KIND = {
    "inkbox:imessage": "imsg",
    "inkbox:sms": "sms",
    "inkbox:email": "mail",
    "inkbox:call": "call",
    "primitive:email": "mail",
}
_STYLE = {
    "inkbox:imessage": "an iMessage text",
    "inkbox:sms": "an SMS text",
    "inkbox:email": "an email",
    "inkbox:call": "a phone call",
    "primitive:email": "an email",
}


# --- call PIN ----------------------------------------------------------------------------------------------------
# Caller ID can be forged and Inkbox's call record carries no verified-caller signal (SDK 0.7.20 `PhoneCall`), so a
# binding may also hold a spoken PIN. Only a salted slow hash is stored; the spoken PIN never reaches the model.

PIN_PROMPT = "Please say your PIN."
PIN_RETRY = "That wasn't right. Please say your PIN."
PIN_OK = "Thanks. How can I help?"
PIN_LOCKED = "Sorry, I can't verify you. Goodbye."
PIN_MAX_TRIES = 3  # a call
PIN_MAX_FAILURES = 5  # in a row across calls, then the binding is locked for PIN_LOCK
PIN_LOCK = timedelta(hours=24)  # bounded, so spoofing the owner's number can't shut them out for good
_PIN_ITERATIONS = 200_000
_NUMBER_WORDS = {
    w: str(i) for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"))
} | {"oh": "0"}


def spoken_digits(text: str) -> str:
    """The digits in what a caller said: `4 8 2 1`, `4821` and `four eight two one` all give `4821`."""
    parts = re.findall(r"\d|[a-z]+", text.lower())
    return "".join(d if d.isdigit() else _NUMBER_WORDS.get(d, "") for d in parts)


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, _PIN_ITERATIONS)
    return f"pbkdf2_sha256${_PIN_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_pin(stored: str, pin: str) -> bool:
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != "pbkdf2_sha256" or not pin:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except ValueError:
        return False


def pin_binding(db: Session, principal: Principal, number: str) -> ProviderBinding | None:
    """This owner's active phone binding for `number` that holds a PIN, or None when no PIN was set."""
    rows = db.scalars(
        select(ProviderBinding).where(
            ProviderBinding.provider == ProviderName.INKBOX,
            ProviderBinding.binding_type.in_(("inkbox_phone", "inkbox_imessage")),
            ProviderBinding.user_id == principal.user.id,
            func.lower(ProviderBinding.external_id) == number.strip().lower(),
            ProviderBinding.status == BindingStatus.ACTIVE,
        )
    )
    return next((r for r in rows if isinstance((r.config or {}).get("pin_hash"), str) and r.config["pin_hash"]), None)


def pin_locked(binding: ProviderBinding) -> bool:
    config = binding.config or {}
    until = config.get("pin_locked_until")
    if until is None:  # a lock written before locks had an end time stays on until an admin re-binds the number
        return int(config.get("pin_failures", 0)) >= PIN_MAX_FAILURES
    try:
        return isinstance(until, str) and datetime.fromisoformat(until) > utcnow()
    except ValueError:
        return True  # an unreadable lock stays a lock


def attempt_pin(db: Session, binding_id: object, spoken: str, *, channel: str = "inkbox:call") -> tuple[bool, bool]:
    """Check one spoken PIN against the binding's hash and count it, as one step under a row lock, so parallel calls
    can't each read an old count. Returns (ok, locked). A lock already in place is checked BEFORE the PIN, so a right
    PIN can't lift it; it ends only when PIN_LOCK has passed or an admin re-binds the number. A wrong PIN adds to the
    count and PIN_MAX_FAILURES of them lock the binding. The caller commits."""
    binding = db.scalars(select(ProviderBinding).where(ProviderBinding.id == binding_id).with_for_update()).first()
    if binding is None or pin_locked(binding):
        return False, True
    config = dict(binding.config or {})
    if config.pop("pin_locked_until", None) is not None:  # the lock ran out: start counting again
        config["pin_failures"] = 0
    ok = verify_pin(str(config.get("pin_hash") or ""), spoken)
    if ok:
        config.pop("pin_failures", None)
        binding.config = config  # a new dict, so the JSON column notices the change
        db.flush()
        return True, False
    failures = int(config.get("pin_failures", 0)) + 1
    config["pin_failures"] = failures
    locked = failures >= PIN_MAX_FAILURES
    if locked:
        config["pin_locked_until"] = (utcnow() + PIN_LOCK).isoformat()
    binding.config = config
    activity_service.record(
        db,
        household_id=binding.household_id,
        event_type="channel.pin_locked" if locked else "channel.pin_failed",
        summary="Calls from a number are paused after repeated wrong PINs."
        if locked
        else "A caller failed the PIN check.",
        actor_type="provider",
        payload={"channel": channel, "failures": failures, "number": _digest(binding.external_id)},
    )
    db.flush()
    return False, locked


class PinGate:
    """Stands between a call and the brain until the caller has said the right PIN: `PIN_MAX_TRIES` guesses a call, and
    `attempt(digits) -> (ok, locked)` (which counts them on the binding) can lock it across calls."""

    def __init__(self, attempt: Callable[[str], tuple[bool, bool]]) -> None:
        self._attempt = attempt
        self._tries = 0
        self.passed = False
        self.locked = False

    def check(self, text: str) -> list[str]:
        if self.locked:
            return [PIN_LOCKED]
        ok, locked = self._attempt(spoken_digits(text))
        if ok:
            self.passed = True
            return [PIN_OK]
        self._tries += 1
        if locked or self._tries >= PIN_MAX_TRIES:
            self.locked = True
            return [PIN_LOCKED]
        return [PIN_RETRY]

    def wrap(self, brain: Callable[[str], Iterable[str]]) -> Callable[[str], Iterable[str]]:
        return lambda text: brain(text) if self.passed else self.check(text)


def conversation_id(owner_id: object, channel: str) -> str:
    """Stable per person and channel kind, so the agent keeps one running thread per channel."""
    return conversation_id_for(str(owner_id), _CHANNEL_KIND[channel])


def _system_line(principal: Principal, turn: InboundTurn) -> str:
    return (
        f"You are {principal.agent.name}, {principal.user.display_name}'s family assistant. "
        f"This is {_STYLE[turn.channel]} from {principal.user.display_name} themselves. "
        "Reply in plain text, briefly, with no markdown. "
        "Never include links or phone numbers you were not asked for."
    )


def _clip(text: str) -> str:
    text = text.strip()
    if not text:
        return EMPTY_REPLY
    if len(text) <= MAX_REPLY_CHARS:
        return text
    return text[: MAX_REPLY_CHARS - 1].rstrip() + "…"


def _digest(ref: str) -> str:
    return hashlib.sha256(ref.strip().lower().encode()).hexdigest()[:10]


def _today_start() -> Any:
    now = utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _count_today(db: Session, event_type: str, *, user_id: object = None, household_id: object = None) -> int:
    stmt = (
        select(func.count())
        .select_from(ActivityEvent)
        .where(ActivityEvent.event_type == event_type, ActivityEvent.created_at >= _today_start())
    )
    if user_id is not None:
        stmt = stmt.where(ActivityEvent.actor_user_id == user_id)
    if household_id is not None:
        stmt = stmt.where(ActivityEvent.household_id == household_id)
    return db.scalar(stmt) or 0


def log_rejected(db: Session, turn: InboundTurn, exc: UnknownSender) -> None:
    """Record a refused sender: a reason code and a short digest of who, never the number or the text."""
    logger.warning("channel turn rejected: %s (%s)", exc.reason, turn.channel)
    if exc.household_id is None:
        return  # we don't know whose household this was for; the server log is the only place
    if _count_today(db, "channel.rejected", household_id=exc.household_id) >= MAX_REJECTIONS_LOGGED_PER_DAY:
        return  # strangers texting the number must not grow the log without bound
    activity_service.record(
        db,
        household_id=exc.household_id,
        event_type="channel.rejected",
        summary="A message from an unrecognized sender was ignored.",
        actor_type="provider",
        payload={"channel": turn.channel, "reason": exc.reason, "sender": _digest(turn.sender_ref)},
    )


def record_inbound(db: Session, principal: Principal, turn: InboundTurn, **extra: Any) -> None:
    activity_service.record(
        db,
        household_id=principal.household.id,
        event_type="channel.inbound",
        summary=f"{principal.user.display_name} messaged {principal.agent.name} ({turn.channel.split(':')[1]}).",
        actor_type="user",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        payload={"channel": turn.channel, "event_id": turn.provider_event_id, "chars": len(turn.text), **extra},
    )


def record_reply_sent(db: Session, principal: Principal, turn: InboundTurn, chars: int) -> None:
    activity_service.record(
        db,
        household_id=principal.household.id,
        event_type="channel.reply_sent",
        summary=f"{principal.agent.name} replied ({turn.channel.split(':')[1]}).",
        actor_type="agent",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        payload={"channel": turn.channel, "event_id": turn.provider_event_id, "chars": chars},
    )


def _record_failure(db: Session, principal: Principal, turn: InboundTurn, kind: str) -> None:
    activity_service.record(
        db,
        household_id=principal.household.id,
        event_type="channel.error",
        summary=f"{principal.agent.name} could not answer a {turn.channel.split(':')[1]} message.",
        actor_type="system",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        payload={"channel": turn.channel, "event_id": turn.provider_event_id, "error": kind},
    )


_COMMAND = re.compile(r"^\s*(approve|deny)\s+([0-9a-f]{8})\s*$", re.IGNORECASE)


def _approval_command(db: Session, principal: Principal, turn: InboundTurn, settings: Any) -> str | None:
    """`APPROVE <code>` / `DENY <code>`, handled here and never sent to the model. Only when ENABLE_CHANNEL_APPROVALS
    is on, only over a channel whose sender can't be forged (iMessage that Inkbox marks `imessage`; SMS and a bare
    number are spoofable), only from the owner (resolve already refused children and non-owners), and only with the
    one-time code issued for an approval that person may decide. Anything else, including "approve everything", is
    not a command."""
    match = _COMMAND.match(turn.text)
    if match is None or not getattr(settings, "enable_channel_approvals", False):
        return None
    if turn.channel != "inkbox:imessage" or turn.reply_ctx.get("service") != "imessage":
        return TEXT_NOT_VERIFIED_REPLY
    approve = match.group(1).lower() == "approve"
    approval = approval_service.find_by_text_code(db, principal, match.group(2))
    if approval is None:
        return NO_MATCH_REPLY
    try:
        approval_service.decide(db, principal, approval.id, approve=approve, note="by text")
    except ServiceError as exc:  # our own refusal text (not a person, not allowed, already decided): safe to relay
        return f"I couldn't do that. {exc.message}"
    activity_service.record(
        db,
        household_id=principal.household.id,
        event_type="channel.approval_by_text",
        summary=f"{principal.user.display_name} {'approved' if approve else 'denied'} a request by text.",
        actor_type="user",
        actor_user_id=principal.user.id,
        payload={"channel": turn.channel, "approval_id": str(approval.id), "approved": approve},
    )
    return "Approved." if approve else "Denied."


def handle_inbound(db: Session, turn: InboundTurn, *, gateway: Any, settings: Any) -> str | None:
    """The reply text for one inbound text or email, or None when nothing must be sent (unknown sender).

    Never raises for provider trouble. The caller commits (services flush)."""
    try:
        principal = resolve(db, turn)
    except UnknownSender as exc:
        log_rejected(db, turn, exc)
        return None
    if (answer := _approval_command(db, principal, turn, settings)) is not None:
        return answer
    cap = int(getattr(settings, "keiki_daily_turn_cap", 200))
    if _count_today(db, "channel.inbound", user_id=principal.user.id) >= cap:
        _record_failure(db, principal, turn, "daily_cap")
        return CAP_REPLY  # fail closed: no gateway call
    record_inbound(db, principal, turn)
    agent_id = (getattr(settings, "keiki_agent_ids", None) or {}).get(principal.user.handle)
    if not agent_id:
        _record_failure(db, principal, turn, "no_agent_id")
        return NOT_SET_UP_REPLY
    text = turn.text.strip()[:MAX_INBOUND_CHARS]
    if turn.channel in ("inkbox:email", "primitive:email"):
        subject = str(turn.reply_ctx.get("subject") or "").strip()
        text = f"Subject: {subject}\n\n{text}"[:MAX_INBOUND_CHARS] if subject else text
    try:
        reply = gateway.complete(
            agent_id, conversation_id(principal.user.id, turn.channel), text, system=_system_line(principal, turn)
        )
    except KeikiBusy:
        _record_failure(db, principal, turn, "busy")
        return BUSY_REPLY
    except KeikiError as exc:
        logger.warning("keiki turn failed: %s", type(exc).__name__)
        _record_failure(db, principal, turn, type(exc).__name__)
        return ERROR_REPLY
    except Exception as exc:  # the transport must always get a reply; log the type only (messages can echo requests)
        logger.warning("keiki turn crashed: %s", type(exc).__name__)
        _record_failure(db, principal, turn, type(exc).__name__)
        return ERROR_REPLY
    return _clip(getattr(reply, "text", "") or "")


# --- calls -------------------------------------------------------------------------------------------------------

_CALL_TURNS: dict[str, int] = {}  # per process, by UTC date: the call-turn daily cap


class CallBrain(VoiceBrain):
    """The voice brain on the phone channel: same filler, time cap, apologies and daily cap, but its own conversation
    (`u<id>-call`) so a phone call and the web voice pages keep separate histories."""

    def __init__(self, gateway: Any, agent_id: str, user_id: str, settings: Any) -> None:
        super().__init__(
            gateway,
            agent_id,
            user_id,
            getattr(settings, "keiki_call_reasoning", "none"),
            daily_cap=int(getattr(settings, "keiki_daily_turn_cap", 200)),
            counter=_CALL_TURNS,
        )
        self._conversation_id = conversation_id_for(user_id, "call")


class _AsyncToSync:
    """Pull an async generator from a worker thread, one piece at a time, on the event loop that owns it. `close()`
    (barge-in, hang-up) cancels the pending pull so the worker thread stops waiting at once."""

    def __init__(self, loop: asyncio.AbstractEventLoop, agen: Any) -> None:
        self._loop = loop
        self._agen = agen
        self._future: concurrent.futures.Future[tuple[bool, str]] | None = None
        self._closed = False

    def __iter__(self) -> "_AsyncToSync":
        return self

    def __next__(self) -> str:
        if self._closed:
            raise StopIteration

        async def pull() -> tuple[bool, str]:
            try:
                return True, await self._agen.__anext__()
            except StopAsyncIteration:
                return False, ""

        self._future = asyncio.run_coroutine_threadsafe(pull(), self._loop)
        try:
            more, piece = self._future.result()
        except concurrent.futures.CancelledError:
            raise StopIteration from None
        if not more:
            self._closed = True
            raise StopIteration
        return piece

    def close(self) -> None:
        self._closed = True
        if self._future is not None:
            self._future.cancel()


def call_brain_for(
    gateway: Any, agent_id: str, user_id: str, settings: Any, loop: asyncio.AbstractEventLoop
) -> Callable[[str], Iterable[str]]:
    """The sync `text -> pieces` brain `CallSession` wants. Streams when the gateway can (the real one can);
    otherwise one blocking `complete` per turn, which `CallSession`'s route already runs in a thread."""
    if hasattr(gateway, "stream_text"):
        brain = CallBrain(gateway, agent_id, user_id, settings)
        return lambda text: _AsyncToSync(loop, brain.reply(text))

    def one_shot(text: str) -> Iterable[str]:
        try:
            reply = gateway.complete(
                agent_id,
                conversation_id_for(user_id, "call"),
                text,
                system=CALL_SYSTEM_LINE,
                reasoning_effort=getattr(settings, "keiki_call_reasoning", "none"),
            )
        except KeikiBusy:
            return [BUSY_REPLY]
        except Exception as exc:  # fixed apology: never speak an exception message
            logger.warning("keiki call turn failed: %s", type(exc).__name__)
            return [ERROR_REPLY]
        return [_clip(getattr(reply, "text", "") or "")]

    return one_shot
