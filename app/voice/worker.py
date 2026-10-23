"""Uhlexa voice worker (spec 22.3-22.7): LiveKit Agents 1.5, run with `scripts/voice_worker.sh dev`.

Pipeline per room: Silero VAD + turn detection -> STT -> wake gate -> `VoiceBrain` (Keiki agent, which calls Uhlexa's
MCP tools) -> TTS -> audio, or a Runway Character face on the hub. The worker holds no user authority and never touches
the database: who is talking comes only from the token-signed dispatch metadata.

Jobs run in threads (`JobExecutorType.THREAD`) so the one-face-at-a-time flag and the daily turn cap are shared.
"""

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterable, Callable
from typing import Any

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobExecutorType,
    StopResponse,
    cli,
    inference,
    llm,
    stt,
)
from livekit.agents.voice import ModelSettings
from livekit.plugins import groq, openai, runway, silero
from livekit.plugins.turn_detector.english import EnglishModel

from app.providers.keiki import KeikiGateway
from app.voice.brain import VoiceBrain
from app.voice.config import VoiceConfig
from app.voice.wake import WAKE_MODES, WakeGate, canonical_wake

logger = logging.getLogger("uhlexa.voice")

ATTR_AWAKE = "uhlexa.voice.awake"
ATTR_AVATAR = "uhlexa.avatar"
STT_PROMPT = "Uhlexa is the name of the family assistant. Sunday groceries, milk, eggs, Katie, Alex."
TTS_INSTRUCTIONS = "Warm, upbeat household assistant. Short and clear."
HUMAN_PREFIX = "human-"

config = VoiceConfig.from_env()
server = AgentServer(job_executor_type=JobExecutorType.THREAD, initialize_process_timeout=60.0)
_face_lock = threading.Lock()


class _KeikiIsTheBrain(llm.LLM):
    """Placeholder so LiveKit runs the LLM pipeline; `UhlexaVoiceAgent.llm_node` replaces every call."""

    def chat(self, **_: Any) -> llm.LLMStream:
        raise RuntimeError("UhlexaVoiceAgent.llm_node handles every turn")


def parse_metadata(raw: str) -> dict[str, Any] | None:
    """The server-built dispatch metadata, or None when unusable (unknown handle, missing ids)."""
    try:
        meta = json.loads(raw or "{}")
    except ValueError:
        return None
    if not isinstance(meta, dict):
        return None
    handle, user_id = meta.get("handle"), meta.get("user_id")
    if not isinstance(handle, str) or not isinstance(user_id, str) or config.agent_id_for(handle) is None:
        return None
    if meta.get("wake_mode") not in WAKE_MODES:
        meta["wake_mode"] = config.default_wake_mode
    return meta


def _stt() -> Any:
    """`livekit` (default) bills STT to the LiveKit project, so no extra provider key is needed."""
    if config.stt_provider == "openai":
        return openai.STT(api_key=config.openai_api_key, model="whisper-1", prompt=STT_PROMPT)
    if config.stt_provider == "groq":
        return groq.STT(api_key=config.groq_api_key, prompt=STT_PROMPT)
    return inference.STT(model=config.stt_model, language="en", extra_kwargs={"keyterm": ["Uhlexa"]})


def _tts(handle: str) -> Any:
    voice = config.tts_voice_for(handle)  # each family member's Uhlexa has its own voice
    if config.tts_provider == "openai":
        kwargs: dict[str, Any] = {"api_key": config.openai_api_key, "instructions": TTS_INSTRUCTIONS}
        if voice:
            kwargs["voice"] = voice
        return openai.TTS(**kwargs)
    return inference.TTS(model=config.tts_model, **({"voice": voice} if voice else {}))


def _face_kwargs() -> dict[str, Any]:
    face = {"avatar_id": config.runway_avatar_id} if config.runway_avatar_id else {"preset_id": config.runway_preset_id}
    return {**face, "api_key": config.runway_api_secret, "max_duration": config.avatar_max_duration_s}


class UhlexaVoiceAgent(Agent):
    def __init__(self, gate: WakeGate, brain: VoiceBrain, on_gate_change: Callable[[], None]) -> None:
        super().__init__(instructions="Relay to the family's Keiki agent.", llm=_KeikiIsTheBrain())
        self._gate = gate
        self._brain = brain
        self._on_gate_change = on_gate_change

    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings) -> AsyncIterable[Any]:
        """Captions and the gate see "Uhlexa", not the "Alexa," STT tends to write (spec 22.5)."""
        async for event in Agent.default.stt_node(self, audio, model_settings):
            if isinstance(event, stt.SpeechEvent):
                for alt in event.alternatives:
                    alt.text = canonical_wake(alt.text)
            yield event

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        decision = self._gate.decide(new_message.text_content or "")
        self._on_gate_change()
        if not decision.respond:
            raise StopResponse()
        if decision.bare_wake:
            self.session.say("Yes?")
            raise StopResponse()
        new_message.content = [decision.text]

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        text = next(
            (m.text_content for m in reversed(chat_ctx.items) if getattr(m, "role", None) == "user" and m.text_content),
            "",
        )
        if not text:
            return
        async for piece in self._brain.reply(text):
            yield piece


