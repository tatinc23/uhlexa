"""Wake gate (spec 22.5): modes, phrase variants, follow-up window, tap-to-talk."""

import pytest

from app.voice.wake import WAKE_MODES, WakeGate, canonical_wake


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_modes_are_the_documented_three() -> None:
    assert WAKE_MODES == ("wake_word", "follow_up", "always")


def test_wake_word_mode_requires_phrase_each_time(clock: FakeClock) -> None:
    gate = WakeGate("wake_word", clock=clock)
    first = gate.decide("Uhlexa, add milk to Sunday groceries")
    assert first.respond and first.text == "add milk to Sunday groceries"
    gate.reply_finished()
    clock.now += 1
    assert not gate.decide("and eggs").respond


def test_follow_up_window_opens_after_reply_and_expires(clock: FakeClock) -> None:
    gate = WakeGate("follow_up", follow_up_s=8.0, clock=clock)
    assert not gate.awake
    assert not gate.decide("add milk").respond
    assert gate.decide("Uhlexa add milk").respond
    gate.reply_finished()
    clock.now += 7.9
    follow = gate.decide("and eggs too")
    assert follow.respond and follow.text == "and eggs too"
    gate.reply_finished()
    clock.now += 8.1
    assert not gate.awake
    assert not gate.decide("and bread").respond


def test_always_mode_responds_without_phrase(clock: FakeClock) -> None:
    gate = WakeGate("always", clock=clock)
    assert gate.awake
    decision = gate.decide("what's on the family board")
    assert decision.respond and decision.text == "what's on the family board"
    stripped = gate.decide("Uhlexa, what's on the board")
    assert stripped.text == "what's on the board"


@pytest.mark.parametrize(
    "heard",
    [
        "Uhlexa, add milk",
        "hey uhlexa add milk",
        "Alexa, add milk",
        "uh lexa add milk",
        "Hey, Uhlexa! add milk",
        "OK Uhlexa: add milk",
        "Ulexa add milk",
        "u lexa, add milk",
        "Ooh lexa add milk",
        "Hulexa add milk",
        "Hey Alexa add milk",
    ],
)
def test_wake_variants(clock: FakeClock, heard: str) -> None:
    decision = WakeGate("wake_word", clock=clock).decide(heard)
    assert decision.respond, heard
    assert decision.text == "add milk", heard
    assert not decision.bare_wake


@pytest.mark.parametrize("heard", ["Uhlexa.", "hey uhlexa", "Alexa?"])
def test_bare_wake_phrase_opens_window(clock: FakeClock, heard: str) -> None:
    gate = WakeGate("follow_up", clock=clock)
    decision = gate.decide(heard)
    assert decision.respond and decision.bare_wake and decision.text == ""
    assert gate.awake
    clock.now += 5
    assert gate.decide("add eggs").respond


def test_open_window_from_tap(clock: FakeClock) -> None:
    gate = WakeGate("wake_word", follow_up_s=8.0, clock=clock)
    gate.open_window()
    assert gate.awake
    assert gate.decide("add milk").respond
    clock.now += 9
    assert not gate.decide("add eggs").respond


def test_phrase_not_at_start_does_not_wake(clock: FakeClock) -> None:
    gate = WakeGate("wake_word", clock=clock)
    assert not gate.decide("tell alexa hi").respond
    assert not gate.decide("the uhlexa app").respond


def test_empty_or_punctuation_only_transcript_never_responds(clock: FakeClock) -> None:
    gate = WakeGate("always", clock=clock)
    assert not gate.decide("").respond
    assert not gate.decide("  ...  ").respond


def test_set_mode_switches_live(clock: FakeClock) -> None:
    gate = WakeGate("wake_word", clock=clock)
    assert not gate.decide("add milk").respond
    gate.set_mode("always")
    assert gate.decide("add milk").respond
    with pytest.raises(ValueError):
        gate.set_mode("whenever")


@pytest.mark.parametrize(
    ("heard", "shown"),
    [
        ("Alexa, add milk and eggs.", "Uhlexa, add milk and eggs."),
        ("Hey Alexa add milk", "Uhlexa, add milk"),
        ("Alexa,", "Uhlexa."),
        ("add milk", "add milk"),
        ("tell alexa hi", "tell alexa hi"),
    ],
)
def test_canonical_wake_for_captions(heard: str, shown: str) -> None:
    assert canonical_wake(heard) == shown


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ValueError):
        WakeGate("sometimes")
