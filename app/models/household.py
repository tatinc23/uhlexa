import enum
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONDict, TimestampMixin, UUIDPkMixin, enum_type, fk
from app.models.user import User


class MembershipRole(enum.StrEnum):
    GUARDIAN = "guardian"
    ADULT = "adult"
    CHILD = "child"


def default_household_settings() -> dict[str, Any]:
    return {"adult_spend_approval_threshold_cents": 5000}


class Household(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "households"

    name: Mapped[str] = mapped_column(sa.String(120))
    slug: Mapped[str] = mapped_column(sa.String(64), unique=True)
    timezone: Mapped[str] = mapped_column(sa.String(64), default="UTC")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=default_household_settings)


class HouseholdMembership(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "household_memberships"
    __table_args__ = (sa.UniqueConstraint("household_id", "user_id", name="uq_household_memberships_household_user"),)

    household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE")
    user_id: Mapped[uuid.UUID] = fk("users.id", ondelete="CASCADE", index=True)
    role: Mapped[MembershipRole] = mapped_column(enum_type(MembershipRole))

    household: Mapped[Household] = relationship()
    user: Mapped[User] = relationship()
