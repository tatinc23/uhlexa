"""SubQ Snap wire contract (Task 9), against `docs/research/2026-10-22-subq-facts.md` and the Decisions API docs
(docs.subq.ai/endpoints/decisions-api).

Everything here is faked at the httpx transport boundary: no test touches the network. The request shape, the
fail-closed answer validator, the retry policy and the image input are pinned to what SubQ documents. Live behaviour
is recorded in the facts file, not asserted here.
"""

import base64
import dataclasses
import inspect
import json
import logging
import struct
import uuid
import zlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.providers import provider_statuses, reset_runtime_state
from app.providers.subq import client as client_module
from app.providers.subq.client import SnapClient
from app.providers.subq.models import DecisionResult
from app.providers.subq.service import INSTRUCTIONS, SnapService
from app.settings import Settings

ENV_NAMES = [name.upper() for name in Settings.model_fields]
KEY = "SENTINEL-subq-key-7c41"
SECRET_STATE_WORD = "SENTINELSTATEWORD"
MODEL = "subq-snap-preview"
URL = "https://api.subq.ai/v1/systemone"

CHOICES = {
    "continue_1": "Same list as the first open thread",
    "continue_2": "Same list as the second open thread",
    "new_thread": "A different topic than every open thread",
}
STATE = {"new_request": "add milk and eggs to Sunday groceries", "open_threads": [{"thread_id": "t_1"}]}

# The choice sets Uhlexa asks for (spec section 10), used to prove every registered question builds a valid request.
QUESTION_CHOICES: dict[str, dict[str, str]] = {
    "thread_routing": CHOICES,
    "approval_escalation": {"require_approval": "needs more", "no_extra_approval": "does not"},
    "context_sufficiency": {"recent_context_enough": "enough", "need_full_transcript": "not enough"},
    "browser_automation": {"use_maritime_browser": "browser", "no_browser_needed": "no browser"},
    "verify_screenshot": {"verified": "all there", "not_verified": "missing", "needs_human": "login wall"},
}


