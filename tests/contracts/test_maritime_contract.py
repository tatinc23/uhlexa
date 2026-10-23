"""Maritime Computers contract tests (plan Task 11).

Everything runs against an in-process fake of the Maritime Computers MCP server. The fake exposes the documented tool
names and argument schemas (docs/research/2026-10-22-maritime-facts.md section 1.2) and answers in the result formats
recorded in that file's "Live run" section; it also exposes the five tools the client must never call, which record any
call so a test can prove they stay untouched. The fake is test code only: product code never fakes a computer.
Keys here are sentinels, not credentials.
"""

import asyncio
import contextlib
import inspect
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.errors import ProviderUnavailable
from app.providers import provider_statuses
from app.providers.maritime.client import (
    ALLOWED_ACTIONS,
    ALLOWED_TOOLS,
    FORBIDDEN_TOOLS,
    Desktop,
    ForbiddenOperation,
    HumanInControl,
    MaritimeClient,
)
from app.providers.maritime.models import RunOutcome, StepRecord, TakeoverState
from app.providers.maritime.service import MaritimeService
from app.settings import Settings

KEY = "mk_SENTINEL-computers-key-0123456789abcdef"
TAKEOVER_LINK = "https://viewer.example.test/computer/cmp_fake?t=SECRET-TAKEOVER-TOKEN-8f3a"
SECRET_PART = "SECRET-TAKEOVER-TOKEN"
COMPUTER_ID = "cmp_fake_1"
MCP_URL = "https://mcp.example.test/mcp"
SAFE_URL = "https://todomvc.example.test/examples/react/dist/"
WALL_URL = "https://uhlexa.example.test/demo/sign-in-wall"

# The plan the live run proved (docs/research "Live run"): every action, in order, for ["milk", "eggs"].
EXPECTED_LIST_ADD_ACTIONS = [
    ("screenshot", {}),
    ("key", {"text": "alt+Tab"}),
    ("key", {"text": "ctrl+shift+n"}),
    ("key", {"text": "ctrl+l"}),
    ("type", {"text": SAFE_URL}),
    ("key", {"text": "Return"}),
    ("wait", {"duration": 3.0}),
    ("left_click", {"coordinate": [733, 256]}),
    ("type", {"text": "milk"}),
    ("key", {"text": "Return"}),
    ("type", {"text": "eggs"}),
    ("key", {"text": "Return"}),
    ("wait", {"duration": 1.0}),
]


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "enable_maritime": True,
        "maritime_computers_key": KEY,
        "maritime_mcp_url": MCP_URL,
        "maritime_safe_target_url": SAFE_URL,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# --- the fake Maritime Computers MCP server ------------------------------------------------------------------------


