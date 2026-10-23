"""Inkbox-tunnel hosting (docs/HOSTING.md): tunnel config and the public access gate."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.hosting.gate import ACCESS_COOKIE, AccessGate
from app.hosting.tunnel import TunnelConfig

TOKEN = "s3cret-access-token-0123456789"


def _inner() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/mcp")
    def mcp() -> dict:
        return {"mcp": True}

    @app.post("/webhooks/inkbox/uhlexa")
    def webhook() -> dict:
        return {"hook": True}

    @app.get("/api/me")
    def me() -> dict:
        return {"me": "alex"}

    @app.get("/voice/hub.html")
    def hub() -> dict:
        return {"page": True}

    @app.websocket("/ws/phone/uhlexa")
    async def call(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("call ok")
        await ws.close()

    @app.websocket("/hosting/ws-echo")
    async def echo(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text(await ws.receive_text())
        await ws.close()

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(AccessGate(_inner(), token=TOKEN))


def test_machine_routes_stay_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.post("/mcp", headers={"authorization": "Bearer agent-token"}).status_code == 200
    assert client.post("/webhooks/inkbox/uhlexa").status_code == 200
    with client.websocket_connect("/ws/phone/uhlexa") as ws:
        assert ws.receive_text() == "call ok"


def test_browser_and_api_routes_need_the_access_cookie(client: TestClient) -> None:
    api = client.get("/api/me")
    assert api.status_code == 401 and api.json()["code"] == "access_required"
    page = client.get("/voice/hub.html", headers={"accept": "text/html"})
    assert page.status_code == 401 and "access link" in page.text.lower()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/hosting/ws-echo") as ws:
        ws.receive_text()


def test_access_link_sets_cookie_then_routes_open(client: TestClient) -> None:
    resp = client.get(f"/access?token={TOKEN}&next=/voice/hub.html", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/voice/hub.html"
    cookie = resp.cookies.get(ACCESS_COOKIE)
    assert cookie and TOKEN not in cookie
    assert "httponly" in resp.headers["set-cookie"].lower()
    assert client.get("/api/me").status_code == 200
    with client.websocket_connect("/hosting/ws-echo") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "ping"


def test_wrong_token_is_refused(client: TestClient) -> None:
    resp = client.get("/access?token=nope", follow_redirects=False)
    assert resp.status_code == 403
    assert ACCESS_COOKIE not in resp.cookies


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example",
        "//evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
        "/%09/evil.example",  # tab: browsers strip it, leaving //evil.example
        "/%0d%0a/evil.example",
        "/%5Cevil.example",  # backslash
        "/%5C%5Cevil.example",
        "/ /evil.example",
    ],
)
def test_next_cannot_redirect_off_site(client: TestClient, target: str) -> None:
    resp = client.get(f"/access?token={TOKEN}&next={target}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/voice/hub.html"


def test_missing_token_config_fails_closed() -> None:
    client = TestClient(AccessGate(_inner(), token=""))
    assert client.get("/api/me").status_code == 503
    assert client.get("/access?token=").status_code == 503
    assert client.get("/health").status_code == 200


def test_tunnel_config_defaults_and_public_url() -> None:
    config = TunnelConfig.from_env({"INKBOX_API_KEY": "ApiKey_x"})
    assert config.handle == "uhlexa"
    assert config.public_url == "https://uhlexa.inkboxwire.com"
    assert config.forward_to == "http://127.0.0.1:8000"
    assert config.problems() == []
    assert "ApiKey_x" not in repr(config)


def test_tunnel_config_problems() -> None:
    assert "INKBOX_API_KEY" in " ".join(TunnelConfig.from_env({}).problems())
    samantha = TunnelConfig.from_env({"INKBOX_API_KEY": "k", "INKBOX_TUNNEL_HANDLE": "samantha"})
    assert any("samantha" in p for p in samantha.problems())
    custom = TunnelConfig.from_env({"INKBOX_API_KEY": "k", "INKBOX_TUNNEL_HANDLE": "Parker-Mom", "APP_PORT": "9000"})
    assert custom.handle == "parker-mom" and custom.forward_to == "http://127.0.0.1:9000"


def test_mcp_without_a_bearer_token_needs_the_access_cookie(client: TestClient) -> None:
    """Without a bearer token the MCP layer falls back to demo auth (X-Uhlexa-User), which anyone could spoof."""
    for headers in ({}, {"x-uhlexa-user": "alex"}, {"authorization": "Basic abc"}):
        resp = client.post("/mcp", headers=headers)
        assert resp.status_code == 401 and resp.json()["code"] == "access_required"
    client.get(f"/access?token={TOKEN}", follow_redirects=False)
    assert client.post("/mcp").status_code == 200  # the simulator's own session, with the access cookie


@pytest.mark.parametrize("path", ["/mcpX", "/healthz-admin", "/readyX", "/webhooksX"])
def test_open_paths_are_anchored(client: TestClient, path: str) -> None:
    resp = client.post(path, headers={"authorization": "Bearer agent-token"})
    assert resp.status_code == 401 and resp.json()["code"] == "access_required"
