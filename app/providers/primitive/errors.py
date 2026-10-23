"""Typed refusals from Primitive, plus the "not documented" reason.

Two kinds of failure, deliberately separate:

* **Outage** (transport error, 401/402/403-forbidden/429/5xx, a response we cannot parse): a plain
  `ProviderUnavailable("primitive", reason)`. `guarded_call` marks the provider `unavailable` in the status badge.
* **Refusal** (Primitive answered and said no: 404, a recipient gate, a memory compare-and-set conflict, a thread that
  moved, or a request our own pre-flight checks refused): a `PrimitiveRejection`. It is still a `ProviderUnavailable`,
  so every caller that falls back on that exception stays safe and an uncaught one maps to a clean 503 instead of a 500,
  but `provider_answered` tells `guarded_call` the provider is reachable, so the badge is not flipped (and a stale
  `unavailable` is cleared). A refusal made locally, before anything was sent, leaves the badge alone.

Callers that care about the difference catch the subclass they know about before `ProviderUnavailable`.
"""

from app.errors import ProviderUnavailable

PROVIDER = "primitive"
NOT_DOCUMENTED = (
    "not documented: Primitive has no thread-creation endpoint (a thread exists once an email is sent or received)"
)


class PrimitiveRejection(ProviderUnavailable):
    """Primitive refused this one request (or we refused it before sending). The provider itself is fine."""

    def __init__(
        self,
        reason: str,
        *,
        primitive_code: str = "rejected",
        http_status: int | None = None,
        provider_answered: bool | None = None,
    ) -> None:
        super().__init__(PROVIDER, reason)
        self.primitive_code = primitive_code  # Primitive's `error.code`, or a local code such as `invalid_request`
        self.http_status = http_status  # None when the request was refused locally, before any HTTP call
        # Read by app.providers.guarded_call: True = Primitive replied, so the badge is `ok`; False = nothing was sent,
        # so the badge is left alone. Defaults to "an HTTP status came back".
        self.provider_answered = http_status is not None if provider_answered is None else provider_answered


class PrimitiveNotFound(PrimitiveRejection):
    """404: no such thread, email or sent email."""


class MemoryConflict(PrimitiveRejection):
    """409 `memory_conflict`: the `if_version` compare-and-set token was stale."""


class NotRepliable(PrimitiveRejection):
    """The thread has no inbound email to reply to, or Primitive says the inbound cannot be replied to (422)."""


class ThreadChanged(PrimitiveRejection):
    """409 `latest_message_mismatch`: a newer message joined the thread, so nothing was sent."""

    def __init__(self, reason: str, *, latest_message_id: str | None, http_status: int | None = 409) -> None:
        super().__init__(reason, primitive_code="latest_message_mismatch", http_status=http_status)
        self.latest_message_id = latest_message_id  # what Primitive reports as newest, for the caller to re-read


class RecipientNotAllowed(PrimitiveRejection):
    """403 `recipient_not_allowed`: the send gates refuse this recipient. Surface as "not sent", never as success."""

    def __init__(self, reason: str, *, gates: tuple[str, ...] = (), http_status: int | None = 403) -> None:
        super().__init__(reason, primitive_code="recipient_not_allowed", http_status=http_status)
        self.gates = gates