class FakeMaritime:
    """A stand-in for mcp.maritime.sh. Records every call it receives, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.frame_id = 0
        self.takeover_mode = "agent"
        self.errors: dict[str, str] = {}  # tool name -> error text returned as an isError result
        self.action_errors: dict[tuple[str, int], str] = {}  # (action, nth call of it) -> error text
        self.action_bodies: dict[tuple[str, int], dict[str, Any]] = {}  # (action, nth) -> extra keys in the text body
        self.replies: dict[str, str] = {}  # tool name -> text that replaces the normal reply
        self.takeover_result: str | None = None
        self._action_counts: dict[str, int] = {}
        self.server = MCPServer("fake-maritime-computers")
        self._register()

    @property
    def tool_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def actions(self) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == "computer"]

    def action_names(self) -> list[str]:
        return [a["action"] for a in self.actions()]

    def action_plan(self) -> list[tuple[str, dict[str, Any]]]:
        """(action, arguments other than computer_id/action) per `computer` call."""
        return [
            (a["action"], {k: v for k, v in a.items() if k not in ("action", "computer_id")}) for a in self.actions()
        ]

    def typed(self) -> list[str]:
        return [a["text"] for a in self.actions() if a["action"] == "type"]

    def _fail_if_configured(self, tool: str) -> None:
        if tool in self.errors:
            raise ToolError(self.errors[tool])

    def _register(self) -> None:
        server = self.server

        @server.tool()
        def get_computer(
            user_id: str | None = None, name: str | None = None, os: str | None = None, arch: str | None = None
        ):
            self.calls.append(("get_computer", {"user_id": user_id, "name": name, "os": os, "arch": arch}))
            self._fail_if_configured("get_computer")
            if "get_computer" in self.replies:
                return self.replies["get_computer"]
            return json.dumps(
                {
                    "arch": "x86_64",
                    "computer_id": COMPUTER_ID,
                    "external_user_id": "u",
                    "hint": "Screenshots are 1200x750 by default.",
                    "mode": "agent",
                    "model_frame": {"height": 750, "width": 1200},
                    "os": "linux",
                    "screen": {"height": 800, "width": 1280},
                    "status": "ready",
                }
            )

        @server.tool()
        def computer(
            computer_id: str,
            action: str,
            coordinate: list[int] | None = None,
            start_coordinate: list[int] | None = None,
            text: str | None = None,
            modifier: str | None = None,
            key: str | None = None,
            repeat: int | None = None,
            scroll_direction: str | None = None,
            scroll_amount: int | None = None,
            duration: float | None = None,
            region: list[int] | None = None,
            no_screenshot: bool | None = None,
            format: str | None = None,
            quality: int | None = None,
        ):
            args = {k: v for k, v in locals().items() if v is not None and k != "self"}
            self.calls.append(("computer", args))
            self._fail_if_configured("computer")
            nth = self._action_counts.get(action, 0) + 1
            self._action_counts[action] = nth
            if (action, nth) in self.action_errors:
                raise ToolError(self.action_errors[(action, nth)])
            if self.takeover_mode == "human":
                raise ToolError("human_in_control: a person holds the desktop, retry after 15 s")
            self.frame_id += 1
            # The text block exactly as recorded live (2026-10-22): every optional field is present and null.
            body = {
                "action": action,
                "blocked": None,
                "coordinate": None,
                "error": None,
                "frame_id": self.frame_id,
                "height": 750,
                "hint": None,
                "is_error": None,
                "mode": None,
                "note": None,
                "ok": None,
                "path": "/data/desktop/screenshots/latest.png",
                "physical": [1280, 800],
                "reason": None,
                "scale": 1.0667,
                "width": 1200,
            }
            body.update(self.action_bodies.get((action, nth), {}))
            return [Image(data=b"\xff\xd8" + f"frame-{self.frame_id}".encode(), format="jpeg"), json.dumps(body)]

        @server.tool()
        def request_takeover(computer_id: str, reason: str, timeout: float | None = None, wait: bool = False):
            self.calls.append(("request_takeover", {"computer_id": computer_id, "reason": reason, "wait": wait}))
            self._fail_if_configured("request_takeover")
            self.takeover_mode = "human"
            if "request_takeover" in self.replies:
                return self.replies["request_takeover"]
            return json.dumps(
                {
                    "expires_at": "2026-10-22T15:08:36Z",
                    "instructions": "Send this link to the person through your own channel.",
                    "status": "waiting",
                    "viewer_url": TAKEOVER_LINK,
                }
            )

        @server.tool()
        def takeover_status(computer_id: str):
            self.calls.append(("takeover_status", {"computer_id": computer_id}))
            self._fail_if_configured("takeover_status")
            if "takeover_status" in self.replies:
                return self.replies["takeover_status"]
            return json.dumps(
                {
                    "last_takeover_note": None,
                    "last_takeover_result": self.takeover_result,
                    "mode": self.takeover_mode,
                    "takeover_expires_at": None,
                }
            )

        @server.tool()
        def close_computer(computer_id: str):
            self.calls.append(("close_computer", {"computer_id": computer_id}))
            self._fail_if_configured("close_computer")
            return json.dumps({"closed": True, "session": {"end_reason": "closed"}})

        # The five tools the client must never call: they only record, so a test can prove they stayed untouched.
        @server.tool()
        def run_shell(computer_id: str, command: str, timeout: float | None = None):
            self.calls.append(("run_shell", {"computer_id": computer_id, "command": command}))
            return "{}"

        @server.tool()
        def read_file(computer_id: str, path: str):
            self.calls.append(("read_file", {"computer_id": computer_id, "path": path}))
            return "{}"

        @server.tool()
        def write_file(computer_id: str, path: str, content: str, encoding: str | None = None):
            self.calls.append(("write_file", {"computer_id": computer_id, "path": path}))
            return "{}"

        @server.tool()
        def android(computer_id: str, action: str, package: str | None = None, url: str | None = None):
            self.calls.append(("android", {"computer_id": computer_id, "action": action}))
            return "{}"

        @server.tool()
        def computer_batch(computer_id: str, actions: list[dict[str, Any]]):
            self.calls.append(("computer_batch", {"computer_id": computer_id, "n": len(actions)}))
            return "{}"


class RecordingClient:
    """Wraps `mcp.Client` and records the per-call read timeout the product passes."""

    def __init__(self, inner: Client) -> None:
        self._inner = inner
        self.timeouts: list[tuple[str, float | None]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.timeouts.append((name, kwargs.get("read_timeout_seconds")))
        return await self._inner.call_tool(name, arguments, **kwargs)


class Harness:
    """A fake server plus a `connect` factory that opens in-process MCP clients to it."""

    def __init__(self, fake: FakeMaritime) -> None:
        self.fake = fake
        self.recorders: list[RecordingClient] = []

    def connect(self) -> contextlib.AbstractAsyncContextManager[Any]:
        @contextlib.asynccontextmanager
        async def _open() -> AsyncIterator[RecordingClient]:
            async with Client(self.fake.server) as inner:
                recorder = RecordingClient(inner)
                self.recorders.append(recorder)
                yield recorder

        return _open()

    def timeouts(self, tool: str) -> list[float | None]:
        return [t for r in self.recorders for name, t in r.timeouts if name == tool]


class ScriptedVerifier:
    """A verifier that answers by matching a keyword of the question; unmatched questions are `not_verified`."""

    def __init__(self, answers: dict[str, str] | None = None, *, default: str = "not_verified") -> None:
        self.answers = answers if answers is not None else {}
        self.default = default
        self.calls: list[tuple[bytes, str]] = []

    def __call__(self, image: bytes, question: str) -> str:
        self.calls.append((image, question))
        for keyword, verdict in self.answers.items():
            if keyword in question:
                return verdict
        return self.default

    def questions(self) -> list[str]:
        return [q for _, q in self.calls]


ALL_VERIFIED = {"Incognito": "verified", "TodoMVC page": "verified", "todo list": "verified", "sign-in": "verified"}


@pytest.fixture
def fake() -> FakeMaritime:
    return FakeMaritime()


@pytest.fixture
def harness(fake: FakeMaritime) -> Harness:
    return Harness(fake)


@pytest.fixture
def client(harness: Harness) -> MaritimeClient:
    return MaritimeClient(make_settings(), f"{MCP_URL}/u/{uuid.uuid4()}", connect=harness.connect)


def make_client(harness: Harness, **kwargs: Any) -> MaritimeClient:
    return MaritimeClient(make_settings(), f"{MCP_URL}/u/{uuid.uuid4()}", connect=harness.connect, **kwargs)


def make_service(harness: Harness, **kwargs: Any) -> MaritimeService:
    settings = make_settings()
    return MaritimeService(
        settings, client_factory=lambda url: MaritimeClient(settings, url, connect=harness.connect), **kwargs
    )


def run_list_add(
    client: MaritimeClient, items: list[str] | None = None, verifier: Any = None
) -> tuple[RunOutcome, list[StepRecord]]:
    steps: list[StepRecord] = []
    outcome = client.run_list_add(
        COMPUTER_ID, SAFE_URL, items if items is not None else ["milk", "eggs"], steps.append, verifier
    )
    return outcome, steps


def assert_only_allowlisted(fake: FakeMaritime) -> None:
    assert set(fake.tool_names) <= ALLOWED_TOOLS, set(fake.tool_names) - ALLOWED_TOOLS
    assert set(fake.action_names()) <= ALLOWED_ACTIONS, set(fake.action_names()) - ALLOWED_ACTIONS
    assert not set(fake.tool_names) & FORBIDDEN_TOOLS


# --- pinned URL: server-side, stable, no input path ----------------------------------------------------------------


def test_pinned_url_server_side_and_stable() -> None:
    service = MaritimeService(make_settings())
    for _ in range(50):
        user_id = uuid.uuid4()
        url = service.pinned_mcp_url(user_id)
        assert url == f"{MCP_URL}/u/{user_id}" == service.pinned_mcp_url(user_id)
        assert url.count(str(user_id)) == 1
    with pytest.raises(TypeError):
        service.pinned_mcp_url("alex")  # type: ignore[arg-type]


def test_no_public_call_path_takes_a_url_or_user_from_tool_or_http_input() -> None:
    """The only connection input is the pinned URL, built from the authenticated user's id inside the service."""
    forbidden = {"pinned_url", "mcp_url", "url", "user", "handle", "headers", "header", "key", "api_key", "token"}
    for cls in (MaritimeClient, MaritimeService):
        for name, member in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_") or name == "pinned_mcp_url":
                continue
            params = set(inspect.signature(member).parameters) - {"self"}
            assert not params & forbidden, f"{cls.__name__}.{name} takes {params & forbidden}"
    # `user_id` exists only on the service (the authenticated uuid.UUID) and never on the wire client's operations.
    for name in ("get_computer", "run_list_add", "run_sign_in_check", "request_takeover", "takeover_status"):
        assert "user_id" not in inspect.signature(getattr(MaritimeClient, name)).parameters
    assert list(inspect.signature(MaritimeService.pinned_mcp_url).parameters) == ["self", "user_id"]


