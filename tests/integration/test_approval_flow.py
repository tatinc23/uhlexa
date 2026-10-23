"""The request -> approval -> browser-task handoff at service level, and its failure modes (Review Focus 1, 2)."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import Forbidden, InvalidTransition, NotFound, ValidationFailed
from app.models import (
    ActivityEvent,
    Approval,
    ApprovalStatus,
    BrowserTaskStatus,
    Conversation,
    Message,
    TaskKind,
    TaskStatus,
    TranscriptProvider,
)
from app.models.task import Task
from app.models.user import User
from app.providers.primitive.models import PrimitiveMemory, PrimitiveReply
from app.services import (
    activity_service,
    approval_service,
    browser_task_service,
    conversation_service,
    task_service,
    thread_state_service,
)
from tests.helpers_services import (
    DEMO_SENTENCE,
    SAFE_URL,
    FakePrimitive,
    FakeSnap,
    browser_tasks,
    enable_maritime,
    give_household_emails,
    jobs,
    real_primitive,
    real_snap,
    second_household_principal,
)

Seeded = dict[str, Principal]


@pytest.fixture
def maritime_on(settings_env: object, monkeypatch: pytest.MonkeyPatch) -> None:
    enable_maritime(monkeypatch)


def ask(db: Session, principal: Principal, text: str = DEMO_SENTENCE, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("source", "rest")
    kwargs.setdefault("snap", real_snap())
    kwargs.setdefault("primitive", real_primitive())
    payload = conversation_service.handle_message(db, principal, text, **kwargs)
    db.commit()
    return payload


def approve(db: Session, principal: Principal, payload: dict[str, Any], *, ok: bool = True) -> Approval:
    approval = approval_service.decide(db, principal, uuid.UUID(payload["approval"]["id"]), approve=ok)
    db.commit()
    return approval


def seeded_thread(db: Session) -> Conversation:
    """The seeded thread: the oldest one on this topic (a test may start a second thread with the same topic)."""
    return db.scalars(
        select(Conversation).where(Conversation.topic_key == "sunday-groceries").order_by(Conversation.created_at)
    ).first()  # type: ignore[return-value]


def events(db: Session, principal: Principal, kind: str) -> list[ActivityEvent]:
    return [e for e in activity_service.list_events(db, principal, limit=200) if e.event_type == kind]


# --- the demo story -------------------------------------------------------------------------------------------------


def test_demo_flow_service_level(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex)

    assert set(payload) == {"conversation", "routing", "user_message", "agent_message", "task", "approval"}
    assert payload["routing"]["action"] == "resumed"
    assert payload["routing"]["decision"]["source"] == "fallback"
    assert payload["conversation"]["id"] == str(seeded_thread(db).id)
    assert payload["task"]["status"] == "pending_approval"
    assert payload["task"]["kind"] == "shopping_list"
    assert payload["task"]["metadata"]["items"] == ["milk", "eggs"]
    assert payload["approval"]["status"] == "pending" and payload["approval"]["can_decide"] is True
    assert payload["user_message"]["body"] == DEMO_SENTENCE
    assert payload["agent_message"]["sender_name"] == "Uhlexa"

    thread = seeded_thread(db)
    assert thread.thread_state["items"] == ["bread", "bananas", "milk", "eggs"]
    assert thread.thread_state["last_actor"] == "Uhlexa"
    assert thread.thread_state_version == 2
    assert jobs(db) == []  # nothing runs before a person approves

    approval = approve(db, alex, payload)
    task = db.get(Task, uuid.UUID(payload["task"]["id"]))
    assert approval.status == ApprovalStatus.APPROVED and approval.decided_by_user_id == alex.user.id
    assert approval.decided_at is not None
    assert task is not None and task.status == TaskStatus.APPROVED
    (job,) = jobs(db, "browser_task_run")
    (browser_task,) = browser_tasks(db)
    assert job.payload["browser_task_id"] == str(browser_task.id) and job.household_id == alex.household.id
    assert browser_task.user_id == alex.user.id and browser_task.agent_id == alex.agent.id
    assert browser_task.task_id == task.id and browser_task.task_type == "list_add_items"
    assert browser_task.status == BrowserTaskStatus.UNAVAILABLE  # Maritime is off in tests: honest, instant
    assert "ENABLE_MARITIME" in (browser_task.last_error or "")
    assert seeded_thread(db).thread_state_version == 2

    chain = [e.event_type for e in reversed(activity_service.list_events(db, alex, limit=50))]
    assert chain == [
        "conversation.resumed",
        "message.routed",
        "task.created",
        "approval.requested",
        "approval.approved",
        "task.status_changed",
        "browser_task.queued",
        "browser_task.unavailable",
    ]


def test_with_maritime_configured_the_browser_task_is_queued_for_the_worker(
    db: Session, seeded: Seeded, maritime_on: None
) -> None:
    alex = seeded["alex"]
    approve(db, alex, ask(db, alex))
    (browser_task,) = browser_tasks(db)
    assert browser_task.status == BrowserTaskStatus.QUEUED and browser_task.requested_url == SAFE_URL
    assert len(jobs(db, "browser_task_run")) == 1


def test_browser_task_is_pinned_to_the_requester_even_when_someone_else_approves(
    db: Session, seeded: Seeded, maritime_on: None
) -> None:
    payload = ask(db, seeded["max"], "add socks to my clothes list")
    assert payload["task"]["status"] == "pending_approval"
    assert payload["approval"]["can_decide"] is False
    approve(db, seeded["katie"], payload)
    (browser_task,) = browser_tasks(db)
    assert browser_task.user_id == seeded["max"].user.id and browser_task.agent_id == seeded["max"].agent.id


def test_deny_marks_denied(db: Session, seeded: Seeded, maritime_on: None) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex)
    approval = approve(db, alex, payload, ok=False)
    task = db.get(Task, uuid.UUID(payload["task"]["id"]))
    assert approval.status == ApprovalStatus.DENIED and task is not None and task.status == TaskStatus.DENIED
    assert jobs(db) == [] and browser_tasks(db) == []
    assert len(events(db, alex, "approval.denied")) == 1 and events(db, alex, "approval.approved") == []
    with pytest.raises(InvalidTransition):
        browser_task_service.start(db, alex, task.id)


def test_deny_records_the_note(db: Session, seeded: Seeded) -> None:
    payload = ask(db, seeded["alex"])
    approval = approval_service.decide(
        db, seeded["alex"], uuid.UUID(payload["approval"]["id"]), approve=False, note="Not today"
    )
    assert approval.decision_note == "Not today"


# --- Review Focus 1: double approve / double start ------------------------------------------------------------------


def test_double_approve_enqueues_once(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex)
    approve(db, alex, payload)
    with pytest.raises(InvalidTransition):
        approve(db, alex, payload)
    db.rollback()
    assert len(jobs(db, "browser_task_run")) == 1
    assert len(browser_tasks(db)) == 1
    assert len(events(db, alex, "approval.approved")) == 1


def test_double_approve_by_two_people_enqueues_once(db: Session, seeded: Seeded, maritime_on: None) -> None:
    payload = ask(db, seeded["alex"])
    approve(db, seeded["alex"], payload)
    with pytest.raises(InvalidTransition):
        approve(db, seeded["katie"], payload)
    db.rollback()
    assert len(jobs(db, "browser_task_run")) == 1


@pytest.mark.parametrize("first_ok", [True, False])
def test_an_approval_can_be_decided_only_once(db: Session, seeded: Seeded, first_ok: bool) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex)
    approve(db, alex, payload, ok=first_ok)
    with pytest.raises(InvalidTransition):
        approve(db, alex, payload, ok=not first_ok)
    db.rollback()
    assert len(jobs(db)) == (1 if first_ok else 0)


def test_double_start_is_rejected_while_a_browser_task_is_active(
    db: Session, seeded: Seeded, maritime_on: None
) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex)
    approve(db, alex, payload)
    task_id = uuid.UUID(payload["task"]["id"])
    with pytest.raises(InvalidTransition, match="already"):
        browser_task_service.start(db, alex, task_id)
    db.rollback()
    assert len(jobs(db, "browser_task_run")) == 1 and len(browser_tasks(db)) == 1


def test_start_after_unavailable_creates_a_fresh_browser_task(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex)
    approve(db, alex, payload)
    task_id = uuid.UUID(payload["task"]["id"])
    retry = browser_task_service.start(db, alex, task_id)
    db.commit()
    first, second = browser_tasks(db)
    assert second.id == retry.id != first.id
    assert (first.status, second.status) == (BrowserTaskStatus.UNAVAILABLE, BrowserTaskStatus.UNAVAILABLE)
    assert len(jobs(db, "browser_task_run")) == 2


def test_start_works_once_maritime_is_configured(db: Session, seeded: Seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex)
    approve(db, alex, payload)
    enable_maritime(monkeypatch)
    retry = browser_task_service.start(db, alex, uuid.UUID(payload["task"]["id"]))
    db.commit()
    assert retry.status == BrowserTaskStatus.QUEUED and retry.requested_url == SAFE_URL


def test_start_requires_an_approved_task(db: Session, seeded: Seeded, maritime_on: None) -> None:
    alex = seeded["alex"]
    pending = uuid.UUID(ask(db, alex)["task"]["id"])
    with pytest.raises(InvalidTransition, match="approved"):
        browser_task_service.start(db, alex, pending)
    assert browser_tasks(db) == [] and jobs(db) == []


def test_start_is_only_for_browser_tasks_and_known_people(db: Session, seeded: Seeded, maritime_on: None) -> None:
    alex = seeded["alex"]
    owned = task_service.create(
        db, alex, title="Milk", kind="shopping_list", items=["milk"], source="rest", snap=real_snap()
    )
    assert owned.approval is not None
    approval_service.decide(db, alex, owned.approval.id, approve=True)
    db.commit()
    with pytest.raises(Forbidden):
        browser_task_service.start(db, seeded["max"], owned.task.id)  # a child who does not own the task
    reminder = task_service.create(db, alex, title="Call", kind="reminder", source="rest", snap=real_snap()).task
    with pytest.raises(InvalidTransition, match="browser"):
        browser_task_service.start(db, alex, reminder.id)
    with pytest.raises(NotFound):
        browser_task_service.start(db, alex, uuid.uuid4())


# --- Review Focus 2: no parseable items -----------------------------------------------------------------------------


def test_no_items_blocks_browser_task(db: Session, seeded: Seeded, maritime_on: None) -> None:
    alex = seeded["alex"]
    payload = ask(db, alex, "add stuff to groceries")
    task = payload["task"]
    assert task["kind"] == "shopping_list" and task["metadata"]["items"] == []
    assert payload["approval"] is None and task["status"] == "approved"
    assert "which items" in payload["agent_message"]["body"].lower()
    with pytest.raises(InvalidTransition, match="no items to add") as caught:
        browser_task_service.start(db, alex, uuid.UUID(task["id"]))
    assert caught.value.status_code == 409 and caught.value.code == "no_items"
    assert browser_tasks(db) == [] and jobs(db) == []
    assert seeded_thread(db).thread_state_version == 1  # nothing to record


def test_whitespace_only_items_request_behaves_the_same(db: Session, seeded: Seeded) -> None:
    payload = ask(db, seeded["alex"], "add  and  to groceries")
    assert payload["task"]["metadata"]["items"] == [] and payload["approval"] is None


# --- who may decide -------------------------------------------------------------------------------------------------


def test_child_cannot_decide_even_their_own_request(db: Session, seeded: Seeded) -> None:
    payload = ask(db, seeded["max"], "add socks to my clothes list")
    approval_id = uuid.UUID(payload["approval"]["id"])
    with pytest.raises(Forbidden):
        approval_service.decide(db, seeded["max"], approval_id, approve=True)
    db.rollback()
    assert db.get(Approval, approval_id).status == ApprovalStatus.PENDING  # type: ignore[union-attr]
    assert jobs(db) == []


def test_adult_cannot_approve_their_own_overspend(db: Session, seeded: Seeded) -> None:
    katie, alex = seeded["katie"], seeded["alex"]
    result = task_service.create(
        db, katie, title="TV", kind="purchase", amount_cents=60_000, source="rest", snap=real_snap()
    )
    db.commit()
    assert result.approval is not None
    with pytest.raises(Forbidden):
        approval_service.decide(db, katie, result.approval.id, approve=True)
    db.rollback()
    approved = approval_service.decide(db, alex, result.approval.id, approve=True)
    assert approved.status == ApprovalStatus.APPROVED
    assert result.task.status == TaskStatus.APPROVED and browser_tasks(db) == []  # a purchase has no automation


def test_approving_a_non_browser_task_completes_it(db: Session, seeded: Seeded) -> None:
    snap = FakeSnap({"approval_escalation": "require_approval"})
    result = task_service.create(db, seeded["katie"], title="Plan", kind="general", source="rest", snap=snap)
    db.commit()
    assert result.approval is not None and result.task.status == TaskStatus.PENDING_APPROVAL
    approval_service.decide(db, seeded["alex"], result.approval.id, approve=True)
    assert result.task.status == TaskStatus.COMPLETED and jobs(db) == []


def test_unknown_approval_is_not_found(db: Session, seeded: Seeded) -> None:
    with pytest.raises(NotFound):
        approval_service.decide(db, seeded["alex"], uuid.uuid4(), approve=True)


def test_approval_views_are_relative_to_the_viewer(db: Session, seeded: Seeded) -> None:
    payload = ask(db, seeded["max"], "add socks to my clothes list")
    approval = db.get(Approval, uuid.UUID(payload["approval"]["id"]))
    assert approval is not None
    assert approval_service.can_decide_for(db, seeded["max"], approval)[0] is False
    assert approval_service.can_decide_for(db, seeded["katie"], approval)[0] is True
    assert [a.id for a in approval_service.list_approvals(db, seeded["alex"], status=ApprovalStatus.PENDING)] == [
        approval.id
    ]
    assert approval_service.get_for(db, seeded["alex"], approval.id).id == approval.id


# --- the conversation side ------------------------------------------------------------------------------------------


def test_agent_reply_explains_the_resume_and_the_approval(db: Session, seeded: Seeded) -> None:
    payload = ask(db, seeded["alex"])
    body = payload["agent_message"]["body"]
    assert "Jordy" in body and "Sunday groceries" in body and "milk and eggs" in body
    assert "approv" in body.lower()
    assert payload["agent_message"]["task_id"] == payload["task"]["id"] == payload["user_message"]["task_id"]


def test_reply_without_approval_just_confirms(db: Session, seeded: Seeded) -> None:
    payload = ask(db, seeded["katie"], "remind me to call grandma")
    assert payload["task"]["status"] == "completed" and payload["approval"] is None
    assert "approv" not in payload["agent_message"]["body"].lower()


def test_message_text_limits_and_html_roundtrip(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    for blank in ("", "   \n "):
        with pytest.raises(ValidationFailed):
            conversation_service.handle_message(
                db, alex, blank, source="rest", snap=real_snap(), primitive=real_primitive()
            )
    with pytest.raises(ValidationFailed):
        conversation_service.handle_message(
            db, alex, "x" * 2001, source="rest", snap=real_snap(), primitive=real_primitive()
        )
    db.rollback()
    hostile = "<img src=x onerror=alert(1)> & \U0001f600"
    payload = ask(db, alex, hostile)
    assert payload["user_message"]["body"] == hostile
    assert ask(db, alex, "y" * 2000)["user_message"]["body"] == "y" * 2000


def test_unknown_or_foreign_conversation_id_is_not_found(db: Session, seeded: Seeded) -> None:
    with pytest.raises(NotFound):
        ask(db, seeded["alex"], conversation_id=uuid.uuid4())
    with pytest.raises(NotFound):
        ask(db, seeded["max"], conversation_id=seeded_thread(db).id)


def test_every_message_is_stored_in_the_local_transcript(db: Session, seeded: Seeded) -> None:
    ask(db, seeded["alex"])
    thread = seeded_thread(db)
    db.refresh(thread)
    bodies = [(m.sender_type.value, m.source) for m in thread.messages]
    assert bodies == [("user", TranscriptProvider.LOCAL), ("agent", TranscriptProvider.LOCAL)] * 2
    assert thread.last_message_at == thread.messages[-1].created_at


def test_context_sufficiency_is_asked_only_when_there_is_a_thread_to_read(db: Session, seeded: Seeded) -> None:
    snap = FakeSnap(
        {
            "thread_routing": lambda c: next(k for k in c if k.startswith("continue_")),
            "context_sufficiency": "need_full_transcript",
        }
    )
    payload = ask(db, seeded["alex"], snap=snap)
    context = payload["routing"]["context"]
    assert context["mode"] == "full_transcript" and context["messages_considered"] == 2
    assert context["decision"]["source"] == "snap" and context["decision"]["ai_assisted"] is True
    assert payload["routing"]["decision"]["ai_assisted"] is True
    fresh = FakeSnap({"context_sufficiency": "recent_context_enough"})
    ask(db, seeded["alex"], "remind me to water the plants", snap=fresh)
    assert fresh.asked("context_sufficiency") == []  # a brand-new thread has no history to judge


def test_context_falls_back_to_recent_when_the_thread_has_items(db: Session, seeded: Seeded) -> None:
    context = ask(db, seeded["alex"])["routing"]["context"]
    assert context["mode"] == "recent" and context["decision"]["source"] == "fallback"
    assert context["state_version"] == 2


# --- thread state ---------------------------------------------------------------------------------------------------


def test_apply_items_dedupes_in_order_without_mutating_the_input() -> None:
    state = {"summary": "S", "items": ["Bread", "bananas"], "open_questions": ["q"], "last_actor": "Jordy"}
    new = thread_state_service.apply_items(state, ["BREAD", "milk", " eggs ", "Milk"], "Uhlexa")
    assert new["items"] == ["Bread", "bananas", "milk", "eggs"]
    assert new["last_actor"] == "Uhlexa" and new["summary"] == "S" and new["open_questions"] == ["q"]
    assert state["items"] == ["Bread", "bananas"] and state["last_actor"] == "Jordy"
    assert thread_state_service.apply_items({}, ["a"], "X")["items"] == ["a"]


def test_save_bumps_the_version_and_mirrors_to_primitive(db: Session, seeded: Seeded) -> None:
    primitive = FakePrimitive()
    thread = seeded_thread(db)
    key = f"uhlexa:thread:{thread.id}:state"
    assert thread_state_service.memory_key(thread) == key
    version = thread_state_service.save(db, thread, {**thread.thread_state, "items": ["x"]}, primitive=primitive)
    assert version == 2 == thread.thread_state_version and thread.thread_state["items"] == ["x"]
    assert primitive.memory[key].value == {"state": thread.thread_state, "version": 2}


def test_save_survives_a_primitive_outage_and_says_so(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    version = thread_state_service.save(db, thread, {"items": ["x"]}, primitive=FakePrimitive(down=True))
    assert version == 2 and thread.thread_state == {"items": ["x"]}
    assert [e.event_type for e in activity_service.list_events(db, seeded["alex"])][0] == "provider.unavailable"


def test_save_with_primitive_disabled_is_silent(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    thread_state_service.save(db, thread, {"items": ["x"]}, primitive=real_primitive())
    assert activity_service.list_events(db, seeded["alex"]) == []


def test_load_prefers_a_newer_primitive_copy_and_falls_back_to_postgres(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    primitive = FakePrimitive()
    assert thread_state_service.load(db, thread, primitive=primitive) == thread.thread_state  # nothing mirrored yet
    key = thread_state_service.memory_key(thread)
    newer = {"summary": "S", "items": ["bread", "bananas", "tea"], "open_questions": [], "last_actor": "Z"}
    primitive.memory[key] = PrimitiveMemory(key, {"state": newer, "version": 5}, "9")
    assert thread_state_service.load(db, thread, primitive=primitive)["items"] == ["bread", "bananas", "tea"]
    primitive.memory[key] = PrimitiveMemory(key, {"state": newer, "version": 1}, "9")  # older than Postgres: ignored
    assert thread_state_service.load(db, thread, primitive=primitive) == thread.thread_state
    primitive.memory[key] = PrimitiveMemory(key, {"garbage": True}, "9")
    assert thread_state_service.load(db, thread, primitive=primitive) == thread.thread_state
    assert thread_state_service.load(db, thread, primitive=FakePrimitive(down=True)) == thread.thread_state
    assert thread_state_service.load(db, thread, primitive=real_primitive()) == thread.thread_state


def test_the_request_is_mirrored_to_primitive_memory(db: Session, seeded: Seeded) -> None:
    primitive = FakePrimitive()
    ask(db, seeded["alex"], primitive=primitive)
    thread = seeded_thread(db)
    stored = primitive.memory[thread_state_service.memory_key(thread)].value
    assert stored["version"] == 2 and stored["state"]["items"] == ["bread", "bananas", "milk", "eggs"]


# --- transcript provider --------------------------------------------------------------------------------------------


def make_primitive_backed(db: Session, *, emails: bool = True) -> Conversation:
    """Link the seeded thread to a Primitive thread. By default the household has addresses, which Primitive needs:
    it only answers emails from them."""
    thread = seeded_thread(db)
    thread.transcript_provider = TranscriptProvider.PRIMITIVE
    thread.primitive_thread_id = "thr_1"
    db.commit()
    if emails:
        give_household_emails(db)
    return thread


def test_agent_replies_post_to_a_primitive_backed_thread_and_are_mirrored(db: Session, seeded: Seeded) -> None:
    thread = make_primitive_backed(db)
    primitive = FakePrimitive()
    ask(db, seeded["alex"], primitive=primitive)
    assert [(t, label) for t, _, label in primitive.replies] == [("thr_1", "Uhlexa")]
    assert primitive.idempotency_keys == [str(thread.messages[-1].id)]  # a retried send cannot email twice
    db.refresh(thread)
    agent_message = thread.messages[-1]
    assert agent_message.source == TranscriptProvider.PRIMITIVE and agent_message.primitive_message_id == "prim-msg-1"
    assert thread.messages[-2].source == TranscriptProvider.LOCAL  # the user's own message is never emailed


def test_primitive_outage_keeps_the_reply_local_and_logs_it(db: Session, seeded: Seeded) -> None:
    thread = make_primitive_backed(db)
    payload = ask(db, seeded["alex"], primitive=FakePrimitive(down=True))
    db.refresh(thread)
    assert thread.messages[-1].source == TranscriptProvider.LOCAL and thread.messages[-1].primitive_message_id is None
    assert payload["agent_message"]["source"] == "local"
    # The state mirror (put_memory) reports the same outage; the reply has its own entry.
    (event,) = [
        e for e in events(db, seeded["alex"], "provider.unavailable") if e.payload["operation"] == "reply_to_thread"
    ]
    assert "unavailable" in event.summary


def test_a_reply_primitive_rejected_is_not_reported_as_sent(db: Session, seeded: Seeded) -> None:
    thread = make_primitive_backed(db)
    primitive = FakePrimitive()
    primitive.reply_result = PrimitiveReply("prim-x", "rejected", (), ("not-allowed@example.com",), False)
    ask(db, seeded["alex"], primitive=primitive)
    db.refresh(thread)
    assert thread.messages[-1].source == TranscriptProvider.LOCAL and thread.messages[-1].primitive_message_id is None
    assert events(db, seeded["alex"], "provider.unavailable")


def test_replies_may_only_go_to_household_addresses(db: Session, seeded: Seeded) -> None:
    make_primitive_backed(db)
    stranger = db.get(User, second_household_principal(db).user.id)
    assert stranger is not None
    stranger.email = "zed@other.example"  # another household: never on the list
    db.commit()
    primitive = FakePrimitive()
    ask(db, seeded["alex"], primitive=primitive)
    assert primitive.allowed_senders == [["alex@parker.example", "katie@parker.example", "max@parker.example"]]


def test_without_household_addresses_nothing_is_emailed_and_the_log_says_why(db: Session, seeded: Seeded) -> None:
    thread = make_primitive_backed(db, emails=False)
    primitive = FakePrimitive()
    payload = ask(db, seeded["alex"], primitive=primitive)
    assert primitive.replies == []  # fail closed: no address on file means no one may receive it
    db.refresh(thread)
    assert thread.messages[-1].source == TranscriptProvider.LOCAL and payload["agent_message"]["source"] == "local"
    (event,) = events(db, seeded["alex"], "provider.unavailable")
    assert "did not send" in event.summary and "household addresses" in event.payload["reason"]


def test_household_addresses_are_this_households_members_only(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    assert conversation_service.household_addresses(db, alex.household.id) == []
    give_household_emails(db)
    other = second_household_principal(db, handle="zed")
    db.get(User, other.user.id).email = "  zed@other.example "  # type: ignore[union-attr]
    db.commit()
    assert conversation_service.household_addresses(db, alex.household.id) == [
        "alex@parker.example",
        "katie@parker.example",
        "max@parker.example",
    ]
    assert conversation_service.household_addresses(db, other.household.id) == ["zed@other.example"]


def test_local_threads_never_call_primitive(db: Session, seeded: Seeded) -> None:
    primitive = FakePrimitive()
    ask(db, seeded["alex"], primitive=primitive)
    assert primitive.replies == []


def test_post_message_updates_the_thread_clock(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    before = thread.last_message_at
    message = conversation_service.post_message(
        db,
        thread,
        sender_type="user",
        body="hello",
        sender_user_id=seeded["katie"].user.id,
        primitive=real_primitive(),
    )
    assert isinstance(message, Message) and thread.last_message_at > before  # type: ignore[operator]
    assert message.source == TranscriptProvider.LOCAL and message.sender_type.value == "user"


def test_a_created_task_kind_is_what_the_parser_said(db: Session, seeded: Seeded) -> None:
    payload = ask(db, seeded["katie"], "buy a $20 gift card for grandma")
    assert payload["task"]["kind"] == TaskKind.PURCHASE.value and payload["task"]["amount_cents"] == 2000
