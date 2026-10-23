"""MCP caller identity (addendum 21.4): per-agent bearer tokens, human-only tools and no fallback, over HTTP."""

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.demo_auth import Principal
from app.db.session import SessionLocal
from app.mcp.auth import hash_token
from app.mcp.server import build_mcp_server, create_mcp_http_app
from app.models import AgentStatus, Approval, ApprovalStatus, BindingStatus, BrowserTask, BrowserTaskStatus
from app.settings import get_settings
from tests.helpers_mcp import bearer, call_tool, mint_token, result_text, rpc
from tests.helpers_services import DEMO_SENTENCE, approved_browser_task, ask, enable_maritime

Seeded = dict[str, Principal]
LINK = "https://control.example/SECRET-TAKEOVER-7c1d"


@pytest.fixture
def keiki_on(monkeypatch: pytest.MonkeyPatch, settings_env: object) -> None:
    monkeypatch.setenv("ENABLE_KEIKI", "true")
    get_settings.cache_clear()


@pytest.fixture
def server(session_factory: sessionmaker[Session], seeded: Seeded, keiki_on: None):
    return build_mcp_server(session_factory)


@pytest.fixture
def http(server) -> Iterator[TestClient]:
    """A person at the simulator: every request carries `X-Uhlexa-User: alex` unless a test overrides it."""
    with TestClient(create_mcp_http_app(server), headers={"X-Uhlexa-User": "alex"}) as client:
        yield client


@pytest.fixture
def bare(server) -> Iterator[TestClient]:
    """No credentials of any kind."""
    with TestClient(create_mcp_http_app(server)) as client:
        yield client


def profile(http: TestClient, headers: dict[str, str] | None = None) -> dict:
    return call_tool(http, "get_agent_profile", headers=headers)["structuredContent"]


# --- who is calling -----------------------------------------------------------------------------------------------


def test_a_valid_token_resolves_to_its_agents_owner(http: TestClient, db: Session, seeded: Seeded) -> None:
    token = mint_token(db, seeded["katie"])
    me = profile(http, bearer(token))
    assert me["acting_for"]["display_name"] == "Katie Parker" and me["agent"]["name"] == "Jordy"
    assert me["caller"] == "agent_token" and me["can_approve"] is False


def test_a_bearer_is_never_combined_with_the_header_or_cookie(http: TestClient, db: Session, seeded: Seeded) -> None:
    token = mint_token(db, seeded["katie"])
    headers = {**bearer(token), "X-Uhlexa-User": "alex", "Cookie": "uhlexa_session=whatever"}
    assert profile(http, headers)["acting_for"]["display_name"] == "Katie Parker"


def test_without_a_bearer_an_explicit_demo_user_is_a_human_session(http: TestClient) -> None:
    assert profile(http)["acting_for"]["display_name"] == "Alex Parker"  # the fixture's X-Uhlexa-User
    me = profile(http, {"X-Uhlexa-User": "katie"})
    assert me["acting_for"]["display_name"] == "Katie Parker" and me["caller"] == "human_session"
    assert me["can_approve"] is True


def test_a_token_in_the_query_string_or_arguments_is_not_accepted(
    http: TestClient, db: Session, seeded: Seeded
) -> None:
    token = mint_token(db, seeded["katie"])
    response = rpc(
        http, "tools/call", {"name": "get_agent_profile", "arguments": {}}, headers={"X-Path": f"/mcp?token={token}"}
    )
    assert response.json()["result"]["structuredContent"]["caller"] == "human_session"
    bad = rpc(http, "tools/call", {"name": "get_agent_profile", "arguments": {"token": token}})
    assert bad.status_code == 200 and profile(http)["caller"] == "human_session"  # extra arguments never name a caller


# --- 401, no fallback ---------------------------------------------------------------------------------------------


def assert_401(response) -> None:
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "unauthorized" and response.headers["www-authenticate"] == "Bearer"


