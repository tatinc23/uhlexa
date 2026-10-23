"""Wire layer for Primitive's REST API. Only reached when ENABLE_PRIMITIVE is on and a key is present.

Base URL `PRIMITIVE_API_BASE_URL` (default https://api.primitive.dev/v1), `Authorization: Bearer prim_...`, envelope
`{success, data, meta}`. Shapes follow the OpenAPI file in docs/research/2026-10-22-primitive-facts.md; they have NOT
been exercised against the live API (no key exists yet, see BLOCKERS.md), so every response is parsed defensively and
anything unexpected fails closed as `ProviderUnavailable` rather than being guessed at.

Failure model (see `errors.py`): an outage (transport error, 401, 402, 403, 429, 5xx, an unparseable response) raises
plain `ProviderUnavailable`; a refusal (404, recipient gate, memory conflict, moved thread, bad local input) raises a
`PrimitiveRejection` subclass. Reasons are built from fixed text, HTTP status and a sanitised error code, never from
exception messages, so a key cannot leak through them.

Primitive is email, so `reply_to_thread` is a composition: `GET /threads/{id}`, then
`POST /emails/{latest inbound}/reply` guarded by `if_latest_message_id` (the newest message in the thread) and an
`Idempotency-Key`. A keyless reply to an email that already has a reply is silently dropped by Primitive, so a key is
always sent.
"""

import hashlib
import json
import re
from collections.abc import Collection
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.errors import ProviderUnavailable
from app.providers.primitive.errors import (
    PROVIDER,
    MemoryConflict,
    NotRepliable,
    PrimitiveNotFound,
    PrimitiveRejection,
    RecipientNotAllowed,
    ThreadChanged,
)
from app.providers.primitive.models import (
    Direction,
    PrimitiveMemory,
    PrimitiveMessage,
    PrimitiveReply,
    PrimitiveThread,
    PrimitiveThreadMessage,
)
from app.settings import Settings

DEFAULT_BASE_URL = "https://api.primitive.dev/v1"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
USER_AGENT = "uhlexa-hackathon/0.1"

MAX_ID_CHARS = 200
MAX_QUERY_CHARS = 1000
MAX_SEARCH_LIMIT = 100  # Primitive's list endpoints take limit 1..100
MAX_MEMORY_KEY_BYTES = 512  # docs/memories.md
MAX_MEMORY_VALUE_BYTES = 65_536
MAX_REPLY_BYTES = 262_144  # body_text + body_html combined, per the ReplyInput schema
MAX_IDEMPOTENCY_KEY_CHARS = 255
MAX_LABEL_CHARS = 80
MAX_BODY_CHARS = 50_000  # an inbound email body is untrusted and unbounded; we mirror at most this much
MAX_REASON_CHARS = 200
MAX_RETRY_AFTER_SECONDS = 3600

_DIRECTIONS: tuple[Direction, ...] = ("inbound", "outbound")
_CODE = re.compile(r"[a-z0-9_]{1,64}")
_GATE_KEYS = ("gate", "name", "code", "id", "type")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


# --- small helpers -------------------------------------------------------------------------------------------------


def _reject(reason: str, code: str = "invalid_request") -> PrimitiveRejection:
    """A request we refuse before sending anything (caller bug or over a documented limit)."""
    return PrimitiveRejection(reason, primitive_code=code)


def _malformed(why: str) -> ProviderUnavailable:
    return ProviderUnavailable(PROVIDER, f"unexpected response from Primitive: {why}")


def _text(row: dict[str, Any], *names: str) -> str:
    """The first non-empty string among `names`, else ''. Null and wrong-typed fields are tolerated."""
    for name in names:
        value = row.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _clip(body: str) -> str:
    return body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS] + "\n[truncated]"


def _clean(text: Any, secret: str) -> str:
    """One short printable line with the API key removed, for use inside a reason string."""
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split())
    if secret:
        flat = flat.replace(secret, "[redacted]")
    return flat[:MAX_REASON_CHARS]


def _code(value: Any) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else ""


def _checked_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_ID_CHARS or not value.isprintable():
        raise _reject(f"invalid {what}")
    return value.strip()


def _path_id(value: Any, what: str) -> str:
    """An id as one URL path segment: percent-encoded in full, so it can never add or remove a segment."""
    return quote(_checked_id(value, what), safe="")


def _memory_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _reject("memory key is empty")
    if len(value.encode("utf-8")) > MAX_MEMORY_KEY_BYTES:
        raise _reject(f"memory key is longer than {MAX_MEMORY_KEY_BYTES} bytes", "memory_key_too_long")
    return value


