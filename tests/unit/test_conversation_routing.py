"""Thread routing: new vs continue vs resume, Snap advice versus the deterministic fallback, and who may resume what."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.db.base import utcnow
from app.errors import NotFound, ValidationFailed
from app.models import (
    Conversation,
    ConversationParticipant,
    ConversationStatus,
    JoinedVia,
    ParticipantType,
    TranscriptProvider,
    Visibility,
)
from app.providers.subq.models import DecisionResult
from app.services import activity_service, conversation_service
from app.services.conversation_service import RouteResult
from tests.helpers_services import DEMO_SENTENCE, FakeSnap, real_snap

Seeded = dict[str, Principal]


def seeded_thread(db: Session) -> Conversation:
    """The seeded thread: the oldest one on this topic (a test may start a second thread with the same topic)."""
    return db.scalars(
        select(Conversation).where(Conversation.topic_key == "sunday-groceries").order_by(Conversation.created_at)
    ).first()  # type: ignore[return-value]


def participants_of(db: Session, conversation: Conversation) -> list[ConversationParticipant]:
    db.refresh(conversation)
    return list(conversation.participants)


def add_thread(db: Session, principal: Principal, title: str, topic_key: str, **fields: object) -> Conversation:
    conversation = Conversation(
        household_id=principal.household.id,
        title=title,
        topic_key=topic_key,
        created_by_user_id=principal.user.id,
        created_by_agent_id=principal.agent.id,
        transcript_provider=TranscriptProvider.LOCAL,
        thread_state={"summary": "", "items": [], "open_questions": [], "last_actor": principal.agent.name},
        last_message_at=utcnow(),
        **fields,
    )
    db.add(conversation)
    db.flush()
    db.add_all(
        [
            ConversationParticipant(
                conversation_id=conversation.id, participant_type=ParticipantType.USER, user_id=principal.user.id
            ),
            ConversationParticipant(
                conversation_id=conversation.id, participant_type=ParticipantType.AGENT, agent_id=principal.agent.id
            ),
        ]
    )
    db.commit()
    return conversation


def test_routes_demo_sentence_to_seeded_thread(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    result = conversation_service.route_message(db, alex, DEMO_SENTENCE, snap=real_snap())
    assert isinstance(result, RouteResult)
    assert result.action == "resumed"
    assert result.conversation.id == seeded_thread(db).id
    mine = [p for p in participants_of(db, result.conversation) if p.agent_id == alex.agent.id]
    assert len(mine) == 1 and mine[0].joined_via == JoinedVia.RESUMED
    assert result.decision.source == "fallback"
    assert result.decision.question_id == "thread_routing"


def test_resume_is_recorded_as_activity_and_is_idempotent(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    conversation_service.route_message(db, alex, DEMO_SENTENCE, snap=real_snap())
    again = conversation_service.route_message(db, alex, DEMO_SENTENCE, snap=real_snap())
    assert again.action == "continued"
    assert len([p for p in participants_of(db, again.conversation) if p.agent_id == alex.agent.id]) == 1
    kinds = [e.event_type for e in activity_service.list_events(db, alex)]
    assert kinds.count("conversation.resumed") == 1
    assert kinds.count("message.routed") == 2


def test_owner_continues_instead_of_resuming(db: Session, seeded: Seeded) -> None:
    result = conversation_service.route_message(db, seeded["katie"], "add butter to sunday groceries", snap=real_snap())
    assert result.action == "continued"
    assert result.conversation.id == seeded_thread(db).id


def test_unrelated_request_starts_new_thread(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    result = conversation_service.route_message(db, alex, "remind me to call grandma", snap=real_snap())
    assert result.action == "started"
    conversation = result.conversation
    assert conversation.id != seeded_thread(db).id
    assert conversation.topic_key != "sunday-groceries"
    assert conversation.created_by_user_id == alex.user.id
    assert conversation.created_by_agent_id == alex.agent.id
    assert conversation.visibility == Visibility.HOUSEHOLD
    assert conversation.transcript_provider == TranscriptProvider.LOCAL
    people = {(p.participant_type, p.user_id or p.agent_id) for p in participants_of(db, conversation)}
    assert people == {(ParticipantType.USER, alex.user.id), (ParticipantType.AGENT, alex.agent.id)}


def test_new_list_thread_is_named_after_the_list(db: Session, seeded: Seeded) -> None:
    result = conversation_service.route_message(db, seeded["alex"], "add rope to garage supplies", snap=real_snap())
    assert result.action == "started"
    assert result.conversation.topic_key == "garage-supplies"
    assert result.conversation.title == "Garage supplies"


def test_partial_list_name_overlap_at_half_continues(db: Session, seeded: Seeded) -> None:
    result = conversation_service.route_message(db, seeded["alex"], "add stuff to groceries", snap=real_snap())
    assert result.action == "resumed"  # {groceries} vs {sunday, groceries}: Jaccard 0.5
    assert result.conversation.id == seeded_thread(db).id


def test_child_cannot_resume_adult_thread(db: Session, seeded: Seeded) -> None:
    with pytest.raises(NotFound):
        conversation_service.route_message(
            db, seeded["max"], DEMO_SENTENCE, conversation_id=seeded_thread(db).id, snap=real_snap()
        )
    with pytest.raises(NotFound):
        conversation_service.get_for(db, seeded["max"], seeded_thread(db).id)


def test_child_never_routes_into_adult_threads_and_starts_their_own(db: Session, seeded: Seeded) -> None:
    max_ = seeded["max"]
    result = conversation_service.route_message(db, max_, DEMO_SENTENCE, snap=real_snap())
    assert result.action == "started"
    assert result.conversation.id != seeded_thread(db).id
    assert [c.id for c in conversation_service.list_for(db, max_)] == [result.conversation.id]


def test_child_can_resume_a_thread_they_participate_in(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    db.add(
        ConversationParticipant(
            conversation_id=thread.id, participant_type=ParticipantType.USER, user_id=seeded["max"].user.id
        )
    )
    db.commit()
    result = conversation_service.route_message(
        db, seeded["max"], "add milk to sunday groceries", conversation_id=thread.id, snap=real_snap()
    )
    assert result.conversation.id == thread.id


def test_explicit_conversation_id_resumes_that_thread(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    result = conversation_service.route_message(
        db, seeded["alex"], "remind me to call grandma", conversation_id=thread.id, snap=real_snap()
    )
    assert result.conversation.id == thread.id and result.action == "resumed"
    assert result.decision.status == "ok" and result.decision.source == "fallback"
    assert result.decision.choice == "explicit_thread"


def test_unknown_conversation_id_is_not_found(db: Session, seeded: Seeded) -> None:
    with pytest.raises(NotFound):
        conversation_service.route_message(db, seeded["alex"], "hi", conversation_id=uuid.uuid4(), snap=real_snap())


def test_archived_threads_are_not_routing_candidates(db: Session, seeded: Seeded) -> None:
    seeded_thread(db).status = ConversationStatus.ARCHIVED
    db.commit()
    result = conversation_service.route_message(db, seeded["alex"], DEMO_SENTENCE, snap=real_snap())
    assert result.action == "started"
    assert result.conversation.topic_key == "sunday-groceries"  # same list name, but a fresh thread


def test_snap_is_offered_authorized_candidates_and_new_thread(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    snap = FakeSnap({"thread_routing": "continue_1"})
    result = conversation_service.route_message(db, seeded["alex"], "remind me to call grandma", snap=snap)
    (_, state, choices) = snap.asked("thread_routing")[0]
    assert set(choices) == {
        "continue_1",
        "continue_2",
        "new_thread",
    }  # option keys, not ids: nothing real goes over the wire
    assert state["open_threads"] == [
        {"option": "continue_1", "title": "Sunday groceries", "topic": "sunday-groceries"},
        {"option": "continue_2", "title": "Katie's birthday dinner", "topic": "katies-birthday-dinner"},
    ]
    assert str(thread.id) not in str(state) + str(choices)
    assert "grandma" in state["new_request"]
    assert result.action == "resumed" and result.conversation.id == thread.id
    assert result.decision.source == "snap"


def test_snap_can_choose_new_thread_over_the_keyword_match(db: Session, seeded: Seeded) -> None:
    snap = FakeSnap({"thread_routing": "new_thread"})
    result = conversation_service.route_message(db, seeded["alex"], DEMO_SENTENCE, snap=snap)
    assert result.action == "started"
    assert result.decision.source == "snap" and result.decision.choice == "new_thread"


@pytest.mark.parametrize(
    "answer",
    [
        DecisionResult.fallback("thread_routing", "unavailable", "down"),
        DecisionResult.fallback("thread_routing", "malformed", "bad json"),
        "continue_9",  # not one of the offered options
        "something_else",
    ],
)
def test_snap_failure_or_nonsense_uses_the_deterministic_fallback(db: Session, seeded: Seeded, answer: object) -> None:
    result = conversation_service.route_message(
        db, seeded["alex"], DEMO_SENTENCE, snap=FakeSnap({"thread_routing": answer})
    )
    assert result.action == "resumed" and result.conversation.id == seeded_thread(db).id
    assert result.decision.source == "fallback"


def test_snap_cannot_route_a_child_into_an_adult_thread(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    snap = FakeSnap({"thread_routing": "continue_1"})  # there is nothing to continue for max: never asked
    result = conversation_service.route_message(db, seeded["max"], DEMO_SENTENCE, snap=snap)
    assert result.action == "started" and result.conversation.id != thread.id
    assert snap.asked("thread_routing") == []  # no authorized candidates, so nothing to ask


def test_at_most_five_candidates_newest_first(db: Session, seeded: Seeded) -> None:
    alex = seeded["alex"]
    now = utcnow()
    for index in range(7):
        thread = add_thread(db, alex, f"Topic {index}", f"topic-{index}")
        thread.last_message_at = now + timedelta(minutes=index)
    db.commit()
    snap = FakeSnap({"thread_routing": "new_thread"})
    conversation_service.route_message(db, alex, "something unrelated entirely", snap=snap)
    (_, state, choices) = snap.asked("thread_routing")[0]
    topics = [t["topic"] for t in state["open_threads"]]
    assert len(choices) == 6 and topics == [f"topic-{i}" for i in (6, 5, 4, 3, 2)]


def test_list_and_get_are_household_and_policy_scoped(db: Session, seeded: Seeded) -> None:
    thread = seeded_thread(db)
    assert thread.id in [c.id for c in conversation_service.list_for(db, seeded["alex"])]
    assert len(conversation_service.list_for(db, seeded["alex"])) == 2
    assert conversation_service.get_for(db, seeded["alex"], thread.id).id == thread.id
    assert conversation_service.list_for(db, seeded["max"]) == []


def test_start_requires_a_title(db: Session, seeded: Seeded) -> None:
    with pytest.raises(ValidationFailed):
        conversation_service.start(db, seeded["alex"], "   ", "x")
    started = conversation_service.start(db, seeded["alex"], "Camping trip", "camping-trip")
    assert started.thread_state["items"] == [] and started.thread_state_version == 1
