"""Who is behind an inbound channel turn (addendum 21.1 flow B, 21.5 "identity binding", 21.8).

An inbound turn names the AGENT that was addressed (`agent_ref`: an Inkbox handle, or the Primitive inbox address)
and the SENDER (`sender_ref`, a number or an address the provider reports). Nobody until a binding says so:

* `agent_ref` -> an active `inkbox_identity` (or `primitive_inbox`) binding -> its `user_id` is the OWNER of that agent;
* `sender_ref` -> an active `inkbox_imessage | inkbox_phone` (or `primitive_email`) binding whose `user_id` is that SAME
  owner. Inkbox email never resolves (see `resolve`): its From address cannot be verified. Primitive email resolves
  only when Primitive itself verified the From address (`turn.sender_verified`).

The Keiki agent acts with its owner's authority, so a household member who is not the owner never gets through, and
children never resolve at all. Anything else raises `UnknownSender`: the caller sends nothing and logs a reason code.
This does not depend on `ENABLE_DEMO_AUTH`: it builds the Principal from bindings, not from a header or cookie.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal, resolve_principal
from app.channels.types import InboundTurn
from app.models import AgentStatus, BindingStatus, MembershipRole, ProviderBinding, ProviderName, User

IDENTITY_BINDING = "inkbox_identity"
EMAIL_CHANNEL = "inkbox:email"
PRIMITIVE_EMAIL_CHANNEL = "primitive:email"
PRIMITIVE_INBOX_BINDING = "primitive_inbox"
# Which binding types can vouch for a sender, by channel. iMessage on a dedicated line may report an Apple ID address
# as the remote, so the iMessage binding is matched case-insensitively on whatever `external_id` holds.
SENDER_BINDING_TYPES: dict[str, tuple[str, ...]] = {
    "inkbox:imessage": ("inkbox_imessage", "inkbox_phone"),
    "inkbox:sms": ("inkbox_phone", "inkbox_imessage"),
    "inkbox:call": ("inkbox_phone", "inkbox_imessage"),
    "inkbox:email": ("inkbox_email",),
    PRIMITIVE_EMAIL_CHANNEL: ("primitive_email",),
}


class UnknownSender(Exception):
    """Fail closed. `reason` is a stable, secret-free code; `household_id` is set when the addressed agent is known,
    so the rejection can be put on that household's activity log (None: only the server log can see it)."""

    def __init__(
        self, reason: str, *, household_id: uuid.UUID | None = None, owner_user_id: uuid.UUID | None = None
    ) -> None:
        self.reason = reason
        self.household_id = household_id
        self.owner_user_id = owner_user_id
        super().__init__(reason)


def _active_bindings(
    db: Session, binding_types: tuple[str, ...], external_id: str, provider: ProviderName = ProviderName.INKBOX
) -> list[ProviderBinding]:
    return list(
        db.scalars(
            select(ProviderBinding).where(
                ProviderBinding.provider == provider,
                ProviderBinding.binding_type.in_(binding_types),
                func.lower(ProviderBinding.external_id) == external_id.strip().lower(),
                ProviderBinding.status == BindingStatus.ACTIVE,
                ProviderBinding.user_id.is_not(None),
            )
        )
    )


def identity_owner(db: Session, handle: str) -> ProviderBinding | None:
    """The active `inkbox_identity` binding for `handle` (case-insensitive), or None."""
    found = _active_bindings(db, (IDENTITY_BINDING,), handle) if handle.strip() else []
    return found[0] if found else None


def _channel_provider(channel: str) -> ProviderName:
    return ProviderName.PRIMITIVE if channel == PRIMITIVE_EMAIL_CHANNEL else ProviderName.INKBOX


def _agent_owner(db: Session, turn: InboundTurn) -> ProviderBinding | None:
    if turn.channel != PRIMITIVE_EMAIL_CHANNEL:
        return identity_owner(db, turn.agent_ref)
    found = (
        _active_bindings(db, (PRIMITIVE_INBOX_BINDING,), turn.agent_ref, ProviderName.PRIMITIVE)
        if turn.agent_ref.strip()
        else []
    )
    return found[0] if found else None


def resolve(db: Session, turn: InboundTurn) -> Principal:
    """The owner's Principal, or `UnknownSender`. Never trusts anything in `turn` beyond the two reference strings."""
    sender_types = SENDER_BINDING_TYPES.get(turn.channel)
    if sender_types is None:
        raise UnknownSender("unsupported_channel")
    identity = _agent_owner(db, turn)
    if identity is None or identity.user_id is None:
        raise UnknownSender("unknown_agent")
    ctx = {"household_id": identity.household_id, "owner_user_id": identity.user_id}
    if turn.channel == EMAIL_CHANNEL:
        # An email From address is forgeable, and the Inkbox webhook and SDK (0.7.20) expose no verified-sender signal
        # (`sender_access` is admission, "not sender trust"). Fail closed until one exists.
        raise UnknownSender("unverified_sender", **ctx)
    if turn.channel == PRIMITIVE_EMAIL_CHANNEL and not turn.sender_verified:
        raise UnknownSender("unverified_sender", **ctx)  # a forged From must never reach the owner's agent
    if not turn.sender_ref.strip():
        raise UnknownSender("no_sender", **ctx)
    bindings = _active_bindings(db, sender_types, turn.sender_ref, _channel_provider(turn.channel))
    if not bindings:
        raise UnknownSender("unbound_sender", **ctx)
    if not any(b.user_id == identity.user_id for b in bindings):
        raise UnknownSender("sender_not_owner", **ctx)
    user = db.get(User, identity.user_id)
    if user is None:
        raise UnknownSender("unknown_agent", **ctx)
    principal = resolve_principal(db, user.handle)
    if principal is None:
        raise UnknownSender("no_principal", **ctx)
    if principal.role == MembershipRole.CHILD:
        raise UnknownSender("child", **ctx)
    if principal.agent.status != AgentStatus.ACTIVE:
        raise UnknownSender("agent_paused", **ctx)
    return principal
