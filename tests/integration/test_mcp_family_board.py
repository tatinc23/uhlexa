"""The Family Board as an MCP App: the tool carries `_meta.ui.resourceUri`, the resource is served with the MCP Apps
MIME type, and the structured result is the same payload `GET /api/board` builds."""

import json
import re
from typing import Any

import pytest
from mcp import Client
from sqlalchemy.orm import Session, sessionmaker

from app.mcp.resources import BOARD_TEMPLATE, BOARD_URI
from app.mcp.server import build_mcp_server
from app.services import board_service
from app.services.board_service import build_family_board
from tests.helpers_services import DEMO_SENTENCE, ask

pytestmark = pytest.mark.anyio


@pytest.fixture
def server(session_factory: sessionmaker[Session], seeded: object):
    return build_mcp_server(session_factory)


async def test_family_board_tool_has_ui_meta(server: object) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["get_family_board"].meta["ui"]["resourceUri"] == BOARD_URI == "ui://uhlexa/family-board.html"
    assert all("ui" not in (t.meta or {}) for name, t in tools.items() if name != "get_family_board")


async def test_board_resource_mime(server: object) -> None:
    async with Client(server) as client:
        listed = (await client.list_resources()).resources
        read = await client.read_resource(BOARD_URI)
    assert [str(r.uri) for r in listed] == [BOARD_URI]
    assert listed[0].mime_type == "text/html;profile=mcp-app" and listed[0].title == "Family Board"
    (contents,) = read.contents
    assert contents.mime_type == "text/html;profile=mcp-app"
    assert contents.text == BOARD_TEMPLATE.read_text(encoding="utf-8")


async def test_family_board_payload_matches_board_service(
    server: object, session_factory: sessionmaker[Session], seeded: dict
) -> None:
    with session_factory() as db:
        ask(db, seeded["alex"], DEMO_SENTENCE)  # a pending approval, a resumed thread and activity to show
    async with Client(server) as client:
        result = await client.call_tool("get_family_board", {})
    assert not result.is_error
    payload = json.loads(json.dumps(result.structured_content))
    with session_factory() as db:
        from app.auth.demo_auth import resolve_principal

        expected = json.loads(json.dumps(build_family_board(db, resolve_principal(db, "alex"))))
    for body in (payload, expected):
        body.pop("generated_at")
    assert payload == expected
    assert [t["id"] for t in payload["tasks"]] == [t["id"] for t in expected["tasks"]] and payload["tasks"]
    assert payload["approvals"]["pending"] and set(payload["providers"]) == set(expected["providers"])
    summary = result.content[0].text
    assert "The Parker Family" in summary and "1 pending approval" in summary


async def test_the_board_text_summary_is_short(server: object) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_family_board", {})
    assert len(result.content) == 1 and len(result.content[0].text) < 300


def test_board_payload_builder_is_the_one_rest_uses() -> None:
    assert board_service.build_family_board is build_family_board


# --- the template -----------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def html() -> str:
    return BOARD_TEMPLATE.read_text(encoding="utf-8")


def test_template_is_self_contained(html: str) -> None:
    assert not re.search(r"https?://", html), "the board makes no external requests and links nothing external"
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", html, re.I)
    assert "@import" not in html and "url(" not in html


def test_template_escapes_by_construction(html: str) -> None:
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in html, banned
    assert "textContent" in html


def test_template_speaks_the_mcp_apps_handshake(html: str) -> None:
    for needle in (
        "ui/initialize",
        "appInfo",
        "appCapabilities",
        "2026-01-26",
        "ui/notifications/initialized",
        "ui/notifications/tool-result",
        "structuredContent",
        "ui/notifications/size-changed",
        "ResizeObserver",
    ):
        assert needle in html, needle


def test_template_standalone_fallback(html: str) -> None:
    for needle in ("/api/board", "same-origin", "X-Uhlexa-User", "URLSearchParams", "3000", "800"):
        assert needle in html
    assert "prefers-color-scheme: dark" in html and 'name="viewport"' in html


def test_template_shows_what_the_board_promises(html: str) -> None:
    for needle in (
        "approvals.pending",
        "a.reason",
        "board.tasks",
        "browser_tasks",
        "takeover_url",
        "verified",
        "board.activity",
        "board.providers",
        "board.members",
        "board.conversations",
    ):
        assert needle in html, needle


def test_board_page_route_serves_the_template(client: Any) -> None:
    response = client.get("/board")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assert response.text == BOARD_TEMPLATE.read_text(encoding="utf-8")
