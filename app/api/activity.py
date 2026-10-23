"""Activity log route (spec section 14)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.auth.dependencies import get_principal
from app.db.session import get_db
from app.schemas.activity import ActivityEventOut
from app.services import activity_service, views

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("")
def list_activity(
    limit: int = Query(50, ge=1, le=200),
    task_id: str | None = None,
    conversation_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ActivityEventOut]:
    events = activity_service.list_events(db, principal, limit=limit, task_id=task_id, conversation_id=conversation_id)
    return [views.activity_out(e) for e in events]
