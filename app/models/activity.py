import enum
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, TimestampMixin, UUIDPkMixin, enum_type, fk


class ActorType(enum.StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    PROVIDER = "provider"


class ActivityEvent(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "activity_events"
    __table_args__ = (sa.Index("ix_activity_events_household_id_created_at", "household_id", "created_at"),)

    household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE")
    event_type: Mapped[str] = mapped_column(sa.String(64))
    actor_type: Mapped[ActorType] = mapped_column(enum_type(ActorType), default=ActorType.SYSTEM)
    actor_user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL")
    actor_agent_id: Mapped[uuid.UUID | None] = fk("agents.id", ondelete="SET NULL")
    task_id: Mapped[uuid.UUID | None] = fk("tasks.id", ondelete="SET NULL")
    conversation_id: Mapped[uuid.UUID | None] = fk("conversations.id", ondelete="SET NULL")
    browser_task_id: Mapped[uuid.UUID | None] = fk("browser_tasks.id", ondelete="SET NULL")
    summary: Mapped[str] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