# --- the real HTTP path: pinned URL and Authorization header, over an in-process ASGI server -------------------------


@contextlib.asynccontextmanager
async def asgi_maritime(
    fake: FakeMaritime, *, accept_key: str = KEY
) -> AsyncIterator[tuple[httpx2.ASGITransport, dict[str, list[str]]]]:
    """Serve the fake over the real Streamable HTTP wire, checking the key like Maritime does (401 otherwise)."""
    seen: dict[str, list[str]] = {"paths": [], "auth": []}
    inner = fake.server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0"
    )

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode(): v.decode() for k, v in scope["headers"]}
            seen["paths"].append(scope["path"])
            seen["auth"].append(headers.get("authorization", ""))
            if headers.get("authorization", "") != f"Bearer {accept_key}":
                body = json.dumps(
                    {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Maritime rejected the key"}, "id": None}
                )
                await send(
                    {"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]}
                )
                await send({"type": "http.response.body", "body": body.encode()})
                return
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}  # /mcp/u/<id> is the same endpoint, pinned
        await inner(scope, receive, send)

    async with fake.server.session_manager.run():
        yield httpx2.ASGITransport(app=app), seen


@pytest.mark.anyio
async def test_auth_header_documented_and_key_not_logged(fake: FakeMaritime, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    user_id = uuid.uuid4()
    pinned = MaritimeService(make_settings()).pinned_mcp_url(user_id)
    async with asgi_maritime(fake) as (transport, seen):
        client = MaritimeClient(make_settings(), pinned, transport=transport)
        assert await client.aget_computer() == COMPUTER_ID
    assert seen["auth"] and set(seen["auth"]) == {f"Bearer {KEY}"}
    assert set(seen["paths"]) == {f"/mcp/u/{user_id}"}
    assert KEY not in caplog.text and KEY not in repr(client)
    assert fake.calls[0][1]["user_id"] is None  # pinned: the user id is never sent as an argument


@pytest.mark.anyio
async def test_wrong_key_is_unavailable_and_never_echoed(fake: FakeMaritime) -> None:
    pinned = MaritimeService(make_settings()).pinned_mcp_url(uuid.uuid4())
    async with asgi_maritime(fake, accept_key="mk_someone-elses-key") as (transport, _seen):
        client = MaritimeClient(make_settings(), pinned, transport=transport)
        with pytest.raises(ProviderUnavailable) as caught:
            await client.aget_computer()
    assert caught.value.provider == "maritime" and KEY not in caught.value.reason and KEY not in str(caught.value)
    assert fake.calls == []


# --- allowlists ----------------------------------------------------------------------------------------------------


def test_allowlists_match_the_spec() -> None:
    assert set(ALLOWED_TOOLS) == {"get_computer", "computer", "request_takeover", "takeover_status", "close_computer"}
    assert set(ALLOWED_ACTIONS) == {"screenshot", "left_click", "double_click", "type", "key", "wait", "scroll", "zoom"}
    assert set(FORBIDDEN_TOOLS) == {"run_shell", "read_file", "write_file", "android", "computer_batch"}
    assert not ALLOWED_TOOLS & FORBIDDEN_TOOLS


@pytest.mark.anyio
@pytest.mark.parametrize("tool", sorted(FORBIDDEN_TOOLS | {"anything_else"}))
async def test_forbidden_tools_are_refused_before_reaching_the_wire(
    client: MaritimeClient, fake: FakeMaritime, tool: str
) -> None:
    async with client.session() as session:
        with pytest.raises(ForbiddenOperation):
            await session.call(tool, {"computer_id": COMPUTER_ID})
    assert fake.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "action",
    [
        "right_click",
        "middle_click",
        "triple_click",
        "mouse_move",
        "left_click_drag",
        "left_mouse_down",
        "hold_key",
        "cursor_position",
        "run_shell",
        "",
    ],
)
async def test_actions_outside_the_allowlist_are_refused(
    client: MaritimeClient, fake: FakeMaritime, action: str
) -> None:
    async with client.session() as session:
        with pytest.raises(ForbiddenOperation):
            await Desktop(session, COMPUTER_ID).act(action)
    assert fake.calls == []


@pytest.mark.anyio
async def test_unknown_action_arguments_are_refused(client: MaritimeClient, fake: FakeMaritime) -> None:
    async with client.session() as session:
        desktop = Desktop(session, COMPUTER_ID)
        with pytest.raises(ForbiddenOperation):
            await desktop.act("type", text="milk", command="rm -rf /")
        with pytest.raises(ForbiddenOperation):
            await desktop.act("type", text="milk", computer_id="someone-elses-computer")
    assert fake.calls == []


# --- get_computer ----------------------------------------------------------------------------------------------------


def test_get_computer_omits_user_id_and_waits_at_least_90s(
    client: MaritimeClient, fake: FakeMaritime, harness: Harness
) -> None:
    assert client.get_computer() == COMPUTER_ID
    assert fake.calls == [("get_computer", {"user_id": None, "name": None, "os": None, "arch": None})]
    (timeout,) = harness.timeouts("get_computer")
    assert timeout is not None and timeout >= 90


@pytest.mark.parametrize(
    "slug",
    [
        "computer_limit",
        "concurrency",
        "plan_lapsed",
        "no_plan",
        "no_capacity",
        "computers_disabled",
        "wake_timeout",
        "wake_transient",
    ],
)
def test_maritime_provider_error_is_unavailable(fake: FakeMaritime, harness: Harness, slug: str) -> None:
    fake.errors["get_computer"] = f"{slug}: Maritime refused this request. retryAfterS: 15"
    service = make_service(harness)
    with pytest.raises(ProviderUnavailable) as caught:
        service.ensure_computer(uuid.uuid4())
    assert caught.value.provider == "maritime" and slug in caught.value.reason
    assert provider_statuses(make_settings())["maritime"].status == "unavailable"


def test_unknown_computer_is_a_refusal_not_an_outage(fake: FakeMaritime, harness: Harness) -> None:
    fake.errors["get_computer"] = "not_found: no such computer"
    service = make_service(harness)
    with pytest.raises(ProviderUnavailable, match="not found"):
        service.ensure_computer(uuid.uuid4())
    assert provider_statuses(make_settings())["maritime"].status == "ok"  # Maritime answered; it is not down


def test_unavailable_clears_on_the_next_success(fake: FakeMaritime, harness: Harness) -> None:
    service = make_service(harness)
    fake.errors["get_computer"] = "computer_limit: no free slot"
    with pytest.raises(ProviderUnavailable):
        service.ensure_computer(uuid.uuid4())
    fake.errors.clear()
    assert service.ensure_computer(uuid.uuid4()) == COMPUTER_ID
    assert service.status.status == "ok"


def test_get_computer_reads_the_live_reply_format_and_prose_fallbacks(
    fake: FakeMaritime, client: MaritimeClient
) -> None:
    fake.replies["get_computer"] = "computer_id: cmp-prose-9, status: ready"
    assert client.get_computer() == "cmp-prose-9"


def test_get_computer_with_no_id_in_the_reply_is_unavailable(fake: FakeMaritime, client: MaritimeClient) -> None:
    fake.replies["get_computer"] = "ok but no id here"
    with pytest.raises(ProviderUnavailable, match="computer id"):
        client.get_computer()


def test_unknown_error_text_is_sanitised(fake: FakeMaritime, client: MaritimeClient) -> None:
    fake.errors["get_computer"] = f"boom with {KEY} and https://leak.example.test/secret-path " + "x" * 500
    with pytest.raises(ProviderUnavailable) as caught:
        client.get_computer()
    reason = caught.value.reason
    assert KEY not in reason and "leak.example.test" not in reason and len(reason) < 300


# --- takeover --------------------------------------------------------------------------------------------------------


def test_request_takeover_does_not_wait_and_returns_the_link(client: MaritimeClient, fake: FakeMaritime) -> None:
    assert client.request_takeover(COMPUTER_ID, "Sign-in needed") == TAKEOVER_LINK
    (call,) = [c for c in fake.calls if c[0] == "request_takeover"]
    assert call[1] == {"computer_id": COMPUTER_ID, "reason": "Sign-in needed", "wait": False}


def test_request_takeover_finds_a_link_in_prose(client: MaritimeClient, fake: FakeMaritime) -> None:
    fake.replies["request_takeover"] = f"Takeover requested. Open {TAKEOVER_LINK} to take control."
    assert client.request_takeover(COMPUTER_ID, "x") == TAKEOVER_LINK


def test_takeover_link_is_a_secret_never_logged_or_in_errors(
    client: MaritimeClient, fake: FakeMaritime, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def client_side_log() -> str:
        # The in-process fake server logs the error it was told to raise (its own logger, not product code).
        return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("mcp.server"))

    client.request_takeover(COMPUTER_ID, "Sign-in needed")
    assert SECRET_PART not in client_side_log()
    fake.errors["request_takeover"] = f"no_capacity: could not mint {TAKEOVER_LINK}"
    with pytest.raises(ProviderUnavailable) as caught:
        client.request_takeover(COMPUTER_ID, "again")
    assert SECRET_PART not in caught.value.reason and SECRET_PART not in str(caught.value)
    assert SECRET_PART not in client_side_log()


def test_request_takeover_without_a_link_in_the_reply_is_unavailable(
    fake: FakeMaritime, client: MaritimeClient
) -> None:
    fake.replies["request_takeover"] = "Takeover requested."
    with pytest.raises(ProviderUnavailable, match="link"):
        client.request_takeover(COMPUTER_ID, "x")


def test_takeover_status_reports_human_then_agent(client: MaritimeClient, fake: FakeMaritime) -> None:
    assert client.takeover_status(COMPUTER_ID) == "agent"
    client.request_takeover(COMPUTER_ID, "login")
    assert client.takeover_status(COMPUTER_ID) == "human"
    fake.takeover_mode = "agent"
    assert client.takeover_status(COMPUTER_ID) == "agent"


def test_takeover_state_carries_how_the_last_takeover_ended(client: MaritimeClient, fake: FakeMaritime) -> None:
    fake.takeover_result = "expired"
    state = client.takeover_state(COMPUTER_ID)
    assert state == TakeoverState(mode="agent", result="expired", note=None, expires_at=None)
    fake.takeover_result = "bogus"
    assert client.takeover_state(COMPUTER_ID).result is None


@pytest.mark.anyio
async def test_resume_only_when_agent_control(client: MaritimeClient, fake: FakeMaritime) -> None:
    """While a person holds the desktop every action is refused; `takeover_status` is the only way back."""
    fake.takeover_mode = "human"
    assert await client.atakeover_status(COMPUTER_ID) == "human"
    async with client.session() as session:
        with pytest.raises(HumanInControl):
            await Desktop(session, COMPUTER_ID).see()
    fake.takeover_mode = "agent"
    assert await client.atakeover_status(COMPUTER_ID) == "agent"
    async with client.session() as session:
        assert (await Desktop(session, COMPUTER_ID).see()).frame_id == 1


def test_takeover_status_unknown_when_the_reply_is_unreadable(fake: FakeMaritime, client: MaritimeClient) -> None:
    fake.replies["takeover_status"] = "no idea"
    assert client.takeover_status(COMPUTER_ID) == "unknown"


def test_close_computer_is_allowlisted(client: MaritimeClient, fake: FakeMaritime) -> None:
    client.close_computer(COMPUTER_ID)
    assert fake.tool_names == ["close_computer"]


# --- sync bridge -----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sync_api_works_from_inside_a_running_event_loop(client: MaritimeClient) -> None:
    """The worker is sync, but a caller may already sit in a loop (e.g. an async test or an embedded thread)."""
    assert await asyncio.to_thread(client.get_computer) == COMPUTER_ID
    assert client.get_computer() == COMPUTER_ID  # blocking inside the loop: handled by the helper-thread fallback


def test_log_lines_never_contain_the_key(client: MaritimeClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client.get_computer()
    assert KEY not in caplog.text and KEY not in repr(client)


# --- run_list_add: the fixed plan ------------------------------------------------------------------------------------


def test_run_list_add_happy_path(client: MaritimeClient, fake: FakeMaritime) -> None:
    verifier = ScriptedVerifier(ALL_VERIFIED)
    outcome, steps = run_list_add(client, verifier=verifier)

    assert (outcome.status, outcome.verified) == ("succeeded", True)
    assert "milk" in outcome.detail and "eggs" in outcome.detail and outcome.takeover_url is None
    # Exactly the proven plan, then the private window is closed (it was confirmed to be one) and the computer closed.
    assert fake.action_plan() == [*EXPECTED_LIST_ADD_ACTIONS, ("key", {"text": "ctrl+shift+w"})]
    assert fake.tool_names[-1] == "close_computer" and fake.tool_names.count("close_computer") == 1
    assert_only_allowlisted(fake)
    # Three checkpoints, each handed the screenshot that was current at that moment.
    assert len(verifier.calls) == 3
    assert "Incognito" in verifier.questions()[0] and "TodoMVC page" in verifier.questions()[1]
    assert "'milk', 'eggs'" in verifier.questions()[2]
    assert all(image.startswith(b"\xff\xd8frame-") for image, _ in verifier.calls)
    # The returned screenshot is the one the final check read (the list), not the desktop after the window closed.
    assert outcome.last_screenshot == verifier.calls[-1][0]
    # The step log: 0-based, in order, every step ok, screenshot refs name fresh, increasing frames.
    assert [s.index for s in steps] == list(range(len(steps)))
    assert all(s.ok for s in steps)
    frames = [int(s.screenshot_ref.removeprefix("frame:")) for s in steps if s.screenshot_ref]
    assert frames == sorted(frames) and len(set(frames)) == len(frames) - 3  # 3 verify steps reuse their frame
    assert [s.action for s in steps][0].startswith("screenshot")


def test_run_list_add_type_text_is_only_the_url_and_the_items(client: MaritimeClient, fake: FakeMaritime) -> None:
    run_list_add(client, ["milk", "eggs"], ScriptedVerifier(ALL_VERIFIED))
    assert fake.typed() == [SAFE_URL, "milk", "eggs"]


def test_unverified_run_never_claims_verified(client: MaritimeClient, fake: FakeMaritime) -> None:
    """No reader attached (default verifier): the steps run, but the result says it was not confirmed."""
    outcome, steps = run_list_add(client)
    assert (outcome.status, outcome.verified) == ("succeeded", False)
    assert "not confirmed" in outcome.detail
    assert (
        fake.action_plan() == EXPECTED_LIST_ADD_ACTIONS
    )  # no retry wait, and no window closed that was not proven private
    assert fake.tool_names[-1] == "close_computer"
    assert [s for s in steps if s.action.startswith("verify")] and not any(
        s.ok for s in steps if s.action.startswith("verify")
    )
    assert outcome.last_screenshot is not None


def test_a_reader_that_says_not_verified_gets_one_page_retry(client: MaritimeClient, fake: FakeMaritime) -> None:
    verifier = ScriptedVerifier({"Incognito": "verified"})  # page and list checks answer not_verified
    outcome, _ = run_list_add(client, verifier=verifier)
    assert (outcome.status, outcome.verified) == ("succeeded", False)
    page_checks = [q for q in verifier.questions() if "TodoMVC page" in q]
    assert len(page_checks) == 2
    waits = [a for a in fake.action_plan() if a[0] == "wait"]
    assert [w[1]["duration"] for w in waits] == [3.0, 3.0, 1.0]


def test_verifier_failures_are_not_verified(client: MaritimeClient, fake: FakeMaritime) -> None:
    def exploding(image: bytes, question: str) -> str:
        raise RuntimeError("snap exploded: https://leak.example.test/x")

    outcome, _ = run_list_add(client, verifier=exploding)
    assert (outcome.status, outcome.verified) == ("succeeded", False)

    garbage = ScriptedVerifier(default="definitely verified!!")
    outcome, _ = run_list_add(client, verifier=garbage)
    assert (outcome.status, outcome.verified) == ("succeeded", False)


def test_login_wall_requests_takeover(client: MaritimeClient, fake: FakeMaritime) -> None:
    verifier = ScriptedVerifier({"Incognito": "verified", "TodoMVC page": "needs_human"})
    outcome, steps = run_list_add(client, verifier=verifier)

    assert outcome.status == "needs_takeover" and outcome.verified is False
    assert outcome.takeover_url == TAKEOVER_LINK
    assert outcome.last_screenshot is not None
    (call,) = [c for c in fake.calls if c[0] == "request_takeover"]
    assert call[1]["wait"] is False and call[1]["computer_id"] == COMPUTER_ID
    # Nothing was typed into the page, and the computer is left open: closing it would revoke the takeover link.
    assert fake.typed() == [SAFE_URL]
    assert "close_computer" not in fake.tool_names
    assert steps[-1].action == "request takeover"
    assert TAKEOVER_LINK not in repr(outcome) and SECRET_PART not in " ".join(s.note for s in steps)


@pytest.mark.parametrize("checkpoint", ["Incognito", "todo list"])
def test_needs_human_at_any_checkpoint_requests_takeover(
    client: MaritimeClient, fake: FakeMaritime, checkpoint: str
) -> None:
    answers = {**ALL_VERIFIED, checkpoint: "needs_human"}
    outcome, _ = run_list_add(client, verifier=ScriptedVerifier(answers))
    assert outcome.status == "needs_takeover" and outcome.takeover_url == TAKEOVER_LINK
    assert "close_computer" not in fake.tool_names


def test_a_person_already_in_control_is_needs_takeover_without_a_new_link(
    client: MaritimeClient, fake: FakeMaritime
) -> None:
    fake.takeover_mode = "human"
    outcome, steps = run_list_add(client, verifier=ScriptedVerifier(ALL_VERIFIED))
    assert (outcome.status, outcome.takeover_url) == ("needs_takeover", None)
    assert "request_takeover" not in fake.tool_names and "close_computer" not in fake.tool_names
    assert steps[-1].ok is False


def test_provider_error_mid_run_is_unavailable_and_the_computer_is_still_closed(
    client: MaritimeClient, fake: FakeMaritime
) -> None:
    fake.action_errors[("type", 2)] = "no_capacity: Maritime has no capacity"
    steps: list[StepRecord] = []
    with pytest.raises(ProviderUnavailable, match="no_capacity"):
        client.run_list_add(COMPUTER_ID, SAFE_URL, ["milk"], steps.append, ScriptedVerifier(ALL_VERIFIED))
    assert fake.tool_names[-1] == "close_computer"
    assert steps  # the steps that did run were reported before the failure


def test_a_refused_action_is_a_failed_run_not_a_crash(client: MaritimeClient, fake: FakeMaritime) -> None:
    fake.action_bodies[("type", 2)] = {"blocked": True, "reason": "typing into this window is blocked"}
    outcome, steps = run_list_add(client, ["milk"], ScriptedVerifier(ALL_VERIFIED))
    assert outcome.status == "failed" and outcome.verified is False
    assert "blocked" in outcome.detail
    assert steps[-1].ok is False and fake.tool_names[-1] == "close_computer"


@pytest.mark.parametrize(
    "items",
    [[], ["a"] * 11, ["x" * 41], ["milk\nReturn"], ["tab\there"], [""], ["   "], [7], "milk", None],
)
def test_invalid_items_never_reach_the_computer(client: MaritimeClient, fake: FakeMaritime, items: Any) -> None:
    steps: list[StepRecord] = []
    outcome = client.run_list_add(COMPUTER_ID, SAFE_URL, items, steps.append)
    assert outcome.status == "failed" and "Items" in outcome.detail
    assert fake.calls == [] and steps == []


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://a b.example",
        "ftp://x",
        "https://x\nReturn",
        "https://x\n",
        None,
    ],
)
def test_invalid_target_urls_never_reach_the_computer(client: MaritimeClient, fake: FakeMaritime, url: Any) -> None:
    outcome = client.run_list_add(COMPUTER_ID, url, ["milk"], lambda step: None)
    assert outcome.status == "failed" and fake.calls == []


