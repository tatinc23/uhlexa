"""Conversations: routing a request to the right thread, resuming another agent's thread, the transcript, and the
end-to-end message flow shared by REST and MCP (spec sections 10, 11 and 13)."""

import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.errors import ProviderUnavailable, ValidationFailed
from app.models import (
    Agent,
    Conversation,
    ConversationParticipant,
    ConversationStatus,
    HouseholdMembership,
    JoinedVia,
    Message,
    ParticipantType,
    SenderType,
    TaskKind,
    TranscriptProvider,
    User,
    Visibility,
)
from app.providers.primitive.errors import PrimitiveRejection
from app.providers.primitive.service import PrimitiveService
from app.providers.subq.models import DecisionResult
from app.providers.subq.service import SnapService
from app.schemas.conversations import MAX_MESSAGE_CHARS, ContextOut, MessageResultOut, RoutingOut
from app.services import access, activity_service, approval_service, task_service, thread_state_service, views
from app.services.clock import tick
from app.services.intent import Intent, parse_intent

ROUTING_QUESTION = "thread_routing"
CONTEXT_QUESTION = "context_sufficiency"
NEW_THREAD = "new_thread"
EXPLICIT_THREAD = "explicit_thread"
RECENT_CONTEXT = "recent_context_enough"
FULL_TRANSCRIPT = "need_full_transcript"
MAX_CANDIDATES = 5
MATCH_THRESHOLD = 0.5
RECENT_WINDOW = 6
SNAP_REQUEST_CHARS = 300
MAX_TITLE_CHARS = 200
NEW_TITLE_CHARS = 60

STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "me",
        "my",
        "our",
        "we",
        "i",
        "and",
        "or",
        "for",
        "of",
        "on",
        "in",
        "at",
        "with",
        "is",
        "it",
        "please",
        "uhlexa",
        "add",
        "put",
        "remind",
        "buy",
        "order",
        "book",
        "get",
        "do",
        "what",
        "list",
    }
)

Action = Literal["continued", "resumed", "started"]


@dataclass(frozen=True)
class RouteResult:
    conversation: Conversation
    action: Action
    decision: DecisionResult


# --- reading --------------------------------------------------------------------------------------------------------


def list_for(db: Session, principal: Principal) -> list[Conversation]:
    """Conversations this caller may read, newest activity first."""
    return access.accessible_conversations(db, principal)


def get_for(db: Session, principal: Principal, conversation_id: uuid.UUID | str) -> Conversation:
    return access.get_accessible(db, principal, conversation_id)


def _agent_name(db: Session, agent_id: uuid.UUID | None) -> str | None:
    return db.scalars(select(Agent.name).where(Agent.id == agent_id)).first() if agent_id else None


def household_addresses(db: Session, household_id: uuid.UUID) -> list[str]:
    """The email addresses of the household's members: the only senders Primitive may answer (it fails closed when
    this is empty, so a thread nobody in the household has an address on can never email an outsider)."""
    rows = db.scalars(
        select(User.email)
        .join(HouseholdMembership, HouseholdMembership.user_id == User.id)
        .where(HouseholdMembership.household_id == household_id, User.email.is_not(None))
    )
    return sorted({email.strip() for email in rows if email and email.strip()})


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:120].strip("-")


# --- starting, resuming, posting ------------------------------------------------------------------------------------


def start(db: Session, principal: Principal, title: str, topic_key: str) -> Conversation:
    title = " ".join((title or "").split())[:MAX_TITLE_CHARS]
    if not title:
        raise ValidationFailed("A conversation needs a title.")
    now = tick()
    conversation = Conversation(
        household_id=principal.household.id,
        title=title,
        topic_key=_slug(topic_key) or _slug(title) or "general",
        visibility=Visibility.HOUSEHOLD,
        status=ConversationStatus.OPEN,
        created_by_user_id=principal.user.id,
        created_by_agent_id=principal.agent.id,
        transcript_provider=TranscriptProvider.LOCAL,  # a Primitive thread only exists once real email flows
        thread_state=thread_state_service.new_state(principal.agent.name),
        thread_state_version=1,
        last_message_at=now,
        created_at=now,
    )
    conversation.participants = [
        ConversationParticipant(
            participant_type=ParticipantType.USER,
            user_id=principal.user.id,
            joined_via=JoinedVia.CREATED,
            joined_at=now,
        ),
        ConversationParticipant(
            participant_type=ParticipantType.AGENT,
            agent_id=principal.agent.id,
            joined_via=JoinedVia.CREATED,
            joined_at=tick(),
        ),
    ]
    db.add(conversation)
    db.flush()
    activity_service.record(
        db,
        household_id=conversation.household_id,
        event_type="conversation.started",
        summary=f"{principal.agent.name} started “{title}”.",
        actor_type="agent",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        conversation_id=conversation.id,
        payload={"topic_key": conversation.topic_key},
    )
    return conversation


