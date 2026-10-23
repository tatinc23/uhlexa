"""Task creation, the stored policy decision, Snap's escalate-only advice, transitions, cancel, and the activity log."""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import Forbidden, InvalidTransition, NotFound, ValidationFailed
from app.models import ApprovalStatus, DecisionSource, TaskKind, TaskStatus
from app.providers.subq.models import DecisionResult
from app.services import activity_service, conversation_service, task_service
from app.services.task_service import TaskCreateResult
from app.settings import get_settings
from tests.helpers_services import FakeSnap, real_snap

Seeded = dict[str, Principal]


def make(db: Session, principal: Principal, **fields: object) -> TaskCreateResult:
    fields.setdefault("source", "rest")
    fields.setdefault("snap", real_snap())
    fields.setdefault("title", "A task")
    fields.setdefault("kind", "general")
    return task_service.create(db, principal, **fields)  # type: ignore[arg-type]


def event_types(db: Session, principal: Principal, **filters: object) -> list[str]:
    return [e.event_type for e in reversed(activity_service.list_events(db, principal, **filters))]  # type: ignore[arg-type]


def test_create_task_records_activity(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    result = make(db, alex, title="Add milk and eggs", kind="shopping_list", items=["milk", "eggs"])
    assert result.task.status == TaskStatus.PENDING_APPROVAL
    assert result.approval is not None and result.decision.requires_approval
    assert event_types(db, alex, task_id=result.task.id) == ["task.created", "approval.requested"]
    created = activity_service.list_events(db, alex, task_id=result.task.id)[-1]
    assert created.actor_type == "agent" and created.actor_agent_id == alex.agent.id
    assert created.household_id == alex.household.id
    assert created.payload["policy"]["rule_id"] == "browser_action_needs_human_ok"


def test_browser_task_stores_the_policy_decision(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    result = make(db, alex, kind="shopping_list", items=["milk", "eggs"], list_name="sunday groceries", description="x")
    task, approval = result.task, result.approval
    assert approval is not None
    assert (task.created_by_user_id, task.requested_by_agent_id) == (alex.user.id, alex.agent.id)
    assert task.kind == TaskKind.SHOPPING_LIST and task.household_id == alex.household.id
    assert task.meta["items"] == ["milk", "eggs"] and task.meta["list_name"] == "sunday groceries"
    assert task.meta["will_use_browser"] is True and task.meta["source"] == "rest"
    assert task.meta["requester_may_approve"] is True
    assert approval.status == ApprovalStatus.PENDING and approval.task_id == task.id
    assert approval.rule_id == "browser_action_needs_human_ok"
    assert approval.requested_for_user_id == alex.user.id and approval.requested_by_agent_id == alex.agent.id
    assert approval.decision_source == DecisionSource.POLICY and approval.decided_at is None


@pytest.mark.parametrize("kind", ["general", "reminder", "chore"])
def test_unreviewed_recording_kinds_complete_immediately(db: Session, seeded: Seeded, kind: str) -> None:
    result = make(db, seeded["katie"], kind=kind, title="Call grandma")
    assert result.approval is None and result.decision.requires_approval is False
    assert result.task.status == TaskStatus.COMPLETED and result.task.completed_at is not None
    assert event_types(db, seeded["katie"], task_id=result.task.id) == ["task.created", "task.status_changed"]


@pytest.mark.parametrize("kind", ["purchase", "booking"])
def test_purchase_and_booking_are_never_faked_as_completed(db: Session, seeded: Seeded, kind: str) -> None:
    result = make(db, seeded["katie"], kind=kind, amount_cents=1000)
    assert result.approval is None
    assert result.task.status == TaskStatus.APPROVED  # no automation exists for these: stays approved, not "completed"


def test_shopping_list_without_items_needs_no_approval_and_stays_open(db: Session, seeded: Seeded) -> None:
    result = make(db, seeded["alex"], kind="shopping_list", items=[], title="Add stuff")
    assert result.approval is None and result.task.status == TaskStatus.APPROVED
    assert result.task.meta["items"] == [] and result.task.meta["will_use_browser"] is False


def test_child_purchase_needs_a_grown_up(db: Session, seeded: Seeded) -> None:
    result = make(db, seeded["max"], kind="purchase", amount_cents=2500)
    assert result.approval is not None and result.approval.rule_id == "child_spend"
    assert result.task.meta["requester_may_approve"] is False
    assert result.approval.requested_for_user_id == seeded["max"].user.id


def test_adult_spend_threshold_comes_from_household_settings(db: Session, seeded: Seeded) -> None:
    katie = seeded["katie"]
    assert make(db, katie, kind="purchase", amount_cents=4999).approval is None
    over = make(db, katie, kind="purchase", amount_cents=5000)
    assert over.approval is not None and over.approval.rule_id == "adult_spend_threshold"
    assert over.task.meta["requester_may_approve"] is False
    katie.household.settings["adult_spend_approval_threshold_cents"] = 1000
    db.commit()
    assert make(db, katie, kind="purchase", amount_cents=1000).approval is not None


def test_approvals_disabled_still_gates_browser_actions(
    db: Session, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_APPROVALS", "false")
    get_settings.cache_clear()
    spend = make(db, seeded["katie"], kind="purchase", amount_cents=999_00)
    assert spend.approval is None and spend.decision.reason == "Approvals disabled by flag"
    created = activity_service.list_events(db, seeded["katie"], task_id=spend.task.id)[-1]
    assert created.payload["policy"]["reason"] == "Approvals disabled by flag"
    browser = make(db, seeded["alex"], kind="shopping_list", items=["milk"])
    assert browser.approval is not None and browser.approval.rule_id == "browser_action_needs_human_ok"


def test_snap_can_add_an_approval_but_marks_it_ai_assisted(db: Session, seeded: Seeded) -> None:
    snap = FakeSnap({"approval_escalation": "require_approval"})
    result = make(db, seeded["katie"], title="Secret surprise party plan", kind="general", snap=snap)
    assert result.approval is not None
    assert result.approval.rule_id == "model_escalation"
    assert result.approval.decision_source == DecisionSource.POLICY_SNAP
    assert result.task.status == TaskStatus.PENDING_APPROVAL
    assert result.decision.source == "policy+snap"
    assert [call[0] for call in snap.calls] == ["approval_escalation"]


def test_snap_asked_with_minimal_synthetic_state_only(db: Session, seeded: Seeded) -> None:
    snap = FakeSnap({"approval_escalation": "no_extra_approval"})
    make(db, seeded["katie"], title="Secret surprise party plan", description="private details", snap=snap)
    (_, state, choices) = snap.calls[0]
    assert set(choices) == {"require_approval", "no_extra_approval"}
    assert set(state) <= {"task_kind", "requester_role", "has_amount", "uses_browser"}
    assert "Secret" not in str(state) and "private" not in str(state)


def test_snap_can_never_remove_an_approval(db: Session, seeded: Seeded) -> None:
    snap = FakeSnap({"approval_escalation": "no_extra_approval"})
    result = make(db, seeded["alex"], kind="shopping_list", items=["milk"], snap=snap)
    assert result.approval is not None and result.approval.rule_id == "browser_action_needs_human_ok"
    assert snap.calls == []  # policy already requires approval: Snap has nothing to add


@pytest.mark.parametrize(
    "answer",
    [
        DecisionResult.fallback("approval_escalation", "unavailable", "down"),
        DecisionResult.fallback("approval_escalation", "malformed", "garbage"),
        "no_extra_approval",
        "something_unrecognised",
    ],
)
def test_snap_failure_or_junk_keeps_the_deterministic_decision(db: Session, seeded: Seeded, answer: object) -> None:
    result = make(db, seeded["katie"], kind="general", snap=FakeSnap({"approval_escalation": answer}))
    assert result.approval is None and result.task.status == TaskStatus.COMPLETED


def test_browser_kind_needs_items(db: Session, seeded: Seeded) -> None:
    with pytest.raises(ValidationFailed, match="item"):
        make(db, seeded["alex"], kind="browser", items=[])
    ok = make(db, seeded["alex"], kind="browser", items=["milk"])
    assert ok.task.meta["will_use_browser"] is True and ok.approval is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "   "},
        {"title": "x" * 201},
        {"kind": "nonsense"},
        {"amount_cents": -1},
        {"amount_cents": 10**12},
        {"items": ["x" * 41]},
        {"description": "d" * 5001},
    ],
)
def test_create_validates_input(db: Session, seeded: Seeded, fields: dict[str, object]) -> None:
    with pytest.raises(ValidationFailed):
        make(db, seeded["alex"], **fields)


