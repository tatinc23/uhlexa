import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import (
    Agent,
    ApprovalStatus,
    BrowserTask,
    BrowserTaskStatus,
    Conversation,
    ConversationParticipant,
    Household,
    HouseholdMembership,
    Job,
    JobStatus,
    JoinedVia,
    MembershipRole,
    Message,
    ParticipantType,
    Task,
    TaskKind,
    TaskStatus,
    User,
)


def _household(db: Session) -> tuple[Household, User, Agent]:
    household = Household(name="The Parker Family", slug="parker")
    user = User(handle="alex", display_name="Alex Parker", is_demo=True)
    db.add_all([household, user])
    db.flush()
    agent = Agent(household_id=household.id, owner_user_id=user.id, name="Uhlexa", is_default=True)
    db.add_all([HouseholdMembership(household_id=household.id, user_id=user.id, role=MembershipRole.GUARDIAN), agent])
    db.flush()
    return household, user, agent


def test_fifteen_tables_registered() -> None:
    assert len(Base.metadata.tables) == 15


def test_enums_are_stored_as_their_string_values(db: Session) -> None:
    household, user, _ = _household(db)
    db.commit()
    assert db.execute(text("SELECT role FROM household_memberships")).scalar_one() == "guardian"
    membership = db.scalars(select(HouseholdMembership)).one()
    assert membership.role is MembershipRole.GUARDIAN and membership.role == "guardian"
    assert (household.slug, user.handle) == ("parker", "alex")


def test_plain_string_assignment_is_accepted_and_validated(db: Session) -> None:
    household, user, _ = _household(db)
    task = Task(
        household_id=household.id, created_by_user_id=user.id, title="t", kind="shopping_list", status="approved"
    )
    db.add(task)
    db.flush()
    assert task.status == TaskStatus.APPROVED
    task.status = "not-a-status"
    with pytest.raises(StatementError, match="not among the defined enum values"):
        db.flush()


