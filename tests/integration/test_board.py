"""The Family Board payload (`board_service.build_family_board`): one builder shared by REST and MCP. It is
viewer-relative (`can_decide`, the takeover link), bounded, policy-filtered and carries no secrets."""

import base64
import json
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.models import BrowserTaskStatus
from app.services import board_service, conversation_service, task_service
from app.services.views import SCREENSHOT_KEY
from app.settings import get_settings
from tests.helpers_services import approve, approved_browser_task, ask, enable_maritime, real_snap

Seeded = dict[str, Principal]

PLANNED_KEYS = {
    "household",
    "viewer",
    "members",
    "conversations",
    "tasks",
    "counts",
    "approvals",
    "browser_tasks",
    "activity",
    "providers",
    "generated_at",
}
LINK = "https://control.example/SECRET-LINK-9f3a"


def board(db: Session, principal: Principal) -> dict[str, Any]:
    return board_service.build_family_board(db, principal)


def test_the_payload_has_the_planned_shape_and_is_plain_json(db: Session, seeded: Seeded) -> None:
    payload = board(db, seeded["alex"])
    assert set(payload) == PLANNED_KEYS
    assert set(payload["approvals"]) == {"pending", "recent"}
    assert json.loads(json.dumps(payload)) == payload  # JSON-ready, no stray objects
    assert datetime.fromisoformat(payload["generated_at"]).tzinfo is not None
    assert payload["viewer"]["handle"] == "alex" and payload["viewer"]["role"] == "guardian"
    assert payload["household"]["id"] == str(seeded["alex"].household.id)


def test_members_are_listed_with_their_agents_and_the_viewer_is_marked(db: Session, seeded: Seeded) -> None:
    members = board(db, seeded["katie"])["members"]
    assert [(m["handle"], m["role"], m["agent"]["name"]) for m in members] == [
        ("alex", "guardian", "Uhlexa"),
        ("katie", "adult", "Jordy"),
        ("max", "child", "Max's Uhlexa"),
    ]
    assert [m["handle"] for m in members if m["is_viewer"]] == ["katie"]


def test_the_seeded_thread_shows_its_participants_and_who_resumed_it(db: Session, seeded: Seeded) -> None:
    ask(db, seeded["alex"])
    (thread,) = [c for c in board(db, seeded["alex"])["conversations"] if c["topic_key"] == "sunday-groceries"]
    assert thread["created_by_agent_name"] == "Jordy"
    resumed = [p for p in thread["participants"] if p["joined_via"] == "resumed"]
    assert [p["name"] for p in resumed] == ["Uhlexa"]


def test_approvals_are_relative_to_the_viewer(db: Session, seeded: Seeded) -> None:
    ask(db, seeded["max"], "add socks to my clothes list")
    seen = {handle: board(db, p)["approvals"]["pending"] for handle, p in seeded.items()}
    assert {handle: [a["can_decide"] for a in pending] for handle, pending in seen.items()} == {
        "alex": [True],
        "katie": [True],
        "max": [False],  # a child never decides, not even their own request
    }
    assert seen["max"][0]["can_decide_reason"]  # the board can say why ("Waiting for a grown-up")
    assert seen["alex"][0]["task"]["items"] == ["socks"] and seen["alex"][0]["task"]["will_use_browser"] is True


def test_a_decided_approval_moves_from_pending_to_recent(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex)
    assert [a["id"] for a in board(db, alex)["approvals"]["pending"]] == [payload["approval"]["id"]]
    approve(db, alex, payload)
    after = board(db, alex)["approvals"]
    assert after["pending"] == []
    (recent,) = after["recent"]
    assert (recent["id"], recent["status"], recent["decided_by_name"]) == (
        payload["approval"]["id"],
        "approved",
        "Alex Parker",
    )
    assert recent["can_decide"] is False  # already decided