def test_unknown_or_child_token_401_without_fallback(http: TestClient, db: Session, seeded: Seeded) -> None:
    alex_header = {"X-Uhlexa-User": "alex"}  # a fallback to this would "work": it must not
    assert_401(rpc(http, "tools/list", headers={**bearer("not-a-real-token"), **alex_header}))
    assert_401(rpc(http, "initialize", headers={**bearer("not-a-real-token"), **alex_header}))
    child = mint_token(db, seeded["max"])
    assert_401(
        rpc(
            http, "tools/call", {"name": "get_agent_profile", "arguments": {}}, headers={**bearer(child), **alex_header}
        )
    )
    assert_401(rpc(http, "tools/list", headers={"Authorization": "Basic Zm9vOmJhcg==", **alex_header}))
    assert_401(rpc(http, "tools/list", headers={"Authorization": "Bearer", **alex_header}))
    assert_401(rpc(http, "tools/list", headers={"Authorization": "", **alex_header}))


def test_revoked_and_disabled_tokens_are_401(http: TestClient, db: Session, seeded: Seeded) -> None:
    revoked = mint_token(db, seeded["katie"], status=BindingStatus.INACTIVE)
    assert_401(rpc(http, "tools/list", headers=bearer(revoked)))
    paused = mint_token(db, seeded["alex"])
    assert rpc(http, "tools/list", headers=bearer(paused)).status_code == 200
    seeded["alex"].agent.status = AgentStatus.PAUSED
    db.commit()
    assert_401(rpc(http, "tools/list", headers=bearer(paused)))


def test_a_token_whose_binding_names_a_different_owner_is_401(http: TestClient, db: Session, seeded: Seeded) -> None:
    token = mint_token(db, seeded["katie"])
    from sqlalchemy import select

    from app.models import ProviderBinding

    binding = db.scalars(select(ProviderBinding).where(ProviderBinding.external_id == hash_token(token))).one()
    binding.user_id = seeded["alex"].user.id  # tampered: user and agent disagree
    db.commit()
    assert_401(rpc(http, "tools/list", headers=bearer(token)))


def test_tokens_are_off_unless_keiki_is_enabled(
    session_factory: sessionmaker[Session], seeded: Seeded, db: Session, settings_env: object
) -> None:
    token = mint_token(db, seeded["katie"])
    with TestClient(create_mcp_http_app(build_mcp_server(session_factory))) as client:
        assert_401(rpc(client, "tools/list", headers={**bearer(token), "X-Uhlexa-User": "alex"}))


def test_demo_auth_off_and_no_token_is_401(
    http: TestClient, db: Session, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_DEMO_AUTH", "false")
    get_settings.cache_clear()
    assert_401(rpc(http, "tools/list", headers={"X-Uhlexa-User": "alex"}))
    token = mint_token(db, seeded["katie"])
    assert rpc(http, "tools/list", headers=bearer(token)).status_code == 200  # tokens do not need demo auth


def test_mcp_without_credentials_is_401_not_default_user(bare: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_settings().demo_default_user == "alex"  # a default user exists, and must not be used
    for method, params in (
        ("tools/list", None),
        (
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}},
        ),
        ("tools/call", {"name": "get_agent_profile", "arguments": {}}),
        ("resources/read", {"uri": "ui://uhlexa/family-board.html"}),
    ):
        assert_401(rpc(bare, method, params))
    assert_401(rpc(bare, "tools/list", headers={"X-Uhlexa-User": "  "}))
    assert_401(rpc(bare, "tools/list", headers={"Cookie": "uhlexa_session=forged.123.abc"}))
    assert rpc(bare, "tools/list", headers={"X-Uhlexa-User": "alex"}).status_code == 200  # explicit credentials work


def test_the_session_cookie_authenticates_without_the_header(bare: TestClient) -> None:
    from app.auth.demo_auth import sign_session

    cookie = {"Cookie": f"uhlexa_session={sign_session('katie')}"}
    assert profile(bare, cookie)["acting_for"]["display_name"] == "Katie Parker"


def test_mcp_rejects_foreign_host_header(http: TestClient, db: Session, seeded: Seeded) -> None:
    token = mint_token(db, seeded["alex"])
    for creds in ({"X-Uhlexa-User": "alex"}, bearer(token)):  # valid credentials do not help a rebinding attacker
        response = rpc(http, "tools/list", headers={**creds, "Host": "evil.example"})
        assert response.status_code == 421, response.text
        assert rpc(http, "tools/list", headers={**creds, "Host": "evil.example:8000"}).status_code == 421
    bad_origin = rpc(http, "tools/list", headers={"Origin": "https://evil.example"})
    assert bad_origin.status_code == 403
    for host in ("localhost:8000", "127.0.0.1:9", "[::1]:8000", "uhlexa.inkboxwire.com", "testserver"):
        assert rpc(http, "tools/list", headers={"Host": host}).status_code == 200, host
    ok_origin = rpc(http, "tools/list", headers={"Origin": "https://uhlexa.inkboxwire.com"})
    assert ok_origin.status_code == 200
    alias = rpc(http, "tools/list", headers={"X-Path": "/mcp/katie", "Host": "evil.example"})
    assert alias.status_code == 421