def test_datetimes_round_trip_as_utc_aware(db: Session) -> None:
    household, _, _ = _household(db)
    db.commit()
    db.refresh(household)
    assert household.created_at.tzinfo is not None
    assert household.created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    job = Job(kind="x", run_after=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    db.add(job)
    db.commit()
    db.refresh(job)
    assert job.run_after == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_naive_datetime_is_rejected(db: Session) -> None:
    db.add(Job(kind="x", run_after=datetime(2026, 1, 1, 12, 0)))
    with pytest.raises(Exception, match="naive"):
        db.flush()


def test_defaults(db: Session) -> None:
    household, user, agent = _household(db)
    conversation = Conversation(household_id=household.id, title="Sunday groceries", topic_key="sunday-groceries")
    job = Job(kind="browser_task_run")
    db.add_all([conversation, job])
    db.flush()
    assert household.settings["adult_spend_approval_threshold_cents"] == 5000
    assert household.timezone == "UTC"
    assert (conversation.thread_state, conversation.thread_state_version) == ({}, 1)
    assert conversation.transcript_provider == "local" and conversation.visibility == "household"
    assert conversation.status == "open"
    assert (job.status, job.attempts, job.max_attempts) == (JobStatus.QUEUED, 0, 3)
    assert agent.status == "active" and agent.config == {}


def test_participant_needs_exactly_one_identity(db: Session) -> None:
    household, user, agent = _household(db)
    conversation = Conversation(household_id=household.id, title="c", topic_key="c")
    db.add(conversation)
    db.flush()
    nested = db.begin_nested()
    db.add(ConversationParticipant(conversation_id=conversation.id, participant_type=ParticipantType.USER))
    with pytest.raises(IntegrityError):
        db.flush()
    nested.rollback()
    nested = db.begin_nested()
    db.add(
        ConversationParticipant(
            conversation_id=conversation.id, participant_type=ParticipantType.USER, user_id=user.id, agent_id=agent.id
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    nested.rollback()
    db.add(
        ConversationParticipant(
            conversation_id=conversation.id, participant_type=ParticipantType.AGENT, agent_id=agent.id
        )
    )
    db.flush()


def test_membership_is_unique_per_household_and_user(db: Session) -> None:
    household, user, _ = _household(db)
    db.add(HouseholdMembership(household_id=household.id, user_id=user.id, role=MembershipRole.ADULT))
    with pytest.raises(IntegrityError):
        db.flush()


def test_foreign_keys_are_enforced_on_sqlite(db: Session) -> None:
    db.add(HouseholdMembership(household_id=uuid.uuid4(), user_id=uuid.uuid4(), role=MembershipRole.CHILD))
    with pytest.raises(IntegrityError):
        db.flush()


def test_deleting_a_household_cascades(db: Session) -> None:
    household, _, _ = _household(db)
    db.commit()
    db.delete(household)
    db.commit()
    assert db.scalars(select(Agent)).all() == []
    assert db.scalars(select(HouseholdMembership)).all() == []


def test_json_columns_track_in_place_mutation(db: Session) -> None:
    household, user, _ = _household(db)
    task = Task(
        household_id=household.id, created_by_user_id=user.id, title="t", kind=TaskKind.BROWSER, status="approved"
    )
    db.add(task)
    db.commit()
    household.settings["adult_spend_approval_threshold_cents"] = 1000
    task.meta["items"] = ["milk"]
    db.commit()
    db.expire_all()
    assert db.get(Household, household.id).settings["adult_spend_approval_threshold_cents"] == 1000  # type: ignore[union-attr]
    assert db.get(Task, task.id).meta == {"items": ["milk"]}  # type: ignore[union-attr]
    assert db.execute(text("SELECT metadata FROM tasks")).scalar_one() is not None


def test_browser_task_defaults(db: Session) -> None:
    household, user, agent = _household(db)
    task = Task(
        household_id=household.id, created_by_user_id=user.id, title="t", kind=TaskKind.BROWSER, status="approved"
    )
    db.add(task)
    db.flush()
    browser_task = BrowserTask(
        household_id=household.id,
        user_id=user.id,
        agent_id=agent.id,
        task_id=task.id,
        task_type="list_add_items",
        requested_url="https://example.com",
    )
    db.add(browser_task)
    db.flush()
    assert browser_task.status == BrowserTaskStatus.QUEUED
    assert (browser_task.steps, browser_task.result) == ([], {})
    assert (browser_task.requires_takeover, browser_task.verified) == (False, False)
    assert ApprovalStatus.PENDING == "pending"


def test_conversation_relationships_order_and_cascade(db: Session) -> None:
    household, user, agent = _household(db)
    conversation = Conversation(household_id=household.id, title="Sunday groceries", topic_key="sunday-groceries")
    conversation.participants = [
        ConversationParticipant(participant_type=ParticipantType.USER, user_id=user.id),
        ConversationParticipant(
            participant_type=ParticipantType.AGENT, agent_id=agent.id, joined_via=JoinedVia.RESUMED
        ),
    ]
    conversation.messages = [
        Message(sender_type="user", sender_user_id=user.id, body="first"),
        Message(sender_type="agent", sender_agent_id=agent.id, body="second"),
    ]
    db.add(conversation)
    db.commit()
    db.expire_all()
    loaded = db.get(Conversation, conversation.id)
    assert [m.body for m in loaded.messages] == ["first", "second"]  # type: ignore[union-attr]
    assert [p.joined_via for p in loaded.participants] == [JoinedVia.CREATED, JoinedVia.RESUMED]  # type: ignore[union-attr]
    assert loaded.participants[1].agent.name == "Uhlexa"  # type: ignore[union-attr]
    db.delete(loaded)
    db.commit()
    assert db.scalars(select(Message)).all() == []
    assert db.scalars(select(ConversationParticipant)).all() == []