def resume(db: Session, principal: Principal, conversation: Conversation) -> bool:
    """The caller's own agent joins the thread (`joined_via=resumed`). Idempotent; returns True when it just joined.
    The caller must already have passed the access check."""
    if access.has_agent_joined(principal, conversation):
        return False
    conversation.participants.append(
        ConversationParticipant(
            participant_type=ParticipantType.AGENT,
            agent_id=principal.agent.id,
            joined_via=JoinedVia.RESUMED,
            joined_at=tick(),
        )
    )
    db.flush()
    started_by = _agent_name(db, conversation.created_by_agent_id) or "someone"
    activity_service.record(
        db,
        household_id=conversation.household_id,
        event_type="conversation.resumed",
        summary=f"{principal.agent.name} resumed “{conversation.title}”, started by {started_by}.",
        actor_type="agent",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        conversation_id=conversation.id,
        payload={"resumed_from_agent_id": str(conversation.created_by_agent_id)},
    )
    return True


def post_message(
    db: Session,
    conversation: Conversation,
    *,
    sender_type: SenderType | str,
    body: str,
    sender_user_id: uuid.UUID | None = None,
    sender_agent_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    primitive: PrimitiveService,
) -> Message:
    """Append to the transcript. Agent replies on a Primitive-backed thread are sent through Primitive and mirrored
    here; if Primitive refuses or is down the reply is kept locally and the outage is logged, never reported as sent."""
    sender = SenderType(sender_type)
    message_id = uuid.uuid4()  # also the Idempotency-Key: a retried send can never email the same reply twice
    source, primitive_message_id = TranscriptProvider.LOCAL, None
    on_primitive = (
        sender == SenderType.AGENT
        and conversation.transcript_provider == TranscriptProvider.PRIMITIVE
        and bool(conversation.primitive_thread_id)
    )
    if on_primitive:
        label = _agent_name(db, sender_agent_id) or "Uhlexa"
        try:
            reply = primitive.reply_to_thread(
                conversation.primitive_thread_id,  # type: ignore[arg-type]
                body,
                sender_label=label,
                idempotency_key=str(message_id),
                allowed_senders=household_addresses(db, conversation.household_id),
            )
        except PrimitiveRejection as exc:  # Primitive, or our own pre-flight check, said no: nothing was sent
            _report_primitive(
                db, conversation, "Primitive did not send the reply; it stays in the local transcript.", exc.reason
            )
        except ProviderUnavailable as exc:
            _report_primitive(
                db, conversation, "Primitive is unavailable; the reply stays in the local transcript.", exc.reason
            )
        else:
            if reply.sent:
                source, primitive_message_id = TranscriptProvider.PRIMITIVE, reply.id
            else:
                _report_primitive(
                    db, conversation, "Primitive did not send the reply; it stays in the local transcript.", "not sent"
                )
    now = tick()
    message = Message(
        id=message_id,
        conversation_id=conversation.id,
        sender_type=sender,
        sender_user_id=sender_user_id,
        sender_agent_id=sender_agent_id,
        body=body,
        task_id=task_id,
        primitive_message_id=primitive_message_id,
        source=source,
        created_at=now,
    )
    db.add(message)
    conversation.last_message_at = now
    db.flush()
    return message


def _report_primitive(db: Session, conversation: Conversation, summary: str, reason: str) -> None:
    activity_service.record(
        db,
        household_id=conversation.household_id,
        event_type="provider.unavailable",
        actor_type="provider",
        conversation_id=conversation.id,
        summary=summary,
        payload={"provider": "primitive", "operation": "reply_to_thread", "reason": reason[:200]},
    )


# --- routing --------------------------------------------------------------------------------------------------------


def _topic_tokens(intent: Intent) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (intent.list_name or intent.title).lower()) if t not in STOPWORDS}


def _fallback_choice(intent: Intent, options: dict[str, Conversation]) -> tuple[str, str]:
    """Best topic-word overlap (Jaccard) >= 0.5 continues that thread; otherwise a new one. Returns (key, detail)."""
    wanted = _topic_tokens(intent)
    best_key, best_score = NEW_THREAD, 0.0
    for key, conversation in options.items():  # newest first, so ties go to the most recent thread
        topic = set(conversation.topic_key.split("-"))
        union = wanted | topic
        score = len(wanted & topic) / len(union) if union else 0.0
        if score > best_score:
            best_key, best_score = key, score
    if best_score >= MATCH_THRESHOLD:
        return best_key, f"topic words match “{options[best_key].topic_key}” ({best_score:.2f})"
    return NEW_THREAD, "no open thread shares enough topic words" if options else "no open threads to continue"


