"""The household activity log: what happened, who did it, in order. Written by every other service."""

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import NotFound
from app.models import ActivityEvent, Task
from app.services import access
from app.services.clock import tick

MAX_SUMMARY_CHARS = 1000
MAX_LIMIT = 200


def record(
    db: Session,
    *,
    household_id: uuid.UUID,
    event_type: str,
    summary: str,
    actor_type: str = "system",
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    browser_task_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> ActivityEvent:
    """Append one event. Never put secrets (keys, takeover links) in `payload`; it is shown to the household."""
    event = ActivityEvent(
        household_id=household_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        task_id=task_id,
        conversation_id=conversation_id,
        browser_task_id=browser_task_id,
        summary=summary[:MAX_SUMMARY_CHARS],
        payload=dict(payload or {}),
        created_at=tick(),
    )
    db.add(event)
    db.flush()
    return event


def list_events(
    db: Session,
    principal: Principal,
    *,
    limit: int = 50,
    task_id: uuid.UUID | str | None = None,
    conversation_id: uuid.UUID | str | None = None,
) -> list[ActivityEvent]:
    """Newest first. Events about conversations the caller cannot read are left out."""
    stmt = select(ActivityEvent).where(ActivityEvent.household_id == principal.household.id)
    if task_id is not None:
        found = db.scalars(
            select(Task.id).where(Task.id == access.as_uuid(task_id), Task.household_id == principal.household.id)
        ).first()
        if found is None:
            raise NotFound("Task not found")
        stmt = stmt.where(ActivityEvent.task_id == found)
    if conversation_id is not None:
        stmt = stmt.where(ActivityEvent.conversation_id == access.get_accessible(db, principal, conversation_id).id)
    elif hidden := access.hidden_conversation_ids(db, principal):
        stmt = stmt.where(or_(ActivityEvent.conversation_id.is_(None), ActivityEvent.conversation_id.not_in(hidden)))
    stmt = stmt.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc()).limit(max(1, min(limit, MAX_LIMIT)))
    return list(db.scalars(stmt))
