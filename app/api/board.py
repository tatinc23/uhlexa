"""Who am I, and the Family Board payload (one builder, shared with MCP)."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.auth.dependencies import get_principal
from app.db.session import get_db
from app.schemas.auth import PrincipalOut
from app.services import board_service

router = APIRouter(prefix="/api", tags=["board"])


@router.get("/me")
def me(principal: Principal = Depends(get_principal)) -> PrincipalOut:
    return PrincipalOut.from_principal(principal)


@router.get("/board")
def get_board(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    return board_service.build_family_board(db, principal)
