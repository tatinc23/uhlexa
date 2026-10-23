"""Demo-only authentication (spec section 8).

Anyone who can reach the server can claim any demo user with the
`X-Uhlexa-User` header (or the configured default user); the signed cookie only protects the cookie itself.
Everything downstream trusts the `Principal` built here and never an id taken from request input.
"""

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Unauthorized
from app.models import Agent, Household, HouseholdMembership, MembershipRole, User
from app.settings import get_settings

# DEMO-AUTH: replace before real users (this whole module, `get_principal`, and the /demo routes)
SESSION_COOKIE = "uhlexa_session"
USER_HEADER = "X-Uhlexa-User"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600
_MAC_CONTEXT = b"uhlexa-demo-session-v1|"  # domain separation: the secret signs nothing else


HUMAN_SESSION = "human_session"  # a person at the simulator, the Family Board or the REST API
AGENT_TOKEN = "agent_token"  # a Keiki agent calling MCP with its per-agent bearer token (addendum 21.4)


@dataclass(frozen=True)
class Principal:
    """Who is acting: the user, their household membership, the household, and the acting agent (the user's default
    agent for a person; the token's own agent for an agent token). `actor_kind` says whether a person or a model is on
    the other end: models never approve (`approve_action`, `resume_after_takeover` refuse `agent_token`)."""

    user: User
    membership: HouseholdMembership
    household: Household
    agent: Agent
    actor_kind: str = HUMAN_SESSION

    @property
    def is_agent_token(self) -> bool:
        return self.actor_kind == AGENT_TOKEN

    @property
    def role(self) -> MembershipRole:
        return self.membership.role

    @property
    def is_child(self) -> bool:
        return self.role == MembershipRole.CHILD

    @property
    def is_guardian_or_adult(self) -> bool:
        return self.role in (MembershipRole.GUARDIAN, MembershipRole.ADULT)


# --- signed session cookie: "<b64 handle>.<issued-at>.<hmac-sha256 hex>" --------------------------------------------


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(payload: str) -> str:
    key = get_settings().app_session_secret.encode()
    return hmac.new(key, _MAC_CONTEXT + payload.encode(), hashlib.sha256).hexdigest()


def sign_session(handle: str, *, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    payload = f"{_b64encode(handle.encode())}.{issued_at}"
    return f"{payload}.{_mac(payload)}"


def read_session(token: str, *, now: int | None = None) -> str | None:
    """The handle a valid, unexpired token was issued for; None for anything else (never raises)."""
    if not token.isascii():
        return None
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(_mac(payload).encode(), signature.encode()):
        return None
    try:
        encoded_handle, issued_at = payload.split(".")
        age = (int(time.time()) if now is None else now) - int(issued_at)
        handle = _b64decode(encoded_handle).decode()
    except ValueError:
        return None
    return handle if age <= SESSION_MAX_AGE_SECONDS else None


# --- principals ---------------------------------------------------------------------------------------------------


def resolve_principal(db: Session, handle: str) -> Principal | None:
    """The principal for a handle, or None when the user is unknown, has no household, or has no default agent."""
    row = db.execute(
        select(User, HouseholdMembership, Household)
        .join(HouseholdMembership, HouseholdMembership.user_id == User.id)
        .join(Household, Household.id == HouseholdMembership.household_id)
        .where(User.handle == handle.strip().lower())
        .order_by(HouseholdMembership.created_at, HouseholdMembership.id)
    ).first()
    if row is None:
        return None
    user, membership, household = row
    agent = db.scalars(
        select(Agent)
        .where(Agent.household_id == household.id, Agent.owner_user_id == user.id, Agent.is_default.is_(True))
        .order_by(Agent.created_at, Agent.id)
    ).first()
    return None if agent is None else Principal(user, membership, household, agent)


def demo_principals(db: Session) -> list[Principal]:
    """Every demo user that can sign in, ordered by handle (the sign-in screen's list)."""
    handles = db.scalars(select(User.handle).where(User.is_demo.is_(True)).order_by(User.handle)).all()
    return [principal for handle in handles if (principal := resolve_principal(db, handle))]


def require_demo_auth() -> None:
    if not get_settings().enable_demo_auth:
        raise Unauthorized("Demo auth is disabled")


# DEMO-AUTH: replace before real users (the unauthenticated header and default user)
def requested_handle(*, session_token: str | None, header_handle: str | None) -> str:
    """Which demo user a request claims to be: signed cookie, then `X-Uhlexa-User`, then DEMO_DEFAULT_USER.

    A cookie that is present but invalid (tampered, expired, secret rotated) is never silently replaced by the default
    user: without a header the request is rejected, so a stale child session cannot become the guardian.
    """
    if session_token and (handle := read_session(session_token)):
        return handle
    if header_handle and header_handle.strip():
        return header_handle
    if session_token:
        raise Unauthorized("Session expired or invalid; sign in again")
    return get_settings().demo_default_user


def authenticate(db: Session, *, session_token: str | None, header_handle: str | None) -> Principal:
    """Shared by REST (`get_principal`) and MCP (header only, no cookie). Raises Unauthorized (401)."""
    require_demo_auth()
    principal = resolve_principal(db, requested_handle(session_token=session_token, header_handle=header_handle))
    if principal is None:
        raise Unauthorized("Unknown demo user")
    return principal
