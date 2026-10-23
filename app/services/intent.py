"""The Day-1 deterministic request parser (spec section 13). No model output decides what an action does.

It understands: "add X and Y to <list>" (shopping list, drives a browser when there are items), reminders, chores,
purchases (with a dollar amount), bookings, the sign-in-wall takeover demo, and falls back to a general note.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.errors import ValidationFailed
from app.models import TaskKind

MAX_ITEMS = 10
MAX_ITEM_CHARS = 40
MAX_TITLE_CHARS = 200
MAX_LIST_NAME_CHARS = 80

LIST_ADD_ITEMS = "list_add_items"
SIGN_IN_CHECK = "sign_in_check"

_LEADERS = (
    re.compile(r"^(?:can|could|would|will)\s+you\s+", re.I),
    re.compile(r"^please\b[\s,]*", re.I),
    re.compile(r"^(?:hey|hi|ok|okay)[\s,]+(?=uhlexa\b)", re.I),
    re.compile(r"^(?:ask|tell)\s+uhlexa\b(?:\s+to\b)?[\s,]*", re.I),
    re.compile(r"^uhlexa\b[\s,:]*", re.I),
)
_TRAILERS = (re.compile(r"[\s.!?,;:]+$"), re.compile(r"[\s,]*\bplease$", re.I))

_LIST_ADD = re.compile(
    r"^(?:add|put|stick)\s+(?:(?P<items>.+?)\s+)?(?:to|on|onto|into)\s+(?P<list>.+)$",
    re.I,
)
_SPLIT_ITEMS = re.compile(r"\s*(?:,|;|&|\+|\band\b)\s*", re.I)
_LEADING_ARTICLE = re.compile(r"^(?:a|an|the|some|more|another|my|our|few)\s+", re.I)
_PLACEHOLDERS = frozenset(
    {"stuff", "things", "thing", "something", "anything", "everything", "it", "them", "item", "items", "some", "more"}
)
_GENERIC_LISTS = frozenset({"list", "lists", ""})

_REMIND = re.compile(r"^remind\b|\breminder\b", re.I)
_CHORE = re.compile(r"\bchores?\b", re.I)
_PURCHASE = re.compile(r"^(?:buy|order|purchase|get me)\b", re.I)
_BOOKING = re.compile(r"^(?:book|reserve|schedule)\b", re.I)
_SIGN_IN_WALL = re.compile(r"\bsign[\s-]?in wall\b|\btakeover demo\b", re.I)

_DOLLARS = (
    re.compile(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)"),
    re.compile(r"\b(\d[\d,]*(?:\.\d{1,2})?)\s*(?:dollars?|bucks|usd)\b", re.I),
)


@dataclass(frozen=True)
class Intent:
    kind: TaskKind
    title: str
    items: list[str]
    list_name: str | None
    amount_cents: int | None
    will_use_browser: bool
    truncated: bool = False
    browser_task_type: str | None = None


def _strip_wrappers(text: str) -> str:
    """Drop assistant addressing ("Ask Uhlexa to ...", "please") from the front and politeness from the back."""
    text = " ".join(text.split())
    previous = None
    while previous != text:
        previous = text
        for pattern in _LEADERS:
            text = pattern.sub("", text, count=1)
        for pattern in _TRAILERS:
            text = pattern.sub("", text, count=1)
        text = text.strip()
    return text


def _clean_item(raw: str) -> str:
    item = raw.strip(" \t.!?\"'`")
    item = re.sub(r"[\s,]*\bplease$", "", item, flags=re.I).strip()
    return _LEADING_ARTICLE.sub("", item, count=1).strip(" .!?\"'`")


def _parse_items(raw: str | None) -> tuple[list[str], bool]:
    """Split "milk, eggs, and bread" into clean, de-duplicated items (at most 10). Returns (items, truncated)."""
    seen: set[str] = set()
    items: list[str] = []
    for token in _SPLIT_ITEMS.split(raw or ""):
        item = _clean_item(token)
        if not item or item.lower() in _PLACEHOLDERS or item.lower() in seen:
            continue
        seen.add(item.lower())
        items.append(item)
    truncated = len(items) > MAX_ITEMS
    items = items[:MAX_ITEMS]
    for item in items:
        if len(item) > MAX_ITEM_CHARS:
            raise ValidationFailed(f"Each item must be {MAX_ITEM_CHARS} characters or fewer.")
    return items, truncated


def _list_name(raw: str) -> str | None:
    name = raw.lower().strip(" .!?,;:\"'`")
    name = re.sub(r"^(?:the|my|our|a|an)\s+", "", name)
    name = re.sub(r"\s+lists?$", "", name)
    name = re.sub(r"^(?:the|my|our)\s+", "", name).strip()
    return None if name in _GENERIC_LISTS else name[:MAX_LIST_NAME_CHARS]


def _amount_cents(text: str) -> int | None:
    for pattern in _DOLLARS:
        if match := pattern.search(text):
            try:
                return int((Decimal(match.group(1).replace(",", "")) * 100).to_integral_value())
            except InvalidOperation:
                return None
    return None


def _title(text: str) -> str:
    title = text[:1].upper() + text[1:]
    return title if len(title) <= MAX_TITLE_CHARS else title[: MAX_TITLE_CHARS - 1] + "…"


def parse_intent(text: str) -> Intent:
    """Turn one request into a typed `Intent`. Raises ValidationFailed for a blank request or an over-long item."""
    cleaned = _strip_wrappers(text)
    if not cleaned:
        raise ValidationFailed("Tell me what you need, for example: add milk and eggs to the grocery list.")
    title = _title(cleaned)

    if match := _LIST_ADD.match(cleaned):
        items, truncated = _parse_items(match.group("items"))
        return Intent(
            kind=TaskKind.SHOPPING_LIST,
            title=title,
            items=items,
            list_name=_list_name(match.group("list")),
            amount_cents=None,
            will_use_browser=bool(items),
            truncated=truncated,
            browser_task_type=LIST_ADD_ITEMS if items else None,
        )
    if _SIGN_IN_WALL.search(cleaned):
        return Intent(TaskKind.BROWSER, title, [], None, None, True, browser_task_type=SIGN_IN_CHECK)
    if _REMIND.search(cleaned):
        kind = TaskKind.REMINDER
    elif _CHORE.search(cleaned):
        kind = TaskKind.CHORE
    elif _PURCHASE.match(cleaned):
        kind = TaskKind.PURCHASE
    elif _BOOKING.match(cleaned):
        kind = TaskKind.BOOKING
    else:
        kind = TaskKind.GENERAL
    amount = _amount_cents(cleaned) if kind in (TaskKind.PURCHASE, TaskKind.BOOKING) else None
    return Intent(kind, title, [], None, amount, False)
