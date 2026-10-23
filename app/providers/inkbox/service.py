"""Inkbox channel service: sends, message fetch and calls over the sync `inkbox` SDK (research section 1).

Everything here is blocking and belongs in the jobs worker, never in an async route. The SDK is imported lazily by the
default client factory, so importing this module needs no SDK. Tests fake the client at `client_factory`.

Error policy, deliberately the same shape as the other providers:

* an outage (network error, 5xx, auth failure, anything unexpected) is `ProviderUnavailable("inkbox", reason)` and the
  status flips to `unavailable` until the next success;
* a refusal where Inkbox answered (iMessage recipient not connected, SMS consent missing, 429) is a typed subclass of
  `ProviderUnavailable` with `provider_answered=True`, so callers that fall back on the base class stay safe and the
  status badge does not flip;
* the SDK's error detail can echo request data, so it is used for matching only and never copied into any message.
"""

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.errors import ProviderUnavailable
from app.providers.inkbox.config import InkboxConfig
from app.providers.inkbox.models import EmailBody, PlacedCall

NAME = "inkbox"
CHANNELS = ("imessage", "sms", "email", "calls")
SMS_MAX_CHARS = 1600
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_NOT_CONNECTED_STATUSES = frozenset({404, 409, 422})


class InkboxRefusal(ProviderUnavailable):
    """Inkbox replied and said no. The provider itself is fine (`provider_answered=True`)."""

    provider_answered = True

    def __init__(self, reason: str, *, http_status: int | None = None) -> None:
        super().__init__(NAME, reason)
        self.http_status = http_status


class NotConnected(InkboxRefusal):
    """iMessage: the recipient has not connected to this identity (or the connection expired). A human texts first."""


class ConsentMissing(InkboxRefusal):
    """SMS: the recipient has not opted in (US: text START) or has opted out (STOP)."""


class RateLimited(InkboxRefusal):
    """429: back off and retry later."""


@dataclass(frozen=True)
class InkboxStatus:
    state: str  # disabled | misconfigured | ok | unavailable
    detail: str
    channels: dict[str, str] = field(default_factory=dict)  # per channel: disabled | misconfigured | ok

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.state, "detail": self.detail, "channels": dict(self.channels)}


def _default_factory(config: InkboxConfig) -> Any:
    from inkbox import Inkbox  # lazy: the SDK is only needed when something is actually sent

    return Inkbox(api_key=config.api_key, base_url=config.base_url)


def _detail_text(exc: Exception) -> str:
    detail = getattr(exc, "detail", "")
    return (detail if isinstance(detail, str) else json.dumps(detail, default=str)).lower()


def _base_url_ok(url: str) -> bool:
    return url.startswith("https://") or url.startswith(("http://localhost", "http://127.0.0.1"))