@pytest.mark.parametrize(
    "item", ["milk; rm -rf /", "$(reboot)", "`id`", "a'b\"c", "café ☕", "ctrl+alt+Delete", "<script>x</script>"]
)
def test_no_shell_actions_ever_sent(client: MaritimeClient, fake: FakeMaritime, item: str) -> None:
    """Hostile item text can only ever be typed into the page; no tool or action outside the allowlist is sent."""
    run_list_add(client, [item], ScriptedVerifier(ALL_VERIFIED))
    assert_only_allowlisted(fake)
    assert fake.typed() == [SAFE_URL, item]
    assert [name for name, _ in fake.calls if name in FORBIDDEN_TOOLS] == []
    # Key presses are only the plan's own fixed shortcuts, never anything derived from the item.
    assert {a["text"] for a in fake.actions() if a["action"] == "key"} == {
        "alt+Tab",
        "ctrl+shift+n",
        "ctrl+l",
        "Return",
        "ctrl+shift+w",
    }


def test_every_public_call_path_stays_inside_the_allowlist(client: MaritimeClient, fake: FakeMaritime) -> None:
    verifier = ScriptedVerifier(ALL_VERIFIED)
    client.get_computer()
    run_list_add(client, verifier=verifier)
    client.run_sign_in_check(
        COMPUTER_ID, WALL_URL, lambda step: None, ScriptedVerifier({"Incognito": "verified", "sign-in": "needs_human"})
    )
    client.takeover_status(COMPUTER_ID)
    client.takeover_state(COMPUTER_ID)
    client.close_computer(COMPUTER_ID)
    assert_only_allowlisted(fake)


