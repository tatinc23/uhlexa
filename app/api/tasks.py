"""Task routes (spec section 14)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.auth.dependencies import get_principal
from app.db.session import get_db
from app.models import TaskStatus
from app.providers.subq.service import SnapService
from app.schemas.tasks import TaskCancelIn, TaskCreateIn, TaskCreateOut, TaskOut
from app.services import approval_service, task_machine, task_service, views
from app.services.providers import get_snap

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
def list_tasks(
    status: TaskStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[TaskOut]:
    return [views.task_out(t) for t in task_service.list_tasks(db, principal, status=status, limit=limit)]


@router.post("")
def create_task(
    body: TaskCreateIn,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    snap: SnapService = Depends(get_snap),
) -> TaskCreateOut:
    result = task_service.create(
        db,
        principal,
        title=body.title,
        kind=body.kind,
        items=body.items,
        description=body.description,
        amount_cents=body.amount_cents,
        conversation_id=body.conversation_id,
        source="rest",
        snap=snap,
    )
    out = TaskCreateOut(
        task=views.task_out(result.task),
        approval=approval_service.view_for(db, principal, result.approval) if result.approval else None,
        decision=views.policy_decision_out(result.decision),
    )
    db.commit()
    return out


@router.get("/{task_id}")
def get_task(task_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> TaskOut:
    return views.task_out(task_machine.get(db, principal, task_id))


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: str,
    body: TaskCancelIn | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> TaskOut:
    task = task_service.cancel(db, principal, task_id, body.reason if body else None)
    out = views.task_out(task)
    db.commit()
    return out
