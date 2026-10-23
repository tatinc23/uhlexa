"""Row -> wire-schema builders. They need the database (names) and the viewer (`can_decide`, takeover link), which is
why they live with the services rather than in `app.schemas`. Shared by REST, MCP and the Family Board."""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.models import (
    Agent,
    Approval,
    BrowserTask,
    Conversation,
    DecisionSource,
    Message,
    SenderType,
    Task,
    TranscriptProvider,
    User,
)
from app.policy import PolicyDecision
from app.providers.subq.models import DecisionResult
from app.schemas.activity import ActivityEventOut
from app.schemas.approvals import ApprovalOut, ApprovalTaskOut, PolicyDecisionOut
from app.schemas.browser_tasks import BrowserStepOut, BrowserTaskOut
from app.schemas.conversations import ConversationOut, DecisionOut, MessageOut, ParticipantOut
from app.schemas.tasks import TaskOut

LOCAL_TRANSCRIPT_LABEL = "Local transcript — Primitive not connected"
PRIMITIVE_TRANSCRIPT_LABEL = "Primitive transcript"
SCREENSHOT_KEY = "screenshot_b64"  # inside BrowserTask.result; never put on the wire


def screenshot_url(browser_task_id: uuid.UUID) -> str:
    return f"/api/browser-tasks/{browser_task_id}/screenshot"


def names(
    db: Session, user_ids: Iterable[uuid.UUID | None], agent_ids: Iterable[uuid.UUID | None]
) -> tuple[dict, dict]:
    """(user id -> display name, agent id -> agent name) for the ids given."""
    users = {i for i in user_ids if i is not None}
    agents = {i for i in agent_ids if i is not None}
    user_names = (
        {i: n for i, n in db.execute(select(User.id, User.display_name).where(User.id.in_(users)))} if users else {}
    )
    agent_names = (
        {i: n for i, n in db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agents)))} if agents else {}
    )
    return user_names, agent_names


def decision_out(result: DecisionResult) -> DecisionOut:
    return DecisionOut(
        question_id=result.question_id,
        choice=result.choice,
        source=result.source,
        status=result.status,
        detail=result.detail,
        confidence=result.confidence,
        ai_assisted=result.source == "snap",
    )


def policy_decision_out(decision: PolicyDecision) -> PolicyDecisionOut:
    return PolicyDecisionOut(
        requires_approval=decision.requires_approval,
        rule_id=decision.rule_id,
        reason=decision.reason,
        approver_roles=list(decision.approver_roles),
        requester_may_approve=decision.requester_may_approve,
        source=decision.source,
    )


def task_out(task: Task) -> TaskOut:
    return TaskOut.model_validate(task)


def activity_out(event: Any) -> ActivityEventOut:
    return ActivityEventOut.model_validate(event)


def message_outs(db: Session, messages: Iterable[Message]) -> list[MessageOut]:
    rows = list(messages)
    user_names, agent_names = names(db, (m.sender_user_id for m in rows), (m.sender_agent_id for m in rows))
    out = []
    for m in rows:
        if m.sender_type == SenderType.AGENT:
            sender = agent_names.get(m.sender_agent_id, "Uhlexa")
        elif m.sender_type == SenderType.USER:
            sender = user_names.get(m.sender_user_id, "Someone")
        else:
            sender = "Uhlexa"
        out.append(
            MessageOut(
                id=m.id,
                conversation_id=m.conversation_id,
                sender_type=m.sender_type,
                sender_user_id=m.sender_user_id,
                sender_agent_id=m.sender_agent_id,
                sender_name=sender,
                body=m.body,
                task_id=m.task_id,
                source=m.source,
                created_at=m.created_at,
            )
        )
    return out