@pytest.fixture(autouse=True)
def hermetic(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """No env leakage, a clean availability overlay, and a recorded (instant) retry back-off."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    reset_runtime_state()
    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", sleeps.append)
    yield sleeps
    reset_runtime_state()


# --- helpers ---------------------------------------------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"enable_subq_snap": True, "subq_api_key": KEY}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def good_body(
    question_id: str = "thread_routing",
    choice: str = "continue_1",
    probabilities: dict[str, float] | None = None,
    confidence: float | None = 0.9,
    model: str = MODEL,
) -> dict[str, Any]:
    probs = probabilities if probabilities is not None else {"continue_1": 0.94, "continue_2": 0.04, "new_thread": 0.02}
    answer: dict[str, Any] = {"type": "choice", "choice": choice, "probabilities": probs}
    if confidence is not None:
        answer["confidence"] = confidence
    return {
        "model": model,
        "answers": {question_id: answer},
        "usage": {"input_tokens": 166, "output_tokens": 0},
    }


class Transport:
    """Plays a script of responses/exceptions (the last one repeats) and records every request."""

    def __init__(self, *script: httpx.Response | Exception) -> None:
        self.script = list(script) or [httpx.Response(200, json=good_body())]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script[min(len(self.requests), len(self.script)) - 1]
        if isinstance(step, Exception):
            raise step
        # A fresh response per call: the same scripted response may be served twice (a retry) or by several tests.
        return httpx.Response(step.status_code, headers=step.headers, content=step.content)

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def service_for(transport: Callable[[httpx.Request], httpx.Response], **overrides: Any) -> SnapService:
    return SnapService(make_settings(**overrides), transport=httpx.MockTransport(transport))


def decide(transport: Transport, question_id: str = "thread_routing", choices: dict | None = None, **kw: Any):
    service = service_for(transport)
    return service.decide(question_id, kw.pop("state", STATE), choices or CHOICES, **kw)


def assert_no_decision(result: DecisionResult, status: str, question_id: str = "thread_routing") -> None:
    assert result.status == status, result
    assert result.question_id == question_id
    assert result.choice is None and result.confidence is None and result.probabilities == {}
    assert result.source == "fallback"
    assert KEY not in result.detail


def make_png(width: int, height: int, rows: list[bytes]) -> bytes:
    """A real grayscale PNG from stdlib only. `rows` are `width` bytes each (0 = black, 255 = white)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 per scanline
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grayscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


TINY_PNG = make_png(1, 1, [b"\xff"])
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
FAKE_WEBP = b"RIFF" + struct.pack("<I", 28) + b"WEBP" + b"VP8 " + b"\x00" * 16


# --- request shape ---------------------------------------------------------------------------------------------


def test_request_shape_matches_docs() -> None:
    transport = Transport()
    state = {"new_request": "add milk", "open_threads": [{"thread_id": "t_1", "title": "Sunday groceries"}]}
    result = decide(transport, state=state)
    assert result.status == "ok"

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"].startswith("application/json")

    body = transport.body
    assert set(body) == {"model", "state", "questions"}  # `images` only when an image is supplied
    assert body["model"] == MODEL
    assert body["state"] == state  # the dict goes through unchanged
    assert set(body["questions"]) == {"thread_routing"}
    question = body["questions"]["thread_routing"]
    assert question == {"type": "choice", "instructions": INSTRUCTIONS["thread_routing"], "criteria": CHOICES}


def test_url_and_model_come_from_settings() -> None:
    transport = Transport()
    service = service_for(
        transport, subq_decisions_base_url="https://snap.example.test/v9/", subq_snap_model="subq-snap-next"
    )
    # The validator pins the model echoed back, so the fake echoes the configured one.
    transport.script = [httpx.Response(200, json=good_body(model="subq-snap-next"))]
    assert service.decide("thread_routing", STATE, CHOICES).status == "ok"
    assert str(transport.requests[0].url) == "https://snap.example.test/v9/systemone"
    assert transport.body["model"] == "subq-snap-next"


def test_request_timeout_is_ten_seconds() -> None:
    transport = Transport()
    decide(transport)
    assert transport.requests[0].extensions["timeout"] == {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}


@pytest.mark.parametrize("question_id", sorted(QUESTION_CHOICES))
def test_every_registered_question_builds_a_valid_choice_request(question_id: str) -> None:
    choices = QUESTION_CHOICES[question_id]
    first = next(iter(choices))
    probs = {key: (0.9 if key == first else 0.1 / (len(choices) - 1)) for key in choices}
    transport = Transport(httpx.Response(200, json=good_body(question_id, first, probs)))
    extra: dict[str, Any] = {"image": TINY_PNG, "image_mime": "image/png"} if question_id == "verify_screenshot" else {}
    result = decide(transport, question_id, choices, **extra)
    assert result.status == "ok" and result.choice == first
    question = transport.body["questions"][question_id]
    assert question["type"] == "choice"
    assert question["instructions"] == INSTRUCTIONS[question_id]
    assert question["criteria"] == choices
    assert 2 <= len(question["criteria"]) <= 255


def test_choice_descriptions_may_be_null_and_option_keys_pass_through() -> None:
    choices = {"continue:7f1c2a9e-0000-4000-8000-000000000001": None, "new_thread": "A different topic"}
    first = next(iter(choices))
    transport = Transport(httpx.Response(200, json=good_body("thread_routing", first, {first: 0.8, "new_thread": 0.2})))
    result = decide(transport, choices=choices)
    assert result.status == "ok" and result.choice == first
    assert transport.body["questions"]["thread_routing"]["criteria"] == choices


def test_state_with_uuid_is_json_encoded_and_inputs_are_not_mutated() -> None:
    thread_id = uuid.uuid4()
    state = {"open_threads": [{"thread_id": thread_id}]}
    choices = dict(CHOICES)
    transport = Transport()
    assert decide(transport, state=state, choices=choices).status == "ok"
    assert transport.body["state"] == {"open_threads": [{"thread_id": str(thread_id)}]}
    assert state == {"open_threads": [{"thread_id": thread_id}]} and choices == CHOICES


# --- answers: parsed and validated ------------------------------------------------------------------------------


def test_valid_answer_parsed() -> None:
    probs = {"continue_1": 0.94, "continue_2": 0.04, "new_thread": 0.02}
    result = decide(Transport(httpx.Response(200, json=good_body(probabilities=probs, confidence=0.9))))
    assert result == DecisionResult("thread_routing", "continue_1", 0.9, probs, "snap", "ok", "")
    assert list(result.probabilities) == list(CHOICES)


def test_real_live_response_is_accepted() -> None:
    """The exact 200 body SubQ returned on 2026-10-22 (facts file, Live probe)."""
    body = {
        "model": MODEL,
        "answers": {
            "thread_routing": {
                "type": "choice",
                "choice": "continue_existing_thread",
                "probabilities": {"continue_existing_thread": 0.9991532564163208, "new_thread": 0.0008467980660498142},
                "confidence": 0.9983065128326416,
            }
        },
        "usage": {"input_tokens": 166, "output_tokens": 0},
    }
    choices = {"continue_existing_thread": "same topic", "new_thread": "different topic"}
    result = decide(Transport(httpx.Response(200, json=body)), choices=choices)
    assert result.status == "ok" and result.choice == "continue_existing_thread"
    assert result.confidence == pytest.approx(0.9983065128326416)


def _mutate(path: list[str | int], value: Any) -> dict[str, Any]:
    body = good_body()
    node: Any = body["answers"]["thread_routing"]
    for step in path[:-1]:
        node = node[step]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return body


_DELETE = object()
MALFORMED_BODIES: dict[str, httpx.Response] = {
    "not_json": httpx.Response(200, content=b"<html>gateway says hi</html>"),
    "json_array": httpx.Response(200, json=["thread_routing"]),
    "json_null": httpx.Response(200, content=b"null"),
    "detail_in_200": httpx.Response(200, json={**good_body(), "detail": {"error_type": "x", "message": "m"}}),
    "no_answers": httpx.Response(200, json={"model": MODEL, "usage": {}}),
    "answers_not_object": httpx.Response(200, json={**good_body(), "answers": []}),
    "answers_empty": httpx.Response(200, json={**good_body(), "answers": {}}),
    "answers_wrong_key": httpx.Response(200, json=good_body("some_other_question")),
    "answers_extra_key": httpx.Response(
        200, json={**good_body(), "answers": {**good_body()["answers"], "extra": {"type": "noul", "noul": 0.5}}}
    ),
    "model_mismatch": httpx.Response(200, json=good_body(model="gpt-something")),
    "answer_not_object": httpx.Response(200, json={**good_body(), "answers": {"thread_routing": "continue_1"}}),
    "type_not_choice": httpx.Response(200, json=_mutate(["type"], "noul")),
    "choice_missing": httpx.Response(200, json=_mutate(["choice"], _DELETE)),
    "choice_not_in_choices": httpx.Response(200, json=_mutate(["choice"], "continue_9")),
    "choice_not_a_string": httpx.Response(200, json=_mutate(["choice"], 1)),
    "choice_null": httpx.Response(200, json=_mutate(["choice"], None)),
    "probabilities_missing": httpx.Response(200, json=_mutate(["probabilities"], _DELETE)),
    "probabilities_not_object": httpx.Response(200, json=_mutate(["probabilities"], [0.9, 0.1])),
    "probability_above_one": httpx.Response(
        200, json=_mutate(["probabilities"], {"continue_1": 1.2, "continue_2": -0.1, "new_thread": -0.1})
    ),
    "probability_negative": httpx.Response(
        200, json=_mutate(["probabilities"], {"continue_1": 1.05, "continue_2": 0.0, "new_thread": -0.05})
    ),
    "probabilities_missing_option": httpx.Response(
        200, json=_mutate(["probabilities"], {"continue_1": 0.95, "continue_2": 0.05})
    ),
    "probabilities_extra_option": httpx.Response(
        200,
        json=_mutate(["probabilities"], {"continue_1": 0.9, "continue_2": 0.04, "new_thread": 0.04, "ghost": 0.02}),
    ),
    "probabilities_do_not_sum_to_one": httpx.Response(
        200, json=_mutate(["probabilities"], {"continue_1": 0.5, "continue_2": 0.2, "new_thread": 0.1})
    ),
    "probability_is_a_string": httpx.Response(
        200, json=_mutate(["probabilities"], {"continue_1": "0.94", "continue_2": 0.04, "new_thread": 0.02})
    ),
    "probability_is_a_bool": httpx.Response(
        200, json=_mutate(["probabilities"], {"continue_1": True, "continue_2": 0.0, "new_thread": 0.0})
    ),
    "probability_is_nan": httpx.Response(
        200,
        content=json.dumps(
            _mutate(["probabilities"], {"continue_1": float("nan"), "continue_2": 0.5, "new_thread": 0.5})
        ),
    ),
    "choice_not_argmax": httpx.Response(200, json=_mutate(["choice"], "new_thread")),
    "confidence_missing": httpx.Response(200, json=_mutate(["confidence"], _DELETE)),
    "confidence_above_one": httpx.Response(200, json=_mutate(["confidence"], 1.5)),
    "confidence_negative": httpx.Response(200, json=_mutate(["confidence"], -0.2)),
    "confidence_a_bool": httpx.Response(200, json=_mutate(["confidence"], True)),
    "confidence_a_string": httpx.Response(200, json=_mutate(["confidence"], "0.9")),
    "confidence_nan": httpx.Response(200, content=json.dumps(_mutate(["confidence"], float("nan")))),
    "unexpected_2xx_status": httpx.Response(201, json=good_body()),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_BODIES))
