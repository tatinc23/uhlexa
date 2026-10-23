"""SnapService fails closed: disabled, misconfigured, unwired or broken Snap yields a no-decision result (Task 4)."""

import dataclasses
import inspect
from typing import Any

import httpx
import pytest

from app.errors import ProviderUnavailable
from app.providers import mark_available, mark_unavailable, provider_statuses, reset_runtime_state
from app.providers.subq.client import SnapClient
from app.providers.subq.models import DecisionResult
from app.providers.subq.service import INSTRUCTIONS, SnapService
from app.settings import Settings

ENV_NAMES = [name.upper() for name in Settings.model_fields]
KEY = "SENTINEL-subq-key-3b18"
CHOICES = {"continue:abc": "Same list as the open thread", "new_thread": "A different topic"}
STATE = {"new_request": "add milk and eggs to Sunday groceries", "open_threads": [{"thread_id": "abc"}]}
DOCUMENTED_QUESTIONS = {
    "thread_routing",
    "approval_escalation",
    "context_sufficiency",
    "browser_automation",
    "verify_screenshot",
}


@pytest.fixture(autouse=True)
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    reset_runtime_state()
    yield
    reset_runtime_state()


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def explode(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected provider HTTP call: {request.method} {request.url}")


def assert_no_decision(result: DecisionResult, *, status: str, question_id: str = "thread_routing") -> None:
    assert result.status == status
    assert result.question_id == question_id
    assert result.choice is None and result.confidence is None and result.probabilities == {}
    assert result.source == "fallback"
    assert KEY not in result.detail


def test_subq_disabled_returns_fallback_result() -> None:
    service = SnapService(make_settings(), transport=httpx.MockTransport(explode))
    result = service.decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, status="disabled")
    assert "ENABLE_SUBQ_SNAP" in result.detail
    assert service.status.status == "disabled"


def test_subq_disabled_does_not_validate_or_touch_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SnapClient, "decide", lambda *a, **k: pytest.fail("client must not be called"))
    result = SnapService(make_settings()).decide("not-a-registered-question", {}, {})
    assert_no_decision(result, status="disabled", question_id="not-a-registered-question")


def test_subq_misconfigured_is_unavailable_with_the_reason() -> None:
    service = SnapService(make_settings(enable_subq_snap=True), transport=httpx.MockTransport(explode))
    result = service.decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, status="unavailable")
    assert "SUBQ_API_KEY" in result.detail
    assert service.status.status == "misconfigured"


def test_subq_unreachable_provider_is_unavailable_not_a_fake_decision() -> None:
    # Task 9 wired the real client (wire contract: tests/contracts/test_subq_contract.py); a flag + key with the
    # provider down must still never invent a choice.
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed")

    settings = make_settings(enable_subq_snap=True, subq_api_key=KEY)
    service = SnapService(settings, transport=httpx.MockTransport(down))
    result = service.decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, status="unavailable")
    assert result.detail == "connection error (ConnectError)"
    status = provider_statuses(settings)["subq"]
    assert status.status == "unavailable" and "connection error" in status.detail
    assert KEY not in status.detail


def test_subq_provider_unavailable_from_the_client_marks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: SnapClient, *args: Any) -> DecisionResult:
        raise ProviderUnavailable("subq", "HTTP 402 insufficient credits")

    monkeypatch.setattr(SnapClient, "decide", fail)
    settings = make_settings(enable_subq_snap=True, subq_api_key=KEY)
    result = SnapService(settings).decide(
        "approval_escalation", STATE, {"require_approval": "x", "no_extra_approval": "y"}
    )
    assert_no_decision(result, status="unavailable", question_id="approval_escalation")
    assert result.detail == "HTTP 402 insufficient credits"
    assert provider_statuses(settings)["subq"].status == "unavailable"


def test_subq_decide_never_raises_and_never_echoes_the_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def bug(self: SnapClient, *args: Any) -> DecisionResult:
        raise RuntimeError(f"state={STATE} key={KEY}")

    monkeypatch.setattr(SnapClient, "decide", bug)
    settings = make_settings(enable_subq_snap=True, subq_api_key=KEY)
    result = SnapService(settings).decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, status="unavailable")
    assert result.detail == "unexpected RuntimeError"
    assert "milk" not in result.detail and "milk" not in provider_statuses(settings)["subq"].detail


def test_subq_transport_errors_are_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(self: SnapClient, *args: Any) -> DecisionResult:
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(SnapClient, "decide", down)
    result = SnapService(make_settings(enable_subq_snap=True, subq_api_key=KEY)).decide(
        "thread_routing", STATE, CHOICES
    )
    assert_no_decision(result, status="unavailable")
    assert result.detail == "ReadTimeout"


def test_subq_ok_result_passes_through_and_clears_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(enable_subq_snap=True, subq_api_key=KEY)
    service = SnapService(settings)
    mark_unavailable("subq", "earlier outage")
    assert provider_statuses(settings)["subq"].status == "unavailable"

    seen: list[tuple[Any, ...]] = []
    good = DecisionResult(
        "thread_routing", "new_thread", 0.93, {"continue:abc": 0.04, "new_thread": 0.96}, "snap", "ok", ""
    )

    def answer(self: SnapClient, *args: Any) -> DecisionResult:
        seen.append(args)
        return good

    monkeypatch.setattr(SnapClient, "decide", answer)
    assert service.decide("thread_routing", STATE, CHOICES) is good
    assert provider_statuses(settings)["subq"].status == "ok"
    question_id, instructions, state, choices = seen[0]
    assert question_id == "thread_routing" and instructions == INSTRUCTIONS["thread_routing"]
    assert state == STATE and choices == CHOICES


def test_subq_unregistered_question_never_reaches_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SnapClient, "decide", lambda *a, **k: pytest.fail("client must not be called"))
    settings = make_settings(enable_subq_snap=True, subq_api_key=KEY)
    mark_available("subq")
    result = SnapService(settings).decide("made_up_question", STATE, CHOICES)
    assert_no_decision(result, status="unavailable", question_id="made_up_question")
    assert "made_up_question" in result.detail
    assert provider_statuses(settings)["subq"].status == "ok"  # our bug, not the provider's outage


def test_instructions_cover_every_documented_question() -> None:
    assert set(INSTRUCTIONS) == DOCUMENTED_QUESTIONS
    assert all(isinstance(text, str) and len(text) > 40 for text in INSTRUCTIONS.values())


def test_decide_signature_and_result_shape() -> None:
    params = inspect.signature(SnapService.decide).parameters
    # The three positional parameters are unchanged; Task 9 added the optional keyword-only image input.
    assert list(params) == ["self", "question_id", "state", "choices", "image", "image_mime"]
    assert params["image"].kind is inspect.Parameter.KEYWORD_ONLY and params["image"].default is None
    assert [f.name for f in dataclasses.fields(DecisionResult)] == [
        "question_id",
        "choice",
        "confidence",
        "probabilities",
        "source",
        "status",
        "detail",
    ]
    result = DecisionResult.fallback("thread_routing", "disabled", "off")
    assert (result.choice, result.confidence, result.probabilities, result.source) == (None, None, {}, "fallback")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.choice = "new_thread"  # type: ignore[misc]
