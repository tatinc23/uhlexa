"""Work born in a thread inherits that thread's visibility (security review 2026-10-22).

Alex's demo request resumes the seeded "Sunday groceries" thread, which Katie (an adult) created. Max is a child and
not a participant, so he may not open that thread, and therefore must not see its task, approval or browser task
either: not on the Family Board, not in a list, and not by direct id (NotFound, so existence does not leak)."""

import base64

import pytest
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import NotFound
from app.services import approval_service, board_service, browser_task_service, task_machine, task_service
from app.services.views import SCREENSHOT_KEY
from tests.helpers_services import approved_browser_task

Seeded = dict[str, Principal]


@pytest.fixture
def adults_thread_work(db: Session, seeded: Seeded) -> dict[str, str]:
    payload, bt = approved_browser_task(db, seeded["alex"])
    bt.result = {**(bt.result or {}), SCREENSHOT_KEY: base64.b64encode(b"\xff\xd8\xffprivate").decode()}
    db.commit()
    return {"task": payload["task"]["id"], "approval": payload["approval"]["id"], "browser_task": str(bt.id)}


def test_child_lists_hide_work_from_an_adults_thread(db: Session, seeded: Seeded, adults_thread_work) -> None:
    max_ = seeded["max"]
    assert adults_thread_work["task"] not in {str(t.id) for t in task_service.list_tasks(db, max_)}
    assert adults_thread_work["approval"] not in {str(a.id) for a in approval_service.list_approvals(db, max_)}
    assert adults_thread_work["browser_task"] not in {str(b.id) for b in browser_task_service.list_recent(db, max_)}


def test_child_family_board_hides_work_from_an_adults_thread(db: Session, seeded: Seeded, adults_thread_work) -> None:
    board = repr(board_service.build_family_board(db, seeded["max"]))
    for value in adults_thread_work.values():
        assert value not in board


def test_child_direct_reads_are_not_found(db: Session, seeded: Seeded, adults_thread_work) -> None:
    max_ = seeded["max"]
    with pytest.raises(NotFound):
        task_machine.get(db, max_, adults_thread_work["task"])
    with pytest.raises(NotFound):
        approval_service.get_for(db, max_, adults_thread_work["approval"])
    with pytest.raises(NotFound):
        browser_task_service.get_for(db, max_, adults_thread_work["browser_task"])
    with pytest.raises(NotFound):
        browser_task_service.screenshot_for(db, max_, adults_thread_work["browser_task"])


def test_adults_still_see_the_same_work(db: Session, seeded: Seeded, adults_thread_work) -> None:
    for handle in ("alex", "katie"):
        who = seeded[handle]
        assert str(task_machine.get(db, who, adults_thread_work["task"]).id) == adults_thread_work["task"]
        assert (
            str(approval_service.get_for(db, who, adults_thread_work["approval"]).id) == adults_thread_work["approval"]
        )
        assert browser_task_service.screenshot_for(db, who, adults_thread_work["browser_task"])[0].endswith(b"private")
        board = repr(board_service.build_family_board(db, who))
        assert adults_thread_work["task"] in board
