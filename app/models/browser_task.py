import enum
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, JSONList, TimestampMixin, UUIDPkMixin, enum_type, fk


class BrowserTaskStatus(enum.StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    NEEDS_TAKEOVER = "needs_takeover"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class BrowserTask(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "browser_tasks"

    household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE", index=True)
    user_id: Mapped[uuid.UUID] = fk("users.id", ondelete="CASCADE")
    agent_id: Mapped[uuid.UUID] = fk("agents.id", ondelete="CASCADE")
    conversation_id: Mapped[uuid.UUID | None] = fk("conversations.id", ondelete="SET NULL")
    task_id: Mapped[uuid.UUID] = fk("tasks.id", ondelete="CASCADE", index=True)
    maritime_computer_id: Mapped[str | None] = mapped_column(sa.String(255))
    task_type: Mapped[str] = mapped_column(sa.String(64))
    requested_url: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[BrowserTaskStatus] = mapped_column(enum_type(BrowserTaskStatus), default=BrowserTaskStatus.QUEUED)
    last_screenshot_ref: Mapped[str | None] = mapped_column(sa.String(512))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    requires_takeover: Mapped[bool] = mapped_column(default=False)
    takeover_url: Mapped[str | None] = mapped_column(sa.Text)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONList, default=list)
    result: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    verified: Mapped[bool] = mapped_column(default=False)