def test_items_are_cleaned_deduped_and_capped(db: Session, seeded: Seeded) -> None:
    items = [" milk ", "Milk", "", *[f"i{n}" for n in range(14)]]
    result = make(db, seeded["alex"], kind="shopping_list", items=items)
    assert result.task.meta["items"] == ["milk", *[f"i{n}" for n in range(9)]]


def test_conversation_must_be_accessible_and_is_linked(db: Session, seeded: Seeded) -> None:
    thread = conversation_service.get_for(db, seeded["alex"], conversation_service.list_for(db, seeded["alex"])[0].id)
    linked = make(db, seeded["alex"], conversation_id=thread.id)
    assert linked.task.conversation_id == thread.id
    with pytest.raises(NotFound):
        make(db, seeded["max"], conversation_id=thread.id)
    with pytest.raises(NotFound):
        make(db, seeded["alex"], conversation_id=uuid.uuid4())


def test_transition_follows_the_status_machine(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    task = make(db, alex, kind="shopping_list", items=["milk"]).task
    with pytest.raises(InvalidTransition):
        task_service.transition(db, task, TaskStatus.COMPLETED, actor=alex)  # must be approved first
    task_service.transition(db, task, TaskStatus.APPROVED, actor=alex)
    task_service.transition(db, task, TaskStatus.RUNNING, actor=None)
    task_service.transition(db, task, TaskStatus.NEEDS_TAKEOVER, actor=None)
    task_service.transition(db, task, TaskStatus.RUNNING, actor=None)
    task_service.transition(db, task, TaskStatus.COMPLETED, actor=None)
    assert task.completed_at is not None
    for target in (TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.APPROVED):
        with pytest.raises(InvalidTransition):
            task_service.transition(db, task, target, actor=None)
    changes = [
        e for e in activity_service.list_events(db, alex, task_id=task.id) if e.event_type == "task.status_changed"
    ]
    assert [(e.payload["from"], e.payload["to"]) for e in reversed(changes)] == [
        ("pending_approval", "approved"),
        ("approved", "running"),
        ("running", "needs_takeover"),
        ("needs_takeover", "running"),
        ("running", "completed"),
    ]
    assert changes[-1].actor_type == "user" and changes[0].actor_type == "system"


def test_a_run_interrupted_by_a_provider_outage_can_return_to_approved(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    task = make(db, alex, kind="shopping_list", items=["milk"]).task
    task_service.transition(db, task, TaskStatus.APPROVED, actor=alex)
    task_service.transition(db, task, TaskStatus.RUNNING, actor=None)
    task_service.transition(db, task, TaskStatus.APPROVED, actor=None)
    assert task.status == TaskStatus.APPROVED and task.completed_at is None


def test_cancel_cancels_the_pending_approval(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    result = make(db, alex, kind="shopping_list", items=["milk"])
    cancelled = task_service.cancel(db, alex, result.task.id, reason="changed my mind")
    assert cancelled.status == TaskStatus.CANCELLED and cancelled.cancelled_at is not None
    assert result.approval is not None and result.approval.status == ApprovalStatus.CANCELLED
    events = activity_service.list_events(db, alex, task_id=result.task.id)
    assert events[0].event_type == "task.cancelled" and events[0].payload["reason"] == "changed my mind"
    with pytest.raises(InvalidTransition):
        task_service.cancel(db, alex, result.task.id)


def test_cancel_permissions(db: Session, seeded: Seeded) -> None:
    katie_task = make(db, seeded["katie"], kind="purchase", amount_cents=100).task
    with pytest.raises(Forbidden):
        task_service.cancel(db, seeded["max"], katie_task.id)  # a child cannot cancel an adult's task
    max_task = make(db, seeded["max"], kind="purchase", amount_cents=100).task
    assert task_service.cancel(db, seeded["alex"], max_task.id).status == TaskStatus.CANCELLED  # a guardian can
    own = make(db, seeded["max"], kind="purchase", amount_cents=200).task
    assert task_service.cancel(db, seeded["max"], own.id).status == TaskStatus.CANCELLED  # a child can cancel their own


def test_get_and_list_tasks(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    first = make(db, alex, kind="purchase", amount_cents=100).task
    second = make(db, seeded["katie"], kind="shopping_list", items=["milk"]).task
    assert task_service.get(db, alex, first.id).id == first.id
    assert task_service.get_for is task_service.get
    assert [t.id for t in task_service.list_tasks(db, alex)] == [second.id, first.id]  # newest first
    assert [t.id for t in task_service.list_tasks(db, alex, status=TaskStatus.PENDING_APPROVAL)] == [second.id]
    assert len(task_service.list_tasks(db, alex, limit=1)) == 1
    with pytest.raises(NotFound):
        task_service.get(db, alex, uuid.uuid4())


def test_list_events_filters_by_task_and_conversation(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    thread = conversation_service.list_for(db, alex)[0]
    linked = make(db, alex, conversation_id=thread.id, title="Linked").task
    make(db, alex, title="Unlinked")
    assert {e.task_id for e in activity_service.list_events(db, alex, task_id=linked.id)} == {linked.id}
    in_thread = activity_service.list_events(db, alex, conversation_id=thread.id)
    assert in_thread and all(e.conversation_id == thread.id for e in in_thread)
    assert len(activity_service.list_events(db, alex, limit=2)) == 2
    with pytest.raises(NotFound):
        activity_service.list_events(db, alex, task_id=uuid.uuid4())
    with pytest.raises(NotFound):
        activity_service.list_events(db, alex, conversation_id=uuid.uuid4())


def test_record_defaults_and_newest_first(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    first = activity_service.record(
        db, household_id=alex.household.id, event_type="provider.unavailable", summary="one"
    )
    second = activity_service.record(
        db, household_id=alex.household.id, event_type="webhook.received", summary="two", payload={"k": 1}
    )
    assert first.actor_type == "system" and first.payload == {}
    assert [e.id for e in activity_service.list_events(db, alex)[:2]] == [second.id, first.id]


def test_activity_about_threads_a_child_cannot_see_is_hidden_from_them(db: Session, seeded: Seeded) -> None:
    alex, max_ = seeded["alex"], seeded["max"]
    thread = conversation_service.list_for(db, alex)[0]
    conversation_service.route_message(db, alex, "add milk to sunday groceries", snap=real_snap())
    make(db, alex, conversation_id=thread.id, title="Hidden")
    assert activity_service.list_events(db, alex, conversation_id=thread.id)
    visible = activity_service.list_events(db, max_)
    assert all(e.conversation_id in (None, *[c.id for c in conversation_service.list_for(db, max_)]) for e in visible)
    assert all(e.conversation_id != thread.id for e in visible)
    with pytest.raises(NotFound):
        activity_service.list_events(db, max_, conversation_id=thread.id)
