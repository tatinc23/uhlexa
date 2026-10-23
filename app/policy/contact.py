"""Outbound contact policy (addendum 21.8): may this adult's agent text or call that person, right now?

Pure: no database, no settings object, no clock. The service gathers the facts (roles, households, the target's
consented bindings, recent send counts) and this decides. Checks run in a fixed order and the first failure wins, so a
refusal always names one reason a person can act on.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

CHANNELS = ("text", "imessage", "call", "email")
MAX_TEXT_CHARS = 300
MAX_CALL_CHARS = 500
ADULT_ROLES = frozenset({"guardian", "adult"})

# Anything that looks like a link: a scheme, `www.`, or a bare domain on a common TLD. Models can be talked into sending
# phishing links, so no link goes out at all.
_LINK = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|ai|co|us|me|app|dev|ly|gg|xyz|link|info|biz)\b)"
)

REASONS = {
    "disabled": "Outbound contact is switched off.",
    "unknown_channel": "That is not a channel Uhlexa can use.",
    "not_adult": "Only an adult can contact someone.",
    "child_target": "Uhlexa does not contact children.",
    "different_household": "That person is in a different household and is not a trusted contact.",
    "link_not_allowed": "A message cannot contain a link.",
    "message_too_long": "The message is too long.",
    "no_binding": "That person has no number or address on this channel.",
    "no_consent": "That person has not agreed to be contacted on this channel.",
    "rate_limited": "Too many messages or calls recently. Try again later.",
}


@dataclass(frozen=True)
class Party:
    user_id: str
    household_id: str
    role: str


@dataclass(frozen=True)
class ContactSettings:
    outbound_enabled: bool = False
    calls_enabled: bool = False
    texts_per_hour: int = 6
    calls_per_hour: int = 2
    per_day: int = 20  # per sender; the household as a whole may send 3x this


@dataclass(frozen=True)
class RecentCounts:
    texts_last_hour: int = 0
    calls_last_hour: int = 0
    sender_last_day: int = 0
    household_last_day: int = 0


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason_code: str | None = None

    @property
    def detail(self) -> str:
        return REASONS.get(self.reason_code or "", "")


def _deny(code: str) -> ContactDecision:
    return ContactDecision(False, code)


def evaluate_contact(
    sender: Party,
    target: Party,
    channel: str,
    message: str,
    *,
    trusted_link: bool,
    bindings: Mapping[str, bool],
    settings: ContactSettings,
    recent_counts: RecentCounts,
) -> ContactDecision:
    """`bindings` maps each channel the target has an address for to whether they recorded consent for it.
    `trusted_link` is True only for an ACTIVE trusted-contacts link between exactly these two adults."""
    if channel not in CHANNELS:
        return _deny("unknown_channel")
    if not settings.outbound_enabled or (channel == "call" and not settings.calls_enabled):
        return _deny("disabled")
    if sender.role not in ADULT_ROLES:
        return _deny("not_adult")
    if target.role == "child":
        return _deny("child_target")
    if target.role not in ADULT_ROLES:
        return _deny("not_adult")
    if sender.household_id != target.household_id and not trusted_link:
        return _deny("different_household")
    if _LINK.search(message):
        return _deny("link_not_allowed")
    if len(message) > (MAX_CALL_CHARS if channel == "call" else MAX_TEXT_CHARS):
        return _deny("message_too_long")
    if channel not in bindings:
        return _deny("no_binding")
    if not bindings[channel]:
        return _deny("no_consent")
    over_hour = (
        recent_counts.calls_last_hour >= settings.calls_per_hour
        if channel == "call"
        else recent_counts.texts_last_hour >= settings.texts_per_hour
    )
    if (
        over_hour
        or recent_counts.sender_last_day >= settings.per_day
        or recent_counts.household_last_day >= settings.per_day * 3
    ):
        return _deny("rate_limited")
    return ContactDecision(True)