def test_mcp_allowed_hosts_setting_extends_the_list(server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "demo.example.test, other.example.test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://public.example.test")
    get_settings.cache_clear()
    with TestClient(create_mcp_http_app(server), headers={"X-Uhlexa-User": "alex"}) as client:
        for host in ("demo.example.test", "other.example.test:443", "public.example.test"):
            assert rpc(client, "tools/list", headers={"Host": host}).status_code == 200, host
        assert rpc(client, "tools/list", headers={"Host": "evil.example"}).status_code == 421


def test_the_demo_user_must_exist(http: TestClient) -> None:
    assert_401(rpc(http, "tools/list", headers={"X-Uhlexa-User": "nobody"}))


def test_token_hash_is_what_is_stored(http: TestClient, db: Session, seeded: Seeded) -> None:
    from sqlalchemy import select

    from app.models import ProviderBinding

    token = mint_token(db, seeded["alex"])
    rows = db.scalars(select(ProviderBinding)).all()
    assert [r.external_id for r in rows] == [hash_token(token)] and token not in repr([r.config for r in rows])


# --- human-only tools ---------------------------------------------------------------------------------------------


def pending_approval(db: Session, principal: Principal) -> str:
    return ask(db, principal, DEMO_SENTENCE)["approval"]["id"]


def test_agent_token_cannot_approve_or_resume(http: TestClient, db: Session, seeded: Seeded) -> None:
    token = mint_token(db, seeded["alex"])
    approval_id = pending_approval(db, seeded["alex"])
    result = call_tool(
        http, "approve_action", {"approval_id": approval_id, "decision": "approve"}, headers=bearer(token)
    )
    assert result["isError"] is True and "human_only" in result_text(result)
    result = call_tool(http, "resume_after_takeover", {"browser_task_id": approval_id}, headers=bearer(token))
    assert result["isError"] is True and "human_only" in result_text(result)
    db.expire_all()
    assert db.get(Approval, __import__("uuid").UUID(approval_id)).status == ApprovalStatus.PENDING
    # the same call from a person goes through
    human = call_tool(http, "approve_action", {"approval_id": approval_id, "decision": "approve"})
    assert human["isError"] is False and human["structuredContent"]["status"] == "approved"


def test_agent_token_can_create_read_and_cancel(http: TestClient, db: Session, seeded: Seeded) -> None:
    headers = bearer(mint_token(db, seeded["alex"]))
    created = call_tool(
        http, "create_family_task", {"title": "Buy a bike", "kind": "purchase", "amount_usd": 500}, headers=headers
    )
    assert created["isError"] is False
    assert created["structuredContent"]["approval"]["can_decide"] is False  # an agent is never offered the decision
    assert created["structuredContent"]["task"]["created_by_user_id"] == str(seeded["alex"].user.id)
    task_id = created["structuredContent"]["task"]["id"]
    status = call_tool(http, "get_task_status", {"task_id": task_id}, headers=headers)["structuredContent"]
    assert status["task"]["status"] == "pending_approval" and status["approvals"][0]["can_decide"] is False
    cancelled = call_tool(http, "cancel_task", {"task_id": task_id}, headers=headers)
    assert cancelled["structuredContent"]["status"] == "cancelled"


def test_agent_token_is_scoped_to_its_own_household_and_owner(http: TestClient, db: Session, seeded: Seeded) -> None:
    from tests.helpers_services import second_household_principal

    outsider = second_household_principal(db)
    theirs = ask(db, outsider, "Remind me to water the plants")["task"]["id"]
    result = call_tool(http, "get_task_status", {"task_id": theirs}, headers=bearer(mint_token(db, seeded["alex"])))
    assert result["isError"] is True and "not_found" in result_text(result)


# --- the takeover link --------------------------------------------------------------------------------------------


