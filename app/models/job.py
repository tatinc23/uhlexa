import enum
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, TimestampMixin, UTCDateTime, UUIDPkMixin, enum_type, fk, utcnow


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (sa.Index("ix_jobs_status_run_after", "status", "run_after"),)

    kind: Mapped[str] = mapped_column(sa.String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    status: Mapped[JobStatus] = mapped_column(enum_type(JobStatus), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    locked_by: Mapped[str | None] = mapped_column(sa.String(128))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    household_id: Mapped[uuid.UUID | None] = fk("households.id", ondelete="CASCADE")
