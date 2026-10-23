"""REST guard rails: children cannot approve, foreign ids are 404 everywhere, message text is bounded and inert."""

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from tests.helpers_services import DEMO_SENTENCE, approved_browser_task, ask, second_household_principal

Seeded = dict[str, Principal]
Headers = Callable[[str], dict[str, str]]


def test_child_approve_403(client: TestClient, seeded: Seeded, as_user: Headers) -> None:
    # Max starts his own thread, so he can see the approval it raises, but a child can never decide it.
    sent = client.post("/api/conversations/messages", json={"text": DEMO_SENTENCE}, headers=as_user("max"))
    assert sent.status_code == 200
    approval = sent.json()["approval"]
    assert approval["status"] == "pending" and approval["can_decide"] is False
    for verb in ("approve", "deny"):
        denied = client.post(f"/api/approvals/{approval['id']}/{verb}", headers=as_user("max"))
        assert denied.status_code == 403
        assert denied.json()["code"] == "forbidden"
    assert client.get("/api/approvals", headers=as_user("max")).status_code == 200


def test_child_gets_404_for_an_adults_thread(client: TestClient, db: Session, seeded: Seeded, as_user: Headers) -> None:
    payload, browser_task = approved_browser_task(db, seeded["alex"])
    ids = {"task": payload["task"]["id"], "approval": payload["approval"]["id"], "browser": str(browser_task.id)}
    conversation_id = payload["conversation"]["id"]
    max_ = as_user("max")
    assert client.get(f"/api/tasks/{ids['task']}", headers=max_).status_code == 404
    assert client.post(f"/api/approvals/{ids['approval']}/approve", headers=max_).status_code == 404
    assert client.get(f"/api/browser-tasks/{ids['browser']}", headers=max_).status_code == 404
    assert client.get(f"/api/conversations/{conversation_id}", headers=max_).status_code == 404
    assert ids["task"] not in {t["id"] for t in client.get("/api/tasks", headers=max_).json()}


def test_foreign_ids_404(client: TestClient, db: Session, seeded: Seeded, as_user: Headers) -> None:
    zed = second_household_principal(db)
    payload, browser_task = approved_browser_task(db, zed)
    pending = ask(db, zed, "Remind me to call grandma")  # a second task that still has its own thread
    task_id, conversation_id = payload["task"]["id"], payload["conversation"]["id"]
    approval_id, browser_id = payload["approval"]["id"], str(browser_task.id)
    alex = as_user("alex")
    assert client.get("/api/me", headers=as_user("zed")).status_code == 200  # the other household's ids are real

    requests = [
        ("GET", f"/api/conversations/{conversation_id}", None),
        ("POST", "/api/conversations/messages", {"text": "hello", "conversation_id": conversation_id}),
        ("GET", f"/api/tasks/{task_id}", None),
        ("POST", f"/api/tasks/{task_id}/cancel", {}),
        ("POST", f"/api/approvals/{approval_id}/approve", None),
        ("POST", f"/api/approvals/{approval_id}/deny", None),
        ("POST", "/api/browser-tasks", {"task_id": task_id}),
        ("GET", f"/api/browser-tasks/{browser_id}", None),
        ("GET", f"/api/browser-tasks/{browser_id}/screenshot", None),
        ("POST", f"/api/browser-tasks/{browser_id}/resume", None),
        ("GET", f"/api/activity?task_id={task_id}", None),
        ("GET", f"/api/activity?conversation_id={conversation_id}", None),
        ("POST", "/api/tasks", {"title": "Sneaky", "kind": "general", "conversation_id": conversation_id}),
        # junk and unknown ids are the same 404 as a foreign one
        ("GET", "/api/tasks/not-a-uuid", None),
        ("GET", f"/api/tasks/{uuid.uuid4()}", None),
    ]
    for method, path, body in requests:
        response = client.request(method, path, json=body, headers=alex)
        assert response.status_code == 404, (method, path, response.status_code, response.text)
        assert response.json()["code"] == "not_found", (method, path)

    # Nothing of zed's leaked into alex's views.
    assert pending["task"]["id"] not in client.get("/api/board", headers=alex).text


def test_message_text_limits_and_html_roundtrip(client: TestClient, seeded: Seeded) -> None:
    assert client.post("/api/conversations/messages", json={"text": "x" * 2001}).status_code == 422
    assert client.post("/api/conversations/messages", json={"text": ""}).status_code == 422
    assert client.post("/api/conversations/messages", json={}).status_code == 422
    assert client.post("/api/conversations/messages", json={"text": "y" * 2000}).status_code == 200

    html = "Remind me to <img onerror=x> & <script>alert(1)</script> later"
    sent = client.post("/api/conversations/messages", json={"text": html})
    assert sent.status_code == 200
    assert sent.headers["content-type"].startswith("application/json")
    assert sent.json()["user_message"]["body"] == html  # stored and returned verbatim; the UI escapes on render
    detail = client.get(f"/api/conversations/{sent.json()['conversation']['id']}").json()
    assert html in [m["body"] for m in detail["messages"]]


def test_unauthenticated_is_401_when_demo_auth_is_off(client: TestClient, seeded: Seeded, monkeypatch) -> None:
    from app.settings import get_settings

    monkeypatch.setenv("ENABLE_DEMO_AUTH", "false")
    get_settings.cache_clear()
    for path in ("/api/tasks", "/api/approvals", "/api/board", "/api/activity", "/api/conversations"):
        assert client.get(path).status_code == 401, path