def _new_thread_names(intent: Intent) -> tuple[str, str]:
    if intent.list_name:
        return intent.list_name.capitalize(), _slug(intent.list_name)
    if intent.kind == TaskKind.SHOPPING_LIST:
        return "Shopping list", "shopping-list"
    words = [t for t in re.findall(r"[a-z0-9]+", intent.title.lower()) if t not in STOPWORDS]
    return intent.title[:NEW_TITLE_CHARS], _slug(" ".join(words[:4])) or "general"


def route_message(
    db: Session,
    principal: Principal,
    text: str,
    *,
    conversation_id: uuid.UUID | str | None = None,
    snap: SnapService,
    intent: Intent | None = None,
) -> RouteResult:
    """Pick the thread for a request: continue one the caller's agent is in, resume another agent's household thread,
    or start a new one. Snap advises; the deterministic rules cover every failure; access rules always apply."""
    intent = intent or parse_intent(text)
    if conversation_id is not None:
        conversation = access.get_accessible(db, principal, conversation_id)
        decision = DecisionResult(
            ROUTING_QUESTION, EXPLICIT_THREAD, None, {}, "fallback", "ok", "Conversation chosen by the caller."
        )
        return _enter(db, principal, conversation, decision)

    candidates = access.accessible_conversations(db, principal, open_only=True)[:MAX_CANDIDATES]
    options = {f"continue_{i}": c for i, c in enumerate(candidates, start=1)}
    advice: DecisionResult | None = None
    if options:
        choices = {key: f"Continue the thread “{c.title}” (topic: {c.topic_key})." for key, c in options.items()}
        choices[NEW_THREAD] = "Start a new thread for this request."
        state = {
            "new_request": " ".join(text.split())[:SNAP_REQUEST_CHARS],
            "open_threads": [{"option": key, "title": c.title, "topic": c.topic_key} for key, c in options.items()],
        }
        advice = snap.decide(ROUTING_QUESTION, state, choices)

    if advice is not None and advice.status == "ok" and advice.choice in (*options, NEW_THREAD):
        key, decision = str(advice.choice), advice
    else:
        key, why = _fallback_choice(intent, options)
        snap_note = f" Snap: {advice.status}." if advice is not None else ""
        decision = DecisionResult(ROUTING_QUESTION, key, None, {}, "fallback", "ok", f"Rules: {why}.{snap_note}")

    if key in options:
        return _enter(db, principal, options[key], decision)
    title, topic_key = _new_thread_names(intent)
    conversation = start(db, principal, title, topic_key)
    return _routed(db, principal, RouteResult(conversation, "started", decision))


def _enter(db: Session, principal: Principal, conversation: Conversation, decision: DecisionResult) -> RouteResult:
    action: Action = "resumed" if resume(db, principal, conversation) else "continued"
    return _routed(db, principal, RouteResult(conversation, action, decision))


def _routed(db: Session, principal: Principal, result: RouteResult) -> RouteResult:
    conversation = result.conversation
    activity_service.record(
        db,
        household_id=conversation.household_id,
        event_type="message.routed",
        summary=f"{principal.agent.name} {result.action} “{conversation.title}”.",
        actor_type="agent",
        actor_user_id=principal.user.id,
        actor_agent_id=principal.agent.id,
        conversation_id=conversation.id,
        payload={"action": result.action, "decision": views.decision_out(result.decision).model_dump(mode="json")},
    )
    return result


# --- the whole flow -------------------------------------------------------------------------------------------------


