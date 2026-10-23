"""The voice brain (spec 22.3): one spoken turn = one Keiki gateway turn, streamed.

The Keiki agent is the LLM and calls Uhlexa's MCP tools itself; this module only shapes the turn for speech: a voice
system line, a filler when the first words are slow, a hard time cap, a daily cap, and a short spoken apology instead
of silence when anything fails. Exceptions are logged by type only (messages can echo request details).
"""

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from app.providers.keiki import KeikiBusy, KeikiError, conversation_id_for

logger = logging.getLogger(__name__)

VOICE_SYSTEM_LINE = "Voice: answer in one or two short spoken sentences. No lists, links, emoji or markdown."
FILLER = "One moment."
TIMEOUT_APOLOGY = "Sorry, that took too long. Please try again."
ERROR_APOLOGY = "Sorry, I couldn't reach my brain just now."
BUSY_MESSAGE = "Hang on, I'm still working on your last request."
CAP_MESSAGE = "I've hit today's voice limit."

_TURNS: dict[str, int] = {}  # per worker process, keyed by UTC date


class StreamingGateway(Protocol):
    def stream_text(
        self, agent_id: str, conversation_id: str, text: str, *, system: str | None, reasoning_effort: str | None
    ) -> AsyncIterator[str]: ...


class VoiceBrain:
    def __init__(
        self,
        gateway: StreamingGateway,
        agent_id: str,
        user_id: str,
        reasoning: str,
        *,
        filler_after_s: float = 2.5,
        cap_s: float = 20.0,
        daily_cap: int = 150,
        counter: dict[str, int] | None = None,
        today: Callable[[], datetime.date] = lambda: datetime.datetime.now(datetime.UTC).date(),
    ) -> None:
        self._gateway = gateway
        self._agent_id = agent_id
        self._conversation_id = conversation_id_for(user_id, "voice")
        self._reasoning = reasoning
        self._filler_after_s = filler_after_s
        self._cap_s = cap_s
        self._daily_cap = daily_cap
        self._counter = _TURNS if counter is None else counter
        self._today = today

    def _take_turn(self) -> bool:
        key = self._today().isoformat()
        if self._counter.get(key, 0) >= self._daily_cap:
            return False
        self._counter[key] = self._counter.get(key, 0) + 1
        return True

    async def reply(self, text: str) -> AsyncIterator[str]:
        """Text pieces to speak, in order. Never raises for provider trouble."""
        if not self._take_turn():
            yield CAP_MESSAGE
            return
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self._cap_s
        stream = self._gateway.stream_text(
            self._agent_id,
            self._conversation_id,
            text,
            system=VOICE_SYSTEM_LINE,
            reasoning_effort=self._reasoning,
        )
        pending: asyncio.Future[Any] = asyncio.ensure_future(anext(stream))
        spoke = filler_sent = False
        try:
            while True:
                wait = deadline - loop.time()
                if not spoke and not filler_sent:
                    wait = min(wait, started + self._filler_after_s - loop.time())
                done, _ = await asyncio.wait({pending}, timeout=max(wait, 0))
                if not done:
                    if not spoke and not filler_sent and loop.time() < deadline:
                        filler_sent = True
                        yield FILLER
                        continue
                    logger.warning("voice turn hit the %.0f s cap", self._cap_s)
                    yield TIMEOUT_APOLOGY
                    return
                try:
                    piece = pending.result()
                except StopAsyncIteration:
                    return
                except KeikiBusy:
                    logger.info("voice turn refused: conversation busy")
                    yield BUSY_MESSAGE
                    return
                except KeikiError as exc:
                    logger.warning("voice turn failed: %s", type(exc).__name__)
                    yield ERROR_APOLOGY
                    return
                spoke = True
                yield piece
                pending = asyncio.ensure_future(anext(stream))
        finally:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