def test_takeover_url_hidden_from_agent_tokens(
    http: TestClient, db: Session, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_maritime(monkeypatch)
    _, bt = approved_browser_task(db, seeded["alex"])
    bt.status, bt.requires_takeover, bt.takeover_url = BrowserTaskStatus.NEEDS_TAKEOVER, True, LINK
    db.commit()
    agent = bearer(mint_token(db, seeded["alex"]))
    person = {"X-Uhlexa-User": "alex"}

    seen = call_tool(http, "get_browser_task_status", {"browser_task_id": str(bt.id)}, headers=person)
    assert seen["structuredContent"]["takeover_url"] == LINK  # the owning person gets the link

    hidden = call_tool(http, "get_browser_task_status", {"browser_task_id": str(bt.id)}, headers=agent)
    body = hidden["structuredContent"]
    assert (
        body["takeover_url"] is None and body["requires_takeover"] is True and "Family Board" in body["takeover_hint"]
    )
    assert LINK not in str(hidden)

    board = call_tool(http, "get_family_board", headers=agent)
    assert LINK not in str(board) and board["structuredContent"]["browser_tasks"][0]["takeover_url"] is None
    status = call_tool(http, "get_task_status", {"task_id": str(bt.task_id)}, headers=agent)
    assert LINK not in str(status)
    activity = call_tool(http, "get_activity", {"limit": 50}, headers=agent)
    assert LINK not in str(activity)
    assert db.get(BrowserTask, bt.id).takeover_url == LINK


# --- transport ----------------------------------------------------------------------------------------------------


def test_http_mcp_initialize_and_tools_list(http: TestClient) -> None:
    init = rpc(
        http,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
    )
    assert init.status_code == 200 and init.headers["content-type"].startswith("application/json")
    assert init.json()["result"]["serverInfo"]["name"] == "uhlexa"
    listed = rpc(http, "tools/list")
    assert listed.status_code == 200
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert len(names) == 12 and {"get_family_board", "approve_action", "continue_conversation"} <= names
    board = next(t for t in listed.json()["result"]["tools"] if t["name"] == "get_family_board")
    assert board["_meta"]["ui"]["resourceUri"] == "ui://uhlexa/family-board.html"


def test_exactly_mcp_and_the_alias_are_served_without_redirects(http: TestClient, db: Session, seeded: Seeded) -> None:
    token = mint_token(db, seeded["katie"])
    via_alias = call_tool(http, "get_agent_profile", headers={**bearer(token), "X-Path": "/mcp/katie"})
    assert via_alias["structuredContent"]["acting_for"]["display_name"] == "Katie Parker"
    post = rpc(http, "tools/list")
    assert post.status_code == 200 and not post.history
    assert http.post("/mcp/", content="{}", follow_redirects=False).status_code == 404
    assert http.get("/elsewhere", follow_redirects=False).status_code == 404
    assert_401(rpc(http, "tools/list", headers={**bearer("nope"), "X-Path": "/mcp/katie"}))


def test_non_mcp_paths_are_not_gated(http: TestClient) -> None:
    assert http.get("/elsewhere", headers=bearer("nope")).status_code == 404


def test_resource_read_over_http_is_the_mcp_app_mime(http: TestClient) -> None:
    response = rpc(http, "resources/read", {"uri": "ui://uhlexa/family-board.html"})
    (content,) = response.json()["result"]["contents"]
    assert content["mimeType"] == "text/html;profile=mcp-app" and "<title>Family Board</title>" in content["text"]


def test_the_app_mounts_at_root_in_the_real_app(
    client: TestClient, engine: Engine, seeded: Seeded, as_user: Callable[[str], dict[str, str]]
) -> None:
    """main.py mounts the MCP app last: /mcp answers (on the module-level SessionLocal, pointed at the test database
    here), and the REST routes still win their own paths."""
    original = SessionLocal.kw.get("bind")
    SessionLocal.configure(bind=engine)
    try:
        response = rpc(client, "tools/list", headers=as_user("alex"))
        assert response.status_code == 200 and len(response.json()["result"]["tools"]) == 12
        called = call_tool(client, "get_agent_profile", headers=as_user("katie"))
        assert called["structuredContent"]["acting_for"]["display_name"] == "Katie Parker"
    finally:
        SessionLocal.configure(bind=original)
    assert client.get("/health").status_code == 200
