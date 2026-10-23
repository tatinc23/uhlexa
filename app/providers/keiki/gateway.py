"""Keiki API gateway client (spec 21.3): OpenAI-compatible chat completions, sync for jobs, async stream for calls."""

import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.providers.keiki.config import KeikiConfig
from app.providers.keiki.errors import KeikiAuthError, KeikiBadRequest, KeikiBusy, KeikiError, KeikiTurnFailed

CHANNELS = ("imsg", "sms", "mail", "call", "voice")
BACKOFF_S = (0.4, 0.8, 1.6, 3.0)
_PATH = "/api/v1/openai/chat/completions"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX_RE = re.compile(r"^[0-9a-f]{8}$")


@dataclass(frozen=True)
class GatewayReply:
    text: str
    conversation_id: str
    trace_id: str | None = None


def conversation_id_for(user_id: str, channel: str) -> str:
    """Stable per person and channel: `u<8 hex of the user id>-<imsg|sms|mail|call>`."""
    if channel not in CHANNELS:
        raise ValueError("unknown channel")
    head = user_id.replace("-", "").lower()[:8]
    if not _HEX_RE.match(head):
        raise ValueError("user id must start with 8 hex characters")
    cid = f"u{head}-{channel}"
    if not _ID_RE.match(cid):
        raise ValueError("invalid conversation id")
    return cid


def _error_for(status: int) -> KeikiError | None:
    if status in (401, 403, 404):
        return KeikiAuthError("key, scope or agent rejected by the gateway", status)
    if status == 409:
        return KeikiBusy("a turn is already running for this conversation", status)
    if status == 400:
        return KeikiBadRequest("gateway rejected the request", status)
    if status >= 400:
        return KeikiTurnFailed("the turn failed", status)
    return None


def _trace_id(data: dict[str, Any]) -> str | None:
    meta = data.get("keiki")
    if not isinstance(meta, dict):
        return None
    for key in ("trace_id", "traceId"):
        if isinstance(meta.get(key), str):
            return meta[key]
    trace = meta.get("trace")
    if isinstance(trace, dict) and isinstance(trace.get("id"), str):
        return trace["id"]
    return None


# Keiki joins the text before and after a tool call with no space ("...for you.Checked your board"): add the space.
_GLUED_SENTENCES = re.compile(r"(?<=[a-z0-9][.!?])(?=[A-Z][a-z])")


def _space_joined_sentences(text: str) -> str:
    return _GLUED_SENTENCES.sub(" ", text)


class KeikiGateway:
    def __init__(
        self,
        config: KeikiConfig,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._transport = transport
        self._async_transport = async_transport
        self._sleep = sleep

    def status(self) -> dict[str, str]:
        c = self._config
        if not c.enabled:
            return {"status": "disabled", "detail": "ENABLE_KEIKI is off"}
        if not c.gateway_key or not c.agent_ids:
            return {"status": "misconfigured", "detail": "gateway key and agent ids are both required"}
        return {"status": "ok", "detail": "configured"}

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._config.timeout_s, connect=self._config.connect_timeout_s)

    def _prepare(
        self, agent_id: str, conversation_id: str, text: str, system: str | None, effort: str | None, stream: bool
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        if self.status()["status"] != "ok":
            raise KeikiError(f"gateway {self.status()['status']}")
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": text}]
        body: dict[str, Any] = {"model": agent_id, "messages": messages, "stream": stream}
        if effort:
            body["reasoning_effort"] = effort
        headers = {"Authorization": f"Bearer {self._config.gateway_key}", "x-conversation-id": conversation_id}
        return self._config.base_url.rstrip("/") + _PATH, headers, body

    def complete(
        self,
        agent_id: str,
        conversation_id: str,
        text: str,
        *,
        system: str | None = None,
        reasoning_effort: str | None = None,
    ) -> GatewayReply:
        url, headers, body = self._prepare(agent_id, conversation_id, text, system, reasoning_effort, False)
        with httpx.Client(transport=self._transport, timeout=self._timeout()) as client:
            for attempt in range(len(BACKOFF_S) + 1):
                try:
                    resp = client.post(url, headers=headers, json=body)
                except httpx.HTTPError as exc:
                    # The turn may already have started: never retry it.
                    raise KeikiTurnFailed(f"network error ({type(exc).__name__})") from None
                if resp.status_code == 409 and attempt < len(BACKOFF_S):
                    self._sleep(BACKOFF_S[attempt])
                    continue
                break
        if (err := _error_for(resp.status_code)) is not None:
            raise err
        return self._parse(resp, conversation_id)

    @staticmethod
    def _parse(resp: httpx.Response, conversation_id: str) -> GatewayReply:
        try:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise KeikiTurnFailed("unreadable gateway response") from None
        if choice.get("finish_reason") == "tool_calls":
            raise KeikiTurnFailed("agent asked for a client tool, none are declared")
        if not isinstance(content, str):
            raise KeikiTurnFailed("gateway response had no text")
        return GatewayReply(
            _space_joined_sentences(content), resp.headers.get("x-conversation-id") or conversation_id, _trace_id(data)
        )

    async def stream_text(
        self,
        agent_id: str,
        conversation_id: str,
        text: str,
        *,
        system: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield `delta.content` strings. Raises on an error frame or a stream that ends without `[DONE]`."""
        url, headers, body = self._prepare(agent_id, conversation_id, text, system, reasoning_effort, True)
        done = False
        try:
            async with (
                httpx.AsyncClient(transport=self._async_transport, timeout=self._timeout()) as client,
                client.stream("POST", url, headers=headers, json=body) as resp,
            ):
                if (err := _error_for(resp.status_code)) is not None:
                    raise err
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        break
                    chunk = _json(payload)
                    if "error" in chunk:
                        raise KeikiTurnFailed("error frame in stream")
                    choices = chunk.get("choices") or []
                    piece = choices[0].get("delta", {}).get("content") if choices else None
                    if isinstance(piece, str) and piece:
                        yield piece
        except httpx.HTTPError as exc:
            raise KeikiTurnFailed(f"network error ({type(exc).__name__})") from None
        if not done:
            raise KeikiTurnFailed("stream ended without [DONE]")


def _json(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except ValueError:
        raise KeikiTurnFailed("unreadable stream frame") from None
    return value if isinstance(value, dict) else {}
