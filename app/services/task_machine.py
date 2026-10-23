"""The task status machine (spec section 6) and the household-scoped task lookup.

Kept below `task_service`, `approval_service` and `browser_task_service` so all three can move tasks along
without importing each other.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import InvalidTransition, NotFound
from app.models import Task, TaskKind, TaskStatus
from app.services import access, activity_service
from app.services.clock import tick

S = TaskStatus

TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.PENDING_APPROVAL: frozenset({S.APPROVED, S.DENIED, S.CANCELLED}),
    S.APPROVED: frozenset({S.RUNNING, S.COMPLETED, S.CANCELLED}),
    # RUNNING -> APPROVED: the run was interrupted by a provider outage and can be started again.
    S.RUNNING: frozenset({S.NEEDS_TAKEOVER, S.COMPLETED, S.FAILED, S.CANCELLED, S.APPROVED}),
    S.NEEDS_TAKEOVER: frozenset({S.RUNNING, S.FAILED, S.CANCELLED}),
}
TERMINAL = frozenset({S.COMPLETED, S.FAILED, S.DENIED, S.CANCELLED})

# Kinds with nothing to automate: once nobody needs to approve them, recording them is the whole job.
RECORDING_KINDS = frozenset({TaskKind.GENERAL, TaskKind.REMINDER, TaskKind.CHORE})
# Kinds a browser task can run for.
BROWSER_KINDS = frozenset({TaskKind.SHOPPING_LIST, TaskKind.BROWSER})


def get(db: Session, principal: Principal, task_id: uuid.UUID | str) -> Task:
    task = db.scalars(
        select(Task).where(
            Task.id == access.as_uuid(task_id),
            Task.household_id == principal.household.id,
            access.visible_conversation_clause(db, principal, Task.conversation_id),
        )
    ).first()
    if task is None:
        raise NotFound("Task not found")
    return task


def transition(
    db: Session, task: Task, new_status: TaskStatus | str, *, actor: Principal | None, note: str | None = None
) -> Task:
    """Move `task` along the machine and log `task.status_changed`. `actor=None` means the system did it."""
    new = TaskStatus(new_status)
    old = TaskStatus(task.status)
    if new not in TRANSITIONS.get(old, frozenset()):
        raise InvalidTransition(f"A task cannot go from {old.value} to {new.value}.")
    task.status = new
    if new == S.COMPLETED:
        task.completed_at = tick()
    elif new == S.CANCELLED:
        task.cancelled_at = tick()
    db.flush()
    payload = {"from": old.value, "to": new.value}
    if note:
        payload["note"] = note[:300]
    activity_service.record(
        db,
        household_id=task.household_id,
        event_type="task.status_changed",
        summary=f"Task “{task.title[:80]}” went from {old.value} to {new.value}.",
        actor_type="user" if actor else "system",
        actor_user_id=actor.user.id if actor else None,
        task_id=task.id,
        conversation_id=task.conversation_id,
        payload=payload,
    )
    return task
