"""The REST API end to end over HTTP (spec section 14): the demo story, the children's guard rails, the page routes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth.demo_auth import Principal
from tests.helpers_services import DEMO_SENTENCE

Seeded = dict[str, Principal]


def test_demo_flow_over_http(client: TestClient, seeded: Seeded) -> None:
    assert client.post("/demo/login", json={"handle": "alex"}).status_code == 200

    sent = client.post("/api/conversations/messages", json={"text": DEMO_SENTENCE})
    assert sent.status_code == 200
    result = sent.json()
    assert result["routing"]["action"] == "resumed"
    assert result["conversation"]["title"] == "Sunday groceries"
    assert result["approval"]["status"] == "pending"
    assert result["approval"]["can_decide"] is True
    assert result["task"]["status"] == "pending_approval"

    approved = client.post(f"/api/approvals/{result['approval']['id']}/approve", json={"note": "go"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert client.post(f"/api/approvals/{result['approval']['id']}/approve").status_code == 409

    board = client.get("/api/board").json()
    assert [t["status"] for t in board["tasks"] if t["id"] == result["task"]["id"]] == ["approved"]
    (browser_task,) = [b for b in board["browser_tasks"] if b["task_id"] == result["task"]["id"]]
    assert browser_task["status"] == "unavailable"  # Maritime is off in tests: honest, never "succeeded"
    assert "approval.approved" in {e["event_type"] for e in board["activity"]}

    detail = client.get(f"/api/conversations/{result['conversation']['id']}").json()
    assert {"bread", "bananas", "milk", "eggs"} <= set(detail["thread_state"]["items"])
    assert [m["body"] for m in detail["messages"]][-2] == DEMO_SENTENCE
    assert result["conversation"]["id"] in {c["id"] for c in client.get("/api/conversations").json()}

    assert client.get(f"/api/browser-tasks/{browser_task['id']}").json()["status"] == "unavailable"
    assert client.get(f"/api/browser-tasks/{browser_task['id']}/screenshot").status_code == 404  # none was taken
    assert client.post(f"/api/browser-tasks/{browser_task['id']}/resume").status_code == 409
    retried = client.post("/api/browser-tasks", json={"task_id": result["task"]["id"]})  # "run or retry": still honest
    assert retried.status_code == 200 and retried.json()["status"] == "unavailable"

    assert client.get(f"/api/tasks/{result['task']['id']}").json()["status"] == "approved"
    assert result["task"]["id"] in {t["id"] for t in client.get("/api/tasks").json()}
    assert any(a["event_type"] == "approval.approved" for a in client.get("/api/activity?limit=50").json())
    assert client.get(f"/api/activity?task_id={result['task']['id']}").status_code == 200


def test_me_returns_the_signed_in_principal(client: TestClient, seeded: Seeded) -> None:
    me = client.get("/api/me", headers={"X-Uhlexa-User": "katie"}).json()
    assert me["user"]["handle"] == "katie" and me["role"] == "adult"
    assert me["agent"]["name"] == "Jordy"


def test_create_and_cancel_task_over_http(client: TestClient, seeded: Seeded) -> None:
    reminder = client.post("/api/tasks", json={"title": "Call grandma", "kind": "reminder"})
    assert reminder.status_code == 200 and reminder.json()["task"]["status"] == "completed"  # nothing to approve

    created = client.post("/api/tasks", json={"title": "Weekend list", "kind": "shopping_list", "items": ["milk"]})
    assert created.status_code == 200
    body = created.json()
    assert body["task"]["status"] == "pending_approval" and body["decision"]["requires_approval"] is True
    pending = client.get("/api/approvals?status=pending").json()
    assert body["approval"]["id"] in {a["id"] for a in pending}

    cancelled = client.post(f"/api/tasks/{body['task']['id']}/cancel", json={"reason": "never mind"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"/api/tasks/{body['task']['id']}/cancel").status_code == 409
    assert body["approval"]["id"] not in {a["id"] for a in client.get("/api/approvals?status=pending").json()}


def test_providers_endpoint_no_secrets(client: TestClient, seeded: Seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinels = {"PRIMITIVE_API_KEY": "sentinel-primitive-key", "SUBQ_API_KEY": "sentinel-subq-key"}
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ENABLE_PRIMITIVE", "true")
    monkeypatch.setenv("ENABLE_SUBQ_SNAP", "true")
    from app.settings import get_settings

    get_settings.cache_clear()
    response = client.get("/api/providers")
    assert response.status_code == 200
    body = response.text
    assert all(value not in body for value in sentinels.values())
    assert {"primitive", "subq", "maritime"} <= set(response.json())


def test_root_redirects_and_board_page(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/simulator/"

    page = tmp_path / "family-board.html"
    page.write_text("<!doctype html><title>Family Board</title>")
    monkeypatch.setattr(main, "BOARD_PAGE", page)
    served = client.get("/board")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("text/html")
    assert "Family Board" in served.text

    monkeypatch.setattr(main, "BOARD_PAGE", tmp_path / "missing.html")
    missing = client.get("/board")
    assert missing.status_code == 503
    assert missing.json()["code"] == "board_page_missing"


def test_health_and_simulator_mount_survive_the_mcp_mount(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/simulator/", follow_redirects=False).status_code in {200, 404}  # 404 until the simulator exists
    assert client.get("/api/providers").status_code == 200


def test_text_code_issued_to_a_decider_only_over_http(client: TestClient, seeded: Seeded) -> None:
    assert client.post("/demo/login", json={"handle": "alex"}).status_code == 200
    created = client.post("/api/tasks", json={"title": "Weekend list", "kind": "shopping_list", "items": ["milk"]})
    approval_id = created.json()["approval"]["id"]
    issued = client.post(f"/api/approvals/{approval_id}/text-code")
    assert issued.status_code == 200
    body = issued.json()
    assert len(body["code"]) == 8 and body["code"] != approval_id[:8] and body["expires_at"]
    assert body["code"] not in str(client.get("/api/approvals").json())  # shown once, never listed
