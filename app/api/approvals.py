"""Approval routes (spec section 14). Only a signed-in person decides; the service enforces who."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.auth.dependencies import get_principal
from app.db.session import get_db
from app.models import ApprovalStatus
from app.schemas.approvals import ApprovalDecisionIn, ApprovalOut, ApprovalTextCodeOut
from app.services import approval_service

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("")
def list_approvals(
    status: ApprovalStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ApprovalOut]:
    rows = approval_service.list_approvals(db, principal, status=status, limit=limit)
    return approval_service.views_for(db, principal, rows)


def _decide(
    approval_id: str, body: ApprovalDecisionIn | None, principal: Principal, db: Session, *, approve: bool
) -> ApprovalOut:
    approval = approval_service.decide(db, principal, approval_id, approve=approve, note=body.note if body else None)
    out = approval_service.view_for(db, principal, approval)
    db.commit()
    return out


@router.post("/{approval_id}/text-code")
def text_code(
    approval_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> ApprovalTextCodeOut:
    """A one-time code to answer this approval with `APPROVE <code>` by iMessage. Shown once; a new call replaces it."""
    code = approval_service.issue_text_code(db, principal, approval_id)
    expires_at = approval_service.get_for(db, principal, approval_id).text_code_expires_at
    db.commit()
    return ApprovalTextCodeOut(code=code, expires_at=expires_at)


@router.post("/{approval_id}/approve")
def approve(
    approval_id: str,
    body: ApprovalDecisionIn | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ApprovalOut:
    return _decide(approval_id, body, principal, db, approve=True)


@router.post("/{approval_id}/deny")
def deny(
    approval_id: str,
    body: ApprovalDecisionIn | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ApprovalOut:
    return _decide(approval_id, body, principal, db, approve=False)
