"""The Family Board payload: one builder shared by `GET /api/board` and the MCP `get_family_board` tool."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.models import (
    Agent,
    ApprovalStatus,
    ConversationStatus,
    HouseholdMembership,
    Task,
    User,
)
from app.providers import provider_statuses
from app.schemas.approvals import ApprovalOut
from app.schemas.auth import HouseholdOut
from app.services import access, activity_service, approval_service, browser_task_service, task_service, views
from app.services.clock import tick
from app.settings import get_settings

TASK_LIMIT = 40
CONVERSATION_LIMIT = 20
RECENT_APPROVALS = 8
BROWSER_TASK_LIMIT = 6
ACTIVITY_LIMIT = 30


def _members(db: Session, principal: Principal) -> list[dict[str, Any]]:
    rows = db.execute(
        select(User, HouseholdMembership.role, Agent)
        .join(HouseholdMembership, HouseholdMembership.user_id == User.id)
        .join(Agent, (Agent.owner_user_id == User.id) & (Agent.household_id == HouseholdMembership.household_id))
        .where(HouseholdMembership.household_id == principal.household.id, Agent.is_default.is_(True))
        .order_by(User.handle)
    )
    return [
        {
            "user_id": str(user.id),
            "handle": user.handle,
            "display_name": user.display_name,
            "role": access.enum_value(role),
            "agent": {"id": str(agent.id), "name": agent.name},
            "is_viewer": user.id == principal.user.id,
        }
        for user, role, agent in rows
    ]


def _counts(db: Session, principal: Principal, conversations: list[Any]) -> dict[str, Any]:
    rows = db.execute(
        select(Task.status, func.count(Task.id))
        .where(Task.household_id == principal.household.id)
        .group_by(Task.status)
    )
    by_status = {status: n for status, n in rows}
    return {
        "tasks": {access.enum_value(status): n for status, n in by_status.items()},
        "pending_approvals": len(approval_service.list_approvals(db, principal, status=ApprovalStatus.PENDING)),
        "open_conversations": sum(1 for c in conversations if c.status == ConversationStatus.OPEN),
        "browser_tasks_active": len(
            [
                bt
                for bt in browser_task_service.list_recent(db, principal, limit=50)
                if bt.status in browser_task_service.ACTIVE
            ]
        ),
    }


def build_family_board(db: Session, principal: Principal) -> dict[str, Any]:
    with access.visibility_snapshot(db):
        return _build_family_board(db, principal)


def _build_family_board(db: Session, principal: Principal) -> dict[str, Any]:
    """Everything the board shows for this viewer, JSON-ready. Household-scoped, conversation-policy filtered, no
    secrets; `can_decide` on each approval and the takeover link on each browser task are viewer-relative."""
    conversations = access.accessible_conversations(db, principal)
    pending = approval_service.list_approvals(db, principal, status=ApprovalStatus.PENDING, limit=50)
    decided = [
        a for a in approval_service.list_approvals(db, principal, limit=50) if a.status != ApprovalStatus.PENDING
    ][:RECENT_APPROVALS]
    approvals: dict[str, list[ApprovalOut]] = {
        "pending": approval_service.views_for(db, principal, pending),
        "recent": approval_service.views_for(db, principal, decided),
    }
    browser_tasks = [
        views.browser_task_out(bt, principal, include_result=False)
        for bt in browser_task_service.list_recent(db, principal, limit=BROWSER_TASK_LIMIT)
    ]
    return {
        "household": HouseholdOut.model_validate(principal.household).model_dump(mode="json"),
        "viewer": {
            "user_id": str(principal.user.id),
            "handle": principal.user.handle,
            "display_name": principal.user.display_name,
            "role": access.enum_value(principal.role),
            "agent": {"id": str(principal.agent.id), "name": principal.agent.name},
        },
        "members": _members(db, principal),
        "conversations": [
            c.model_dump(mode="json") for c in views.conversation_outs(db, conversations[:CONVERSATION_LIMIT])
        ],
        "tasks": [
            views.task_out(t).model_dump(mode="json") for t in task_service.list_tasks(db, principal, limit=TASK_LIMIT)
        ],
        "counts": _counts(db, principal, conversations),
        "approvals": {key: [a.model_dump(mode="json") for a in value] for key, value in approvals.items()},
        "browser_tasks": [bt.model_dump(mode="json") for bt in browser_tasks],
        "activity": [
            views.activity_out(e).model_dump(mode="json")
            for e in activity_service.list_events(db, principal, limit=ACTIVITY_LIMIT)
        ],
        "providers": {name: status.as_dict() for name, status in provider_statuses(get_settings()).items()},
        "generated_at": tick().isoformat(),
    }
