"""Voice API (spec 22.4, 22.7): status without secrets, sessions only for signed-in users with a Keiki agent,
metadata from the Principal, wake-mode settings scoped to the caller's own agent."""

import base64
import json
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.models import Agent

ENV = {
    "ENABLE_VOICE": "true",
    "LIVEKIT_URL": "wss://example.livekit.cloud",
    "LIVEKIT_API_KEY": "APIkey123",
    "LIVEKIT_API_SECRET": "lk-secret-value-0123456789abcdef",
    "ENABLE_KEIKI": "true",
    "KEIKI_GATEWAY_KEY": "keiki-secret-value",
    "KEIKI_AGENT_IDS": json.dumps({"alex": "agent-a", "katie": "agent-j"}),
    "ENABLE_AVATAR": "true",
    "RUNWAYML_API_SECRET": "runway-secret-value",
    "RUNWAY_AVATAR_ID": "avatar-123",
}
SECRETS = ("lk-secret-value-0123456789abcdef", "keiki-secret-value", "runway-secret-value")


VOICE_NAMES = (
    *ENV,
    "VOICE_AGENT_NAME",
    "VOICE_DEFAULT_WAKE_MODE",
    "RUNWAY_PRESET_ID",
    "KEIKI_API_BASE_URL",
)


@pytest.fixture(autouse=True)
def hermetic_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's shell must not leak voice settings into these tests."""
    for name in VOICE_NAMES:
        monkeypatch.setenv(name, "")


@pytest.fixture
def voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


def _metadata(token: str) -> dict:
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return json.loads(claims["roomConfig"]["agents"][0]["metadata"])


def test_status_has_no_secrets(client: TestClient, voice_env: None) -> None:
    resp = client.get("/api/voice/status")
    assert resp.status_code == 200
    assert resp.json()["voice"]["status"] == "ok"
    for secret in SECRETS:
        assert secret not in resp.text


def test_session_requires_principal(
    client: TestClient, seeded: dict[str, Principal], voice_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.settings import get_settings

    monkeypatch.setenv("ENABLE_DEMO_AUTH", "false")
    get_settings.cache_clear()
    resp = client.post("/api/voice/session", json={"surface": "echo"})
    assert resp.status_code == 401


def test_session_503_when_voice_disabled(
    client: TestClient, seeded: dict[str, Principal], as_user: Callable[[str], dict[str, str]]
) -> None:
    resp = client.post("/api/voice/session", json={"surface": "echo"}, headers=as_user("alex"))
    assert resp.status_code == 503
    assert resp.json()["provider"] == "voice"


def test_session_refuses_user_without_keiki_agent(
    client: TestClient, seeded: dict[str, Principal], as_user: Callable[[str], dict[str, str]], voice_env: None
) -> None:
    resp = client.post("/api/voice/session", json={"surface": "echo"}, headers=as_user("max"))
    assert resp.status_code == 409
    assert resp.json()["code"] == "voice_not_set_up"
    assert "token" not in resp.json()


def test_session_metadata_comes_from_principal(
    client: TestClient, seeded: dict[str, Principal], as_user: Callable[[str], dict[str, str]], voice_env: None
) -> None:
    resp = client.post("/api/voice/session", json={"surface": "echo", "handle": "katie"}, headers=as_user("alex"))
    assert resp.status_code == 200
    body = resp.json()
    meta = _metadata(body["token"])
    assert meta["handle"] == "alex"
    assert meta["user_id"] == str(seeded["alex"].user.id)
    assert meta["surface"] == "echo" and meta["wake_mode"] == "follow_up"
    assert body["url"] == ENV["LIVEKIT_URL"] and body["room"].startswith("uhlexa-echo-alex-")
    for secret in SECRETS:
        assert secret not in resp.text


def test_session_avatar_only_for_hub(
    client: TestClient, seeded: dict[str, Principal], as_user: Callable[[str], dict[str, str]], voice_env: None
) -> None:
    hub = client.post("/api/voice/session", json={"surface": "hub"}, headers=as_user("alex")).json()
    app = client.post("/api/voice/session", json={"surface": "app"}, headers=as_user("alex")).json()
    assert hub["avatar"] is True and _metadata(hub["token"])["avatar"] is True
    assert app["avatar"] is False and _metadata(app["token"])["avatar"] is False


def test_session_rejects_unknown_surface(
    client: TestClient, seeded: dict[str, Principal], as_user: Callable[[str], dict[str, str]], voice_env: None
) -> None:
    resp = client.post("/api/voice/session", json={"surface": "fridge"}, headers=as_user("alex"))
    assert resp.status_code == 422


def test_settings_roundtrip_and_validation(
    client: TestClient,
    db: Session,
    seeded: dict[str, Principal],
    as_user: Callable[[str], dict[str, str]],
    voice_env: None,
) -> None:
    assert client.get("/api/voice/settings", headers=as_user("alex")).json() == {
        "wake_mode": "follow_up",
        "modes": ["wake_word", "follow_up", "always"],
    }
    assert client.put("/api/voice/settings", json={"wake_mode": "loud"}, headers=as_user("alex")).status_code == 422
    resp = client.put("/api/voice/settings", json={"wake_mode": "always"}, headers=as_user("alex"))
    assert resp.status_code == 200 and resp.json()["wake_mode"] == "always"
    assert client.get("/api/voice/settings", headers=as_user("alex")).json()["wake_mode"] == "always"
    assert client.get("/api/voice/settings", headers=as_user("katie")).json()["wake_mode"] == "follow_up"
    db.expire_all()
    katie_agent = db.get(Agent, seeded["katie"].agent.id)
    assert "voice" not in (katie_agent.config or {})
    session = client.post("/api/voice/session", json={"surface": "echo"}, headers=as_user("alex")).json()
    assert _metadata(session["token"])["wake_mode"] == "always"


@pytest.mark.parametrize("page", ["echo", "app", "hub"])
def test_voice_pages_are_served(client: TestClient, page: str) -> None:
    short = client.get(f"/voice/{page}", follow_redirects=False)
    assert short.status_code == 307 and short.headers["location"] == f"/voice/{page}.html"
    html = client.get(f"/voice/{page}.html")
    assert html.status_code == 200 and "/voice-vendor/livekit-client.umd.min.js" in html.text
    assert client.get("/voice-vendor/livekit-client.umd.min.js").status_code == 200