def _check_memory_value(value: Any) -> None:
    if not isinstance(value, dict):
        raise _reject("memory value must be a JSON object")
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        raise _reject("memory value is not JSON-serialisable", "invalid_value") from None
    if size > MAX_MEMORY_VALUE_BYTES:
        raise _reject(f"memory value is larger than {MAX_MEMORY_VALUE_BYTES} bytes", "memory_value_too_large")


def _version_token(value: Any) -> str:
    token = str(value).strip() if isinstance(value, str | int) and not isinstance(value, bool) else ""
    if not token or len(token) > 64 or not token.isascii() or not token.isprintable():
        raise _reject("invalid memory version")
    return token


def _compose_reply(body: Any, sender_label: Any) -> str:
    text = body.strip() if isinstance(body, str) else ""
    if not text:
        raise _reject("reply body is empty")
    label = " ".join(str(sender_label or "").split())[:MAX_LABEL_CHARS]
    if label:
        text = f"{text}\n\n-- {label}"
    if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
        raise _reject(f"reply body is larger than {MAX_REPLY_BYTES} bytes", "body_too_large")
    return text


def _supplied_idempotency_key(value: Any) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_CHARS or not key.isascii() or not key.isprintable():
        raise _reject("invalid idempotency key")
    return key


def _derived_idempotency_key(thread_id: str, target: str, text: str) -> str:
    digest = hashlib.sha256(f"{thread_id}\x00{target}\x00{text}".encode()).hexdigest()[:40]
    return f"uhlexa-reply-{digest}"


# --- response mapping ----------------------------------------------------------------------------------------------


