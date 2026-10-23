"""No cross-household reads or writes (spec sections 2 and 8): another household's ids are `NotFound` (404, never 403,
so existence does not leak) from every service entry point, in both directions, and never show up in a listing."""

import base64
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import NotFound
from app.models import BrowserTask
from app.services import (
    activity_service,
    approval_service,
    board_service,
    browser_task_service,
    conversation_service,
    task_service,
)
from app.services.views import SCREENSHOT_KEY
from tests.helpers_services import (
    FakeMaritime,
    approve,
    approved_browser_task,
    ask,
    real_primitive,
    real_snap,
    second_household_principal,
)

Seeded = dict[str, Principal]


@dataclass(frozen=True)
class Ids:
    """One household's records: a thread, a task that was approved (with its approval and browser task), and a second
    approval that is still pending."""

    conversation: uuid.UUID
    task: uuid.UUID
    approval: uuid.UUID
    browser_task: uuid.UUID
    pending_approval: uuid.UUID

    def all(self) -> list[str]:
        return [
            str(value)
            for value in (self.conversation, self.task, self.approval, self.browser_task, self.pending_approval)
        ]


def build(db: Session, owner: Principal) -> Ids:
    """Leave a full set of records behind for `owner`'s household (Maritime is off: the browser task is unavailable)."""
    pending = ask(db, owner, "add tea to the pantry list")["approval"]["id"]
    payload, bt = approved_browser_task(db, owner)
    bt.result = {**(bt.result or {}), SCREENSHOT_KEY: base64.b64encode(b"\xff\xd8\xffsecret-screenshot").decode()}
    db.commit()
    return Ids(
        conversation=uuid.UUID(payload["conversation"]["id"]),
        task=uuid.UUID(payload["task"]["id"]),
        approval=uuid.UUID(payload["approval"]["id"]),
        browser_task=bt.id,
        pending_approval=uuid.UUID(pending),
    )


@dataclass(frozen=True)
class World:
    parker: Principal  # Alex, guardian of The Parker Family
    stranger: Principal  # Zed, guardian of Other Family
    parker_ids: Ids
    stranger_ids: Ids


@pytest.fixture
def world(db: Session, seeded: Seeded) -> World:
    stranger = second_household_principal(db)
    return World(seeded["alex"], stranger, build(db, seeded["alex"]), build(db, stranger))


Call = Callable[[Session, Principal, Ids], Any]

# Every way a caller can name a record by id. Each must answer NotFound when the id belongs to another household.
ACCESSORS: dict[str, Call] = {
    "conversation_service.get_for": lambda db, who, ids: conversation_service.get_for(db, who, ids.conversation),
    "task_service.get": lambda db, who, ids: task_service.get(db, who, ids.task),
    "task_service.get_for": lambda db, who, ids: task_service.get_for(db, who, ids.task),
    "approval_service.get_for": lambda db, who, ids: approval_service.get_for(db, who, ids.approval),
    "approval_service.get_for(lock)": lambda db, who, ids: approval_service.get_for(db, who, ids.approval, lock=True),
    "approval_service.decide(approve)": lambda db, who, ids: approval_service.decide(
        db, who, ids.pending_approval, approve=True
    ),
    "approval_service.decide(deny)": lambda db, who, ids: approval_service.decide(
        db, who, ids.pending_approval, approve=False
    ),
    "browser_task_service.get_for": lambda db, who, ids: browser_task_service.get_for(db, who, ids.browser_task),
    "browser_task_service.get_status": lambda db, who, ids: browser_task_service.get_status(db, who, ids.browser_task),
    "browser_task_service.screenshot_for": lambda db, who, ids: browser_task_service.screenshot_for(
        db, who, ids.browser_task
    ),
    "browser_task_service.start": lambda db, who, ids: browser_task_service.start(db, who, ids.task),
    "browser_task_service.resume_after_takeover": lambda db, who, ids: browser_task_service.resume_after_takeover(
        db,
        who,
        ids.browser_task,
        maritime=FakeMaritime(),  # type: ignore[arg-type]
    ),
    "task_service.cancel": lambda db, who, ids: task_service.cancel(db, who, ids.task, reason="not yours"),
    "activity_service.list_events(task_id)": lambda db, who, ids: activity_service.list_events(
        db, who, task_id=ids.task
    ),
    "activity_service.list_events(conversation_id)": lambda db, who, ids: activity_service.list_events(
        db, who, conversation_id=ids.conversation
    ),
    "task_service.create(conversation_id)": lambda db, who, ids: task_service.create(
        db, who, title="Sneak in", kind="general", conversation_id=ids.conversation, source="rest", snap=real_snap()
    ),
    "conversation_service.handle_message(conversation_id)": lambda db, who, ids: conversation_service.handle_message(
        db, who, "hello", conversation_id=ids.conversation, source="rest", snap=real_snap(), primitive=real_primitive()
    ),
}


