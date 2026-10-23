"""Stand-ins and builders for service-level tests. Nothing here touches the network.

`FakeSnap`, `FakePrimitive` and `FakeMaritime` implement the same methods the services call on the real provider
services, so tests can script answers and failures without HTTP.
"""

import uuid
from collections.abc import Callable, Collection
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal, resolve_principal
from app.errors import ProviderUnavailable
from app.models import (
    Agent,
    BrowserTask,
    Household,
    HouseholdMembership,
    Job,
    MembershipRole,
    User,
)
from app.providers import ProviderStatus
from app.providers.maritime.models import RunOutcome, StepRecord, TakeoverState, Verifier
from app.providers.primitive.errors import NotRepliable
from app.providers.primitive.models import PrimitiveMemory, PrimitiveMessage, PrimitiveReply
from app.providers.primitive.service import PrimitiveService
from app.providers.subq.models import DecisionResult
from app.providers.subq.service import SnapService
from app.settings import get_settings

DEMO_SENTENCE = "Ask Uhlexa to add milk and eggs to Sunday groceries."
SAFE_URL = "https://todomvc.com/examples/react/dist/"

Answer = str | DecisionResult | Callable[[dict[str, str]], str | DecisionResult]


class FakeSnap:
    """Scripted Snap: `answers` maps question_id -> choice key, a DecisionResult, or a callable(choices) -> either.

    `calls` holds (question_id, state, choices) per `decide`; `images` is index-aligned with it and holds
    (question_id, image, image_mime) so a test can see which questions came with a picture (`image` is None if none)."""

    def __init__(self, answers: dict[str, Answer] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.images: list[tuple[str, bytes | None, str]] = []

    @property
    def status(self) -> ProviderStatus:
        return ProviderStatus("subq", "ok", True, True, "fake")

    def asked(self, question_id: str) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
        return [call for call in self.calls if call[0] == question_id]

    def decide(
        self,
        question_id: str,
        state: dict,
        choices: dict[str, str],
        *,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> DecisionResult:
        self.calls.append((question_id, state, choices))
        self.images.append((question_id, image, image_mime))
        answer = self.answers.get(question_id)
        if callable(answer):
            answer = answer(choices)
        if answer is None:
            return DecisionResult.fallback(question_id, "unavailable", "fake snap has no answer")
        if isinstance(answer, DecisionResult):
            return answer
        probabilities = {key: (0.9 if key == answer else 0.1 / max(len(choices) - 1, 1)) for key in choices}
        return DecisionResult(question_id, answer, 0.9, probabilities, "snap", "ok", "fake")


class FakePrimitive:
    """In-memory Primitive: a memory store, scripted outage, and a record of replies and fetched messages."""

    def __init__(self, *, down: bool = False, messages: dict[str, PrimitiveMessage] | None = None) -> None:
        self.down = down
        self.memory: dict[str, PrimitiveMemory] = {}
        self.replies: list[tuple[str, str, str]] = []
        self.idempotency_keys: list[str | None] = []
        self.allowed_senders: list[list[str]] = []  # per reply that went out, aligned with `replies`
        self.messages = messages or {}
        self.reply_result: PrimitiveReply | None = None
        self.email_replies: list[tuple[str, str, str]] = []  # reply_to_email: (email id, body, label)

    @property
    def status(self) -> ProviderStatus:
        if self.down:
            return ProviderStatus("primitive", "unavailable", True, True, "Unavailable: fake outage")
        return ProviderStatus("primitive", "ok", True, True, "fake")

    def _check(self) -> None:
        if self.down:
            raise ProviderUnavailable("primitive", "fake outage")

    def get_memory(self, key: str) -> PrimitiveMemory | None:
        self._check()
        return self.memory.get(key)

    def put_memory(self, key: str, value: dict) -> PrimitiveMemory:
        self._check()
        previous = self.memory.get(key)
        version = str(int(previous.version) + 1) if previous else "1"
        self.memory[key] = PrimitiveMemory(key, value, version)
        return self.memory[key]

    def reply_to_thread(
        self,
        thread_id: str,
        body: str,
        *,
        sender_label: str,
        idempotency_key: str | None = None,
        allowed_senders: Collection[str] = (),
    ) -> PrimitiveReply:
        self._check()
        if not allowed_senders:  # the real client fails closed before any HTTP call: nobody may receive the reply
            raise NotRepliable(
                "no household addresses were given, so no one may receive this reply",
                primitive_code="sender_not_allowed",
            )
        self.replies.append((thread_id, body, sender_label))
        self.idempotency_keys.append(idempotency_key)
        self.allowed_senders.append(sorted(allowed_senders))
        return self.reply_result or PrimitiveReply(f"prim-msg-{len(self.replies)}", "sent", ("a@x",), (), False)

    def reply_to_email(self, email_id: str, body: str, *, sender_label: str, idempotency_key: str) -> PrimitiveReply:
        self._check()
        self.email_replies.append((email_id, body, sender_label))
        self.idempotency_keys.append(idempotency_key)
        return self.reply_result or PrimitiveReply(f"prim-msg-{len(self.email_replies)}", "sent", ("a@x",), (), False)

    def get_message(self, message_id: str) -> PrimitiveMessage:
        self._check()
        try:
            return self.messages[message_id]
        except KeyError:
            raise ProviderUnavailable("primitive", "message not found") from None


class FakeMaritime:
    """Scripted Maritime: steps are reported through `on_step`, then `outcome` is returned (or `run_error` raised).

    `screenshot` (bytes) makes a run read it through the `verifier` it was given, as the real runner does at each
    checkpoint; the verdicts land in `verdicts`. `verifiers` holds the verifier of every run (None when not passed)."""

    def __init__(
        self,
        outcome: RunOutcome | None = None,
        *,
        actions: tuple[str, ...] = ("navigate", "type milk", "type eggs", "verify"),
        computer_id: str = "comp-1",
        control: str = "agent",
        ensure_error: ProviderUnavailable | None = None,
        run_error: Exception | None = None,
        takeover_link: str = "https://control.example/SECRET-LINK",
        takeover_result: str | None = "success",
        screenshot: bytes | None = None,
    ) -> None:
        self.outcome = outcome or RunOutcome("succeeded", True, None, b"\xff\xd8fake-jpeg", "all items visible")
        self.actions = actions
        self.computer_id = computer_id
        self.control = control
        self.ensure_error = ensure_error
        self.run_error = run_error
        self.takeover_link = takeover_link
        self.takeover_result = takeover_result
        self.screenshot = screenshot
        self.verifiers: list[Verifier | None] = []
        self.verdicts: list[str] = []
        self.calls: list[tuple[Any, ...]] = []

    @property
    def status(self) -> ProviderStatus:
        return ProviderStatus("maritime", "ok", True, True, "fake")

    def ensure_computer(self, user_id: uuid.UUID) -> str:
        self.calls.append(("ensure_computer", user_id))
        if self.ensure_error:
            raise self.ensure_error
        return self.computer_id

    def _run(self, on_step: Callable[[StepRecord], None], verifier: Verifier | None) -> RunOutcome:
        self.verifiers.append(verifier)
        for index, action in enumerate(self.actions):
            on_step(StepRecord(index, action, True, f"step {index}", None))
        if verifier is not None and self.screenshot is not None:
            self.verdicts.append(verifier(self.screenshot, "Is the list on screen complete?"))
        if self.run_error:
            raise self.run_error
        return self.outcome

    def run_list_add(
        self,
        user_id: uuid.UUID,
        computer_id: str,
        target_url: str,
        items: list[str],
        on_step: Callable[[StepRecord], None],
        *,
        verifier: Verifier | None = None,
    ) -> RunOutcome:
        self.calls.append(("run_list_add", user_id, computer_id, target_url, list(items)))
        return self._run(on_step, verifier)

    def run_sign_in_check(
        self,
        user_id: uuid.UUID,
        computer_id: str,
        page_url: str,
        on_step: Callable[[StepRecord], None],
        *,
        verifier: Verifier | None = None,
    ) -> RunOutcome:
        self.calls.append(("run_sign_in_check", user_id, computer_id, page_url))
        return self._run(on_step, verifier)

    def request_takeover(self, user_id: uuid.UUID, computer_id: str, reason: str) -> str:
        self.calls.append(("request_takeover", user_id, computer_id, reason))
        return self.takeover_link

    def takeover_status(self, user_id: uuid.UUID, computer_id: str) -> str:
        self.calls.append(("takeover_status", user_id, computer_id))
        return self.control

    def takeover_state(self, user_id: uuid.UUID, computer_id: str) -> TakeoverState:
        """`control` is the mode; `takeover_result` is how the last takeover ended (None: no result yet)."""
        self.calls.append(("takeover_state", user_id, computer_id))
        return TakeoverState(self.control, self.takeover_result)  # type: ignore[arg-type]

    def close_computer(self, user_id: uuid.UUID, computer_id: str) -> None:
        self.calls.append(("close_computer", user_id, computer_id))


def real_snap() -> SnapService:
    """The real service under hermetic settings: disabled, so it answers with a fallback and never calls out."""
    return SnapService(get_settings())


def real_primitive() -> PrimitiveService:
    return PrimitiveService(get_settings())


def enable_maritime(monkeypatch: pytest.MonkeyPatch, *, target: str = SAFE_URL) -> None:
    """Flag on + a dummy key: config status `ok`, so browser tasks are queued instead of marked unavailable."""
    monkeypatch.setenv("ENABLE_MARITIME", "true")
    monkeypatch.setenv("MARITIME_COMPUTERS_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("MARITIME_SAFE_TARGET_URL", target)
    get_settings.cache_clear()


def jobs(db: Session, kind: str | None = None) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at)
    if kind:
        stmt = stmt.where(Job.kind == kind)
    return list(db.scalars(stmt))


def browser_tasks(db: Session) -> list[BrowserTask]:
    return list(db.scalars(select(BrowserTask).order_by(BrowserTask.created_at)))


def ask(db: Session, principal: Principal, text: str = DEMO_SENTENCE, **kwargs: Any) -> dict[str, Any]:
    """The whole message flow as the REST route runs it (Snap and Primitive disabled unless passed), committed."""
    from app.services import conversation_service

    kwargs.setdefault("source", "rest")
    kwargs.setdefault("snap", real_snap())
    kwargs.setdefault("primitive", real_primitive())
    payload = conversation_service.handle_message(db, principal, text, **kwargs)
    db.commit()
    return payload


def approve(db: Session, principal: Principal, payload: dict[str, Any], *, ok: bool = True) -> None:
    from app.services import approval_service

    approval_service.decide(db, principal, uuid.UUID(payload["approval"]["id"]), approve=ok)
    db.commit()


def approved_browser_task(
    db: Session, principal: Principal, text: str = DEMO_SENTENCE
) -> tuple[dict[str, Any], BrowserTask]:
    """Ask, approve, and return (the message payload, the queued browser task). Call `enable_maritime` first."""
    payload = ask(db, principal, text)
    approve(db, principal, payload)
    (browser_task,) = [bt for bt in browser_tasks(db) if str(bt.task_id) == payload["task"]["id"]]
    return payload, browser_task


def give_household_emails(db: Session, domain: str = "parker.example") -> dict[str, str]:
    """Give every user in the database an address (`<handle>@<domain>`), committed. Returns handle -> address.

    Primitive may only answer household addresses, so a Primitive-backed thread needs these to send a reply."""
    addresses: dict[str, str] = {}
    for user in db.scalars(select(User)):
        user.email = addresses[user.handle] = f"{user.handle}@{domain}"
    db.commit()
    return addresses


def second_household_principal(db: Session, *, handle: str = "zed") -> Principal:
    """A user in a different household, with a default agent, committed."""
    household = Household(name="Other Family", slug=f"other-{handle}")
    user = User(handle=handle, display_name=handle.title(), is_demo=True)
    db.add_all([household, user])
    db.flush()
    db.add(HouseholdMembership(household_id=household.id, user_id=user.id, role=MembershipRole.GUARDIAN))
    db.add(Agent(household_id=household.id, owner_user_id=user.id, name=f"{handle.title()}'s Uhlexa", is_default=True))
    db.commit()
    principal = resolve_principal(db, handle)
    assert principal is not None
    return principal