def test_malformed_output_fails_closed(name: str) -> None:
    transport = Transport(MALFORMED_BODIES[name])
    result = decide(transport)
    assert_no_decision(result, "malformed")
    assert result.detail  # says why, without echoing the provider's body
    assert len(transport.requests) == 1  # malformed is final: no retry


def test_malformed_answer_does_not_mark_the_provider_unavailable() -> None:
    settings = make_settings()
    SnapService(settings, transport=httpx.MockTransport(Transport(MALFORMED_BODIES["not_json"]))).decide(
        "thread_routing", STATE, CHOICES
    )
    assert provider_statuses(settings)["subq"].status == "ok"  # the provider answered; the answer was unusable


def test_low_confidence_fails_closed_at_the_default_threshold() -> None:
    result = decide(Transport(httpx.Response(200, json=good_body(confidence=0.59))))
    assert_no_decision(result, "malformed")
    assert "confidence" in result.detail.lower()


def test_confidence_exactly_at_the_threshold_is_accepted() -> None:
    result = decide(Transport(httpx.Response(200, json=good_body(confidence=0.6))))
    assert result.status == "ok" and result.confidence == 0.6


def test_min_confidence_comes_from_settings() -> None:
    assert make_settings().subq_min_confidence == 0.6
    strict = service_for(Transport(httpx.Response(200, json=good_body(confidence=0.8))), subq_min_confidence=0.9)
    assert_no_decision(strict.decide("thread_routing", STATE, CHOICES), "malformed")
    lax = service_for(Transport(httpx.Response(200, json=good_body(confidence=0.3))), subq_min_confidence=0.2)
    assert lax.decide("thread_routing", STATE, CHOICES).status == "ok"


