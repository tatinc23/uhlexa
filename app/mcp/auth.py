"""Who is calling the MCP server (addendum 21.4).

Two kinds of caller, never mixed:

* **A Keiki agent** sends `Authorization: Bearer <token>`. The sha256 of the token is looked up in
  `provider_bindings(provider=keiki, binding_type=mcp_token, external_id=<hash>)`; that binding names the agent, the
  agent names its owner, and the owner's household membership becomes the `Principal` (`actor_kind="agent_token"`).
  An unknown, revoked, child-owned or paused-agent token is a 401 with **no fallback**: a bad token never turns into
  the demo user, and a bearer header is never combined with the cookie or `X-Uhlexa-User`.
* **A person** (the simulator, the Family Board MCP app): only when `ENABLE_DEMO_AUTH` is on, and only by presenting the
  session cookie or an explicit `X-Uhlexa-User` (`actor_kind="human_session"`). `/mcp` never falls back to
  `DEMO_DEFAULT_USER`: no credentials is a 401.

Tokens are accepted only in the `Authorization` header: never in a query string and never as a tool argument.
"""

import hashlib
from collections.abc import Callable, Mapping
from http.cookies import CookieError, SimpleCookie
from typing import Any

import anyio
from mcp.server.mcpserver import Context
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.demo_auth import (
    AGENT_TOKEN,
    SESSION_COOKIE,
    USER_HEADER,
    Principal,
    authenticate,
    require_demo_auth,
)
from app.errors import Unauthorized
from app.models import (
    Agent,
    AgentStatus,
    BindingStatus,
    Household,
    HouseholdMembership,
    MembershipRole,
    ProviderBinding,
    ProviderName,
    User,
)
from app.settings import get_settings

TOKEN_BINDING_TYPE = "mcp_token"
MCP_PATH = "/mcp"
_INVALID_TOKEN = "Invalid or revoked agent token"  # one message for every failure: it must not say which check failed


def hash_token(token: str) -> str:
    """The value stored as `external_id` (hex sha256). Plaintext tokens are never stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized(_INVALID_TOKEN)
    return token


def agent_principal(db: Session, token: str) -> Principal:
    """The principal behind an agent token, or `Unauthorized`. Checks, in order: agent tokens are switched on, the
    token's hash has an active binding, the agent exists and is active, its owner belongs to the agent's household and
    is not a child."""
    if not get_settings().enable_keiki:
        raise Unauthorized("Agent tokens are disabled")
    binding = db.scalars(
        select(ProviderBinding).where(
            ProviderBinding.provider == ProviderName.KEIKI,
            ProviderBinding.binding_type == TOKEN_BINDING_TYPE,
            ProviderBinding.external_id == hash_token(token),
            ProviderBinding.status == BindingStatus.ACTIVE,
        )
    ).first()
    if binding is None or binding.agent_id is None:
        raise Unauthorized(_INVALID_TOKEN)
    agent = db.get(Agent, binding.agent_id)
    if agent is None or agent.status != AgentStatus.ACTIVE or agent.household_id != binding.household_id:
        raise Unauthorized(_INVALID_TOKEN)
    if binding.user_id is not None and binding.user_id != agent.owner_user_id:
        raise Unauthorized(_INVALID_TOKEN)
    membership = db.scalars(
        select(HouseholdMembership).where(
            HouseholdMembership.household_id == agent.household_id,
            HouseholdMembership.user_id == agent.owner_user_id,
        )
    ).first()
    user = db.get(User, agent.owner_user_id)
    household = db.get(Household, agent.household_id)
    if membership is None or user is None or household is None or membership.role == MembershipRole.CHILD:
        raise Unauthorized(_INVALID_TOKEN)
    return Principal(user, membership, household, agent, actor_kind=AGENT_TOKEN)


def _session_cookie(headers: Mapping[str, str]) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    try:
        jar: SimpleCookie = SimpleCookie()
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def principal_from_headers(db: Session, headers: Mapping[str, str] | None) -> Principal:
    """Shared by the tools (`resolve_mcp_principal`) and the HTTP gate.

    Over HTTP `headers` is always set, and then a caller must present credentials: a bearer token, or (demo auth only)
    the session cookie or an explicit `X-Uhlexa-User`. There is **no `DEMO_DEFAULT_USER` fallback on /mcp**: a request
    with nothing is a 401. `headers is None` means a non-HTTP transport (the in-process test client, stdio), which no
    remote caller can reach; only that path uses the default demo user."""
    if headers is None:
        return authenticate(db, session_token=None, header_handle=None)  # in-process only, never reachable over /mcp
    authorization = headers.get("authorization")
    if authorization is not None:
        return agent_principal(db, _bearer_token(authorization))  # no fallback, nothing else is consulted
    # DEMO-AUTH: replace before real users (the simulator and the Family Board MCP app are signed in this way)
    require_demo_auth()
    cookie = _session_cookie(headers)
    handle = (headers.get(USER_HEADER.lower()) or "").strip()
    if not cookie and not handle:
        raise Unauthorized("Credentials required: a bearer token, the session cookie or X-Uhlexa-User")
    return authenticate(db, session_token=cookie, header_handle=handle or None)


def resolve_mcp_principal(ctx: Context, db: Session) -> Principal:
    """The principal for one tool call. Raises `Unauthorized` (the tool wrapper turns it into a tool error; the HTTP
    gate has already answered 401 for a request that reaches here over HTTP with a bad token)."""
    return principal_from_headers(db, ctx.headers)


class McpAuthGate:
    """ASGI middleware: answer 401 for `/mcp` and `/mcp/{alias}` before the MCP layer sees a bad token.

    A tool error cannot be an HTTP status, and the addendum wants a real 401 for unknown, revoked, child-owned or
    disabled tokens. Every MCP request is checked, including `initialize` and `tools/list`, so a revoked token learns
    nothing about the server. Other paths pass straight through."""

    def __init__(self, app: ASGIApp, session_factory: Callable[[], Session]) -> None:
        self.app = app
        self._session_factory = session_factory

    def _check(self, headers: Mapping[str, str]) -> None:
        with self._session_factory() as db:
            principal_from_headers(db, headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == MCP_PATH or path.startswith(MCP_PATH + "/")):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        try:
            await anyio.to_thread.run_sync(self._check, headers)
        except Unauthorized as exc:
            body: dict[str, Any] = exc.body()
            await JSONResponse(body, status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        await self.app(scope, receive, send)
