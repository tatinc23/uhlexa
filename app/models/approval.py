import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPkMixin, enum_type, fk


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"


class DecisionSource(enum.StrEnum):
    POLICY = "policy"
    POLICY_SNAP = "policy+snap"


class Approval(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "approvals"

    household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE", index=True)
    task_id: Mapped[uuid.UUID] = fk("tasks.id", ondelete="CASCADE", index=True)
    requested_by_agent_id: Mapped[uuid.UUID | None] = fk("agents.id", ondelete="SET NULL")
    requested_for_user_id: Mapped[uuid.UUID] = fk("users.id", ondelete="CASCADE")
    decided_by_user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL")
    status: Mapped[ApprovalStatus] = mapped_column(enum_type(ApprovalStatus), default=ApprovalStatus.PENDING)
    rule_id: Mapped[str] = mapped_column(sa.String(64))
    reason: Mapped[str] = mapped_column(sa.Text, default="")
    decision_source: Mapped[DecisionSource] = mapped_column(enum_type(DecisionSource), default=DecisionSource.POLICY)
    decision_note: Mapped[str | None] = mapped_column(sa.Text)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # `APPROVE <code>` by text: a random one-time code, kept only as a hash and cleared when the approval is decided.
    text_code_hash: Mapped[str | None] = mapped_column(sa.String(64))
    text_code_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