# --- transport and provider errors ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "401"), (402, "402"), (403, "403"), (404, "404"), (301, "301")],
)
def test_credential_and_other_client_errors_are_unavailable_and_never_retried(
    status: int, needle: str, hermetic: list[float]
) -> None:
    transport = Transport(httpx.Response(status, json={"detail": {"error_type": "auth_error", "message": "nope"}}))
    settings = make_settings()
    result = SnapService(settings, transport=httpx.MockTransport(transport)).decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, "unavailable")
    assert needle in result.detail
    assert len(transport.requests) == 1 and hermetic == []  # no retry, no back-off
    status_after = provider_statuses(settings)["subq"]
    assert status_after.status == "unavailable" and needle in status_after.detail


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_retryable_status_is_retried_once_then_unavailable(status: int, hermetic: list[float]) -> None:
    transport = Transport(httpx.Response(status, json={"detail": {"error_type": "server_error", "message": "x"}}))
    settings = make_settings()
    result = SnapService(settings, transport=httpx.MockTransport(transport)).decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, "unavailable")
    assert str(status) in result.detail
    assert len(transport.requests) == 2  # one retry, no more
    assert hermetic == [client_module.RETRY_BACKOFF_SECONDS]
    assert provider_statuses(settings)["subq"].status == "unavailable"


