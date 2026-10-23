import enum
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, TimestampMixin, UTCDateTime, UUIDPkMixin, enum_type
from app.models.binding import ProviderName


class WebhookStatus(enum.StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


class WebhookEvent(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "webhook_events"
    __table_args__ = (sa.UniqueConstraint("provider", "external_event_id", name="uq_webhook_events_provider_event"),)

    provider: Mapped[ProviderName] = mapped_column(enum_type(ProviderName))
    external_event_id: Mapped[str] = mapped_column(sa.String(255))
    event_type: Mapped[str] = mapped_column(sa.String(128))
    signature_valid: Mapped[bool]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    status: Mapped[WebhookStatus] = mapped_column(enum_type(WebhookStatus), default=WebhookStatus.RECEIVED)
    error: Mapped[str | None] = mapped_column(sa.Text)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
