"""`snap_verifier`: the screenshot reader the Maritime runner calls. Snap advises; unclear means `not_verified`."""

import pytest

from app.providers.subq.models import DecisionResult
from app.services import browser_task_service
from app.services.browser_task_service import MAX_QUESTION_CHARS, snap_verifier
from tests.helpers_services import FakeSnap, real_snap

JPEG = b"\xff\xd8\xff\xe0jpeg-bytes"
PNG = b"\x89PNG\r\n\x1a\npng-bytes"
WEBP = b"RIFF\x10\x00\x00\x00WEBPwebp-bytes"


@pytest.mark.parametrize("verdict", ["verified", "not_verified", "needs_human"])
def test_a_clean_answer_is_passed_through(verdict: str) -> None:
    snap = FakeSnap({"verify_screenshot": verdict})
    assert snap_verifier(snap)(JPEG, "Is milk on the list?") == verdict  # type: ignore[arg-type]


def test_the_image_goes_to_snap_with_its_real_media_type_and_a_minimal_state() -> None:
    snap = FakeSnap({"verify_screenshot": "verified"})
    verify = snap_verifier(snap)  # type: ignore[arg-type]
    for image, mime in ((JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp"), (b"unknown", "image/jpeg")):
        verify(image, "Is milk on the list?")
        assert snap.images[-1] == ("verify_screenshot", image, mime)
    (_, state, choices) = snap.calls[0]
    assert state == {"check": "Is milk on the list?"}  # the question and nothing else: no names, no ids
    assert set(choices) == {"verified", "not_verified", "needs_human"}


@pytest.mark.parametrize(
    "answer",
    [
        None,  # FakeSnap has no answer: an unavailable fallback
        DecisionResult.fallback("verify_screenshot", "disabled", "Snap is off"),
        DecisionResult.fallback("verify_screenshot", "unavailable", "outage"),
        DecisionResult.fallback("verify_screenshot", "malformed", "garbage"),
        DecisionResult("verify_screenshot", "maybe", 0.9, {}, "snap", "ok", "not one of the three"),
        DecisionResult("verify_screenshot", None, None, {}, "snap", "ok", "no choice"),
        DecisionResult("verify_screenshot", "verified", 0.9, {}, "snap", "unavailable", "status says it failed"),
    ],
)
def test_anything_but_a_clean_answer_is_not_verified(answer: DecisionResult | None) -> None:
    snap = FakeSnap({} if answer is None else {"verify_screenshot": answer})
    assert snap_verifier(snap)(JPEG, "Is milk on the list?") == "not_verified"  # type: ignore[arg-type]


def test_disabled_snap_never_claims_a_screenshot_is_verified() -> None:
    assert snap_verifier(real_snap())(JPEG, "Is milk on the list?") == "not_verified"


def test_the_whole_question_reaches_snap_up_to_the_cap() -> None:
    """The runner lists every item it wants to see (up to 10 x 40 characters); cutting the list short would let Snap
    'verify' items it was never asked about."""
    items = ", ".join(f"'{'x' * 40}'" for _ in range(10))
    question = f"Does the todo list on screen show all of these items: {items}? Answer needs_human if a login is shown."
    assert len(question) < MAX_QUESTION_CHARS
    snap = FakeSnap({"verify_screenshot": "verified"})
    snap_verifier(snap)(JPEG, question)  # type: ignore[arg-type]
    assert snap.calls[0][1] == {"check": question}
    snap_verifier(snap)(JPEG, "q" * (MAX_QUESTION_CHARS + 500))  # type: ignore[arg-type]
    assert len(snap.calls[1][1]["check"]) == MAX_QUESTION_CHARS


def test_image_mime_sniffing() -> None:
    assert browser_task_service._image_mime(PNG) == "image/png"
    assert browser_task_service._image_mime(WEBP) == "image/webp"
    assert browser_task_service._image_mime(JPEG) == "image/jpeg"
    assert browser_task_service._image_mime(b"") == "image/jpeg"