@server.rtc_session(agent_name=config.agent_name)
async def entrypoint(ctx: JobContext) -> None:
    meta = parse_metadata(ctx.job.metadata)
    if meta is None:
        logger.warning("voice job refused: unusable dispatch metadata")
        ctx.shutdown("unusable metadata")
        return
    await ctx.connect()
    local = ctx.room.local_participant
    attrs: dict[str, str] = {ATTR_AWAKE: "", ATTR_AVATAR: "off"}

    def publish(**changes: str) -> None:
        changes = {k: v for k, v in changes.items() if attrs.get(k) != v}
        if changes:
            attrs.update(changes)
            asyncio.ensure_future(local.set_attributes(changes))

    gate = WakeGate(meta["wake_mode"], follow_up_s=config.follow_up_s)
    publish_awake = lambda: publish(**{ATTR_AWAKE: "true" if gate.awake else "false"})  # noqa: E731
    brain = VoiceBrain(
        KeikiGateway(config.keiki_config()),
        config.agent_id_for(meta["handle"]) or "",
        meta["user_id"],
        config.keiki_call_reasoning,
        daily_cap=config.daily_turn_cap,
    )
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=_stt(),
        tts=_tts(meta["handle"]),
        turn_handling={"turn_detection": EnglishModel(), "preemptive_generation": {"enabled": False}},
    )

    face: runway.AvatarSession | None = None
    holds_face = False
    if meta.get("avatar") and config.status()["avatar"]["status"] == "ok":
        holds_face = _face_lock.acquire(blocking=False)
        if not holds_face:
            logger.info("face busy: another hub holds the Runway session; voice only")
            publish(**{ATTR_AVATAR: "failed"})
        else:
            publish(**{ATTR_AVATAR: "starting"})
            try:
                face = runway.AvatarSession(**_face_kwargs())
                await face.start(session, room=ctx.room)  # before session.start (sets the audio output)
                publish(**{ATTR_AVATAR: "on"})
            except Exception as exc:  # no credits, bad id, network: keep voice, show the ring
                logger.warning("Runway face failed to start: %s", type(exc).__name__)
                face = None
                _face_lock.release()
                holds_face = False
                publish(**{ATTR_AVATAR: "failed"})

    async def close_face() -> None:
        nonlocal face, holds_face
        if face is not None:
            with contextlib.suppress(Exception):
                await face.aclose()
            face = None
            publish(**{ATTR_AVATAR: "off"})
        if holds_face:
            _face_lock.release()
            holds_face = False

    ctx.add_shutdown_callback(close_face)

    stopping = asyncio.Event()

    @session.on("close")
    def _closed(ev: Any) -> None:
        # The person left (RoomIO closes the session on disconnect): give the face back and end the job.
        stopping.set()
        asyncio.ensure_future(close_face())
        ctx.shutdown("voice session closed")

    loop = asyncio.get_running_loop()
    last_activity = loop.time()

    @session.on("agent_state_changed")
    def _agent_state(ev: Any) -> None:
        nonlocal last_activity
        last_activity = loop.time()
        if ev.old_state == "speaking" and ev.new_state != "speaking":
            gate.reply_finished()
            publish_awake()

    @session.on("user_state_changed")
    def _user_state(ev: Any) -> None:
        nonlocal last_activity
        last_activity = loop.time()

    @ctx.room.on("data_received")
    def _data(packet: rtc.DataPacket) -> None:
        sender = packet.participant
        if sender is None or not sender.identity.startswith(HUMAN_PREFIX):
            return
        try:
            msg = json.loads(packet.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "open":
            gate.open_window()
        elif msg.get("type") == "wake_mode" and msg.get("value") in WAKE_MODES:
            gate.set_mode(msg["value"])
        publish_awake()

    await session.start(agent=UhlexaVoiceAgent(gate, brain, publish_awake), room=ctx.room)
    publish_awake()

    while not stopping.is_set():  # the window expires on its own; idle hubs give the face back
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=1.0)
        publish_awake()
        if face is not None and loop.time() - last_activity > config.idle_close_s:
            logger.info("closing idle Runway face after %s s", config.idle_close_s)
            await close_face()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli.run_app(server)