def test_timeout_is_retried_once_then_unavailable(hermetic: list[float]) -> None:
    transport = Transport(httpx.ReadTimeout("timed out"))
    settings = make_settings()
    result = SnapService(settings, transport=httpx.MockTransport(transport)).decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, "unavailable")
    assert "timeout" in result.detail.lower()
    assert len(transport.requests) == 2 and len(hermetic) == 1
    assert provider_statuses(settings)["subq"].status == "unavailable"


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(429, json={"detail": {"error_type": "rate_limit", "message": "slow down"}}),
        httpx.Response(503, json={"detail": {"error_type": "unavailable", "message": "later"}}),
        httpx.ConnectTimeout("connect timed out"),
    ],
    ids=["429", "503", "timeout"],
)
def test_one_retry_can_recover(first: httpx.Response | Exception, hermetic: list[float]) -> None:
    transport = Transport(first, httpx.Response(200, json=good_body()))
    settings = make_settings()
    result = SnapService(settings, transport=httpx.MockTransport(transport)).decide("thread_routing", STATE, CHOICES)
    assert result.status == "ok" and result.choice == "continue_1"
    assert len(transport.requests) == 2 and len(hermetic) == 1
    assert provider_statuses(settings)["subq"].status == "ok"


def test_unavailable_clears_on_the_next_success() -> None:
    settings = make_settings()
    down = Transport(httpx.Response(401, json={}))
    SnapService(settings, transport=httpx.MockTransport(down)).decide("thread_routing", STATE, CHOICES)
    assert provider_statuses(settings)["subq"].status == "unavailable"
    up = Transport()
    assert (
        SnapService(settings, transport=httpx.MockTransport(up)).decide("thread_routing", STATE, CHOICES).status == "ok"
    )
    assert provider_statuses(settings)["subq"].status == "ok"


@pytest.mark.parametrize("status", [400, 422])
def test_400_and_422_are_never_retried_and_do_not_mark_the_provider_down(status: int, hermetic: list[float]) -> None:
    body = {"detail": {"error_type": "validation_error", "message": f"questions/thread_routing: {SECRET_STATE_WORD}"}}
    transport = Transport(httpx.Response(status, json=body))
    settings = make_settings()
    result = SnapService(settings, transport=httpx.MockTransport(transport)).decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, "malformed")
    assert str(status) in result.detail and "validation_error" in result.detail
    assert SECRET_STATE_WORD not in result.detail  # the provider's message can echo request fragments
    assert len(transport.requests) == 1 and hermetic == []
    assert provider_statuses(settings)["subq"].status == "ok"  # the request was wrong, not the provider


def test_connection_error_is_unavailable_and_not_retried(hermetic: list[float]) -> None:
    transport = Transport(httpx.ConnectError("name resolution failed for api.subq.ai"))
    settings = make_settings()
    result = SnapService(settings, transport=httpx.MockTransport(transport)).decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, "unavailable")
    assert "ConnectError" in result.detail and "name resolution" not in result.detail
    assert len(transport.requests) == 1 and hermetic == []


