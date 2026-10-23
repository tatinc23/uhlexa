"""Voice config (spec 22.8): statuses, secrets never in status or repr, Keiki agent map."""

import json

from app.voice.config import VoiceConfig

FULL = {
    "ENABLE_VOICE": "true",
    "LIVEKIT_URL": "wss://example.livekit.cloud",
    "LIVEKIT_API_KEY": "APIkey123",
    "LIVEKIT_API_SECRET": "lk-secret-value",
    "ENABLE_KEIKI": "true",
    "KEIKI_GATEWAY_KEY": "keiki-secret-value",
    "KEIKI_AGENT_IDS": json.dumps({"alex": "agent-a", "katie": "agent-j"}),
    "ENABLE_AVATAR": "true",
    "RUNWAYML_API_SECRET": "runway-secret-value",
    "RUNWAY_AVATAR_ID": "avatar-123",
}
SECRETS = ("lk-secret-value", "keiki-secret-value", "runway-secret-value")


def test_disabled_by_default() -> None:
    status = VoiceConfig.from_env({}).status()
    assert status["voice"]["status"] == "disabled"
    assert status["avatar"]["status"] == "disabled"


def test_ok_when_everything_present() -> None:
    status = VoiceConfig.from_env(FULL).status()
    assert status["voice"] == {"status": "ok", "detail": "configured"}
    assert status["avatar"]["status"] == "ok"


def test_status_never_contains_secret_values() -> None:
    for env in (FULL, {**FULL, "LIVEKIT_URL": ""}, {**FULL, "RUNWAY_AVATAR_ID": ""}):
        config = VoiceConfig.from_env(env)
        dumped = json.dumps(config.status()) + repr(config)
        for secret in SECRETS:
            assert secret not in dumped


def test_status_misconfigured_when_livekit_missing() -> None:
    status = VoiceConfig.from_env({**FULL, "LIVEKIT_API_SECRET": ""}).status()["voice"]
    assert status["status"] == "misconfigured"
    assert "LIVEKIT_API_SECRET" in status["detail"]


def test_keiki_agent_ids_json() -> None:
    config = VoiceConfig.from_env(FULL)
    assert config.agent_id_for("alex") == "agent-a"
    assert config.agent_id_for("max") is None
    bad = VoiceConfig.from_env({**FULL, "KEIKI_AGENT_IDS": "{not json"})
    assert bad.status()["voice"] == {"status": "misconfigured", "detail": "KEIKI_AGENT_IDS is not valid JSON"}


def test_avatar_preset_fallback_and_missing_face() -> None:
    preset = VoiceConfig.from_env({**FULL, "RUNWAY_AVATAR_ID": "", "RUNWAY_PRESET_ID": "influencer"})
    assert preset.status()["avatar"] == {"status": "ok", "detail": "preset face"}
    none = VoiceConfig.from_env({**FULL, "RUNWAY_AVATAR_ID": ""})
    assert none.status()["avatar"]["status"] == "misconfigured"


def test_bad_default_wake_mode_falls_back() -> None:
    assert VoiceConfig.from_env({"VOICE_DEFAULT_WAKE_MODE": "sometimes"}).default_wake_mode == "follow_up"
    assert VoiceConfig.from_env({"VOICE_DEFAULT_WAKE_MODE": "always"}).default_wake_mode == "always"


def test_keiki_config_carries_reasoning_and_ids() -> None:
    keiki = VoiceConfig.from_env({**FULL, "KEIKI_CALL_REASONING": "low"}).keiki_config()
    assert keiki.enabled and keiki.call_reasoning == "low"
    assert dict(keiki.agent_ids) == {"alex": "agent-a", "katie": "agent-j"}


def test_each_person_has_their_own_tts_voice() -> None:
    voices = json.dumps({"alex": "voice-uhlexa", "katie": "voice-jordy"})
    config = VoiceConfig.from_env({**FULL, "VOICE_TTS_VOICES": voices, "VOICE_TTS_VOICE": "fallback"})
    assert config.tts_voice_for("alex") == "voice-uhlexa" and config.tts_voice_for("katie") == "voice-jordy"
    assert config.tts_voice_for("max") == "fallback"
    assert VoiceConfig.from_env({**FULL, "VOICE_TTS_VOICES": "{bad"}).tts_voice_for("alex") == ""
