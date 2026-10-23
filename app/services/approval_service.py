"""Approvals: a person (never the model) decides; the policy engine says who may (spec sections 9 and 13)."""

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.db.base import utcnow
from app.errors import Forbidden, InvalidTransition, NotFound, ValidationFailed
from app.models import Approval, ApprovalStatus, DecisionSource, Task, TaskStatus
from app.policy import PolicyDecision, can_decide
from app.schemas.approvals import ApprovalOut
from app.services import access, activity_service, browser_task_service, task_machine, views
from app.services.clock import tick

MAX_NOTE_CHARS = 1000
MAX_LIMIT = 200
TEXT_CODE_TTL = timedelta(hours=1)


def request(db: Session, principal: Principal, task: Task, decision: PolicyDecision) -> Approval:
    """Open a pending approval for `task`, on behalf of the requesting user and their agent."""
    approval = Approval(
        household_id=task.household_id,
        task_id=task.id,
        requested_by_agent_id=principal.agent.id,
        requested_for_user_id=principal.user.id,
        status=ApprovalStatus.PENDING,
        rule_id=decision.rule_id or "unspecified",
        reason=decision.reason,
        decision_source=DecisionSource.POLICY_SNAP if decision.source == "policy+snap" else DecisionSource.POLICY,
        created_at=tick(),
    )
    db.add(approval)
    db.flush()
    activity_service.record(
        db,
        household_id=task.household_id,
        event_type="approval.requested",
        summary=f"{principal.agent.name} needs approval for “{task.title[:80]}”.",
        actor_type="agent",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        task_id=task.id,
        conversation_id=task.conversation_id,
        payload={"approval_id": str(approval.id), "rule_id": approval.rule_id, "reason": approval.reason},
    )
    return approval


def get_for(db: Session, principal: Principal, approval_id: uuid.UUID | str, *, lock: bool = False) -> Approval:
    stmt = (
        select(Approval)
        .join(Task, Task.id == Approval.task_id)
        .where(
            Approval.id == access.as_uuid(approval_id),
            Approval.household_id == principal.household.id,
            access.visible_conversation_clause(db, principal, Task.conversation_id),
        )
    )
    approval = db.scalars(stmt.with_for_update(of=Approval) if lock else stmt).first()
    if approval is None:
        raise NotFound("Approval not found")
    return approval


def list_approvals(
    db: Session, principal: Principal, *, status: ApprovalStatus | str | None = None, limit: int = 50
) -> list[Approval]:
    stmt = (
        select(Approval)
        .join(Task, Task.id == Approval.task_id)
        .where(
            Approval.household_id == principal.household.id,
            access.visible_conversation_clause(db, principal, Task.conversation_id),
        )
    )
    if status is not None:
        stmt = stmt.where(Approval.status == ApprovalStatus(status))
    stmt = stmt.order_by(Approval.created_at.desc(), Approval.id.desc()).limit(max(1, min(limit, MAX_LIMIT)))
    return list(db.scalars(stmt))


def can_decide_for(db: Session, principal: Principal, approval: Approval, task: Task | None = None) -> tuple[bool, str]:
    """May this person press Approve/Deny right now? Returns (allowed, reason)."""
    task = task or db.get(Task, approval.task_id)
    return can_decide(
        approval_status=access.enum_value(approval.status),
        rule_id=approval.rule_id,
        requester_may_approve=bool((task.meta if task else {}).get("requester_may_approve")),
        decider_role=access.enum_value(principal.role),
        decider_user_id=principal.user.id,
        requested_for_user_id=approval.requested_for_user_id,
    )


def views_for(db: Session, principal: Principal, approvals: list[Approval]) -> list[ApprovalOut]:
    return views.approval_outs(db, approvals, lambda a, t: can_decide_for(db, principal, a, t))


def view_for(db: Session, principal: Principal, approval: Approval) -> ApprovalOut:
    return views_for(db, principal, [approval])[0]


