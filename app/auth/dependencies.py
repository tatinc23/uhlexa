"""FastAPI dependencies for authentication."""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.demo_auth import SESSION_COOKIE, USER_HEADER, Principal, authenticate
from app.db.session import get_db


# DEMO-AUTH: replace before real users
def get_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    """Cookie `uhlexa_session`, then header `X-Uhlexa-User`, then DEMO_DEFAULT_USER. 401 when demo auth is off or the
    handle is unknown."""
    return authenticate(
        db, session_token=request.cookies.get(SESSION_COOKIE), header_handle=request.headers.get(USER_HEADER)
    )
