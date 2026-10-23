import enum
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, TimestampMixin, UTCDateTime, UUIDPkMixin, enum_type, fk


class ProviderName(enum.StrEnum):
    PRIMITIVE = "primitive"
    SUBQ = "subq"
    MARITIME = "maritime"
    KEIKI = "keiki"
    INKBOX = "inkbox"


class BindingStatus(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"


class ProviderBinding(UUIDPkMixin, TimestampMixin, Base):
    """Links a user/agent to a provider-side resource (e.g. a Maritime computer id, the sha256 of a Keiki agent's MCP
    token, an Inkbox identity or a verified phone number). `config` is non-secret only."""

    __tablename__ = "provider_bindings"
    __table_args__ = (
        sa.UniqueConstraint("provider", "binding_type", "user_id", name="uq_provider_bindings_provider_type_user"),
        # One owner per external identity: a token hash, phone number or address can bind to exactly one user.
        sa.Index("uq_provider_bindings_provider_type_external", "provider", "binding_type", "external_id", unique=True),
    )

    household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE", index=True)
    user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="CASCADE")
    agent_id: Mapped[uuid.UUID | None] = fk("agents.id", ondelete="CASCADE")
    provider: Mapped[ProviderName] = mapped_column(enum_type(ProviderName))
    binding_type: Mapped[str] = mapped_column(sa.String(64))
    external_id: Mapped[str] = mapped_column(sa.String(255))
    status: Mapped[BindingStatus] = mapped_column(enum_type(BindingStatus), default=BindingStatus.ACTIVE)
    config: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
