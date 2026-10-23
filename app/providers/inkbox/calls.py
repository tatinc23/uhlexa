"""The Inkbox call media WebSocket as a pure protocol object (research section 3, mode 1: Inkbox does STT and TTS).

No framework imports. The route owns the socket: it calls `accept(headers)` for the handshake (closing 1008 on
`HandshakeRejected`), then feeds each decoded JSON message to `on_event`/`stream_event` and sends what comes back. The
brain is a sync callable `text -> str | Iterable[str]`, run per FINAL transcript; the route should run it in a thread.
"""

import json
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from app.providers.inkbox.webhook import InkboxSignatureError, verify

CLOSE_POLICY_VIOLATION = 1008
MAX_CALL_CONTEXT_BYTES = 8000
FALLBACK_REPLY = "Sorry, I could not do that."
RESPONSE_HEADERS = {"X-Use-Inkbox-Speech-To-Text": "true", "X-Use-Inkbox-Text-To-Speech": "true"}


class HandshakeRejected(Exception):
    """The WebSocket upgrade failed verification. Close with `close_code` (1008). `code` is stable and secret-free."""

    close_code = CLOSE_POLICY_VIOLATION

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


Brain = Callable[[str], "str | Iterable[str]"]


class CallSession:
    def __init__(self, handle: str, signing_key: str, brain: Brain, *, clock: Callable[[], float] = time.time) -> None:
        self.handle = handle
        self._signing_key = signing_key
        self._brain = brain
        self._clock = clock
        self.call_id = ""
        self.greeting = (
            ""  # spoken once when the call starts (inbound only; set by the route after the caller is known)
        )
        self._greeted = False
        self.ended = False
        self._accepted = False
        self._generation = 0  # bumped by barge-in and stop; a running stream compares it to know it was cancelled
        self._active: Any = None  # the brain's in-flight iterator, closed on cancel

    # -- handshake -------------------------------------------------------------------------------------------

    def accept(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Verify the upgrade (signature over the exact `X-Call-Context` value) and return the response headers."""
        lowered = {k.lower(): v for k, v in headers.items()}
        context = lowered.get("x-call-context")
        if context is None:
            raise HandshakeRejected("missing_call_context")
        raw = context.encode()
        if len(raw) > MAX_CALL_CONTEXT_BYTES:
            raise HandshakeRejected("call_context_too_large")
        try:
            verify(headers, raw, self._signing_key, now=self._clock())
        except InkboxSignatureError as exc:
            raise HandshakeRejected(exc.code) from None
        try:
            parsed = json.loads(context)
        except ValueError:
            raise HandshakeRejected("invalid_call_context") from None
        if not isinstance(parsed, dict):
            raise HandshakeRejected("invalid_call_context")
        call_id = parsed.get("call_id")
        self.call_id = call_id if isinstance(call_id, str) else ""
        self._accepted = True
        return dict(RESPONSE_HEADERS)

    # -- events ----------------------------------------------------------------------------------------------

    def on_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Handle one inbound event and return every event to send back (a whole turn, buffered)."""
        return list(self.stream_event(event))

    def stream_event(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Like `on_event` but lazy, so deltas can be sent as the brain produces them and a barge-in can cut it off."""
        if not self._accepted or self.ended:
            return iter(())
        kind = event.get("event")
        if kind == "transcript":
            return self._answer(event)
        if kind == "start" and self.greeting and not self._greeted:
            self._greeted = True
            return iter(
                [
                    {"event": "text", "delta": self.greeting, "done": False, "turn_id": "greeting"},
                    {"event": "text", "done": True, "turn_id": "greeting"},
                ]
            )
        if kind == "barge_in":
            self._cancel()
            return iter([{"event": "clear"}])
        if kind == "stop":
            self._cancel()
            self.ended = True
        return iter(())  # start, unknown events and malformed events need no answer

    def hangup(self) -> dict[str, Any]:
        self._cancel()
        self.ended = True
        return {"event": "stop", "reason": "agent_hang_up"}

    # -- internals -------------------------------------------------------------------------------------------

    def _cancel(self) -> None:
        self._generation += 1
        active, self._active = self._active, None
        close = getattr(active, "close", None)
        if callable(close):
            close()

    def _answer(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        text = event.get("text")
        if event.get("is_final") is not True or not isinstance(text, str) or not text.strip():
            return iter(())  # interim transcripts and blank finals are never answered
        turn_id = event.get("turn_id")
        generation = self._generation
        return self._stream_turn(text, turn_id, generation)

    def _stream_turn(self, text: str, turn_id: Any, generation: int) -> Iterator[dict[str, Any]]:
        def delta(piece: str) -> dict[str, Any]:
            return {"event": "text", "delta": piece, "done": False, "turn_id": turn_id}

        try:
            produced = self._brain(text)
            chunks = iter([produced]) if isinstance(produced, str) else iter(produced)
            self._active = chunks
            for piece in chunks:
                if generation != self._generation:
                    return  # barged in: stop speaking, and no `done`
                if piece:
                    yield delta(piece)
        except Exception:
            if generation != self._generation:
                return
            yield delta(FALLBACK_REPLY)  # fixed text: an exception message may hold anything
        if generation != self._generation:
            return
        self._active = None
        yield {"event": "text", "done": True, "turn_id": turn_id}
