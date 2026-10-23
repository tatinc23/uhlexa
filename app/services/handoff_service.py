"""Same-household handoffs: one member's Uhlexa asks another member's Uhlexa to take over an errand.

A handoff moves a request, never permission or data. It is limited to plain errands (a chore): no money, no browser, no
approvals are passed along, and the receiving side runs the same policy engine as for its own tasks. The receiving
Uhlexa accepts a plain errand by itself (that is the policy for low-risk chores, not a model decision) and adds it to
its person's errands; anything the policy wants approved waits for that person.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import AGENT_TOKEN, Principal
from app.db.base import utcnow
from app.errors import Forbidden, NotFound, ValidationFailed
from app.jobs import dispatcher
from app.models import (
    Agent,
    AgentStatus,
    Household,
    HouseholdMembership,
    MembershipRole,
    Task,
    TaskKind,
    TaskStatus,
    User,
)
from app.providers.subq.service import SnapService
from app.services import access, activity_service, providers, task_machine, task_service
from app.settings import get_settings

MAX_TITLE_CHARS = 200
JOB_KIND = "handoff_accept"


def _resolve_member(db: Session, principal: Principal, who: str) -> tuple[User, HouseholdMembership]:
    """A member of the caller's own household by handle or first name. Anyone else is simply not found."""
    key = " ".join((who or "").split()).lower()
    if not key:
        raise ValidationFailed("Say who the errand is for.")
    rows = db.execute(
        select(User, HouseholdMembership)
        .join(HouseholdMembership, HouseholdMembership.user_id == User.id)
        .where(HouseholdMembership.household_id == principal.household.id)
    ).all()
    matches = [
        (user, membership)
        for user, membership in rows
        if user.handle == key or user.display_name.lower() == key or user.display_name.lower().split()[0] == key
    ]
    if len(matches) != 1:
        raise NotFound(f"No household member called {who[:40]!r}.")
    return matches[0]


def hand_off(
    db: Session, principal: Principal, *, to: str, title: str, details: str | None, snap: SnapService
) -> dict[str, Task | None]:
    """Hand a plain errand to another member's Uhlexa. Returns the handoff task and the errand it became."""
    if not principal.is_guardian_or_adult:
        raise Forbidden("Only a grown-up's Uhlexa can hand off errands.")
    title = " ".join((title or "").split())
    if not title or len(title) > MAX_TITLE_CHARS:
        raise ValidationFailed(f"An errand needs a title of at most {MAX_TITLE_CHARS} characters.")
    user, membership = _resolve_member(db, principal, to)
    if user.id == principal.user.id:
        raise ValidationFailed("That errand is already yours.")
    if membership.role == MembershipRole.CHILD:
        raise Forbidden("Errands are not handed to a child's Uhlexa.")
    agent = db.scalars(
        select(Agent).where(
            Agent.household_id == principal.household.id,
            Agent.owner_user_id == user.id,
            Agent.is_default.is_(True),
            Agent.status == AgentStatus.ACTIVE,
        )
    ).first()
    if agent is None:
        raise NotFound("That member has no Uhlexa yet.")
    handoff = Task(
        household_id=principal.household.id,
        created_by_user_id=principal.user.id,
        requested_by_agent_id=principal.agent.id,
        assignee_user_id=user.id,
        title=f"Ask {agent.name}: {title}"[:200],
        description=(details or "").strip()[:5000],
        kind=TaskKind.HANDOFF,
        status=TaskStatus.APPROVED,
        meta={
            "to_user_id": str(user.id),
            "to_agent": agent.name,
            "from_agent": principal.agent.name,
            "errand_title": title,
        },
    )
    db.add(handoff)
    db.flush()
    activity_service.record(
        db,
        household_id=handoff.household_id,
        event_type="handoff.sent",
        summary=f"From {principal.agent.name}: {title[:120]}",
        actor_type="agent",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        task_id=handoff.id,
        payload={"to_agent": agent.name},
    )
    delay = get_settings().handoff_accept_delay_seconds
    if delay > 0:
        dispatcher.enqueue(
            db,
            JOB_KIND,
            {"handoff_task_id": str(handoff.id)},
            household_id=handoff.household_id,
            run_after=utcnow() + timedelta(seconds=delay),
        )
        return {"handoff": handoff, "errand": None}
    return {"handoff": handoff, "errand": accept(db, handoff.id, snap=snap)}


def accept(db: Session, handoff_task_id: uuid.UUID | str, *, snap: SnapService) -> Task | None:
    """The receiving Uhlexa takes the errand: the same policy as for its own task decides, a plain chore needs no
    approval so it lands in the person's errands at once. Safe to run twice (the second run does nothing)."""
    handoff = db.get(Task, access.as_uuid(handoff_task_id))
    if handoff is None or handoff.kind != TaskKind.HANDOFF or handoff.status != TaskStatus.APPROVED:
        return None
    if handoff.meta.get("errand_task_id"):
        return None
    membership = db.scalars(
        select(HouseholdMembership).where(
            HouseholdMembership.household_id == handoff.household_id,
            HouseholdMembership.user_id == handoff.assignee_user_id,
        )
    ).first()
    user = db.get(User, handoff.assignee_user_id)
    household = db.get(Household, handoff.household_id)
    agent = db.scalars(
        select(Agent).where(
            Agent.household_id == handoff.household_id,
            Agent.owner_user_id == handoff.assignee_user_id,
            Agent.is_default.is_(True),
            Agent.status == AgentStatus.ACTIVE,
        )
    ).first()
    if membership is None or user is None or household is None or agent is None:
        task_machine.transition(db, handoff, TaskStatus.FAILED, actor=None, note="The receiver is no longer here")
        return None
    receiver = Principal(user, membership, household, agent, actor_kind=AGENT_TOKEN)
    first = user.display_name.split()[0]
    title = str(handoff.meta.get("errand_title") or handoff.title)[:MAX_TITLE_CHARS]
    from_agent = str(handoff.meta.get("from_agent") or "Uhlexa")
    result = task_service.create(
        db,
        receiver,
        title=title,
        kind=TaskKind.CHORE,
        description=handoff.description or None,
        source="handoff",
        snap=snap,
    )
    errand = result.task
    errand.meta = {
        **errand.meta,
        "errands_for": user.handle,
        "handoff_task_id": str(handoff.id),
        "from_agent": from_agent,
    }
    handoff.meta = {**handoff.meta, "errand_task_id": str(errand.id)}
    if errand.status == TaskStatus.COMPLETED:
        event, summary = "handoff.accepted", f"{agent.name}: added to {first}'s errands: {title[:120]}"
    else:
        event, summary = "handoff.waiting", f"{agent.name}: waiting for {first} to approve: {title[:120]}"
    activity_service.record(
        db,
        household_id=handoff.household_id,
        event_type=event,
        summary=summary,
        actor_type="system",  # the policy accepted it; the receiver's agent is named, not impersonated
        task_id=errand.id,
        payload={"from_agent": from_agent, "receiver_agent": agent.name, "handoff_task_id": str(handoff.id)},
    )
    if errand.status == TaskStatus.COMPLETED:
        task_machine.transition(db, handoff, TaskStatus.COMPLETED, actor=None, note=f"{agent.name} accepted")
    return errand


def handle_job(db: Session, payload: dict[str, Any]) -> None:
    accept(db, payload["handoff_task_id"], snap=providers.get_snap())
