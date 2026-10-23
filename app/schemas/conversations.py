"""Wire schemas for conversations, messages and the routed-message response (spec section 14)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import (
    ConversationStatus,
    JoinedVia,
    ParticipantType,
    SenderType,
    TranscriptProvider,
    Visibility,
)
from app.schemas.approvals import ApprovalOut
from app.schemas.tasks import TaskOut

MAX_MESSAGE_CHARS = 2000


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: uuid.UUID | None = None


class ParticipantOut(BaseModel):
    participant_type: ParticipantType
    user_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    name: str
    joined_via: JoinedVia
    joined_at: datetime


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sender_type: SenderType
    sender_user_id: uuid.UUID | None
    sender_agent_id: uuid.UUID | None
    sender_name: str
    body: str
    task_id: uuid.UUID | None
    source: TranscriptProvider
    created_at: datetime


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    topic_key: str
    visibility: Visibility
    status: ConversationStatus
    transcript_provider: TranscriptProvider
    transcript_label: str
    created_by_user_id: uuid.UUID | None
    created_by_agent_id: uuid.UUID | None
    created_by_agent_name: str | None
    participants: list[ParticipantOut]
    resumed_by: list[str]
    thread_state: dict[str, Any]
    thread_state_version: int
    last_message_at: datetime | None
    created_at: datetime


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut]


class DecisionOut(BaseModel):
    """One routing or context decision. `ai_assisted` is true only when Snap's answer was the one used."""

    question_id: str
    choice: str | None
    source: Literal["snap", "fallback"]
    status: str
    detail: str
    confidence: float | None
    ai_assisted: bool


class ContextOut(BaseModel):
    mode: Literal["recent", "full_transcript"]
    messages_considered: int
    state_version: int
    decision: DecisionOut


class RoutingOut(BaseModel):
    action: Literal["continued", "resumed", "started"]
    thread_id: uuid.UUID
    decision: DecisionOut
    context: ContextOut | None = None


class MessageResultOut(BaseModel):
    conversation: ConversationOut
    routing: RoutingOut
    user_message: MessageOut
    agent_message: MessageOut
    task: TaskOut | None
    approval: ApprovalOut | None
