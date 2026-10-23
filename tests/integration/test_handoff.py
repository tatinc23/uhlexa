# ruff: noqa: F811
"""Same-household handoffs (`handoff_task`): a plain errand moves to another Uhlexa and policy accepts it."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActivityEvent, Job, Task, TaskKind, TaskStatus
from app.services import handoff_service
from app.settings import get_settings
from tests.integration.test_mcp_tools import as_handle, call, data, server  # noqa: F401  (fixtures)

pytestmark = pytest.mark.anyio


async def test_handoff_is_accepted_and_lands_in_katies_errands(server: Any, db: Session) -> None:
    result = data(await call(server, "handoff_task", to="Katie", title="Pick up the cake"))
    assert result["accepted"] is True
    assert result["handoff"]["kind"] == "handoff" and result["handoff"]["status"] == "completed"
    errand = result["errand"]
    assert errand["title"] == "Pick up the cake" and errand["kind"] == "chore" and errand["status"] == "completed"
    assert errand["metadata"]["errands_for"] == "katie" and errand["metadata"]["from_agent"] == "Uhlexa"
    summaries = [e.summary for e in db.scalars(select(ActivityEvent).order_by(ActivityEvent.created_at))]
    assert "From Uhlexa: Pick up the cake" in summaries
    assert "Jordy: added to Katie's errands: Pick up the cake" in summaries


async def test_the_errand_belongs_to_the_receiver(server: Any, db: Session) -> None:
    data(await call(server, "handoff_task", to="katie", title="Pick up the cake"))
    errand = db.scalars(select(Task).where(Task.kind == TaskKind.CHORE)).one()
    handoff = db.scalars(select(Task).where(Task.kind == TaskKind.HANDOFF)).one()
    assert errand.created_by_user_id == handoff.assignee_user_id != handoff.created_by_user_id
    assert errand.status == TaskStatus.COMPLETED


@pytest.mark.parametrize("to", ["alex", "Max", "nobody", "Grammy"])
async def test_handoff_refuses_self_children_and_strangers(server: Any, to: str) -> None:
    result = await call(server, "handoff_task", to=to, title="Pick up the cake")
    assert result.is_error


async def test_a_child_cannot_hand_off(server: Any, as_handle: Callable[[str], None]) -> None:
    as_handle("max")
    assert (await call(server, "handoff_task", to="alex", title="Buy candy")).is_error


async def test_handoff_needs_a_title(server: Any) -> None:
    assert (await call(server, "handoff_task", to="katie", title="   ")).is_error


async def test_with_a_delay_the_handoff_waits_then_the_job_accepts_it(
    server: Any, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HANDOFF_ACCEPT_DELAY_SECONDS", "3")
    get_settings.cache_clear()
    result = data(await call(server, "handoff_task", to="katie", title="Pick up the cake"))
    assert result["accepted"] is False and result["errand"] is None
    assert result["handoff"]["status"] == "approved"
    job = db.scalars(select(Job).where(Job.kind == handoff_service.JOB_KIND)).one()
    assert job.payload == {"handoff_task_id": result["handoff"]["id"]}
    assert not db.scalars(select(Task).where(Task.kind == TaskKind.CHORE)).all()  # nothing accepted yet

    handoff_service.handle_job(db, job.payload)
    handoff_service.handle_job(db, job.payload)  # a retry changes nothing
    errands = db.scalars(select(Task).where(Task.kind == TaskKind.CHORE)).all()
    assert [e.title for e in errands] == ["Pick up the cake"]
    assert db.get(Task, uuid.UUID(job.payload["handoff_task_id"])).status == TaskStatus.COMPLETED


async def test_acceptance_is_logged_as_a_policy_action_not_as_the_receiver(server: Any, db: Session) -> None:
    data(await call(server, "handoff_task", to="katie", title="Pick up the cake"))
    accepted = db.scalars(select(ActivityEvent).where(ActivityEvent.event_type == "handoff.accepted")).one()
    assert accepted.actor_type == "system" and accepted.actor_user_id is None and accepted.actor_agent_id is None
    assert accepted.payload["receiver_agent"] == "Jordy"