class InkboxService:
    def __init__(self, config: InkboxConfig, client_factory: Callable[[InkboxConfig], Any] | None = None) -> None:
        self._config = config
        self._factory = client_factory or _default_factory
        self._client: Any = None
        self._lock = threading.Lock()
        self._failure: str | None = None  # runtime overlay: last live call failed

    # -- status ----------------------------------------------------------------------------------------------

    def _channel_flags(self) -> dict[str, bool]:
        c = self._config
        return {"imessage": c.imessage, "sms": c.sms, "email": c.email, "calls": c.calls}

    def _base_state(self) -> tuple[str, str]:
        if not self._config.enabled:
            return "disabled", "Disabled (ENABLE_INKBOX=false)"
        if not self._config.api_key.strip():
            return "misconfigured", "ENABLE_INKBOX is on but INKBOX_API_KEY is not set"
        if not _base_url_ok(self._config.base_url):
            return "misconfigured", "INKBOX_BASE_URL must be https (localhost excepted)"
        return "ok", "Enabled and configured"

    def _channel_state(self, channel: str, base: str) -> str:
        if base == "disabled" or not self._channel_flags()[channel]:
            return "disabled"
        if base != "ok":
            return "misconfigured"
        if channel == "calls" and not any(v.strip() for v in self._config.signing_keys.values()):
            return "misconfigured"  # a call handshake can only be verified with a signing key
        return "ok"

    def status(self) -> InkboxStatus:
        base, detail = self._base_state()
        channels = {ch: self._channel_state(ch, base) for ch in CHANNELS}
        if base == "ok" and self._failure:
            return InkboxStatus("unavailable", f"Unavailable: {self._failure}", channels)
        return InkboxStatus(base, detail, channels)

    # -- plumbing --------------------------------------------------------------------------------------------

    def _require(self, channel: str) -> None:
        base, detail = self._base_state()
        if base != "ok":
            raise ProviderUnavailable(NAME, detail)
        if not self._channel_flags()[channel]:
            raise ProviderUnavailable(NAME, f"{channel} channel is disabled")

    def _identity(self, handle: str) -> Any:
        with self._lock:
            if self._client is None:
                self._client = self._factory(self._config)
            client = self._client
        return client.get_identity(handle)

    def _call(self, channel: str, handle: str, fn: Callable[[Any], Any]) -> Any:
        try:
            result = fn(self._identity(handle))
        except InkboxRefusal:
            raise
        except Exception as exc:
            raise self._map(exc, channel) from None
        self._failure = None
        return result

    def _map(self, exc: Exception, channel: str) -> ProviderUnavailable:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            text = _detail_text(exc)
            mapped: InkboxRefusal | None = None
            if status == 429:
                mapped = RateLimited("rate limited by Inkbox", http_status=status)
            elif status == 403 and channel == "sms" and "opted" in text:
                mapped = ConsentMissing("recipient has not opted in to SMS (or opted out)", http_status=status)
            elif status in _NOT_CONNECTED_STATUSES and channel == "imessage":
                mapped = NotConnected("recipient is not connected to this iMessage identity", http_status=status)
            if mapped is not None:
                self._failure = None  # Inkbox answered, so it is reachable
                return mapped
            reason = f"HTTP {status}"
        else:
            reason = type(exc).__name__  # the type only: a message may echo a key or a URL
        self._failure = reason
        return ProviderUnavailable(NAME, reason)

    @staticmethod
    def _check_key(idempotency_key: str) -> None:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required for every send")

    @staticmethod
    def _one_target(to: Any, conversation_id: str | None) -> None:
        if bool(to) == bool(conversation_id):
            raise ValueError("pass exactly one of `to` and `conversation_id`")

    # -- sends -----------------------------------------------------------------------------------------------

    def send_imessage(
        self,
        handle: str,
        *,
        text: str,
        idempotency_key: str,
        to: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        """Returns the provider message id. Reply by `conversation_id` only while the connection is active."""
        self._check_key(idempotency_key)
        self._one_target(to, conversation_id)
        self._require("imessage")
        sent = self._call(
            "imessage",
            handle,
            lambda i: i.send_imessage(
                to=to, conversation_id=conversation_id, text=text, idempotency_key=idempotency_key
            ),
        )
        return str(sent.id)

    def send_text(
        self,
        handle: str,
        *,
        text: str,
        idempotency_key: str,
        to: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        self._check_key(idempotency_key)
        self._one_target(to, conversation_id)
        if len(text) > SMS_MAX_CHARS:
            raise ValueError(f"SMS text is limited to {SMS_MAX_CHARS} characters")
        self._require("sms")
        sent = self._call(
            "sms",
            handle,
            lambda i: i.send_text(to=to, conversation_id=conversation_id, text=text, idempotency_key=idempotency_key),
        )
        return str(sent.id)

    def send_email(
        self,
        handle: str,
        *,
        to: list[str],
        subject: str,
        body_text: str,
        idempotency_key: str,
        in_reply_to_message_id: str | None = None,
    ) -> str:
        self._check_key(idempotency_key)
        if not to:
            raise ValueError("at least one recipient is required")
        self._require("email")
        sent = self._call(
            "email",
            handle,
            lambda i: i.send_email(
                to=to,
                subject=subject,
                body_text=body_text,
                in_reply_to_message_id=in_reply_to_message_id,
                idempotency_key=idempotency_key,
            ),
        )
        return str(sent.id)

    def get_message(self, handle: str, message_id: str) -> EmailBody:
        """Full body of an inbound email (the webhook body may be truncated). Fetching marks it read."""
        self._require("email")
        detail = self._call("email", handle, lambda i: i.get_message(message_id))
        return EmailBody(
            message_id=message_id,
            subject=str(getattr(detail, "subject", "") or ""),
            from_address=str(getattr(detail, "from_address", "") or ""),
            body_text=str(getattr(detail, "body_text", "") or ""),
        )

    # -- calls -----------------------------------------------------------------------------------------------

    def place_call(self, handle: str, to_number: str, client_websocket_url: str) -> PlacedCall:
        """Ring `to_number` from the identity; Inkbox then opens `client_websocket_url` (our `CallSession` endpoint).

        The SDK exposes no idempotency key for calls, so callers must not blind-retry. Only call consenting adults.
        """
        self._require("calls")
        if not _E164.match(to_number):
            raise ValueError("to_number must be E.164, e.g. +15551230001")
        if not client_websocket_url.startswith("wss://") or len(client_websocket_url) <= len("wss://"):
            raise ValueError("client_websocket_url must be a wss:// URL")
        call = self._call(
            "calls",
            handle,
            lambda i: i.place_call(
                to_number=to_number, client_websocket_url=client_websocket_url, mode="client_websocket"
            ),
        )
        return PlacedCall(call_id=str(call.id), status=str(getattr(call, "status", "")))

    def call_remote_number(self, handle: str, call_id: str) -> str | None:
        """The other party of `call_id`, from the call record (`X-Call-Context` is not trusted: undocumented).

        The SDK has `list_calls` but no get-by-id, so this scans the most recent 50 calls; a very old id gives None.
        """
        self._require("calls")
        calls = self._call("calls", handle, lambda i: i.list_calls(limit=50))
        for call in calls:
            if str(call.id) == call_id:
                return str(call.remote_phone_number)
        return None