def test_counts_follow_the_data(
    db: Session, seeded: Seeded, settings_env: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_maritime(monkeypatch)
    alex = seeded["alex"]
    approved_browser_task(db, alex)  # approved, browser task queued
    ask(db, alex, "add tea to the pantry list")  # pending approval, new thread
    ask(db, seeded["katie"], "remind me to call grandma")  # completes at once
    counts = board(db, alex)["counts"]
    assert counts["tasks"] == {"approved": 1, "pending_approval": 1, "completed": 1}
    assert counts["pending_approvals"] == 1
    assert counts["open_conversations"] == 4  # Sunday groceries, birthday dinner, Pantry, and the reminder's thread
    assert counts["browser_tasks_active"] == 1


def test_activity_is_newest_first_and_names_the_chain(db: Session, seeded: Seeded) -> None:
    ask(db, seeded["alex"])
    events = board(db, seeded["alex"])["activity"]
    stamps = [e["created_at"] for e in events]
    assert stamps == sorted(stamps, reverse=True)
    assert [e["event_type"] for e in events][:2] == ["approval.requested", "task.created"]
    assert "conversation.resumed" in [e["event_type"] for e in events]


def test_a_child_sees_neither_adult_threads_nor_the_activity_about_them(db: Session, seeded: Seeded) -> None:
    ask(db, seeded["alex"])  # touches Katie's seeded thread
    max_board = board(db, seeded["max"])
    assert "sunday-groceries" not in [c["topic_key"] for c in max_board["conversations"]]
    hidden = {c["id"] for c in board(db, seeded["alex"])["conversations"]} - {
        c["id"] for c in max_board["conversations"]
    }
    assert hidden and not {e["conversation_id"] for e in max_board["activity"]} & hidden


def test_the_takeover_link_is_for_the_owner_only(
    db: Session, seeded: Seeded, settings_env: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_maritime(monkeypatch)
    _, bt = approved_browser_task(db, seeded["alex"])
    bt.status, bt.requires_takeover, bt.takeover_url = BrowserTaskStatus.NEEDS_TAKEOVER, True, LINK
    db.commit()
    links = {handle: board(db, seeded[handle])["browser_tasks"][0] for handle in ("alex", "katie")}
    assert links["alex"]["takeover_url"] == LINK and links["alex"]["is_owner"] is True
    assert links["katie"]["takeover_url"] is None and links["katie"]["is_owner"] is False
    assert board(db, seeded["max"])["browser_tasks"] == []  # the adults' thread is hidden from a child entirely
    assert LINK not in json.dumps([board(db, seeded["katie"]), board(db, seeded["max"])])
    assert LINK not in json.dumps(board(db, seeded["alex"])["activity"])  # never written to the log


def test_screenshot_bytes_and_results_stay_off_the_board(
    db: Session, seeded: Seeded, settings_env: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_maritime(monkeypatch)
    _, bt = approved_browser_task(db, seeded["alex"])
    encoded = base64.b64encode(b"\xff\xd8\xffthe-screenshot-bytes").decode()
    bt.result = {**bt.result, SCREENSHOT_KEY: encoded}
    db.commit()
    payload = board(db, seeded["alex"])
    (card,) = payload["browser_tasks"]
    assert card["result"] == {} and SCREENSHOT_KEY not in json.dumps(payload) and encoded not in json.dumps(payload)
    assert card["status"] == "queued" and card["verified"] is False and card["steps"] == []


def test_every_list_is_bounded(db: Session, seeded: Seeded) -> None:
    katie = seeded["katie"]
    for n in range(board_service.TASK_LIMIT + 5):
        task_service.create(db, katie, title=f"Chore {n}", kind="chore", source="rest", snap=real_snap())
    for n in range(board_service.CONVERSATION_LIMIT + 5):
        conversation_service.start(db, katie, f"Thread {n}", f"thread-{n}")
    db.commit()
    payload = board(db, katie)
    assert len(payload["tasks"]) == board_service.TASK_LIMIT
    assert payload["tasks"][0]["title"] == f"Chore {board_service.TASK_LIMIT + 4}"  # newest first
    assert len(payload["conversations"]) == board_service.CONVERSATION_LIMIT
    assert len(payload["activity"]) == board_service.ACTIVITY_LIMIT
    assert payload["counts"]["tasks"] == {"completed": board_service.TASK_LIMIT + 5}  # counts are not capped
    assert payload["counts"]["open_conversations"] == board_service.CONVERSATION_LIMIT + 5 + 2  # + the seeded threads


def test_the_provider_block_is_the_status_payload_without_secrets(
    db: Session, seeded: Seeded, settings_env: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "SENTINEL-subq-key-board-77"
    monkeypatch.setenv("ENABLE_SUBQ_SNAP", "true")
    monkeypatch.setenv("SUBQ_API_KEY", secret)
    monkeypatch.setenv("ENABLE_PRIMITIVE", "true")  # flag on, no key
    get_settings.cache_clear()
    providers = board(db, seeded["alex"])["providers"]
    assert {name: p["status"] for name, p in providers.items()} == {
        "primitive": "misconfigured",
        "subq": "ok",
        "maritime": "disabled",
        "keiki": "disabled",
        "inkbox": "disabled",
    }
    assert all(set(p) == {"status", "enabled", "configured", "detail"} for p in providers.values())
    assert secret not in json.dumps(providers)
