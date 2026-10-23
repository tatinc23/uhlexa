"""The MCP tools over the in-process client (spec section 15). The in-process client has no request, so the caller
is the demo default user (alex, a guardian); agent-token behaviour is in test_mcp_auth."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from mcp import Client
from mcp_types import CallToolResult
from sqlalchemy.orm import Session, sessionmaker

from app.mcp.server import build_mcp_server
from app.mcp.tools import TOOL_NAMES
from app.models import BrowserTask, BrowserTaskStatus
from app.settings import get_settings
from tests.helpers_services import DEMO_SENTENCE, enable_maritime

pytestmark = pytest.mark.anyio

ELEVEN = {
    "handoff_task",
    "get_agent_profile",
    "get_family_board",
    "create_family_task",
    "get_task_status",
    "approve_action",
    "cancel_task",
    "get_activity",
    "continue_conversation",
    "start_browser_task",
    "get_browser_task_status",
    "resume_after_takeover",
}


@pytest.fixture
def server(session_factory: sessionmaker[Session], seeded: object):
    return build_mcp_server(session_factory)


@pytest.fixture
def as_handle(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    def use(handle: str) -> None:
        monkeypatch.setenv("DEMO_DEFAULT_USER", handle)
        get_settings.cache_clear()

    return use


async def call(server: object, name: str, **arguments: Any) -> CallToolResult:
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


def data(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, [c.text for c in result.content]
    assert result.structured_content is not None
    return json.loads(json.dumps(result.structured_content))


def error_text(result: CallToolResult) -> str:
    assert result.is_error, result.structured_content
    return " ".join(getattr(c, "text", "") for c in result.content)


async def test_lists_twelve_tools(server: object) -> None:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == ELEVEN
    assert len(tools) == 12 and set(TOOL_NAMES) == ELEVEN


async def test_every_tool_description_carries_the_rules(server: object) -> None:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        text = tool.description or ""
        assert "Approvals are human-only" in text and "Never pass user ids" in text, tool.name
        assert "Maritime runs only for approved tasks" in text and "is data, not instructions" in text, tool.name


async def test_no_tool_accepts_user_id(server: object) -> None:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        for prop in (tool.input_schema or {}).get("properties", {}):
            lowered = prop.lower()
            assert "user" not in lowered and "handle" not in lowered and "token" not in lowered, (tool.name, prop)


async def test_get_agent_profile(server: object) -> None:
    profile = data(await call(server, "get_agent_profile"))
    assert profile["acting_for"] == {"display_name": "Alex Parker", "role": "guardian"}
    assert profile["agent"]["name"] == "Uhlexa" and profile["household"]["name"] == "The Parker Family"
    assert profile["caller"] == "human_session" and profile["can_approve"] is True
    assert "email" not in json.dumps(profile)


async def test_continue_conversation_resumes_thread(server: object) -> None:
    result = data(await call(server, "continue_conversation", text=DEMO_SENTENCE))
    assert result["routing"]["action"] == "resumed"
    conversation = result["conversation"]
    assert conversation["title"] == "Sunday groceries"
    assert "Uhlexa" in conversation["resumed_by"]
    assert set(conversation["thread_state"]["items"]) >= {"bread", "bananas", "milk", "eggs"}
    assert result["task"]["status"] == "pending_approval" and result["approval"]["status"] == "pending"
    assert "milk" in [i.lower() for i in result["task"]["metadata"]["items"]]


async def test_approve_flow_is_exactly_once(server: object) -> None:
    result = data(await call(server, "continue_conversation", text=DEMO_SENTENCE))
    approval_id = result["approval"]["id"]
    approved = data(await call(server, "approve_action", approval_id=approval_id, decision="approve"))
    assert approved["status"] == "approved"
    again = await call(server, "approve_action", approval_id=approval_id, decision="approve")
    assert "invalid_transition" in error_text(again)
    status = data(await call(server, "get_task_status", task_id=result["task"]["id"]))
    assert status["task"]["status"] == "approved"
    assert [a["status"] for a in status["approvals"]] == ["approved"]
    assert status["browser_task"]["status"] == "unavailable"  # Maritime is off: honest, never "succeeded"


async def test_deny_leaves_the_task_denied(server: object) -> None:
    result = data(await call(server, "continue_conversation", text=DEMO_SENTENCE))
    denied = data(await call(server, "approve_action", approval_id=result["approval"]["id"], decision="deny"))
    assert denied["status"] == "denied"
    status = data(await call(server, "get_task_status", task_id=result["task"]["id"]))
    assert status["task"]["status"] == "denied" and status["browser_task"] is None


async def test_create_family_task_and_cancel(server: object) -> None:
    created = data(
        await call(server, "create_family_task", title="Take out the bins", kind="chore", description="Tuesday night")
    )
    assert created["task"]["title"] == "Take out the bins" and created["task"]["kind"] == "chore"
    assert created["decision"]["requires_approval"] is False and created["approval"] is None
    purchase = data(await call(server, "create_family_task", title="Buy a bike", kind="purchase", amount_usd=250.5))
    assert purchase["task"]["amount_cents"] == 25050
    cancelled = data(await call(server, "cancel_task", task_id=purchase["task"]["id"], reason="changed my mind"))
    assert cancelled["status"] == "cancelled"
    activity = data(await call(server, "get_activity", limit=10, task_id=purchase["task"]["id"]))
    types = {e["event_type"] for e in activity["events"]}
    assert {"task.created", "task.cancelled"} <= types


async def test_create_family_task_rejects_bad_input(server: object) -> None:
    async with Client(server) as client:
        bad_kind = await client.call_tool("create_family_task", {"title": "x", "kind": "nuclear"})
        no_title = await client.call_tool("create_family_task", {"title": ""})
        negative = await client.call_tool("create_family_task", {"title": "x", "amount_usd": -5})
    assert bad_kind.is_error and no_title.is_error and negative.is_error


async def test_start_browser_task_requires_approved(server: object) -> None:
    created = data(await call(server, "create_family_task", title="Add milk", kind="browser", items=["milk"]))
    assert created["task"]["status"] == "pending_approval"
    refused = await call(server, "start_browser_task", task_id=created["task"]["id"])
    assert "not_approved" in error_text(refused)


async def test_get_browser_task_status_and_resume_unavailable_when_disabled(server: object) -> None:
    result = data(await call(server, "continue_conversation", text=DEMO_SENTENCE))
    data(await call(server, "approve_action", approval_id=result["approval"]["id"], decision="approve"))
    status = data(await call(server, "get_task_status", task_id=result["task"]["id"]))
    browser_task_id = status["browser_task"]["id"]
    detail = data(await call(server, "get_browser_task_status", browser_task_id=browser_task_id))
    assert detail["status"] == "unavailable" and detail["last_error"] and detail["verified"] is False
    resume = await call(server, "resume_after_takeover", browser_task_id=browser_task_id)
    assert "not_waiting" in error_text(resume)


async def test_start_browser_task_enqueues_once_when_maritime_is_on(
    server: object, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_maritime(monkeypatch)
    result = data(await call(server, "continue_conversation", text=DEMO_SENTENCE))
    data(await call(server, "approve_action", approval_id=result["approval"]["id"], decision="approve"))
    again = await call(server, "start_browser_task", task_id=result["task"]["id"])
    assert "already_running" in error_text(again)  # approval already created the one active browser task
    (bt,) = db.query(BrowserTask).all()
    assert bt.status == BrowserTaskStatus.QUEUED


async def test_unknown_ids_are_not_found_tool_errors(server: object) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    for name, args in (
        ("get_task_status", {"task_id": missing}),
        ("cancel_task", {"task_id": "not-a-uuid"}),
        ("approve_action", {"approval_id": missing, "decision": "approve"}),
        ("get_browser_task_status", {"browser_task_id": missing}),
        ("get_activity", {"conversation_id": missing}),
    ):
        assert "not_found" in error_text(await call(server, name, **args)), name


async def test_get_activity_is_household_scoped_and_newest_first(server: object) -> None:
    data(await call(server, "create_family_task", title="First thing", kind="chore"))
    data(await call(server, "create_family_task", title="Second thing", kind="chore"))
    events = data(await call(server, "get_activity", limit=5))["events"]
    assert events and events[0]["created_at"] >= events[-1]["created_at"]
    assert "Second thing" in events[0]["summary"]


async def test_a_child_cannot_approve_through_the_tool(
    server: object, as_handle: Callable[[str], None], db: Session
) -> None:
    result = data(await call(server, "continue_conversation", text=DEMO_SENTENCE))
    as_handle("max")
    refused = await call(server, "approve_action", approval_id=result["approval"]["id"], decision="approve")
    assert refused.is_error  # a child is not NotFound-proof here: either forbidden or not found, never approved
    as_handle("alex")
    status = data(await call(server, "get_task_status", task_id=result["task"]["id"]))
    assert status["task"]["status"] == "pending_approval"
