"""Tasks: create (with the policy decision and Snap's escalate-only advice), list, cancel. The status machine lives in
`task_machine`; approvals in `approval_service`; browser runs in `browser_task_service`."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import Forbidden, ValidationFailed
from app.models import Approval, Task, TaskKind, TaskStatus
from app.policy import PolicyDecision, evaluate_task, merge_model_advice
from app.providers.subq.service import SnapService
from app.services import access, activity_service, approval_service, browser_task_service, task_machine
from app.services.clock import tick
from app.services.intent import MAX_ITEM_CHARS, MAX_ITEMS, MAX_LIST_NAME_CHARS, SIGN_IN_CHECK, Intent, parse_intent
from app.settings import get_settings

__all__ = [
    "Intent",
    "TaskCreateResult",
    "cancel",
    "create",
    "get",
    "get_for",
    "list_tasks",
    "parse_intent",
    "transition",
]

MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 5000
MAX_AMOUNT_CENTS = 100_000_000  # $1,000,000: anything larger is a typo or an attack
MAX_LIMIT = 200

get = task_machine.get
get_for = get
transition = task_machine.transition

_ESCALATION_CHOICES = {
    "require_approval": "A person should approve this task before anything happens.",
    "no_extra_approval": "The fixed approval rules are enough; no extra approval is needed.",
}


@dataclass(frozen=True)
class TaskCreateResult:
    task: Task
    approval: Approval | None
    decision: PolicyDecision


def _clean_kind(kind: TaskKind | str) -> TaskKind:
    try:
        return TaskKind(kind)
    except ValueError:
        raise ValidationFailed(f"Unknown task kind {str(kind)[:40]!r}.") from None


def _clean_items(items: list[str] | None) -> list[str]:
    """Trim, drop blanks, de-duplicate case-insensitively (first spelling wins), keep the first 10."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in items or []:
        if not isinstance(raw, str):
            raise ValidationFailed("Items must be text.")
        item = " ".join(raw.split())
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        cleaned.append(item)
    cleaned = cleaned[:MAX_ITEMS]
    if any(len(item) > MAX_ITEM_CHARS for item in cleaned):
        raise ValidationFailed(f"Each item must be {MAX_ITEM_CHARS} characters or fewer.")
    return cleaned


def create(
    db: Session,
    principal: Principal,
    *,
    title: str,
    kind: TaskKind | str,
    items: list[str] | None = None,
    description: str | None = None,
    amount_cents: int | None = None,
    conversation_id: uuid.UUID | str | None = None,
    source: str,
    snap: SnapService,
    list_name: str | None = None,
    browser_task_type: str | None = None,
) -> TaskCreateResult:
    """Create a task for `principal`. The deterministic policy decides whether a person must approve; Snap may only
    add an approval, never remove one. Records `task.created` (+ `approval.requested`)."""
    settings = get_settings()
    kind = _clean_kind(kind)
    title = " ".join((title or "").split())
    if not title or len(title) > MAX_TITLE_CHARS:
        raise ValidationFailed(f"A title is required and can be at most {MAX_TITLE_CHARS} characters.")
    description = (description or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValidationFailed(f"A description can be at most {MAX_DESCRIPTION_CHARS} characters.")
    if amount_cents is not None and not 0 <= amount_cents <= MAX_AMOUNT_CENTS:
        raise ValidationFailed("The amount must be between $0 and $1,000,000.")
    items = _clean_items(items)
    if kind == TaskKind.BROWSER and not items and browser_task_type != SIGN_IN_CHECK:
        raise ValidationFailed("A browser task needs at least one item to add.")
    conversation = access.get_accessible(db, principal, conversation_id) if conversation_id is not None else None

    will_use_browser = kind == TaskKind.BROWSER or (kind == TaskKind.SHOPPING_LIST and bool(items))
    decision = evaluate_task(
        requester_role=access.enum_value(principal.role),
        kind=kind.value,
        amount_cents=amount_cents,
        will_use_browser=will_use_browser,
        household_settings=principal.household.settings,
        approvals_enabled=settings.enable_approvals,
    )
    if not decision.requires_approval and decision.approvals_enabled:
        advice = snap.decide(
            "approval_escalation",
            {  # minimal and synthetic: no titles, notes or names ever go to the model
                "task_kind": kind.value,
                "requester_role": access.enum_value(principal.role),
                "has_amount": bool(amount_cents),
                "uses_browser": will_use_browser,
            },
            _ESCALATION_CHOICES,
        )
        decision = merge_model_advice(decision, advice.choice if advice.status == "ok" else None)

    meta: dict[str, object] = {
        "items": items,
        "list_name": (list_name or "").strip().lower()[:MAX_LIST_NAME_CHARS] or None,
        "will_use_browser": will_use_browser,
        "source": source,
        "requester_may_approve": decision.requester_may_approve,
    }
    if will_use_browser:
        meta["browser_task_type"] = browser_task_type or "list_add_items"
    task = Task(
        household_id=principal.household.id,
        conversation_id=conversation.id if conversation else None,
        created_by_user_id=principal.user.id,
        requested_by_agent_id=principal.agent.id,
        title=title,
        description=description,
        kind=kind,
        status=TaskStatus.PENDING_APPROVAL if decision.requires_approval else TaskStatus.APPROVED,
        amount_cents=amount_cents,
        meta=meta,
        created_at=tick(),
    )
    db.add(task)
    db.flush()
    activity_service.record(
        db,
        household_id=task.household_id,
        event_type="task.created",
        summary=f"{principal.agent.name} created “{title[:80]}”.",
        actor_type="agent",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        task_id=task.id,
        conversation_id=task.conversation_id,
        payload={
            "kind": kind.value,
            "source": source,
            "policy": {
                "requires_approval": decision.requires_approval,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
                "source": decision.source,
                "approver_roles": list(decision.approver_roles),
            },
        },
    )
    approval = None
    if decision.requires_approval:
        approval = approval_service.request(db, principal, task, decision)
    elif kind in task_machine.RECORDING_KINDS:
        task_machine.transition(db, task, TaskStatus.COMPLETED, actor=None)
    return TaskCreateResult(task, approval, decision)


def list_tasks(
    db: Session, principal: Principal, *, status: TaskStatus | str | None = None, limit: int = 50
) -> list[Task]:
    """Household tasks, newest first."""
    stmt = select(Task).where(
        Task.household_id == principal.household.id,
        access.visible_conversation_clause(db, principal, Task.conversation_id),
    )
    if status is not None:
        stmt = stmt.where(Task.status == TaskStatus(status))
    return list(db.scalars(stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(max(1, min(limit, MAX_LIMIT)))))


def cancel(db: Session, principal: Principal, task_id: uuid.UUID | str, reason: str | None = None) -> Task:
    """Cancel a non-final task: the requester or any guardian/adult may. Cancels its approval and browser task too."""
    task = get(db, principal, task_id)
    if not (task.created_by_user_id == principal.user.id or principal.is_guardian_or_adult):
        raise Forbidden("Only the requester or a grown-up can cancel this task.")
    clean_reason = (reason or "").strip()[:500] or None
    transition(db, task, TaskStatus.CANCELLED, actor=principal, note=clean_reason)
    approval_service.cancel_pending(db, task)
    browser_task_service.cancel_active(db, task, principal)
    activity_service.record(
        db,
        household_id=task.household_id,
        event_type="task.cancelled",
        summary=f"{principal.user.display_name} cancelled “{task.title[:80]}”.",
        actor_type="user",
        actor_user_id=principal.user.id,
        task_id=task.id,
        conversation_id=task.conversation_id,
        payload={"reason": clean_reason},
    )
    return task
