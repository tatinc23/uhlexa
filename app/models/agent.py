import enum
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONDict, TimestampMixin, UUIDPkMixin, enum_type, fk
from app.models.user import User


class AgentStatus(enum.StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class Agent(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "agents"

    household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE", index=True)
    owner_user_id: Mapped[uuid.UUID] = fk("users.id", ondelete="CASCADE", index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    persona: Mapped[str] = mapped_column(sa.Text, default="")
    is_default: Mapped[bool] = mapped_column(default=False)
    status: Mapped[AgentStatus] = mapped_column(enum_type(AgentStatus), default=AgentStatus.ACTIVE)
    config: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    owner: Mapped[User] = relationship()