def test_key_and_state_never_appear_in_detail_status_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    hostile = {"detail": {"error_type": f"leak_{KEY}", "message": f"key={KEY} state={SECRET_STATE_WORD}"}}
    state = {"new_request": f"add {SECRET_STATE_WORD} to the list"}
    settings = make_settings()
    scripts: list[Transport] = [
        Transport(httpx.Response(401, json=hostile)),
        Transport(httpx.Response(422, json=hostile)),
        Transport(httpx.Response(500, content=f"boom {KEY} {SECRET_STATE_WORD}".encode())),
        Transport(httpx.Response(200, content=f"not json {KEY} {SECRET_STATE_WORD}".encode())),
        Transport(httpx.ConnectError(f"cannot reach {KEY}")),
        Transport(httpx.ReadTimeout(f"timeout {KEY}")),
    ]
    caplog.set_level(logging.DEBUG)
    for transport in scripts:
        result = SnapService(settings, transport=httpx.MockTransport(transport)).decide(
            "thread_routing", state, CHOICES
        )
        assert KEY not in result.detail and SECRET_STATE_WORD not in result.detail
        status = provider_statuses(settings)["subq"]
        assert KEY not in status.detail and SECRET_STATE_WORD not in status.detail
        assert KEY not in json.dumps(status.as_dict())
    assert KEY not in caplog.text and SECRET_STATE_WORD not in caplog.text


def test_an_unexpected_client_bug_still_returns_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"bug with {KEY}")

    result = service_for(boom).decide("thread_routing", STATE, CHOICES)
    assert_no_decision(result, "unavailable")
    assert result.detail == "unexpected RuntimeError"


# --- pre-send validation (nothing leaves the process) ----------------------------------------------------------


def explode(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"nothing should be sent, got {request.method} {request.url}")


def _direct(question_id: str, choices: Any, state: Any = None, **kw: Any) -> DecisionResult:
    client = SnapClient(make_settings(), httpx.MockTransport(explode))
    return client.decide(question_id, "Decide.", STATE if state is None else state, choices, **kw)


def test_question_id_over_100_chars_is_rejected_before_sending() -> None:
    result = _direct("q" * 101, CHOICES)
    assert_no_decision(result, "unavailable", "q" * 101)
    assert "question_id" in result.detail


def test_question_id_of_exactly_100_chars_is_sent() -> None:
    question_id = "q" * 100
    transport = Transport(httpx.Response(200, json=good_body(question_id)))
    client = SnapClient(make_settings(), httpx.MockTransport(transport))
    assert client.decide(question_id, "Decide.", STATE, CHOICES).status == "ok"
    assert list(transport.body["questions"]) == [question_id]


@pytest.mark.parametrize("question_id", ["", None, 7])
def test_blank_or_non_string_question_id_is_rejected_before_sending(question_id: Any) -> None:
    assert _direct(question_id, CHOICES).status == "unavailable"


def test_fewer_than_two_choices_is_rejected_before_sending() -> None:
    assert_no_decision(_direct("thread_routing", {"only_one": "x"}), "unavailable")
    assert_no_decision(_direct("thread_routing", {}), "unavailable")


def test_more_than_255_choices_is_rejected_before_sending() -> None:
    assert_no_decision(_direct("thread_routing", {f"c{i}": None for i in range(256)}), "unavailable")


def test_exactly_255_choices_is_sent() -> None:
    choices = {f"c{i}": None for i in range(255)}
    probs = {key: 0.0 for key in choices}
    probs["c0"] = 1.0
    transport = Transport(httpx.Response(200, json=good_body("thread_routing", "c0", probs, 1.0)))
    client = SnapClient(make_settings(), httpx.MockTransport(transport))
    result = client.decide("thread_routing", "Decide.", STATE, choices)
    assert result.status == "ok" and len(transport.body["questions"]["thread_routing"]["criteria"]) == 255