def _object(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _malformed("`data` is not an object")
    return data


def _thread_message(row: Any) -> PrimitiveThreadMessage | None:
    if not isinstance(row, dict):
        return None
    direction, message_id = row.get("direction"), _text(row, "id")
    if direction not in _DIRECTIONS or not message_id:  # an unfamiliar direction was added after this client
        return None
    return PrimitiveThreadMessage(
        message_id, direction, _text(row, "from"), _text(row, "subject"), _text(row, "timestamp")
    )


def _thread(data: dict[str, Any]) -> PrimitiveThread:
    thread_id = _text(data, "id")
    if not thread_id:
        raise _malformed("thread has no id")
    rows = data.get("messages")
    messages = tuple(m for m in map(_thread_message, rows if isinstance(rows, list) else []) if m is not None)

    def newest(direction: str) -> str | None:
        return next((m.id for m in reversed(messages) if m.direction == direction), None)

    count = data.get("message_count")
    return PrimitiveThread(
        id=thread_id,
        subject=_text(data, "subject"),
        message_count=count if isinstance(count, int) and not isinstance(count, bool) else len(messages),
        # The live OpenAPI file has latest_inbound_id / latest_outbound_id; the copy in the SDK repo does not.
        latest_inbound_id=_text(data, "latest_inbound_id") or newest("inbound"),
        latest_outbound_id=_text(data, "latest_outbound_id") or newest("outbound"),
        messages=messages,
    )


def _inbound_message(row: dict[str, Any]) -> PrimitiveMessage:
    message_id = _text(row, "id")
    if not message_id:
        raise _malformed("email has no id")
    return PrimitiveMessage(
        id=message_id,
        direction="inbound",
        thread_id=_text(row, "thread_id") or None,
        sender=_text(row, "from_email", "sender", "from_header_email"),
        subject=_text(row, "subject"),
        body_text=_clip(_text(row, "body_text")),
        timestamp=_text(row, "received_at", "created_at"),
        recipient=_address(_text(row, "to_email", "recipient")),
        from_verified=_verified_sender(row),
    )


def _verified_sender(row: dict[str, Any]) -> bool:
    """Primitive's `from_verified`, but only when the header From and the envelope From are the same address, so the
    address we match against a binding is the one that was verified. Anything missing or different: not verified."""
    header, envelope = _address(_text(row, "from_header_email")), _address(_text(row, "from_email"))
    return row.get("from_verified") is True and bool(header) and header == envelope


def _sent_message(row: dict[str, Any]) -> PrimitiveMessage:
    message_id = _text(row, "id")
    if not message_id:
        raise _malformed("sent email has no id")
    return PrimitiveMessage(
        id=message_id,
        direction="outbound",
        thread_id=_text(row, "thread_id") or None,
        sender=_text(row, "from_address", "from_header"),
        subject=_text(row, "subject"),
        body_text=_clip(_text(row, "body_text")),
        timestamp=_text(row, "created_at", "status_changed_at"),
    )


def _search_message(row: Any) -> PrimitiveMessage | None:
    if not isinstance(row, dict) or row.get("direction") not in _DIRECTIONS or not _text(row, "id"):
        return None
    direction = row["direction"]
    return PrimitiveMessage(
        id=_text(row, "id"),
        direction=direction,
        thread_id=_text(row, "thread_id") or None,
        sender=_text(row, "from_email", "sender", "from_address", "from_header_email"),
        subject=_text(row, "subject"),
        body_text=_clip(_text(row, "body_text")),
        timestamp=_text(row, "received_at", "created_at"),
    )


def _memory(data: dict[str, Any], key: str, sent_value: dict[str, Any] | None = None) -> PrimitiveMemory:
    value = data.get("value", sent_value)  # a PUT response may omit the value it just stored
    if not isinstance(value, dict):
        raise _malformed("memory value is not a JSON object")
    version = data.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        version = str(version)
    if not isinstance(version, str) or not version:
        raise _malformed("memory has no version")
    return PrimitiveMemory(key=_text(data, "key") or key, value=value, version=version)


def _reply(data: dict[str, Any]) -> PrimitiveReply:
    reply_id, status = _text(data, "id"), _text(data, "status")
    if not reply_id or not status:
        raise _malformed("send result has no id or status")

    def strings(name: str) -> tuple[str, ...]:
        items = data.get(name)
        return tuple(item for item in items if isinstance(item, str)) if isinstance(items, list) else ()

    return PrimitiveReply(
        id=reply_id,
        status=status,
        accepted=strings("accepted"),
        rejected=strings("rejected"),
        idempotent_replay=data.get("idempotent_replay") is True,
        dedup_reason=_text(data, "dedup_reason") or None,
    )


def _gates(error: dict[str, Any]) -> tuple[str, ...]:
    details = error.get("details")
    raw = error.get("gates") or (details.get("gates") if isinstance(details, dict) else None)
    found: list[str] = []
    for gate in raw if isinstance(raw, list) else []:
        if isinstance(gate, dict):
            gate = next((gate[name] for name in _GATE_KEYS if isinstance(gate.get(name), str)), "")
        name = gate if isinstance(gate, str) else ""
        if name and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
            found.append(name)
    return tuple(found)


def _address(value: str) -> str:
    """`Mom <MOM@Example.com>` -> `mom@example.com`; '' when there is no address."""
    address = parseaddr(value)[1].strip().lower() if value else ""
    return address if "@" in address else ""


def _newest_allowed_inbound(thread: PrimitiveThread, allowed: frozenset[str]) -> str | None:
    """The newest inbound email whose sender is on the allowlist (the thread view lists oldest first)."""
    for message in reversed(thread.messages):
        if message.direction == "inbound" and _address(message.sender) in allowed:
            return message.id
    return None


def _newest_message_id(thread: PrimitiveThread) -> str | None:
    """The newest message overall (the thread view lists oldest first): what `if_latest_message_id` must be.

    Not `latest_inbound_id`: once we have replied, our own outbound message is the newest, so guarding on the inbound
    id would make every second reply fail with `latest_message_mismatch`.
    """
    return thread.messages[-1].id if thread.messages else None


# --- the client ----------------------------------------------------------------------------------------------------


class PrimitiveClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    # -- plumbing

    def _api_key(self) -> str:
        key = (self._settings.primitive_api_key or "").strip()
        if not key:
            raise ProviderUnavailable(PROVIDER, "PRIMITIVE_API_KEY is not set")
        return key

    def _base_url(self) -> str:
        raw = (self._settings.primitive_api_base_url or "").strip() or DEFAULT_BASE_URL
        try:
            parts = urlsplit(raw)
            host = parts.hostname
        except ValueError:
            host, parts = None, None
        # The bearer key must never travel in clear text: https, or plain http to loopback for local fakes only.
        if (
            parts is None
            or not host
            or not (parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS))
        ):
            raise ProviderUnavailable(PROVIDER, "PRIMITIVE_API_BASE_URL must be an https URL")
        return raw.rstrip("/")

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """One request. Returns the envelope's `data`; raises the mapped error for anything else."""
        key = self._api_key()
        url = self._base_url() + path
        request_headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        }
        try:
            # follow_redirects stays False: a redirect must never carry the Authorization header to another host.
            with httpx.Client(transport=self._transport, timeout=TIMEOUT, follow_redirects=False) as http:
                response = http.request(method, url, params=params, json=body, headers=request_headers)
        except httpx.TimeoutException:
            raise ProviderUnavailable(PROVIDER, "request to Primitive timed out") from None
        except httpx.InvalidURL:
            raise ProviderUnavailable(PROVIDER, "PRIMITIVE_API_BASE_URL is not a valid URL") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(PROVIDER, f"could not reach Primitive ({type(exc).__name__})") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if 200 <= response.status_code < 300:
            if not isinstance(payload, dict) or payload.get("success") is not True or "data" not in payload:
                raise _malformed(f"HTTP {response.status_code} without a success envelope")
            return payload["data"]
        raise self._error(response.status_code, payload, response.headers, key)

    @staticmethod
    def _error(status: int, payload: Any, headers: httpx.Headers, key: str) -> ProviderUnavailable:
        error = payload.get("error") if isinstance(payload, dict) else None
        error = error if isinstance(error, dict) else {}
        code = _code(error.get("code"))
        label = f"HTTP {status} {code}" if code else f"HTTP {status}"
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        if status == 401:
            return ProviderUnavailable(PROVIDER, f"Primitive rejected the API key ({label})")
        if status == 402:
            return ProviderUnavailable(PROVIDER, f"Primitive usage credit is exhausted ({label})")
        if status == 403 and code == "recipient_not_allowed":
            return RecipientNotAllowed(
                f"Primitive does not allow sending to this recipient ({label})", gates=_gates(error)
            )
        if status == 403:
            return ProviderUnavailable(PROVIDER, f"Primitive denied access ({label})")
        if status == 404:
            return PrimitiveNotFound(
                f"not found in Primitive ({label})", primitive_code=code or "not_found", http_status=404
            )
        if status == 409 and code == "memory_conflict":
            return MemoryConflict(f"memory changed since it was read ({label})", primitive_code=code, http_status=409)
        if status == 409 and code == "latest_message_mismatch":
            newest = details.get("latest_message_id")
            return ThreadChanged(
                f"a newer message joined the thread, nothing was sent ({label})",
                latest_message_id=newest if isinstance(newest, str) and newest else None,
            )
        if status == 422 and code == "inbound_not_repliable":
            return NotRepliable(f"Primitive cannot reply to this email ({label})", primitive_code=code, http_status=422)
        if status == 429:
            wait = headers.get("retry-after", "")
            usable = wait.isascii() and wait.isdigit() and len(wait) <= 4 and int(wait) <= MAX_RETRY_AFTER_SECONDS
            hint = f"; retry after {wait}s" if usable else ""
            return ProviderUnavailable(PROVIDER, f"Primitive rate limited this request ({label}){hint}")
        if status >= 500:
            return ProviderUnavailable(PROVIDER, f"Primitive server error ({label})")
        if status >= 400:
            message = _clean(error.get("message"), key)
            reason = f"Primitive refused the request ({label})" + (f": {message}" if message else "")
            return PrimitiveRejection(reason, primitive_code=code or "rejected", http_status=status)
        return ProviderUnavailable(PROVIDER, f"unexpected response from Primitive ({label})")

    # -- threads and messages

    def get_thread(self, thread_id: str) -> PrimitiveThread:
        """`GET /threads/{id}`: metadata plus the message list (no bodies; the list is capped)."""
        data = self._call("GET", f"/threads/{_path_id(thread_id, 'thread id')}")
        return _thread(_object(data))

    def get_message(self, message_id: str) -> PrimitiveMessage:
        """`GET /emails/{id}` (inbound), else `GET /sent-emails/{id}` (outbound): the two id namespaces are separate."""
        escaped = _path_id(message_id, "message id")
        try:
            data = self._call("GET", f"/emails/{escaped}")
        except PrimitiveNotFound:
            return _sent_message(_object(self._call("GET", f"/sent-emails/{escaped}")))
        return _inbound_message(_object(data))

    def reply_to_thread(
        self,
        thread_id: str,
        body: str,
        *,
        sender_label: str,
        idempotency_key: str | None = None,
        allowed_senders: Collection[str] = (),
    ) -> PrimitiveReply:
        """Compose a reply: thread view -> newest inbound email from an allowed sender -> `POST /emails/{id}/reply`.

        Primitive derives the recipient from the email being answered, so the reply only ever targets an inbound email
        whose sender is in `allowed_senders` (the household's own addresses). An outsider who writes into the thread
        never receives the agent's reply; an empty allowlist refuses before any HTTP call (fail closed).

        Raises `NotRepliable` when no inbound email qualifies (the documented `send-mail` fallback is not used),
        `ThreadChanged` when a newer message arrived, `RecipientNotAllowed` when the send gates refuse the recipient.
        The returned `PrimitiveReply.sent` is the only honest "was it sent" signal.
        """
        text = _compose_reply(body, sender_label)
        supplied = None if idempotency_key is None else _supplied_idempotency_key(idempotency_key)
        allowed = frozenset(a for a in map(_address, allowed_senders) if a)
        if not allowed:
            raise NotRepliable(
                "no household addresses were given, so no one may receive this reply",
                primitive_code="sender_not_allowed",
            )
        thread = self.get_thread(thread_id)
        if not thread.latest_inbound_id:
            raise NotRepliable(
                "the thread has no inbound email to reply to", primitive_code="no_inbound_email", provider_answered=True
            )
        target = _newest_allowed_inbound(thread, allowed)
        if target is None and not thread.messages:
            # The thread view listed no rows (capped or trimmed): read the latest inbound itself to check its sender.
            latest = self.get_message(thread.latest_inbound_id)
            if latest.direction == "inbound" and _address(latest.sender) in allowed:
                target = latest.id
        if target is None:
            raise NotRepliable(
                "no inbound email in this thread is from a household address",
                primitive_code="sender_not_allowed",
                provider_answered=True,
            )
        payload: dict[str, Any] = {"body_text": text}
        newest = _newest_message_id(thread)
        if newest:
            payload["if_latest_message_id"] = newest
        key = supplied or _derived_idempotency_key(thread.id, target, text)
        data = self._call(
            "POST",
            f"/emails/{_path_id(target, 'email id')}/reply",
            body=payload,
            headers={"Idempotency-Key": key},
        )
        return _reply(_object(data))

    def reply_to_email(self, email_id: str, body: str, *, sender_label: str, idempotency_key: str) -> PrimitiveReply:
        """`POST /emails/{id}/reply` to exactly this inbound email (Primitive derives the recipient from it). For the
        email channel: the email we verified is the one answered, never another message found in its thread."""
        text = _compose_reply(body, sender_label)
        key = _supplied_idempotency_key(idempotency_key)
        data = self._call(
            "POST",
            f"/emails/{_path_id(email_id, 'email id')}/reply",
            body={"body_text": text},
            headers={"Idempotency-Key": key},
        )
        return _reply(_object(data))

    def search_messages(self, query: str, *, thread_id: str | None = None, limit: int = 20) -> list[PrimitiveMessage]:
        """`GET /emails/search` (keyword; semantic search is plan-gated). Inbound and outbound, bodies included."""
        q = query.strip() if isinstance(query, str) else ""
        if not q or len(q) > MAX_QUERY_CHARS:
            raise _reject("search query is empty or too long")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise _reject("search limit must be an integer")
        params = {
            "q": q,
            "direction": "any",
            "limit": str(max(1, min(limit, MAX_SEARCH_LIMIT))),
            "include": "body_text",
        }
        if thread_id is not None:
            params["thread_id"] = _checked_id(thread_id, "thread id")
        rows = self._call("GET", "/emails/search", params=params)
        if not isinstance(rows, list):
            raise _malformed("search `data` is not a list")
        return [m for m in map(_search_message, rows) if m is not None]

    # -- memories (org-scoped JSON key-value store)

    def get_memory(self, key: str) -> PrimitiveMemory | None:
        """`GET /memories?key=`. None when the key does not exist (404)."""
        name = _memory_key(key)
        try:
            data = self._call("GET", "/memories", params={"key": name})
        except PrimitiveNotFound:
            return None
        return _memory(_object(data), name)

    def put_memory(self, key: str, value: dict, *, if_version: str | None = None) -> PrimitiveMemory:
        """`PUT /memories`. With `if_version` it is compare-and-set: a stale token raises `MemoryConflict`."""
        name = _memory_key(key)
        _check_memory_value(value)
        body: dict[str, Any] = {"key": name, "value": value}
        if if_version is not None:
            body["if_version"] = _version_token(if_version)
        data = self._call("PUT", "/memories", body=body)
        return _memory(_object(data), name, sent_value=value)

    def delete_memory(self, key: str) -> bool:
        """`DELETE /memories?key=`. True when something was deleted; a missing key is False, not an error."""
        name = _memory_key(key)
        try:
            data = self._call("DELETE", "/memories", params={"key": name})
        except PrimitiveNotFound:
            return False
        return _object(data).get("deleted") is True
