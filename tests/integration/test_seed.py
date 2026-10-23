import contextlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import seed
from app.auth.demo_auth import Principal
from app.db.base import Base
from app.models import (
    ActivityEvent,
    Agent,
    Conversation,
    ConversationParticipant,
    Household,
    HouseholdMembership,
    JoinedVia,
    MembershipRole,
    Message,
    ParticipantType,
    SenderType,
    Task,
    TaskKind,
    TaskStatus,
    TranscriptProvider,
    User,
    Visibility,
)
from app.settings import get_settings


def _count(db: Session, model: type[Base]) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def _only_conversation(db: Session) -> Conversation:
    return db.scalars(select(Conversation).where(Conversation.topic_key == seed.GROCERIES_TOPIC_KEY)).one()


def _development(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


# --- seed_demo ---------------------------------------------------------------------------------------------------


def test_seed_is_idempotent(db: Session) -> None:
    seed.seed_demo(db)
    first_ids = {model: set(db.scalars(select(model.id))) for model in (User, Household, Agent, Conversation)}
    seed.seed_demo(db)
    db.flush()

    assert _count(db, Household) == 1
    assert _count(db, User) == 3
    assert _count(db, HouseholdMembership) == 3
    assert _count(db, Agent) == 3
    assert db.scalar(select(func.count()).select_from(Agent).where(Agent.is_default.is_(True))) == 3
    assert _count(db, Conversation) == 2
    assert _count(db, ConversationParticipant) == 4
    assert _count(db, Message) == 4
    assert {model: set(db.scalars(select(model.id))) for model in first_ids} == first_ids


def test_seed_household_members_and_agents(db: Session) -> None:
    seed.seed_demo(db)
    household = db.scalars(select(Household)).one()
    assert (household.name, household.slug) == ("The Parker Family", "parker")
    assert household.settings["adult_spend_approval_threshold_cents"] == 5000

    rows = db.execute(
        select(User, HouseholdMembership.role, Agent)
        .join(HouseholdMembership, HouseholdMembership.user_id == User.id)
        .join(Agent, Agent.owner_user_id == User.id)
        .order_by(User.handle)
    ).all()
    assert [(u.handle, u.display_name, role, a.name) for u, role, a in rows] == [
        ("alex", "Alex Parker", MembershipRole.GUARDIAN, "Uhlexa"),
        ("katie", "Katie Parker", MembershipRole.ADULT, "Jordy"),
        ("max", "Max Parker", MembershipRole.CHILD, "Max's Uhlexa"),
    ]
    assert all(u.is_demo and u.email is None for u, _, _ in rows)
    assert all(a.household_id == household.id and a.is_default for _, _, a in rows)


def test_seeded_sunday_groceries_thread(db: Session) -> None:
    seed.seed_demo(db)
    conversation = _only_conversation(db)
    katie = db.scalars(select(User).where(User.handle == "katie")).one()
    katies_agent = db.scalars(select(Agent).where(Agent.owner_user_id == katie.id)).one()

    assert conversation.title == "Sunday groceries"
    assert conversation.topic_key == "sunday-groceries"
    assert conversation.visibility == Visibility.HOUSEHOLD
    assert conversation.transcript_provider == TranscriptProvider.LOCAL
    assert conversation.primitive_thread_id is None
    assert conversation.created_by_user_id == katie.id
    assert conversation.created_by_agent_id == katies_agent.id
    assert conversation.thread_state == {
        "summary": "Sunday grocery list for the Parker family.",
        "items": ["bread", "bananas"],
        "open_questions": [],
        "last_actor": "Jordy",
    }
    assert conversation.thread_state_version == 1

    assert [(p.participant_type, p.user_id, p.agent_id, p.joined_via) for p in conversation.participants] == [
        (ParticipantType.USER, katie.id, None, JoinedVia.CREATED),
        (ParticipantType.AGENT, None, katies_agent.id, JoinedVia.CREATED),
    ]


def test_seeded_messages_are_local_and_recent_enough_to_route(db: Session) -> None:
    seed.seed_demo(db)
    conversation = _only_conversation(db)
    first, second = conversation.messages
    assert (first.sender_type, first.body) == (SenderType.USER, "We need bread and bananas for Sunday")
    assert second.sender_type == SenderType.AGENT and "bread" in second.body and "bananas" in second.body
    assert first.sender_user_id is not None and first.sender_agent_id is None
    assert second.sender_agent_id is not None and second.sender_user_id is None
    assert {first.source, second.source} == {TranscriptProvider.LOCAL}
    assert first.primitive_message_id is None and second.primitive_message_id is None
    assert first.created_at < second.created_at
    assert conversation.last_message_at == second.created_at  # candidates are ordered by recency


def test_seed_leaves_demo_progress_alone(db: Session) -> None:
    seed.seed_demo(db)
    conversation = _only_conversation(db)
    conversation.thread_state = {**conversation.thread_state, "items": ["bread", "bananas", "milk"]}
    conversation.thread_state_version = 2
    db.flush()

    seed.seed_demo(db)
    db.refresh(conversation)
    assert conversation.thread_state["items"] == ["bread", "bananas", "milk"]
    assert conversation.thread_state_version == 2


def test_seed_restores_a_missing_thread(db: Session) -> None:
    seed.seed_demo(db)
    db.delete(_only_conversation(db))
    db.flush()
    seed.seed_demo(db)
    conversation = _only_conversation(db)
    assert conversation.topic_key == "sunday-groceries" and conversation.thread_state["items"] == ["bread", "bananas"]


def test_seed_user_ids_are_stable(db: Session) -> None:
    """User ids are part of the pinned Maritime URL, so a reseed must not mint new ones (it would orphan computers)."""
    seed.seed_demo(db)
    ids = dict(db.execute(select(User.handle, User.id)).all())
    assert len(set(ids.values())) == 3
    assert ids == {handle: seed.demo_user_id(handle) for handle in ("alex", "katie", "max")}
    assert seed.demo_user_id("alex") == seed.demo_user_id("alex") != seed.demo_user_id("max")
    assert isinstance(seed.demo_user_id("alex"), uuid.UUID)


def test_seeded_fixture_returns_principals(seeded: dict[str, Principal]) -> None:
    assert set(seeded) == {"alex", "katie", "max"}
    assert seeded["max"].is_child and seeded["alex"].agent.name == "Uhlexa"


@pytest.mark.parametrize("seed_value", ["true", "false"])
def test_seed_command_seeds_regardless_of_the_flag(
    db: Session, monkeypatch: pytest.MonkeyPatch, seed_value: str
) -> None:
    """`python -m app.seed` is an explicit request; SEED_DEMO_DATA only gates the entrypoint and the lifespan."""

    @contextlib.contextmanager
    def scope() -> Iterator[Session]:
        yield db
        db.commit()

    monkeypatch.setattr(seed, "session_scope", scope)
    monkeypatch.setenv("SEED_DEMO_DATA", seed_value)
    get_settings.cache_clear()
    seed.main()
    assert _count(db, User) == 3 and _count(db, Conversation) == 2


# --- reset_demo and the /demo routes -----------------------------------------------------------------------------

GUARDIAN = {"X-Uhlexa-User": "alex"}


def _dirty(db: Session, seeded: dict[str, Principal]) -> None:
    """Leave scenario data behind: a task, an activity event, a stray household with a user."""
    alex = seeded["alex"]
    db.add(
        Task(
            household_id=alex.household.id,
            created_by_user_id=alex.user.id,
            title="Stray task",
            kind=TaskKind.GENERAL,
            status=TaskStatus.APPROVED,
        )
    )
    db.add(ActivityEvent(household_id=alex.household.id, event_type="task.created", summary="stray"))
    other = Household(name="Other", slug="other")
    db.add(other)
    db.flush()
    db.add(User(handle="zed", display_name="Zed"))
    conversation = _only_conversation(db)
    conversation.thread_state = {"items": ["gone"]}
    db.commit()


@pytest.mark.filterwarnings("error::sqlalchemy.exc.SAWarning")
def test_reset_reseeds(
    client: TestClient, db: Session, seeded: dict[str, Principal], monkeypatch: pytest.MonkeyPatch
) -> None:
    _dirty(db, seeded)
    _development(monkeypatch)

    response = client.post("/demo/reset", headers=GUARDIAN)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "reset"
    assert [u["user"]["handle"] for u in body["users"]] == ["alex", "katie", "max"]

    assert _count(db, Household) == 1 and _count(db, User) == 3 and _count(db, Agent) == 3
    assert _count(db, Task) == 0
    assert _only_conversation(db).thread_state["items"] == ["bread", "bananas"]
    assert db.scalars(select(Household.slug)).one() == "parker"
    assert db.scalar(select(func.count()).select_from(User).where(User.handle == "zed")) == 0
    # the wipe leaves exactly one trace: the reset itself, on the new household's feed
    events = db.scalars(select(ActivityEvent)).all()
    assert [(e.event_type, e.household_id) for e in events] == [("demo.reset", db.scalars(select(Household.id)).one())]


@pytest.mark.filterwarnings("error::sqlalchemy.exc.SAWarning")
def test_reset_keeps_user_ids_so_pinned_computers_survive(
    client: TestClient, db: Session, seeded: dict[str, Principal], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {handle: principal.user.id for handle, principal in seeded.items()}
    _development(monkeypatch)
    assert client.post("/demo/reset", headers=GUARDIAN).status_code == 200
    assert dict(db.execute(select(User.handle, User.id)).all()) == before


@pytest.mark.filterwarnings("error::sqlalchemy.exc.SAWarning")
def test_reset_works_on_an_empty_database(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset must not need an existing user: it is the way out of an unseeded or broken demo."""
    _development(monkeypatch)
    assert _count(db, User) == 0
    assert client.post("/demo/reset", headers=GUARDIAN).status_code == 200
    assert _count(db, User) == 3 and _count(db, Conversation) == 2


def test_reset_404_outside_development(
    client: TestClient, db: Session, seeded: dict[str, Principal], monkeypatch: pytest.MonkeyPatch
) -> None:
    for env in ("test", "production"):
        monkeypatch.setenv("APP_ENV", env)
        monkeypatch.setenv("APP_SESSION_SECRET", "a-real-secret")
        get_settings.cache_clear()
        response = client.post("/demo/reset", headers=GUARDIAN)
        assert response.status_code == 404, env
        assert response.json()["code"] == "not_found"
    assert _count(db, Conversation) == 2  # nothing was wiped
    assert _count(db, ActivityEvent) == 0


def test_reset_needs_demo_auth(
    client: TestClient, db: Session, seeded: dict[str, Principal], monkeypatch: pytest.MonkeyPatch
) -> None:
    _development(monkeypatch, ENABLE_DEMO_AUTH="false")
    response = client.post("/demo/reset", headers=GUARDIAN)
    assert response.status_code == 401
    assert _count(db, ActivityEvent) == 0


def test_reset_needs_an_explicit_header(
    client: TestClient, db: Session, seeded: dict[str, Principal], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cookie and the default-user fallback don't authorize a reset; only the custom header does."""
    _development(monkeypatch)
    response = client.post("/demo/reset")
    assert response.status_code == 401
    assert _count(db, ActivityEvent) == 0


@pytest.mark.parametrize("handle", ["katie", "max", "nobody"])
def test_reset_needs_a_guardian(
    client: TestClient, db: Session, seeded: dict[str, Principal], monkeypatch: pytest.MonkeyPatch, handle: str
) -> None:
    _development(monkeypatch)
    response = client.post("/demo/reset", headers={"X-Uhlexa-User": handle})
    assert response.status_code == (401 if handle == "nobody" else 403)
    assert _count(db, Conversation) == 2  # nothing was wiped
    assert _count(db, ActivityEvent) == 0


def test_demo_users_is_public_and_lists_role_and_agent(client: TestClient, seeded: dict[str, Principal]) -> None:
    response = client.get("/demo/users")  # no cookie, no header
    assert response.status_code == 200
    users = response.json()["users"]
    assert [(u["user"]["handle"], u["role"], u["agent"]["name"]) for u in users] == [
        ("alex", "guardian", "Uhlexa"),
        ("katie", "adult", "Jordy"),
        ("max", "child", "Max's Uhlexa"),
    ]
    assert users[0]["household"]["slug"] == "parker"
    assert users[0]["user"]["display_name"] == "Alex Parker"
    assert "email" not in str(users)


def test_demo_users_stays_available_with_demo_auth_off(
    client: TestClient, seeded: dict[str, Principal], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_DEMO_AUTH", "false")
    get_settings.cache_clear()
    assert client.get("/demo/users").status_code == 200


def test_demo_users_empty_before_seeding(client: TestClient) -> None:
    assert client.get("/demo/users").json() == {"users": []}


def test_login_through_the_real_app(client: TestClient, seeded: dict[str, Principal]) -> None:
    response = client.post("/demo/login", json={"handle": "alex"})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == str(seeded["alex"].user.id)
    assert body["household"]["name"] == "The Parker Family"
    assert body["agent"]["name"] == "Uhlexa" and body["role"] == "guardian"
    assert "uhlexa_session" in client.cookies