def _decide_context(snap: SnapService, state: dict[str, Any], prior_messages: int) -> tuple[str, DecisionResult]:
    """Is the short state enough to act on, or should the agent read the full transcript first?"""
    choices = {
        RECENT_CONTEXT: "The thread summary and recent messages are enough to act on this request.",
        FULL_TRANSCRIPT: "The whole conversation has to be read before acting.",
    }
    advice = snap.decide(
        CONTEXT_QUESTION,
        {  # counts only: no thread text leaves the server
            "item_count": len(state.get("items", [])),
            "has_summary": bool(state.get("summary")),
            "message_count": min(prior_messages, 100),
        },
        choices,
    )
    if advice.status == "ok" and advice.choice in choices:
        return str(advice.choice), advice
    key = RECENT_CONTEXT if state.get("items") else FULL_TRANSCRIPT
    return key, DecisionResult(
        CONTEXT_QUESTION, key, None, {}, "fallback", "ok", "Rules: recent context is enough when the thread has items."
    )


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _compose_reply(
    *,
    route: RouteResult,
    creator_agent: str | None,
    result: task_service.TaskCreateResult,
) -> str:
    """The agent's answer, built from facts (never model text) so it cannot promise what did not happen."""
    task, parts = result.task, []
    title = route.conversation.title  # a thread is one list: "the Sunday groceries list"
    if route.action == "resumed":
        who = f" that {creator_agent} started" if creator_agent else ""
        parts.append(f"I picked up the {title} thread{who}, so everything stays in one place.")
    elif route.action == "started":
        parts.append(f"I started a new thread, “{title}”.")
    items: list[str] = task.meta.get("items", [])
    if task.kind == TaskKind.SHOPPING_LIST and items:
        parts.append(f"I noted {_join(items)} on the {title} list.")
    elif task.kind == TaskKind.SHOPPING_LIST:
        parts.append(f"Which items should I add to the {title} list?")
    elif task.kind == TaskKind.BROWSER:
        parts.append("I set up the browser check you asked for.")
    elif task.kind in (TaskKind.PURCHASE, TaskKind.BOOKING):
        parts.append(f"I wrote down the {task.kind.value}, but I can't place orders or bookings on my own yet.")
    else:
        parts.append("Got it, I noted that.")
    if result.approval is not None:
        parts.append(f"I need approval first: {result.decision.reason}")
    return " ".join(parts)


def handle_message(
    db: Session,
    principal: Principal,
    text: str,
    *,
    conversation_id: uuid.UUID | str | None = None,
    source: str,
    snap: SnapService,
    primitive: PrimitiveService,
) -> dict[str, Any]:
    """One request, start to finish: route -> post -> read state -> parse -> create task (+approval) -> update the
    thread state -> agent reply. Flushes only; the caller commits. Returns the `MessageResultOut` as JSON-ready data."""
    body = (text or "").strip()
    if not body:
        raise ValidationFailed("Say something first.")
    if len(body) > MAX_MESSAGE_CHARS:
        raise ValidationFailed(f"Messages can be at most {MAX_MESSAGE_CHARS} characters.")
    intent = parse_intent(body)  # raises before any write if the request is unusable

    route = route_message(db, principal, body, conversation_id=conversation_id, snap=snap, intent=intent)
    conversation = route.conversation
    prior = db.scalar(select(func.count(Message.id)).where(Message.conversation_id == conversation.id)) or 0
    state = thread_state_service.load(db, conversation, primitive=primitive)
    context_mode, context_decision = None, None
    if route.action != "started":  # a brand-new thread has no history to judge
        context_mode, context_decision = _decide_context(snap, state, prior)

    user_message = post_message(
        db, conversation, sender_type="user", body=body, sender_user_id=principal.user.id, primitive=primitive
    )
    result = task_service.create(
        db,
        principal,
        title=intent.title,
        kind=intent.kind,
        items=intent.items,
        description=body,
        amount_cents=intent.amount_cents,
        conversation_id=conversation.id,
        source=source,
        snap=snap,
        list_name=intent.list_name,
        browser_task_type=intent.browser_task_type,
    )
    task = result.task
    user_message.task_id = task.id
    if task.kind == TaskKind.SHOPPING_LIST and task.meta["items"]:
        thread_state_service.save(
            db,
            conversation,
            thread_state_service.apply_items(state, task.meta["items"], principal.agent.name),
            primitive=primitive,
        )

    reply = _compose_reply(
        route=route,
        creator_agent=_agent_name(db, conversation.created_by_agent_id),
        result=result,
    )
    agent_message = post_message(
        db,
        conversation,
        sender_type="agent",
        body=reply,
        sender_agent_id=principal.agent.id,
        task_id=task.id,
        primitive=primitive,
    )
    context = None
    if context_decision is not None and context_mode is not None:
        window = prior if context_mode == FULL_TRANSCRIPT else min(prior, RECENT_WINDOW)
        context = ContextOut(
            mode="full_transcript" if context_mode == FULL_TRANSCRIPT else "recent",
            messages_considered=window,
            state_version=conversation.thread_state_version,
            decision=views.decision_out(context_decision),
        )
    first, second = views.message_outs(db, [user_message, agent_message])
    return MessageResultOut(
        conversation=views.conversation_out(db, conversation),
        routing=RoutingOut(
            action=route.action,
            thread_id=conversation.id,
            decision=views.decision_out(route.decision),
            context=context,
        ),
        user_message=first,
        agent_message=second,
        task=views.task_out(task),
        approval=approval_service.view_for(db, principal, result.approval) if result.approval else None,
    ).model_dump(mode="json")
