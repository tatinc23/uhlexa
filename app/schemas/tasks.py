"""Wire schemas for tasks."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models import TaskKind, TaskStatus
from app.schemas.approvals import ApprovalOut, PolicyDecisionOut


class TaskCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: TaskKind = TaskKind.GENERAL
    items: list[str] | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=5000)
    amount_cents: int | None = Field(default=None, ge=0)
    conversation_id: uuid.UUID | None = None


class TaskCancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class TaskOut(BaseModel):
    # `Task.meta` is the column named "metadata"; the wire name is `metadata` (spec section 6).
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    conversation_id: uuid.UUID | None
    created_by_user_id: uuid.UUID
    requested_by_agent_id: uuid.UUID | None
    title: str
    description: str
    kind: TaskKind
    status: TaskStatus
    amount_cents: int | None
    metadata: dict[str, Any] = Field(validation_alias=AliasChoices("meta", "metadata"))
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None


class TaskCreateOut(BaseModel):
    task: TaskOut
    approval: ApprovalOut | None
    decision: PolicyDecisionOut
