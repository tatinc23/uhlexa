import enum
import uuid
from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPkMixin, enum_type, fk


class TrustedContactStatus(enum.StrEnum):
    OFFERED = "offered"  # one adult offered; nothing is allowed yet
    ACTIVE = "active"  # the other adult accepted: handoffs and contact may cross households
    REVOKED = "revoked"  # either side ended it (or declined the offer); final


class TrustedContact(UUIDPkMixin, TimestampMixin, Base):
    """A link between two ADULT users in DIFFERENT households, active only after both opt in (offer, then accept).

    Either side can revoke at any time. The link moves a request or a message, never permission or data."""

    __tablename__ = "trusted_contacts"

    requester_user_id: Mapped[uuid.UUID] = fk("users.id", ondelete="CASCADE", index=True)
    requester_household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE")
    target_user_id: Mapped[uuid.UUID] = fk("users.id", ondelete="CASCADE", index=True)
    target_household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE")
    status: Mapped[TrustedContactStatus] = mapped_column(
        enum_type(TrustedContactStatus), default=TrustedContactStatus.OFFERED
    )
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_by_user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL")
