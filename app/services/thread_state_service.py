"""The compact thread state. Postgres (`conversations.thread_state`) is authoritative; when Primitive is active the
state is mirrored to Primitive memory and read back on resume, falling back to Postgres (spec section 11)."""

import copy
from typing import Any

from sqlalchemy.orm import Session

from app.errors import ProviderUnavailable
from app.models import Conversation
from app.providers.primitive.service import PrimitiveService
from app.services import activity_service

MAX_REASON_CHARS = 200
_QUIET = ("disabled", "misconfigured")  # normal states for most deployments: nothing to report


def memory_key(conversation: Conversation) -> str:
    return f"uhlexa:thread:{conversation.id}:state"


def new_state(actor_label: str) -> dict[str, Any]:
    return {"summary": "", "items": [], "open_questions": [], "last_actor": actor_label}


def apply_items(state: dict[str, Any], items: list[str], actor_label: str) -> dict[str, Any]:
    """A new state with `items` appended: case-insensitive dedupe, existing order kept. Never mutates `state`."""
    new = copy.deepcopy(state)
    merged: list[str] = [item for item in new.get("items", []) if isinstance(item, str)]
    seen = {item.lower() for item in merged}
    for raw in items:
        item = raw.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            merged.append(item)
    new["items"] = merged
    new["last_actor"] = actor_label
    return new


def load(db: Session, conversation: Conversation, *, primitive: PrimitiveService) -> dict[str, Any]:
    """The state to act on: the Primitive copy only when it is newer than Postgres, otherwise Postgres."""
    local = copy.deepcopy(dict(conversation.thread_state or {}))
    if primitive.status.status in _QUIET:
        return local
    try:
        memory = primitive.get_memory(memory_key(conversation))
    except ProviderUnavailable:
        return local
    if memory is None:
        return local
    state, version = memory.value.get("state"), memory.value.get("version")
    if (
        isinstance(state, dict)
        and isinstance(version, int)
        and not isinstance(version, bool)
        and version > conversation.thread_state_version
    ):
        return copy.deepcopy(state)
    return local


def save(db: Session, conversation: Conversation, state: dict[str, Any], *, primitive: PrimitiveService) -> int:
    """Persist `state` to Postgres, bump the version, and mirror to Primitive. A Primitive outage is recorded in the
    activity log and never fails the save. Returns the new version."""
    version = conversation.thread_state_version + 1
    conversation.thread_state = copy.deepcopy(state)
    conversation.thread_state_version = version
    db.flush()
    if primitive.status.status in _QUIET:
        return version
    try:
        primitive.put_memory(memory_key(conversation), {"state": copy.deepcopy(state), "version": version})
    except ProviderUnavailable as exc:
        activity_service.record(
            db,
            household_id=conversation.household_id,
            event_type="provider.unavailable",
            actor_type="provider",
            conversation_id=conversation.id,
            summary="Primitive is unavailable, so this thread's state was kept in Postgres only.",
            payload={"provider": "primitive", "operation": "put_memory", "reason": exc.reason[:MAX_REASON_CHARS]},
        )
    return version
