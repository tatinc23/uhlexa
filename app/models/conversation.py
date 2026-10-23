import enum
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONDict, TimestampMixin, UTCDateTime, UUIDPkMixin, enum_type, fk, utcnow
from app.models.agent import Agent
from app.models.user import User


class Visibility(enum.StrEnum):
    HOUSEHOLD = "household"
    PRIVATE = "private"


class ConversationStatus(enum.StrEnum):
    OPEN = "open"
    ARCHIVED = "archived"


class TranscriptProvider(enum.StrEnum):
    PRIMITIVE = "primitive"
    LOCAL = "local"


class ParticipantType(enum.StrEnum):
    USER = "user"
    AGENT = "agent"


class JoinedVia(enum.StrEnum):
    CREATED = "created"
    RESUMED = "resumed"
    INVITED = "invited"


class SenderType(enum.StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class Conversation(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    household_id: Mapped[uuid.UUID] = fk("households.id", ondelete="CASCADE", index=True)
    title: Mapped[str] = mapped_column(sa.String(200))
    topic_key: Mapped[str] = mapped_column(sa.String(120), index=True)
    visibility: Mapped[Visibility] = mapped_column(enum_type(Visibility), default=Visibility.HOUSEHOLD)
    status: Mapped[ConversationStatus] = mapped_column(enum_type(ConversationStatus), default=ConversationStatus.OPEN)
    created_by_user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL")
    created_by_agent_id: Mapped[uuid.UUID | None] = fk("agents.id", ondelete="SET NULL")
    transcript_provider: Mapped[TranscriptProvider] = mapped_column(
        enum_type(TranscriptProvider), default=TranscriptProvider.LOCAL
    )
    primitive_thread_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    # Authoritative copy of the compact thread state; thread_state_version increments on every save.
    thread_state: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    thread_state_version: Mapped[int] = mapped_column(default=1)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    participants: Mapped[list["ConversationParticipant"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ConversationParticipant.joined_at",
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", passive_deletes=True, order_by="Message.created_at"
    )


class ConversationParticipant(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "conversation_participants"
    __table_args__ = (
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND agent_id IS NULL) OR (user_id IS NULL AND agent_id IS NOT NULL)",
            name="exactly_one_identity",
        ),
        sa.UniqueConstraint("conversation_id", "user_id", name="uq_conversation_participants_conversation_user"),
        sa.UniqueConstraint("conversation_id", "agent_id", name="uq_conversation_participants_conversation_agent"),
    )

    conversation_id: Mapped[uuid.UUID] = fk("conversations.id", ondelete="CASCADE")
    participant_type: Mapped[ParticipantType] = mapped_column(enum_type(ParticipantType))
    user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="CASCADE")
    agent_id: Mapped[uuid.UUID | None] = fk("agents.id", ondelete="CASCADE")
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    joined_via: Mapped[JoinedVia] = mapped_column(enum_type(JoinedVia), default=JoinedVia.CREATED)

    conversation: Mapped[Conversation] = relationship(back_populates="participants")
    user: Mapped[User | None] = relationship()
    agent: Mapped[Agent | None] = relationship()


class Message(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (sa.Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),)

    conversation_id: Mapped[uuid.UUID] = fk("conversations.id", ondelete="CASCADE")
    sender_type: Mapped[SenderType] = mapped_column(enum_type(SenderType))
    sender_user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL")
    sender_agent_id: Mapped[uuid.UUID | None] = fk("agents.id", ondelete="SET NULL")
    body: Mapped[str] = mapped_column(sa.Text)
    task_id: Mapped[uuid.UUID | None] = fk("tasks.id", ondelete="SET NULL")
    primitive_message_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    source: Mapped[TranscriptProvider] = mapped_column(enum_type(TranscriptProvider), default=TranscriptProvider.LOCAL)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
