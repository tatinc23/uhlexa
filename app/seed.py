"""Demo data (spec section 7): the Parker household, one default agent each, and Katie's "Sunday groceries" thread.

`seed_demo` is idempotent and only fills gaps, so demo progress survives a restart; `reset_demo` returns to the
starting state. Run `python -m app.seed` to seed a migrated database (the container entrypoint does this).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import Base, utcnow
from app.db.session import session_scope
from app.models import (
    ActivityEvent,
    ActorType,
    Agent,
    Conversation,
    ConversationParticipant,
    ConversationStatus,
    Household,
    HouseholdMembership,
    JoinedVia,
    MembershipRole,
    Message,
    ParticipantType,
    SenderType,
    TranscriptProvider,
    User,
    Visibility,
)
from app.models.household import default_household_settings

logger = logging.getLogger(__name__)

HOUSEHOLD_NAME = "The Parker Family"
HOUSEHOLD_SLUG = "parker"
GROCERIES_TITLE = "Sunday groceries"
GROCERIES_TOPIC_KEY = "sunday-groceries"
BIRTHDAY_TITLE = "Katie's birthday dinner"
BIRTHDAY_TOPIC_KEY = "katies-birthday-dinner"

# User ids are derived from the handle, not random. They are part of each user's pinned Maritime URL, so a reset or a
# second environment must not mint new ids and orphan the computers already created for these users.
_USER_ID_NAMESPACE = uuid.UUID("5d1c0f2e-7a49-4c63-9a52-3b0e5f8d6a17")


def demo_user_id(handle: str) -> uuid.UUID:
    return uuid.uuid5(_USER_ID_NAMESPACE, f"user:{handle}")


@dataclass(frozen=True)
class _Member:
    handle: str
    display_name: str
    role: MembershipRole
    agent_name: str
    persona: str


MEMBERS = (
    _Member(
        "alex",
        "Alex Parker",
        MembershipRole.GUARDIAN,
        "Uhlexa",
        "Organized planner who keeps errands, calendars and household decisions moving.",
    ),
    _Member(
        "katie",
        "Katie Parker",
        MembershipRole.ADULT,
        "Jordy",
        "Practical shopper who keeps the grocery lists and meal plans in order.",
    ),
    _Member(
        "max",
        "Max Parker",
        MembershipRole.CHILD,
        "Max's Uhlexa",
        "Friendly helper for a kid: simple answers, and asks a grown-up before anything that costs money.",
    ),
)


def _ensure[T: Base](db: Session, model: type[T], keys: dict[str, Any], **defaults: Any) -> T:
    """The row matching `keys`, or a new one built from `keys` and `defaults`."""
    row = db.scalars(select(model).filter_by(**keys)).first()
    if row is None:
        row = model(**keys, **defaults)
        db.add(row)
        db.flush()
    return row


def _seed_thread(
    db: Session,
    household: Household,
    owner: User,
    agent: Agent,
    *,
    title: str,
    topic_key: str,
    summary: str,
    items: list[str],
    user_message: str,
    agent_message: str,
    days_ago: int,
) -> None:
    """One earlier-in-the-week thread started by `owner` and their agent. Skipped when the topic already exists."""
    if db.scalars(
        select(Conversation.id).where(Conversation.household_id == household.id, Conversation.topic_key == topic_key)
    ).first():
        return
    started = utcnow() - timedelta(days=days_ago)
    replied = started + timedelta(seconds=20)
    conversation = Conversation(
        household_id=household.id,
        title=title,
        topic_key=topic_key,
        visibility=Visibility.HOUSEHOLD,
        status=ConversationStatus.OPEN,
        created_by_user_id=owner.id,
        created_by_agent_id=agent.id,
        transcript_provider=TranscriptProvider.LOCAL,
        thread_state={"summary": summary, "items": items, "open_questions": [], "last_actor": agent.name},
        last_message_at=replied,
        created_at=started,
        updated_at=replied,
    )
    db.add(conversation)
    db.flush()
    db.add_all(
        [
            ConversationParticipant(
                conversation_id=conversation.id,
                participant_type=ParticipantType.USER,
                user_id=owner.id,
                joined_via=JoinedVia.CREATED,
                joined_at=started,
            ),
            ConversationParticipant(
                conversation_id=conversation.id,
                participant_type=ParticipantType.AGENT,
                agent_id=agent.id,
                joined_via=JoinedVia.CREATED,
                joined_at=started + timedelta(seconds=1),
            ),
            Message(
                conversation_id=conversation.id,
                sender_type=SenderType.USER,
                sender_user_id=owner.id,
                body=user_message,
                source=TranscriptProvider.LOCAL,
                created_at=started,
            ),
            Message(
                conversation_id=conversation.id,
                sender_type=SenderType.AGENT,
                sender_agent_id=agent.id,
                body=agent_message,
                source=TranscriptProvider.LOCAL,
                created_at=replied,
            ),
        ]
    )


def _seed_sunday_groceries(db: Session, household: Household, katie: User, agent: Agent) -> None:
    _seed_thread(
        db,
        household,
        katie,
        agent,
        title=GROCERIES_TITLE,
        topic_key=GROCERIES_TOPIC_KEY,
        summary="Sunday grocery list for the Parker family.",
        items=["bread", "bananas"],
        user_message="We need bread and bananas for Sunday",
        agent_message="Got it. I started the Sunday groceries list with bread and bananas.",
        days_ago=3,
    )


def _seed_birthday_dinner(db: Session, household: Household, katie: User, agent: Agent) -> None:
    """Saturday's shopping list, started by Jordy (the video's opening question and the A2A beat)."""
    _seed_thread(
        db,
        household,
        katie,
        agent,
        title=BIRTHDAY_TITLE,
        topic_key=BIRTHDAY_TOPIC_KEY,
        summary="Shopping list for Katie's birthday dinner on Saturday at six.",
        items=["paper plates", "napkins"],
        user_message="Start the shopping list for my birthday dinner on Saturday at six",
        agent_message=(
            "Done. I started the Saturday shopping list for Katie's birthday dinner with paper plates and napkins."
        ),
        days_ago=4,
    )


def seed_demo(db: Session) -> None:
    """Create whatever of the demo household is missing. Flushes; the caller commits."""
    household = _ensure(
        db,
        Household,
        {"slug": HOUSEHOLD_SLUG},
        name=HOUSEHOLD_NAME,
        settings=default_household_settings(),
    )
    agents: dict[str, Agent] = {}
    users: dict[str, User] = {}
    for member in MEMBERS:
        user = _ensure(
            db,
            User,
            {"handle": member.handle},
            id=demo_user_id(member.handle),
            display_name=member.display_name,
            is_demo=True,
        )
        _ensure(db, HouseholdMembership, {"household_id": household.id, "user_id": user.id}, role=member.role)
        agents[member.handle] = _ensure(
            db,
            Agent,
            {"household_id": household.id, "owner_user_id": user.id, "is_default": True},
            name=member.agent_name,
            persona=member.persona,
        )
        users[member.handle] = user
    _seed_sunday_groceries(db, household, users["katie"], agents["katie"])
    _seed_birthday_dinner(db, household, users["katie"], agents["katie"])
    db.flush()


def reset_demo(db: Session) -> None:
    """Wipe every table and reseed. Works on an empty or half-broken database. Flushes; the caller commits."""
    db.expunge_all()  # the reseeded users reuse their ids, which would collide with stale identity-map entries
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    seed_demo(db)
    household = db.scalars(select(Household)).one()
    db.add(
        ActivityEvent(
            household_id=household.id,
            event_type="demo.reset",
            actor_type=ActorType.SYSTEM,
            summary="Demo data was reset to the starting state.",
        )
    )
    db.flush()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with session_scope() as db:
        seed_demo(db)
    logger.info("demo data ready (household %r)", HOUSEHOLD_SLUG)


if __name__ == "__main__":
    main()
