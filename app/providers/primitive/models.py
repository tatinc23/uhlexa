"""Our own view of Primitive data (docs/research/2026-10-22-primitive-facts.md). Ids are opaque strings.

Primitive is email infrastructure: a thread is an email conversation, a message is one email. Nothing here carries
authorization; Postgres and the policy engine decide who may read or resume a thread.
"""

from dataclasses import dataclass
from typing import Any, Literal

Direction = Literal["inbound", "outbound"]


@dataclass(frozen=True)
class PrimitiveEvent:
    id: str  # top-level event id, stable across retries and replays: the dedupe key
    type: str  # body `event`, e.g. "email.received"
    thread_id: str | None
    message_id: str | None  # the inbound email id, usable with get_message
    payload: dict[str, Any]


@dataclass(frozen=True)
class PrimitiveMemory:
    key: str
    value: dict[str, Any]
    version: str  # compare-and-set token (`if_version`)


@dataclass(frozen=True)
class PrimitiveThreadMessage:
    id: str
    direction: Direction
    sender: str
    subject: str
    timestamp: str


@dataclass(frozen=True)
class PrimitiveThread:
    id: str
    subject: str
    message_count: int
    latest_inbound_id: str | None
    latest_outbound_id: str | None
    messages: tuple[PrimitiveThreadMessage, ...]  # bodies are omitted by the thread view; may be capped


@dataclass(frozen=True)
class PrimitiveMessage:
    id: str
    direction: Direction
    thread_id: str | None
    sender: str
    subject: str
    body_text: str
    timestamp: str
    recipient: str = ""  # inbound only: the address it was sent to (which agent inbox)
    from_verified: bool = False  # inbound only: Primitive checked the From address (DMARC-aligned)


#: Send statuses (`SentEmailStatus` in the OpenAPI file) that mean the email did not go out.
NOT_SENT_STATUSES = frozenset({"agent_failed", "gate_denied", "bounced", "canceled", "unknown"})


@dataclass(frozen=True)
class PrimitiveReply:
    id: str
    status: str
    accepted: tuple[str, ...]
    rejected: tuple[str, ...]  # recipient gates: surface as "not sent", never as success
    idempotent_replay: bool
    dedup_reason: str | None = None  # why a replay was collapsed, e.g. `parent_already_replied` (nothing was sent)

    @property
    def sent(self) -> bool:
        """True only when Primitive accepted the reply for delivery. Anything doubtful is "not sent"."""
        return (
            bool(self.accepted)
            and not self.rejected
            and self.status not in NOT_SENT_STATUSES
            and self.dedup_reason != "parent_already_replied"  # a keyless reply to an already-answered email
        )