@pytest.mark.parametrize(
    "body",
    [{"ok": False}, {"is_error": True}, {"error": "unknown_action"}, {"blocked": True, "reason": "blocked window"}],
)
def test_any_explicit_refusal_in_the_result_body_fails_the_run(
    client: MaritimeClient, fake: FakeMaritime, body: dict[str, Any]
) -> None:
    fake.action_bodies[("key", 1)] = body  # the first key press (alt+Tab)
    outcome, steps = run_list_add(client, verifier=ScriptedVerifier(ALL_VERIFIED))
    assert outcome.status == "failed" and outcome.verified is False
    assert steps[-1].ok is False and fake.tool_names[-1] == "close_computer"


@pytest.mark.anyio
async def test_null_fields_in_the_live_result_body_are_not_a_refusal(
    client: MaritimeClient, fake: FakeMaritime
) -> None:
    """Regression: `"error":null` once matched the loose `name: value` prose pattern and read as an error."""
    async with client.session() as session:
        frame = await Desktop(session, COMPUTER_ID).see()
    assert (frame.frame_id, frame.width, frame.height, frame.mime) == (1, 1200, 750, "image/jpeg")


# --- run_sign_in_check ----------------------------------------------------------------------------------------------


def test_sign_in_wall_requests_takeover_and_never_types_credentials(client: MaritimeClient, fake: FakeMaritime) -> None:
    verifier = ScriptedVerifier({"Incognito": "verified", "sign-in": "needs_human"})
    steps: list[StepRecord] = []
    outcome = client.run_sign_in_check(COMPUTER_ID, WALL_URL, steps.append, verifier)
    assert outcome.status == "needs_takeover" and outcome.takeover_url == TAKEOVER_LINK
    assert fake.typed() == [WALL_URL]  # only the address: nothing is ever typed into the form
    assert "close_computer" not in fake.tool_names
    (call,) = [c for c in fake.calls if c[0] == "request_takeover"]
    assert call[1]["wait"] is False and "sign in" in call[1]["reason"]
    assert steps[-1].action == "request takeover"