def conversation_outs(db: Session, conversations: Iterable[Conversation]) -> list[ConversationOut]:
    rows = list(conversations)
    user_ids = [p.user_id for c in rows for p in c.participants] + [c.created_by_user_id for c in rows]
    agent_ids = [p.agent_id for c in rows for p in c.participants] + [c.created_by_agent_id for c in rows]
    user_names, agent_names = names(db, user_ids, agent_ids)
    out = []
    for c in rows:
        participants = [
            ParticipantOut(
                participant_type=p.participant_type,
                user_id=p.user_id,
                agent_id=p.agent_id,
                name=user_names.get(p.user_id, "Someone") if p.user_id else agent_names.get(p.agent_id, "Uhlexa"),
                joined_via=p.joined_via,
                joined_at=p.joined_at,
            )
            for p in c.participants
        ]
        is_local = c.transcript_provider == TranscriptProvider.LOCAL
        out.append(
            ConversationOut(
                id=c.id,
                title=c.title,
                topic_key=c.topic_key,
                visibility=c.visibility,
                status=c.status,
                transcript_provider=c.transcript_provider,
                transcript_label=LOCAL_TRANSCRIPT_LABEL if is_local else PRIMITIVE_TRANSCRIPT_LABEL,
                created_by_user_id=c.created_by_user_id,
                created_by_agent_id=c.created_by_agent_id,
                created_by_agent_name=agent_names.get(c.created_by_agent_id),
                participants=participants,
                resumed_by=[p.name for p in participants if p.agent_id and p.joined_via.value == "resumed"],
                thread_state=dict(c.thread_state or {}),
                thread_state_version=c.thread_state_version,
                last_message_at=c.last_message_at,
                created_at=c.created_at,
            )
        )
    return out


def conversation_out(db: Session, conversation: Conversation) -> ConversationOut:
    return conversation_outs(db, [conversation])[0]


def approval_outs(db: Session, approvals: Iterable[Approval], decide_check: Any) -> list[ApprovalOut]:
    """`decide_check(approval, task) -> (allowed, reason)` keeps the policy call in approval_service."""
    rows = list(approvals)
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_({a.task_id for a in rows})))} if rows else {}
    user_names, _ = names(db, [a.requested_for_user_id for a in rows] + [a.decided_by_user_id for a in rows], [])
    out = []
    for a in rows:
        task = tasks[a.task_id]
        allowed, reason = decide_check(a, task)
        out.append(
            ApprovalOut(
                id=a.id,
                task_id=a.task_id,
                task=ApprovalTaskOut(
                    id=task.id,
                    title=task.title,
                    kind=task.kind,
                    amount_cents=task.amount_cents,
                    items=list(task.meta.get("items", [])),
                    will_use_browser=bool(task.meta.get("will_use_browser")),
                ),
                status=a.status,
                rule_id=a.rule_id,
                reason=a.reason,
                decision_source=a.decision_source,
                ai_assisted=a.decision_source == DecisionSource.POLICY_SNAP,
                requested_for_user_id=a.requested_for_user_id,
                requested_for_name=user_names.get(a.requested_for_user_id),
                requested_by_agent_id=a.requested_by_agent_id,
                decided_by_user_id=a.decided_by_user_id,
                decided_by_name=user_names.get(a.decided_by_user_id),
                decision_note=a.decision_note,
                decided_at=a.decided_at,
                created_at=a.created_at,
                can_decide=allowed,
                can_decide_reason=reason,
            )
        )
    return out


def browser_task_out(bt: BrowserTask, principal: Principal, *, include_result: bool = True) -> BrowserTaskOut:
    """The takeover link is a secret: only the owning user sees it, and only while a takeover is pending.

    `include_result=False` never touches `bt.result` (list queries defer it: it can hold a screenshot)."""
    is_owner = bt.user_id == principal.user.id
    return BrowserTaskOut(
        id=bt.id,
        task_id=bt.task_id,
        conversation_id=bt.conversation_id,
        user_id=bt.user_id,
        agent_id=bt.agent_id,
        task_type=bt.task_type,
        requested_url=bt.requested_url,
        status=bt.status,
        requires_takeover=bt.requires_takeover,
        takeover_url=bt.takeover_url if is_owner and bt.requires_takeover else None,
        last_screenshot_ref=bt.last_screenshot_ref,
        last_error=bt.last_error,
        steps=[BrowserStepOut(**step) for step in bt.steps or []],
        verified=bt.verified,
        result={k: v for k, v in (bt.result or {}).items() if k != SCREENSHOT_KEY} if include_result else {},
        is_owner=is_owner,
        created_at=bt.created_at,
        updated_at=bt.updated_at,
    )
