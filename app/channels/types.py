"""Provider-neutral channel types (plan Task 13). Adapters build `InboundTurn`; the channel service answers with
`ChannelReply`. Nothing here knows about HTTP, Keiki or Inkbox wire formats."""

from dataclasses import dataclass, field
from typing import Any, Literal

Channel = Literal[
    "keiki:telegram",
    "keiki:slack",
    "keiki:sms",
    "keiki:imessage",
    "keiki:email",
    "keiki:livetest",
    "inkbox:imessage",
    "inkbox:sms",
    "inkbox:email",
    "inkbox:call",
    "primitive:email",
]


@dataclass(frozen=True)
class InboundTurn:
    channel: str  # a `Channel` value
    provider_event_id: str  # dedupe key at the provider (envelope id / turn id)
    agent_ref: str  # which agent was addressed: Inkbox handle or Keiki agent id
    sender_ref: str  # who spoke: E.164 number or email address. Unverified until a binding resolves it
    text: str
    held_back: bool = False
    # The provider itself vouches for the sender (Primitive's `from_verified`, read back from its API). Email needs it.
    sender_verified: bool = False
    reply_ctx: dict[str, Any] = field(default_factory=dict)  # non-secret routing data for the reply


@dataclass(frozen=True)
class ChannelReply:
    text: str
    approval_hint: str | None = None
