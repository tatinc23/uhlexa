"""Conversation access: the policy rule applied to rows, shared by every service. A conversation the caller may not
see is `NotFound` (never `Forbidden`), so existence does not leak."""

import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Any

from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from app.auth.demo_auth import Principal
from app.errors import NotFound
from app.models import Conversation, ConversationStatus, HouseholdMembership
from app.policy import can_access_conversation


def as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """Ids arrive as UUIDs (REST path params) or strings (MCP); junk is NotFound, not a 500."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise NotFound() from None


def enum_value(value: Any) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def _creator_roles(db: Session, household_id: uuid.UUID, user_ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {user_id for user_id in user_ids if user_id is not None}
    if not wanted:
        return {}
    rows = db.execute(
        select(HouseholdMembership.user_id, HouseholdMembership.role).where(
            HouseholdMembership.household_id == household_id, HouseholdMembership.user_id.in_(wanted)
        )
    )
    return {user_id: enum_value(role) for user_id, role in rows}


def is_participant(principal: Principal, conversation: Conversation) -> bool:
    """The caller's user, or the caller's own agent, is on the thread."""
    return any(p.user_id == principal.user.id or p.agent_id == principal.agent.id for p in conversation.participants)


def has_agent_joined(principal: Principal, conversation: Conversation) -> bool:
    return any(p.agent_id == principal.agent.id for p in conversation.participants)


def _allowed(principal: Principal, conversation: Conversation, creator_role: str | None) -> bool:
    return can_access_conversation(
        same_household=conversation.household_id == principal.household.id,
        visibility=enum_value(conversation.visibility),
        is_participant=is_participant(principal, conversation),
        principal_role=enum_value(principal.role),
        created_by_role=creator_role,
    )


def can_access(db: Session, principal: Principal, conversation: Conversation) -> bool:
    roles = _creator_roles(db, conversation.household_id, [conversation.created_by_user_id])
    return _allowed(principal, conversation, roles.get(conversation.created_by_user_id))  # type: ignore[arg-type]


_SNAPSHOT = "uhlexa.visibility_snapshot"


@contextmanager
def visibility_snapshot(db: Session) -> Iterator[None]:
    """Within the block, who-may-see-which-conversation is computed once per caller and reused. For read-only builders
    (the Family Board) that would otherwise repeat the same four queries for every list they assemble: on Neon each
    statement is ~0.1 s. Nothing that writes may run inside it."""
    already = _SNAPSHOT in db.info
    db.info.setdefault(_SNAPSHOT, {})
    try:
        yield
    finally:
        if not already:
            db.info.pop(_SNAPSHOT, None)


def accessible_conversations(db: Session, principal: Principal, *, open_only: bool = False) -> list[Conversation]:
    """Household conversations this caller may read, newest activity first."""
    cache: dict[Any, Any] | None = db.info.get(_SNAPSHOT)
    key = ("accessible", principal.user.id, principal.household.id, open_only)
    if cache is not None and key in cache:
        return cache[key]
    result = _accessible_conversations(db, principal, open_only=open_only)
    if cache is not None:
        cache[key] = result
    return result


def _accessible_conversations(db: Session, principal: Principal, *, open_only: bool = False) -> list[Conversation]:
    stmt = (
        select(Conversation)
        .where(Conversation.household_id == principal.household.id)
        .options(selectinload(Conversation.participants))
        .order_by(
            func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(),
            Conversation.created_at.desc(),
            Conversation.id,
        )
    )
    if open_only:
        stmt = stmt.where(Conversation.status == ConversationStatus.OPEN)
    rows = list(db.scalars(stmt))
    roles = _creator_roles(db, principal.household.id, [c.created_by_user_id for c in rows])
    return [c for c in rows if _allowed(principal, c, roles.get(c.created_by_user_id))]  # type: ignore[arg-type]


def get_accessible(db: Session, principal: Principal, conversation_id: uuid.UUID | str) -> Conversation:
    conversation = db.scalars(
        select(Conversation).where(
            Conversation.id == as_uuid(conversation_id), Conversation.household_id == principal.household.id
        )
    ).first()
    if conversation is None or not can_access(db, principal, conversation):
        raise NotFound("Conversation not found")
    return conversation


def hidden_conversation_ids(db: Session, principal: Principal) -> set[uuid.UUID]:
    """Household conversations this caller may NOT see (their activity is hidden too)."""
    cache: dict[Any, Any] | None = db.info.get(_SNAPSHOT)
    key = ("hidden", principal.user.id, principal.household.id)
    if cache is not None and key in cache:
        return cache[key]
    everything = set(db.scalars(select(Conversation.id).where(Conversation.household_id == principal.household.id)))
    hidden = everything - {c.id for c in accessible_conversations(db, principal)}
    if cache is not None:
        cache[key] = hidden
    return hidden


def visible_conversation_clause(db: Session, principal: Principal, column: Any) -> Any:
    """SQL condition: `column` (a conversation id) is empty or belongs to a conversation this caller may see.

    Tasks, approvals and browser tasks born in a thread inherit that thread's visibility, so a child never sees work
    from an adults' thread through the board, a list or a direct id (that id answers NotFound)."""
    hidden = hidden_conversation_ids(db, principal)
    return or_(column.is_(None), column.not_in(hidden)) if hidden else true()
