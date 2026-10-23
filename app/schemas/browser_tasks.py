"""Wire schemas for browser tasks. Screenshot bytes are never inlined here (fetch the screenshot route), and the
takeover link is a secret that only the owning user gets."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models import BrowserTaskStatus


class BrowserTaskStartIn(BaseModel):
    task_id: uuid.UUID


class BrowserStepOut(BaseModel):
    index: int
    action: str
    ok: bool
    note: str = ""
    screenshot_ref: str | None = None


class BrowserTaskOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    conversation_id: uuid.UUID | None
    user_id: uuid.UUID
    agent_id: uuid.UUID
    task_type: str
    requested_url: str
    status: BrowserTaskStatus
    requires_takeover: bool
    takeover_url: str | None  # only for the owning user, only while a takeover is pending
    last_screenshot_ref: str | None
    last_error: str | None
    steps: list[BrowserStepOut]
    verified: bool
    result: dict[str, Any]
    is_owner: bool
    created_at: datetime
    updated_at: datetime
