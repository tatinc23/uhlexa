"""Worker dispatch metadata (spec 22.3, 22.7). Needs the worker venv: `.venv-voice/bin/python -m pytest --noconftest
tests/unit/test_voice_worker.py`; the main suite skips it."""

import json

import pytest

worker = pytest.importorskip("app.voice.worker", exc_type=ImportError)

from app.voice.config import VoiceConfig  # noqa: E402

USER_ID = "0a1b2c3d-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = VoiceConfig.from_env(
        {"KEIKI_AGENT_IDS": json.dumps({"alex": "agent-a"}), "VOICE_DEFAULT_WAKE_MODE": "always"}
    )
    monkeypatch.setattr(worker, "config", config)


def test_metadata_for_known_handle() -> None:
    meta = worker.parse_metadata(json.dumps({"handle": "alex", "user_id": USER_ID, "wake_mode": "wake_word"}))
    assert meta is not None and meta["wake_mode"] == "wake_word"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        json.dumps({"handle": "max", "user_id": USER_ID}),
        json.dumps({"handle": "alex"}),
        json.dumps({"user_id": USER_ID}),
    ],
)
def test_unusable_metadata_is_refused(raw: str) -> None:
    assert worker.parse_metadata(raw) is None


def test_bad_wake_mode_falls_back_to_default() -> None:
    meta = worker.parse_metadata(json.dumps({"handle": "alex", "user_id": USER_ID, "wake_mode": "loud"}))
    assert meta is not None and meta["wake_mode"] == "always"
