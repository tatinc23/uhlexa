"""Wire schema for the activity log."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models import ActorType


class ActivityEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    actor_type: ActorType
    actor_user_id: uuid.UUID | None
    actor_agent_id: uuid.UUID | None
    task_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    browser_task_id: uuid.UUID | None
    summary: str
    payload: dict[str, Any]
    created_at: datetime