@pytest.mark.parametrize(
    "choices",
    [{"": "blank key", "b": "x"}, {"a": 5, "b": "x"}, {1: "int key", "b": "x"}, ["a", "b"]],
    ids=["blank_key", "non_string_description", "non_string_key", "not_a_dict"],
)
def test_malformed_choices_are_rejected_before_sending(choices: Any) -> None:
    assert_no_decision(_direct("thread_routing", choices), "unavailable")


def test_state_that_cannot_be_encoded_is_rejected_before_sending() -> None:
    assert_no_decision(_direct("thread_routing", CHOICES, state={"x": float("nan")}), "unavailable")
    assert_no_decision(_direct("thread_routing", CHOICES, state={"x": "y" * (14 * 1024 * 1024)}), "unavailable")


def test_pre_send_rejection_through_the_service_leaves_the_provider_ok() -> None:
    settings = make_settings()
    service = SnapService(settings, transport=httpx.MockTransport(explode))
    result = service.decide("thread_routing", STATE, {"only_one": "x"})
    assert_no_decision(result, "unavailable")
    assert provider_statuses(settings)["subq"].status == "ok"  # our bug, not an outage


# --- image input (documented: top-level `images`, mediaType + raw base64) --------------------------------------


def test_decide_signature_adds_only_optional_keywords() -> None:
    params = inspect.signature(SnapService.decide).parameters
    assert list(params) == ["self", "question_id", "state", "choices", "image", "image_mime"]
    assert [params[name].kind for name in ("question_id", "state", "choices")] == [
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    ] * 3
    assert params["image"].kind is inspect.Parameter.KEYWORD_ONLY and params["image"].default is None
    assert params["image_mime"].kind is inspect.Parameter.KEYWORD_ONLY and params["image_mime"].default == "image/jpeg"
    assert [f.name for f in dataclasses.fields(DecisionResult)] == [
        "question_id",
        "choice",
        "confidence",
        "probabilities",
        "source",
        "status",
        "detail",
    ]


@pytest.mark.parametrize(
    ("image", "mime"),
    [(TINY_PNG, "image/png"), (FAKE_JPEG, "image/jpeg"), (FAKE_WEBP, "image/webp")],
    ids=["png", "jpeg", "webp"],
)
def test_image_request_shape_matches_docs(image: bytes, mime: str) -> None:
    choices = QUESTION_CHOICES["verify_screenshot"]
    probs = {"verified": 0.95, "not_verified": 0.03, "needs_human": 0.02}
    transport = Transport(httpx.Response(200, json=good_body("verify_screenshot", "verified", probs)))
    state = {"expected_items": ["milk", "eggs"]}
    result = decide(transport, "verify_screenshot", choices, state=state, image=image, image_mime=mime)
    assert result.status == "ok" and result.choice == "verified" and result.source == "snap"

    body = transport.body
    assert set(body) == {"model", "state", "questions", "images"}
    assert body["state"] == state
    assert len(body["images"]) == 1
    entry = body["images"][0]
    assert set(entry) == {"mediaType", "base64"}
    assert entry["mediaType"] == mime
    assert not entry["base64"].startswith("data:") and "\n" not in entry["base64"]
    assert base64.b64decode(entry["base64"], validate=True) == image  # raw base64 of the exact bytes


def test_image_default_mime_is_jpeg() -> None:
    probs = {"verified": 0.9, "not_verified": 0.06, "needs_human": 0.04}
    transport = Transport(httpx.Response(200, json=good_body("verify_screenshot", "verified", probs)))
    decide(transport, "verify_screenshot", QUESTION_CHOICES["verify_screenshot"], image=FAKE_JPEG)
    assert transport.body["images"][0]["mediaType"] == "image/jpeg"