def _code_hash(approval_id: uuid.UUID, code: str) -> str:
    return hashlib.sha256(f"{approval_id}:{code.strip().lower()}".encode()).hexdigest()


def issue_text_code(db: Session, principal: Principal, approval_id: uuid.UUID | str) -> str:
    """A fresh one-time code for deciding this approval by text, for someone who may decide it. Random, not derived
    from the approval id; only its hash is stored, so it is shown once. A new code replaces any earlier one."""
    approval = get_for(db, principal, approval_id, lock=True)
    allowed, reason = can_decide_for(db, principal, approval)
    if not allowed:
        raise Forbidden(reason)
    code = secrets.token_hex(4)
    approval.text_code_hash = _code_hash(approval.id, code)
    approval.text_code_expires_at = utcnow() + TEXT_CODE_TTL
    db.flush()
    return code


def find_by_text_code(db: Session, principal: Principal, code: str) -> Approval | None:
    """The one pending approval this person can see whose unexpired code matches, else None."""
    now = utcnow()
    for approval in list_approvals(db, principal, status=ApprovalStatus.PENDING, limit=MAX_LIMIT):
        if (
            approval.text_code_hash is not None
            and approval.text_code_expires_at is not None
            and approval.text_code_expires_at > now
            and secrets.compare_digest(approval.text_code_hash, _code_hash(approval.id, code))
        ):
            return approval
    return None


def cancel_pending(db: Session, task: Task) -> None:
    """The task was cancelled: its open approvals can no longer be decided."""
    for approval in db.scalars(
        select(Approval).where(Approval.task_id == task.id, Approval.status == ApprovalStatus.PENDING)
    ):
        approval.status = ApprovalStatus.CANCELLED
        approval.text_code_hash = None
    db.flush()


def decide(
    db: Session, principal: Principal, approval_id: uuid.UUID | str, *, approve: bool, note: str | None = None
) -> Approval:
    """Approve or deny. Exactly once: a second call is `InvalidTransition` (409), so a double click, two tabs or MCP
    plus the UI at once enqueue one browser job. Postgres serializes callers with a row lock."""
    clean_note = (note or "").strip() or None
    if clean_note and len(clean_note) > MAX_NOTE_CHARS:
        raise ValidationFailed(f"A note can be at most {MAX_NOTE_CHARS} characters.")
    approval = get_for(db, principal, approval_id, lock=True)
    if approval.status != ApprovalStatus.PENDING:
        raise InvalidTransition(f"This approval was already {access.enum_value(approval.status)}.")
    task = db.get(Task, approval.task_id)
    if task is None:
        raise NotFound("Task not found")
    allowed, reason = can_decide_for(db, principal, approval, task)
    if not allowed:
        raise Forbidden(reason)

    approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
    approval.decided_by_user_id = principal.user.id
    approval.decided_at = tick()
    approval.decision_note = clean_note
    approval.text_code_hash = None  # single use
    db.flush()
    verb = "approved" if approve else "denied"
    activity_service.record(
        db,
        household_id=approval.household_id,
        event_type=f"approval.{verb}",
        summary=f"{principal.user.display_name} {verb} “{task.title[:80]}”.",
        actor_type="user",
        actor_user_id=principal.user.id,
        task_id=task.id,
        conversation_id=task.conversation_id,
        payload={"approval_id": str(approval.id), "rule_id": approval.rule_id, "note": clean_note},
    )
    task_machine.transition(db, task, TaskStatus.APPROVED if approve else TaskStatus.DENIED, actor=principal)
    if approve:
        _settle_approved(db, principal, task)
    return approval


def _settle_approved(db: Session, principal: Principal, task: Task) -> None:
    """What an approval unlocks: a browser run, or (for kinds with nothing to automate) recording the task as done.
    Purchases and bookings stay `approved`: no automation exists for them, and we never fake a completion."""
    if task.meta.get("will_use_browser"):
        browser_task_service.create_for_task(db, task, principal)
    elif task.kind in task_machine.RECORDING_KINDS:
        task_machine.transition(db, task, TaskStatus.COMPLETED, actor=None)
