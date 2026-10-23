"""Demo sign-in and reset routes (spec sections 8 and 14)."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.auth.demo_auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    USER_HEADER,
    demo_principals,
    require_demo_auth,
    resolve_principal,
    sign_session,
)
from app.db.session import get_db
from app.errors import Forbidden, NotFound, Unauthorized
from app.models import MembershipRole
from app.schemas.auth import DemoResetOut, DemoUsersOut, LoginIn, PrincipalOut
from app.seed import reset_demo
from app.settings import get_settings

# DEMO-AUTH: replace before real users
router = APIRouter(prefix="/demo", tags=["demo"])


def _users_out(db: Session) -> list[PrincipalOut]:
    return [PrincipalOut.from_principal(principal) for principal in demo_principals(db)]


@router.get("/users")
def list_users(db: Session = Depends(get_db)) -> DemoUsersOut:
    """The sign-in screen's user list. Public, and still served when demo auth is off."""
    return DemoUsersOut(users=_users_out(db))


@router.post("/login", dependencies=[Depends(require_demo_auth)])
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> PrincipalOut:
    principal = resolve_principal(db, body.handle)
    if principal is None:
        raise Unauthorized("Unknown demo user")
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(principal.user.handle),
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return PrincipalOut.from_principal(principal)


@router.post("/reset")
def reset(request: Request, db: Session = Depends(get_db)) -> DemoResetOut:
    """Wipe and reseed. Development only: any other environment answers 404 as if the route did not exist.

    Needs an explicit `X-Uhlexa-User` header naming a guardian. The cookie and the default-user fallback don't count,
    and a custom header forces a CORS preflight, so another site can't trigger a reset. The one exception is a
    database with no demo users yet, where reset is the way out of a broken demo.
    """
    if not get_settings().is_development:
        raise NotFound()
    require_demo_auth()
    handle = request.headers.get(USER_HEADER)
    if not handle:
        raise Unauthorized(f"Reset needs the {USER_HEADER} header")
    if demo_principals(db):
        principal = resolve_principal(db, handle)
        if principal is None:
            raise Unauthorized("Unknown demo user")
        if principal.role != MembershipRole.GUARDIAN:
            raise Forbidden("Only a guardian can reset the demo")
    reset_demo(db)
    db.commit()
    return DemoResetOut(users=_users_out(db))
