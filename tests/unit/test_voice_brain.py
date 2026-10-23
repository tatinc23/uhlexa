"""Keiki voice brain (spec 22.3, 22.7): streaming, filler, time cap, errors become short spoken apologies, daily cap."""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from app.providers.keiki import KeikiBusy, KeikiTurnFailed, conversation_id_for
from app.voice.brain import (
    BUSY_MESSAGE,
    CAP_MESSAGE,
    ERROR_APOLOGY,
    FILLER,
    TIMEOUT_APOLOGY,
    VOICE_SYSTEM_LINE,
    VoiceBrain,
)

pytestmark = pytest.mark.anyio
USER_ID = "0a1b2c3d-0000-4000-8000-000000000001"


class FakeGateway:
    def __init__(self, pieces: list[str], delays: list[float] | None = None, error: Exception | None = None) -> None:
        self.pieces = pieces
        self.delays = delays or [0.0] * len(pieces)
        self.error = error
        self.calls: list[dict] = []

    async def stream_text(self, agent_id: str, conversation_id: str, text: str, **kw: object) -> AsyncIterator[str]:
        self.calls.append({"agent_id": agent_id, "conversation_id": conversation_id, "text": text, **kw})
        for piece, delay in zip(self.pieces, self.delays, strict=True):
            await asyncio.sleep(delay)
            yield piece
        if self.error is not None:
            raise self.error


def _brain(gateway: FakeGateway, **kw: object) -> VoiceBrain:
    defaults: dict[str, object] = {"filler_after_s": 0.05, "cap_s": 0.5, "daily_cap": 5, "counter": {}}
    return VoiceBrain(gateway, "agent-a", USER_ID, "none", **{**defaults, **kw})  # type: ignore[arg-type]


async def _collect(brain: VoiceBrain, text: str = "add milk") -> list[str]:
    return [piece async for piece in brain.reply(text)]


async def test_streams_deltas_in_order() -> None:
    assert await _collect(_brain(FakeGateway(["Added ", "milk."]))) == ["Added ", "milk."]


async def test_sends_voice_system_line_and_conversation_id() -> None:
    gateway = FakeGateway(["ok"])
    await _collect(_brain(gateway), "what's on the board")
    call = gateway.calls[0]
    assert call["agent_id"] == "agent-a"
    assert call["conversation_id"] == conversation_id_for(USER_ID, "voice") == "u0a1b2c3d-voice"
    assert call["text"] == "what's on the board"
    assert call["system"] == VOICE_SYSTEM_LINE
    assert call["reasoning_effort"] == "none"


async def test_slow_first_delta_speaks_filler() -> None:
    out = await _collect(_brain(FakeGateway(["Done."], delays=[0.15])))
    assert out == [FILLER, "Done."]


async def test_filler_only_once() -> None:
    out = await _collect(_brain(FakeGateway(["a", "b"], delays=[0.12, 0.12]), cap_s=1.0))
    assert out.count(FILLER) == 1 and out[-2:] == ["a", "b"]


async def test_total_cap_yields_timeout_apology() -> None:
    out = await _collect(_brain(FakeGateway(["late"], delays=[1.0]), cap_s=0.2))
    assert out == [FILLER, TIMEOUT_APOLOGY]


async def test_gateway_error_becomes_apology() -> None:
    out = await _collect(_brain(FakeGateway(["Adding "], error=KeikiTurnFailed("the turn failed", 500))))
    assert out == ["Adding ", ERROR_APOLOGY]


async def test_busy_conversation_says_so() -> None:
    out = await _collect(_brain(FakeGateway([], error=KeikiBusy("a turn is already running", 409))))
    assert out == [BUSY_MESSAGE]


async def test_daily_cap() -> None:
    counter: dict[str, int] = {}
    brain = _brain(FakeGateway(["ok"]), daily_cap=2, counter=counter)
    assert await _collect(brain) == ["ok"]
    assert await _collect(brain) == ["ok"]
    assert await _collect(brain) == [CAP_MESSAGE]


async def test_no_key_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    error = KeikiTurnFailed("network error (ConnectError) Bearer sk-should-not-appear", None)
    await _collect(_brain(FakeGateway([], error=error)))
    assert "sk-should-not-appear" not in caplog.text
    assert "KeikiTurnFailed" in caplog.text


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
