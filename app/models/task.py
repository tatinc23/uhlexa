import enum
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, TimestampMixin, UTCDateTime, UUIDPkMixin, enum_type, fk


class TaskKind(enum.StrEnum):
    GENERAL = "general"
    REMINDER = "reminder"
    CHORE = "chore"
    SHOPPING_LIST = "shopping_list"
    PURCHASE = "purchase"
    BOOKING = "booking"
    BROWSER = "browser"
    HANDOFF = "handoff"  # one member's Uhlexa asks another's to take over a request (addendum 21.7)


class TaskStatus(enum.StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RUNNING = "running"
    NEEDS_TAKEOVER = "needs_takeover"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


class Task(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "tasks"

    household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE", index=True)
    conversation_id: Mapped[uuid.UUID | None] = fk("conversations.id", ondelete="SET NULL")
    created_by_user_id: Mapped[uuid.UUID] = fk("users.id", ondelete="CASCADE")
    requested_by_agent_id: Mapped[uuid.UUID | None] = fk("agents.id", ondelete="SET NULL")
    # Who a handoff is for, so "handoffs to me" is an indexed query (JSON containment is not portable to SQLite).
    assignee_user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL", index=True)
    title: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str] = mapped_column(sa.Text, default="")
    kind: Mapped[TaskKind] = mapped_column(enum_type(TaskKind))
    status: Mapped[TaskStatus] = mapped_column(enum_type(TaskStatus))
    amount_cents: Mapped[int | None]
    # Column is named "metadata" (spec); the attribute cannot be, because `metadata` is reserved by SQLAlchemy.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONDict, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
