from collections.abc import Collection
from typing import NoReturn

import httpx

from app.errors import ProviderUnavailable
from app.providers import ProviderStatus, guarded_call, provider_statuses
from app.providers.primitive.client import PrimitiveClient
from app.providers.primitive.errors import NOT_DOCUMENTED
from app.providers.primitive.models import PrimitiveMemory, PrimitiveMessage, PrimitiveReply, PrimitiveThread
from app.settings import Settings

NAME = "primitive"


class PrimitiveService:
    """Transcript and memory assistance. Disabled or misconfigured raises `ProviderUnavailable("primitive", reason)`.

    Callers (thread_state_service, conversation_service) catch that and fall back to Postgres. A refusal from Primitive
    itself (not found, recipient gate, memory conflict, moved thread) raises a `PrimitiveRejection`, which is a
    `ProviderUnavailable` too but leaves the status badge alone; catch the subclass first when the difference matters.

    `create_thread` exists only to say so honestly: Primitive documents no endpoint for it (a thread appears when an
    email is sent or received), so it raises `ProviderUnavailable("primitive", "not documented: ...")`, never an outage.
    """

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._client = PrimitiveClient(settings, transport)

    @property
    def status(self) -> ProviderStatus:
        return provider_statuses(self._settings)[NAME]

    def get_thread(self, thread_id: str) -> PrimitiveThread:
        return guarded_call(self._settings, NAME, self._client.get_thread, thread_id)

    def get_message(self, message_id: str) -> PrimitiveMessage:
        return guarded_call(self._settings, NAME, self._client.get_message, message_id)

    def reply_to_thread(
        self,
        thread_id: str,
        body: str,
        *,
        sender_label: str,
        idempotency_key: str | None = None,
        allowed_senders: Collection[str] = (),
    ) -> PrimitiveReply:
        """Pass `str(message.id)` as `idempotency_key`: Primitive silently drops a keyless second reply to one email.
        `allowed_senders` = the household's own addresses; only their emails are ever answered (empty: refused)."""
        return guarded_call(
            self._settings,
            NAME,
            self._client.reply_to_thread,
            thread_id,
            body,
            sender_label=sender_label,
            idempotency_key=idempotency_key,
            allowed_senders=allowed_senders,
        )

    def reply_to_email(self, email_id: str, body: str, *, sender_label: str, idempotency_key: str) -> PrimitiveReply:
        """Answer exactly this inbound email (the email channel answers the email it verified, nothing else)."""
        return guarded_call(
            self._settings,
            NAME,
            self._client.reply_to_email,
            email_id,
            body,
            sender_label=sender_label,
            idempotency_key=idempotency_key,
        )

    def search_messages(self, query: str, *, thread_id: str | None = None, limit: int = 20) -> list[PrimitiveMessage]:
        return guarded_call(self._settings, NAME, self._client.search_messages, query, thread_id=thread_id, limit=limit)

    def get_memory(self, key: str) -> PrimitiveMemory | None:
        return guarded_call(self._settings, NAME, self._client.get_memory, key)

    def put_memory(self, key: str, value: dict, *, if_version: str | None = None) -> PrimitiveMemory:
        """With `if_version` (a `PrimitiveMemory.version` from `get_memory`) this is compare-and-set."""
        return guarded_call(self._settings, NAME, self._client.put_memory, key, value, if_version=if_version)

    def delete_memory(self, key: str) -> bool:
        return guarded_call(self._settings, NAME, self._client.delete_memory, key)

    def create_thread(self, title: str) -> NoReturn:
        raise ProviderUnavailable(NAME, NOT_DOCUMENTED)