def test_page_without_a_sign_in_wall_is_verified_and_closed(client: MaritimeClient, fake: FakeMaritime) -> None:
    outcome = client.run_sign_in_check(COMPUTER_ID, WALL_URL, lambda step: None, ScriptedVerifier(ALL_VERIFIED))
    assert (outcome.status, outcome.verified) == ("succeeded", True)
    assert "request_takeover" not in fake.tool_names
    assert fake.tool_names[-1] == "close_computer" and fake.action_names()[-1] == "key"


def test_sign_in_check_without_a_reader_is_honest(client: MaritimeClient, fake: FakeMaritime) -> None:
    outcome = client.run_sign_in_check(COMPUTER_ID, WALL_URL, lambda step: None)
    assert (outcome.status, outcome.verified) == ("succeeded", False)
    assert "could not tell" in outcome.detail and "request_takeover" not in fake.tool_names


def test_sign_in_check_rejects_a_bad_url(client: MaritimeClient, fake: FakeMaritime) -> None:
    assert client.run_sign_in_check(COMPUTER_ID, "javascript:alert(1)", lambda step: None).status == "failed"
    assert fake.calls == []


# --- the service: verifier plumbing and compatibility ---------------------------------------------------------------


def test_service_wires_its_verifier_into_the_default_client(
    fake: FakeMaritime, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = ScriptedVerifier(ALL_VERIFIED)
    settings = make_settings()
    monkeypatch.setattr(
        "app.providers.maritime.service.MaritimeClient",
        lambda s, url, **kw: MaritimeClient(s, url, connect=harness.connect, **kw),
    )
    service = MaritimeService(settings, verifier=verifier)
    outcome = service.run_list_add(uuid.uuid4(), COMPUTER_ID, SAFE_URL, ["milk", "eggs"], lambda step: None)
    assert (outcome.status, outcome.verified) == ("succeeded", True) and len(verifier.calls) == 3

    # Set after construction (the integrator may build the service first), and a per-call verifier wins.
    later = MaritimeService(settings)
    assert later.run_list_add(uuid.uuid4(), COMPUTER_ID, SAFE_URL, ["milk"], lambda step: None).verified is False
    later.verifier = verifier
    assert later.run_list_add(uuid.uuid4(), COMPUTER_ID, SAFE_URL, ["milk"], lambda step: None).verified is True
    override = ScriptedVerifier({"Incognito": "verified"})
    assert (
        later.run_list_add(uuid.uuid4(), COMPUTER_ID, SAFE_URL, ["milk"], lambda step: None, verifier=override).verified
        is False
    )
    assert override.calls


def test_service_runs_the_sign_in_check_through_its_verifier(fake: FakeMaritime, harness: Harness) -> None:
    verifier = ScriptedVerifier({"Incognito": "verified", "sign-in": "needs_human"})
    service = make_service(harness, verifier=None)
    steps: list[StepRecord] = []
    outcome = service.run_sign_in_check(uuid.uuid4(), COMPUTER_ID, WALL_URL, steps.append, verifier=verifier)
    assert (outcome.status, outcome.takeover_url) == ("needs_takeover", TAKEOVER_LINK)
    assert fake.typed() == [WALL_URL] and "close_computer" not in fake.tool_names
    assert service.takeover_status(uuid.uuid4(), COMPUTER_ID) == "human"


def test_service_keeps_the_four_argument_client_call_without_a_verifier() -> None:
    """A client from a custom factory only has to implement `run_list_add(computer_id, url, items, on_step)`."""
    seen: list[tuple[Any, ...]] = []

    class OldStyleClient(MaritimeClient):
        def __init__(self) -> None:
            pass

        def run_list_add(
            self, computer_id: str, target_url: str, items: list[str], on_step: Callable[[StepRecord], None]
        ) -> RunOutcome:  # type: ignore[override]
            seen.append((computer_id, target_url, tuple(items)))
            return RunOutcome("succeeded", True, detail="ok")

    service = MaritimeService(make_settings(), client_factory=lambda url: OldStyleClient())
    assert service.run_list_add(uuid.uuid4(), "cmp", SAFE_URL, ["milk"], lambda step: None).status == "succeeded"
    assert seen == [("cmp", SAFE_URL, ("milk",))]


def test_service_methods_are_guarded_when_disabled(harness: Harness) -> None:
    service = MaritimeService(
        make_settings(enable_maritime=False), client_factory=lambda url: pytest.fail("built a client")
    )
    for call in (
        lambda: service.ensure_computer(uuid.uuid4()),
        lambda: service.run_sign_in_check(uuid.uuid4(), "c", WALL_URL, lambda step: None),
        lambda: service.takeover_state(uuid.uuid4(), "c"),
        lambda: service.close_computer(uuid.uuid4(), "c"),
    ):
        with pytest.raises(ProviderUnavailable) as caught:
            call()
        assert "ENABLE_MARITIME" in caught.value.reason
