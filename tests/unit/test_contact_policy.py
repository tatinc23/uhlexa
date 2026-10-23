"""The pure outbound-contact policy (addendum 21.8)."""

import pytest

from app.policy.contact import ContactSettings, Party, RecentCounts, evaluate_contact

ALEX = Party("u-alex", "h-parker", "guardian")
JORDAN = Party("u-katie", "h-parker", "adult")
MAX = Party("u-max", "h-parker", "child")
SAM = Party("u-sam", "h-lee", "adult")
ON = ContactSettings(outbound_enabled=True, calls_enabled=True)
ALL_CHANNELS = {"text": True, "imessage": True, "call": True, "email": True}


def decide(target=JORDAN, channel="text", message="Running late", **kw):
    args = {
        "sender": ALEX,
        "trusted_link": False,
        "bindings": ALL_CHANNELS,
        "settings": ON,
        "recent_counts": RecentCounts(),
    }
    args.update(kw)
    return evaluate_contact(args.pop("sender"), target, channel, message, **args)


def test_an_adult_may_contact_a_consenting_household_adult() -> None:
    assert decide().allowed and decide().reason_code is None


def test_disabled_switches() -> None:
    assert decide(settings=ContactSettings()).reason_code == "disabled"
    assert decide(channel="call", settings=ContactSettings(outbound_enabled=True)).reason_code == "disabled"
    assert decide(channel="text", settings=ContactSettings(outbound_enabled=True)).allowed


def test_child_sender_and_child_target() -> None:
    assert decide(sender=MAX).reason_code == "not_adult"
    assert decide(target=MAX).reason_code == "child_target"


def test_a_different_household_needs_an_active_trusted_link() -> None:
    assert decide(target=SAM).reason_code == "different_household"
    assert decide(target=SAM, trusted_link=True).allowed


def test_a_link_never_reaches_a_child() -> None:
    assert decide(target=Party("u-kid", "h-lee", "child"), trusted_link=True).reason_code == "child_target"


@pytest.mark.parametrize(
    "message", ["see https://x.example/a", "go to www.evil.test", "visit evil.com now", "ftp://a", "x.io"]
)
def test_links_are_refused(message: str) -> None:
    assert decide(message=message).reason_code == "link_not_allowed"


def test_length_limits_differ_for_calls() -> None:
    assert decide(message="a" * 300).allowed
    assert decide(message="a" * 301).reason_code == "message_too_long"
    assert decide(channel="call", message="a" * 500).allowed
    assert decide(channel="call", message="a" * 501).reason_code == "message_too_long"


def test_binding_and_consent() -> None:
    assert decide(bindings={}).reason_code == "no_binding"
    assert decide(bindings={"text": False}).reason_code == "no_consent"
    assert decide(channel="email", bindings={"text": True}).reason_code == "no_binding"


def test_rate_limits() -> None:
    assert decide(recent_counts=RecentCounts(texts_last_hour=6)).reason_code == "rate_limited"
    assert decide(recent_counts=RecentCounts(texts_last_hour=5)).allowed
    assert decide(channel="call", recent_counts=RecentCounts(calls_last_hour=2)).reason_code == "rate_limited"
    assert decide(recent_counts=RecentCounts(sender_last_day=20)).reason_code == "rate_limited"
    assert decide(recent_counts=RecentCounts(household_last_day=60)).reason_code == "rate_limited"


def test_unknown_channel() -> None:
    assert decide(channel="pigeon").reason_code == "unknown_channel"
