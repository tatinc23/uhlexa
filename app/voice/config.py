"""Voice + avatar configuration (spec 22.8), read from the environment.

The app reads this for the voice API; the worker reads it for its keys. Statuses follow base spec section 5: `ok`
only when the flag is on and every credential is present. Secrets are `repr=False` and never appear in `status()`.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from dotenv import dotenv_values

from app.providers.keiki.config import DEFAULT_BASE_URL, KeikiConfig
from app.voice.wake import WAKE_MODES

_TRUE = {"1", "true", "yes", "on"}


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    return default if raw is None or raw.strip() == "" else raw.strip().lower() in _TRUE


def _num(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name) or default)
    except ValueError:
        return default


def _agent_ids(raw: str) -> tuple[dict[str, str], bool]:
    """(handle -> Keiki agent id, parsed ok)."""
    if not raw.strip():
        return {}, True
    try:
        value = json.loads(raw)
    except ValueError:
        return {}, False
    if not isinstance(value, dict):
        return {}, False
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str) and v}, True


@dataclass(frozen=True)
class VoiceConfig:
    enable_voice: bool = False
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = field(default="", repr=False)
    agent_name: str = "uhlexa-voice"
    stt_provider: str = "livekit"  # livekit (LiveKit Inference, billed to the LiveKit project) | openai | groq
    tts_provider: str = "livekit"  # livekit | openai
    stt_model: str = "deepgram/nova-3"
    tts_model: str = "cartesia/sonic-3"
    groq_api_key: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    tts_voice: str = ""
    tts_voices: Mapping[str, str] = field(default_factory=dict)  # handle -> voice id; falls back to tts_voice
    default_wake_mode: str = "follow_up"
    follow_up_s: float = 8.0
    enable_avatar: bool = False
    runway_api_secret: str = field(default="", repr=False)
    runway_avatar_id: str = ""
    runway_preset_id: str = ""
    avatar_max_duration_s: int = 300
    idle_close_s: int = 120
    daily_turn_cap: int = 150
    enable_keiki: bool = False
    keiki_base_url: str = DEFAULT_BASE_URL
    keiki_gateway_key: str = field(default="", repr=False)
    keiki_agent_ids: Mapping[str, str] = field(default_factory=dict)
    keiki_agent_ids_valid: bool = True
    keiki_timeout_s: float = 120.0
    keiki_call_reasoning: str = "none"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "VoiceConfig":
        """`env` for tests; else the process env over `.env`, as `app.settings` does (not under APP_ENV=test)."""
        if env is None:
            dotenv = {} if os.environ.get("APP_ENV") == "test" else dotenv_values(".env")
            env = {**{k: v for k, v in dotenv.items() if v is not None}, **os.environ}
        agent_ids, ids_ok = _agent_ids(env.get("KEIKI_AGENT_IDS", ""))
        wake = env.get("VOICE_DEFAULT_WAKE_MODE", "follow_up").strip()
        return cls(
            enable_voice=_bool(env, "ENABLE_VOICE"),
            livekit_url=env.get("LIVEKIT_URL", "").strip(),
            livekit_api_key=env.get("LIVEKIT_API_KEY", "").strip(),
            livekit_api_secret=env.get("LIVEKIT_API_SECRET", "").strip(),
            agent_name=env.get("VOICE_AGENT_NAME", "").strip() or "uhlexa-voice",
            stt_provider=env.get("VOICE_STT_PROVIDER", "").strip() or "livekit",
            tts_provider=env.get("VOICE_TTS_PROVIDER", "").strip() or "livekit",
            stt_model=env.get("VOICE_STT_MODEL", "").strip() or "deepgram/nova-3",
            tts_model=env.get("VOICE_TTS_MODEL", "").strip() or "cartesia/sonic-3",
            groq_api_key=env.get("GROQ_API_KEY", "").strip(),
            openai_api_key=env.get("OPENAI_API_KEY", "").strip(),
            tts_voice=env.get("VOICE_TTS_VOICE", "").strip(),
            tts_voices=_agent_ids(env.get("VOICE_TTS_VOICES", ""))[0],
            default_wake_mode=wake if wake in WAKE_MODES else "follow_up",
            follow_up_s=_num(env, "VOICE_FOLLOW_UP_S", 8.0),
            enable_avatar=_bool(env, "ENABLE_AVATAR"),
            runway_api_secret=env.get("RUNWAYML_API_SECRET", "").strip(),
            runway_avatar_id=env.get("RUNWAY_AVATAR_ID", "").strip(),
            runway_preset_id=env.get("RUNWAY_PRESET_ID", "").strip(),
            avatar_max_duration_s=int(_num(env, "AVATAR_MAX_DURATION_S", 300)),
            idle_close_s=int(_num(env, "VOICE_IDLE_CLOSE_S", 120)),
            daily_turn_cap=int(_num(env, "VOICE_DAILY_TURN_CAP", 150)),
            enable_keiki=_bool(env, "ENABLE_KEIKI"),
            keiki_base_url=env.get("KEIKI_API_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            keiki_gateway_key=env.get("KEIKI_GATEWAY_KEY", "").strip(),
            keiki_agent_ids=agent_ids,
            keiki_agent_ids_valid=ids_ok,
            keiki_timeout_s=_num(env, "KEIKI_GATEWAY_TIMEOUT_S", 120.0),
            keiki_call_reasoning=env.get("KEIKI_CALL_REASONING", "").strip() or "none",
        )

    def tts_voice_for(self, handle: str) -> str:
        return self.tts_voices.get(handle) or self.tts_voice

    def agent_id_for(self, handle: str) -> str | None:
        return self.keiki_agent_ids.get(handle) or None

    def keiki_config(self) -> KeikiConfig:
        return KeikiConfig(
            enabled=self.enable_keiki,
            base_url=self.keiki_base_url,
            gateway_key=self.keiki_gateway_key,
            agent_ids=dict(self.keiki_agent_ids),
            timeout_s=self.keiki_timeout_s,
            call_reasoning=self.keiki_call_reasoning,
        )

    def _voice_status(self) -> dict[str, str]:
        if not self.enable_voice:
            return {"status": "disabled", "detail": "ENABLE_VOICE is off"}
        missing = [
            name
            for name, value in (
                ("LIVEKIT_URL", self.livekit_url),
                ("LIVEKIT_API_KEY", self.livekit_api_key),
                ("LIVEKIT_API_SECRET", self.livekit_api_secret),
            )
            if not value
        ]
        if not self.enable_keiki:
            missing.append("ENABLE_KEIKI")
        if not self.keiki_gateway_key:
            missing.append("KEIKI_GATEWAY_KEY")
        if not self.keiki_agent_ids_valid:
            return {"status": "misconfigured", "detail": "KEIKI_AGENT_IDS is not valid JSON"}
        if not self.keiki_agent_ids:
            missing.append("KEIKI_AGENT_IDS")
        if missing:
            return {"status": "misconfigured", "detail": "missing: " + ", ".join(missing)}
        return {"status": "ok", "detail": "configured"}

    def _avatar_status(self) -> dict[str, str]:
        if not self.enable_avatar:
            return {"status": "disabled", "detail": "ENABLE_AVATAR is off"}
        if not self.runway_api_secret:
            return {"status": "misconfigured", "detail": "missing: RUNWAYML_API_SECRET"}
        if not (self.runway_avatar_id or self.runway_preset_id):
            return {"status": "misconfigured", "detail": "missing: RUNWAY_AVATAR_ID or RUNWAY_PRESET_ID"}
        return {"status": "ok", "detail": "custom face" if self.runway_avatar_id else "preset face"}

    def status(self) -> dict[str, dict[str, str]]:
        return {"voice": self._voice_status(), "avatar": self._avatar_status()}
