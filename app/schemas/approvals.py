"""Wire schemas for approvals. `can_decide` is relative to the viewer, so the UI shows Approve/Deny only to people
who may press them (children see "Waiting for a grown-up")."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import ApprovalStatus, DecisionSource, TaskKind


class ApprovalTaskOut(BaseModel):
    """Just enough of the task to render the approval card."""

    id: uuid.UUID
    title: str
    kind: TaskKind
    amount_cents: int | None
    items: list[str]
    will_use_browser: bool


class ApprovalOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    task: ApprovalTaskOut
    status: ApprovalStatus
    rule_id: str
    reason: str
    decision_source: DecisionSource
    ai_assisted: bool
    requested_for_user_id: uuid.UUID
    requested_for_name: str | None
    requested_by_agent_id: uuid.UUID | None
    decided_by_user_id: uuid.UUID | None
    decided_by_name: str | None
    decision_note: str | None
    decided_at: datetime | None
    created_at: datetime
    can_decide: bool
    can_decide_reason: str


class ApprovalDecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ApprovalTextCodeOut(BaseModel):
    code: str
    expires_at: datetime | None


class PolicyDecisionOut(BaseModel):
    requires_approval: bool
    rule_id: str | None
    reason: str
    approver_roles: list[str]
    requester_may_approve: bool
    source: str
