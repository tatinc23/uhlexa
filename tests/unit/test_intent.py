"""The Day-1 deterministic intent parser: no model output ever decides what an action does (spec section 13)."""

import pytest

from app.errors import ValidationFailed
from app.models import TaskKind
from app.services.intent import Intent, parse_intent
from app.services.task_service import parse_intent as task_service_parse_intent


def test_parse_items() -> None:
    intent = parse_intent("add milk and eggs to Sunday groceries")
    assert intent.items == ["milk", "eggs"]
    assert intent.list_name == "sunday groceries"
    assert intent.kind == TaskKind.SHOPPING_LIST
    assert intent.will_use_browser is True
    assert intent.title == "Add milk and eggs to Sunday groceries"


def test_task_service_reexports_the_parser() -> None:
    assert task_service_parse_intent is parse_intent


def test_demo_sentence_with_assistant_prefix_and_trailing_period() -> None:
    intent = parse_intent("Ask Uhlexa to add milk and eggs to Sunday groceries.")
    assert (intent.items, intent.list_name) == (["milk", "eggs"], "sunday groceries")


@pytest.mark.parametrize(
    "text",
    [
        "Uhlexa, please add milk and eggs to Sunday groceries",
        "can you ask uhlexa to add milk and eggs to the Sunday groceries list please",
        "  ADD   Milk   and   Eggs   to   Sunday   Groceries!  ",
    ],
)
def test_prefixes_case_and_whitespace_are_forgiven(text: str) -> None:
    intent = parse_intent(text)
    assert [item.lower() for item in intent.items] == ["milk", "eggs"]
    assert intent.kind == TaskKind.SHOPPING_LIST


def test_reminder_has_no_browser() -> None:
    intent = parse_intent("remind me to call grandma")
    assert intent.kind == TaskKind.REMINDER
    assert intent.will_use_browser is False
    assert intent.items == []
    assert intent.title == "Remind me to call grandma"


def test_comma_and_oxford_list_gives_three_items_and_no_named_list() -> None:
    intent = parse_intent("add milk, eggs, and bread to the list")
    assert intent.items == ["milk", "eggs", "bread"]
    assert intent.list_name is None


def test_more_than_ten_items_are_truncated_and_flagged() -> None:
    names = [f"item{i}" for i in range(14)]
    intent = parse_intent(f"add {', '.join(names)} to the weekend list")
    assert intent.items == names[:10]
    assert intent.truncated is True
    assert parse_intent("add milk to groceries").truncated is False


def test_item_over_forty_characters_is_rejected() -> None:
    long_item = "x" * 41
    with pytest.raises(ValidationFailed, match="40"):
        parse_intent(f"add milk and {long_item} to groceries")
    assert parse_intent(f"add {'x' * 40} to groceries").items == ["x" * 40]


@pytest.mark.parametrize(
    "text", ["add stuff to groceries", "add  and  to groceries", "add things and stuff to groceries"]
)
def test_requests_with_no_parseable_items(text: str) -> None:
    intent = parse_intent(text)
    assert intent.kind == TaskKind.SHOPPING_LIST
    assert intent.items == []
    assert intent.will_use_browser is False
    assert intent.list_name == "groceries"


def test_duplicates_are_dropped_case_insensitively_in_order() -> None:
    assert parse_intent("add Milk, eggs, milk and EGGS and bread to groceries").items == ["Milk", "eggs", "bread"]


def test_leading_articles_are_stripped_from_items() -> None:
    assert parse_intent("add some milk and an apple to groceries").items == ["milk", "apple"]


@pytest.mark.parametrize(
    ("text", "kind", "cents"),
    [
        ("buy a $60 gift for grandma", TaskKind.PURCHASE, 6000),
        ("order pizza for $12.50", TaskKind.PURCHASE, 1250),
        ("buy a gift for 25 dollars", TaskKind.PURCHASE, 2500),
        ("order new soccer cleats", TaskKind.PURCHASE, None),
        ("book a table for four on Friday", TaskKind.BOOKING, None),
        ("do the dishes chore for Max", TaskKind.CHORE, None),
        ("what is the weather", TaskKind.GENERAL, None),
    ],
)
def test_other_kinds_and_amounts(text: str, kind: TaskKind, cents: int | None) -> None:
    intent = parse_intent(text)
    assert (intent.kind, intent.amount_cents) == (kind, cents)
    assert intent.will_use_browser is False


def test_list_requests_ignore_dollar_amounts_in_item_text() -> None:
    assert parse_intent("add $5 candy to groceries").amount_cents is None


@pytest.mark.parametrize("text", ["", "   ", "Ask Uhlexa to", "please"])
def test_blank_requests_are_rejected(text: str) -> None:
    with pytest.raises(ValidationFailed):
        parse_intent(text)


def test_intent_is_a_plain_value() -> None:
    intent = parse_intent("add milk to groceries")
    assert isinstance(intent, Intent)
    assert intent == parse_intent("add milk to groceries")