@pytest.mark.parametrize("name", ACCESSORS)
@pytest.mark.parametrize("direction", ["stranger_reads_parker", "parker_reads_stranger"])
def test_household_isolation(db: Session, world: World, name: str, direction: str) -> None:
    caller, ids = (
        (world.stranger, world.parker_ids)
        if direction == "stranger_reads_parker"
        else (world.parker, world.stranger_ids)
    )
    with pytest.raises(NotFound) as caught:
        ACCESSORS[name](db, caller, ids)
    assert caught.value.status_code == 404  # 404 and never 403: a foreign id looks exactly like a missing one
    db.rollback()


@pytest.mark.parametrize("name", ACCESSORS)
def test_a_missing_or_malformed_id_looks_the_same_as_a_foreign_one(db: Session, world: World, name: str) -> None:
    nothing = Ids(*(uuid.uuid4() for _ in range(5)))
    with pytest.raises(NotFound) as missing:
        ACCESSORS[name](db, world.parker, nothing)
    db.rollback()
    with pytest.raises(NotFound) as foreign:
        ACCESSORS[name](db, world.parker, world.stranger_ids)
    db.rollback()
    assert (missing.value.status_code, missing.value.code, missing.value.message) == (
        foreign.value.status_code,
        foreign.value.code,
        foreign.value.message,
    )


@pytest.mark.parametrize("junk", ["not-a-uuid", "", "0" * 40, "../../etc/passwd"])
def test_junk_ids_are_not_found_not_a_crash(db: Session, world: World, junk: str) -> None:
    alex = world.parker
    for lookup in (
        conversation_service.get_for,
        task_service.get,
        approval_service.get_for,
        browser_task_service.get_for,
        browser_task_service.screenshot_for,
    ):
        with pytest.raises(NotFound):
            lookup(db, alex, junk)
    with pytest.raises(NotFound):
        approval_service.decide(db, alex, junk, approve=True)


def test_nothing_changed_when_a_foreign_decision_was_refused(db: Session, world: World) -> None:
    with pytest.raises(NotFound):
        approval_service.decide(db, world.stranger, world.parker_ids.pending_approval, approve=True)
    db.rollback()
    still_pending = approval_service.get_for(db, world.parker, world.parker_ids.pending_approval)
    assert still_pending.status.value == "pending" and still_pending.decided_by_user_id is None


def test_lists_only_hold_this_households_records(db: Session, world: World) -> None:
    for me, mine, theirs in (
        (world.parker, world.parker_ids, world.stranger_ids),
        (world.stranger, world.stranger_ids, world.parker_ids),
    ):
        assert {t.id for t in task_service.list_tasks(db, me)} >= {mine.task}
        assert not {t.id for t in task_service.list_tasks(db, me)} & {theirs.task}
        assert {a.id for a in approval_service.list_approvals(db, me)} == {mine.approval, mine.pending_approval}
        assert not {c.id for c in conversation_service.list_for(db, me)} & {theirs.conversation}
        assert mine.conversation in {c.id for c in conversation_service.list_for(db, me)}
        assert {b.id for b in browser_task_service.list_recent(db, me)} == {mine.browser_task}
        events = activity_service.list_events(db, me, limit=200)
        assert events and {e.household_id for e in events} == {me.household.id}
        assert not {e.task_id for e in events} & {theirs.task}


def test_the_family_board_never_shows_another_household(db: Session, world: World) -> None:
    for me, theirs, handles in (
        (world.parker, world.stranger_ids, {"alex", "katie", "max"}),
        (world.stranger, world.parker_ids, {"zed"}),
    ):
        board = board_service.build_family_board(db, me)
        blob = json.dumps(board)
        assert not [foreign_id for foreign_id in theirs.all() if foreign_id in blob]
        assert board["household"]["id"] == str(me.household.id)
        assert {m["handle"] for m in board["members"]} == handles
        assert board["counts"]["pending_approvals"] == 1  # only this household's own pending approval


def test_the_stranger_sees_none_of_the_parkers_text(db: Session, world: World) -> None:
    blob = json.dumps(board_service.build_family_board(db, world.stranger)).lower()
    for word in ("parker", "katie", "alex's uhlexa", "max's uhlexa", "bread", "bananas"):  # names and the seeded items
        assert word not in blob


def test_a_foreign_browser_task_row_is_untouched_by_a_refused_resume(db: Session, world: World) -> None:
    before = db.get(BrowserTask, world.parker_ids.browser_task)
    assert before is not None
    status = before.status
    with pytest.raises(NotFound):
        browser_task_service.resume_after_takeover(
            db,
            world.stranger,
            world.parker_ids.browser_task,
            maritime=FakeMaritime(),  # type: ignore[arg-type]
        )
    db.rollback()
    db.refresh(before)
    assert before.status == status


def test_approving_in_one_household_never_creates_work_in_the_other(db: Session, world: World) -> None:
    approve(db, world.stranger, {"approval": {"id": str(world.stranger_ids.pending_approval)}})
    stranger_browser = {b.id for b in browser_task_service.list_recent(db, world.stranger)}
    parker_browser = {b.id for b in browser_task_service.list_recent(db, world.parker)}
    assert len(stranger_browser) == 2 and not stranger_browser & parker_browser
    assert parker_browser == {world.parker_ids.browser_task}