@pytest.mark.parametrize(
    ("image", "mime", "needle"),
    [
        (b"", "image/png", "empty"),
        (TINY_PNG, "image/gif", "mediaType"),
        (TINY_PNG, "IMAGE/PNG", "mediaType"),
        (TINY_PNG, "image/jpeg", "match"),  # PNG bytes labelled JPEG: SubQ would 422 it
        (FAKE_JPEG, "image/png", "match"),
        (b"not an image at all", "image/png", "match"),
        (b"\xff\xd8\xff" + b"\x00" * (4 * 1024 * 1024), "image/jpeg", "4 MiB"),
    ],
    ids=["empty", "gif", "uppercase_mime", "png_as_jpeg", "jpeg_as_png", "garbage", "over_4_mib"],
)
def test_bad_images_are_rejected_before_sending(image: bytes, mime: str, needle: str) -> None:
    service = SnapService(make_settings(), transport=httpx.MockTransport(explode))
    result = service.decide(
        "verify_screenshot",
        {"expected_items": ["milk"]},
        QUESTION_CHOICES["verify_screenshot"],
        image=image,
        image_mime=mime,
    )
    assert_no_decision(result, "unavailable", "verify_screenshot")
    assert needle in result.detail
    assert provider_statuses(make_settings())["subq"].status == "ok"


def test_image_of_exactly_4_mib_is_sent() -> None:
    image = b"\xff\xd8\xff" + b"\x00" * (4 * 1024 * 1024 - 3)
    assert len(image) == 4 * 1024 * 1024
    probs = {"verified": 0.9, "not_verified": 0.06, "needs_human": 0.04}
    transport = Transport(httpx.Response(200, json=good_body("verify_screenshot", "verified", probs)))
    result = decide(transport, "verify_screenshot", QUESTION_CHOICES["verify_screenshot"], image=image)
    assert result.status == "ok"
    assert len(base64.b64decode(transport.body["images"][0]["base64"])) == 4 * 1024 * 1024


def test_verify_screenshot_without_an_image_never_asks_snap() -> None:
    """A text-only 'verified' would be a guess. The caller falls back to an honest not_verified."""
    service = SnapService(make_settings(), transport=httpx.MockTransport(explode))
    result = service.decide("verify_screenshot", {"expected_items": ["milk"]}, QUESTION_CHOICES["verify_screenshot"])
    assert_no_decision(result, "unavailable", "verify_screenshot")
    assert "image" in result.detail
    assert provider_statuses(make_settings())["subq"].status == "ok"


def test_text_questions_send_no_images_key() -> None:
    transport = Transport()
    decide(transport)
    assert "images" not in transport.body


def test_disabled_and_misconfigured_with_an_image_never_call_out() -> None:
    choices = QUESTION_CHOICES["verify_screenshot"]
    off = SnapService(Settings(_env_file=None), transport=httpx.MockTransport(explode))
    assert_no_decision(
        off.decide("verify_screenshot", {}, choices, image=TINY_PNG, image_mime="image/png"),
        "disabled",
        "verify_screenshot",
    )
    no_key = SnapService(Settings(_env_file=None, enable_subq_snap=True), transport=httpx.MockTransport(explode))
    result = no_key.decide("verify_screenshot", {}, choices, image=TINY_PNG, image_mime="image/png")
    assert_no_decision(result, "unavailable", "verify_screenshot")
    assert "SUBQ_API_KEY" in result.detail


def test_image_bytes_never_appear_in_logs_or_detail(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    encoded = base64.b64encode(TINY_PNG).decode()
    transport = Transport(httpx.Response(500, content=encoded.encode()))
    service = service_for(transport)
    result = service.decide(
        "verify_screenshot", {}, QUESTION_CHOICES["verify_screenshot"], image=TINY_PNG, image_mime="image/png"
    )
    assert encoded not in result.detail and encoded not in caplog.text


# --- the PNG helper itself (used by the live screenshot probe) ---------------------------------------------------


def test_make_png_produces_a_valid_png() -> None:
    png = make_png(2, 2, [b"\x00\xff", b"\xff\x00"])
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (2, 2)
