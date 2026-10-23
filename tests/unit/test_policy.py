"""Policy engine: spec section 9. Pure functions, so no fixtures and no DB."""

from __future__ import annotations

import dataclasses
import uuid
from enum import StrEnum

import pytest

from app.policy import (
    PolicyDecision,
    can_access_conversation,
    can_decide,
    evaluate_task,
    merge_model_advice,
)

SETTINGS = {"adult_spend_approval_threshold_cents": 5000}


def evaluate(
    role: str = "adult",
    kind: str = "general",
    amount_cents: int | None = None,
    browser: bool = False,
    settings: dict | None = SETTINGS,
    approvals_enabled: bool = True,
) -> PolicyDecision:
    return evaluate_task(
        requester_role=role,
        kind=kind,
        amount_cents=amount_cents,
        will_use_browser=browser,
        household_settings=settings,
        approvals_enabled=approvals_enabled,
    )


# --- evaluate_task ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["adult", "guardian"])
def test_adult_browser_task_needs_human_ok_and_requester_may_approve(role: str) -> None:
    d = evaluate(role=role, kind="shopping_list", browser=True)
    assert d.requires_approval is True
    assert d.rule_id == "browser_action_needs_human_ok"
    assert d.requester_may_approve is True
    assert d.approver_roles == ("guardian", "adult")
    assert d.source == "policy"
    assert d.reason


def test_browser_kind_without_flag_still_counts_as_browser() -> None:
    assert evaluate(kind="browser", browser=False).rule_id == "browser_action_needs_human_ok"


def test_child_purchase_needs_guardian_or_adult() -> None:
    d = evaluate(role="child", kind="purchase", amount_cents=None)
    assert d.requires_approval is True
    assert d.rule_id == "child_spend"
    assert d.approver_roles == ("guardian", "adult")
    assert d.requester_may_approve is False


def test_child_any_positive_amount_is_spend() -> None:
    assert evaluate(role="child", kind="general", amount_cents=1).rule_id == "child_spend"


def test_child_booking_is_external_action() -> None:
    d = evaluate(role="child", kind="booking")
    assert d.requires_approval is True
    assert d.rule_id == "child_external_action"
    assert d.approver_roles == ("guardian", "adult")
    assert d.requester_may_approve is False


def test_child_browser_task_is_first_matched_by_browser_rule_and_never_self_approves() -> None:
    # Spec order: rule 1 (browser) precedes rule 3, so a child's browser task is named by rule 1.
    d = evaluate(role="child", kind="shopping_list", browser=True)
    assert d.rule_id == "browser_action_needs_human_ok"
    assert d.requester_may_approve is False
    assert d.approver_roles == ("guardian", "adult")


def test_child_chore_without_spend_needs_no_approval() -> None:
    d = evaluate(role="child", kind="chore")
    assert d.requires_approval is False
    assert d.rule_id is None


def test_adult_spend_at_threshold_needs_a_different_adult() -> None:
    d = evaluate(kind="purchase", amount_cents=5000)
    assert d.requires_approval is True
    assert d.rule_id == "adult_spend_threshold"
    assert d.requester_may_approve is False
    assert d.approver_roles == ("guardian", "adult")
    assert "$50.00" in d.reason


def test_adult_spend_just_below_threshold_needs_no_approval() -> None:
    assert evaluate(kind="purchase", amount_cents=4999).requires_approval is False


def test_adult_twenty_dollar_general_task_needs_no_approval() -> None:
    d = evaluate(kind="general", amount_cents=2000)
    assert d.requires_approval is False
    assert d.rule_id is None
    assert d.approver_roles == ()
    assert d.source == "policy"


def test_adult_purchase_with_unknown_amount_needs_no_approval() -> None:
    assert evaluate(kind="purchase", amount_cents=None).requires_approval is False


@pytest.mark.parametrize(
    ("settings", "amount", "expected"),
    [
        ({"adult_spend_approval_threshold_cents": 10000}, 5000, False),
        ({"adult_spend_approval_threshold_cents": 10000}, 10000, True),
        ({}, 5000, True),
        (None, 4999, False),
        ({"adult_spend_approval_threshold_cents": "lots"}, 5000, True),
        ({"adult_spend_approval_threshold_cents": True}, 5000, True),
    ],
)
def test_threshold_comes_from_household_settings_with_safe_default(
    settings: dict | None, amount: int, expected: bool
) -> None:
    assert evaluate(kind="purchase", amount_cents=amount, settings=settings).requires_approval is expected


def test_overlapping_rules_take_the_strictest_self_approval_stance() -> None:
    # An adult's $60 browser purchase is named by rule 1, but must not be self-approvable (threshold rule).
    d = evaluate(kind="purchase", amount_cents=6000, browser=True)
    assert d.rule_id == "browser_action_needs_human_ok"
    assert d.requester_may_approve is False


def test_unknown_requester_role_is_treated_as_a_child() -> None:
    d = evaluate(role="visitor", kind="purchase")
    assert d.rule_id == "child_spend"
    assert d.requester_may_approve is False


def test_negative_amount_is_not_spend() -> None:
    assert evaluate(role="child", kind="general", amount_cents=-500).requires_approval is False


def test_str_enum_kind_is_accepted() -> None:
    class Kind(StrEnum):
        PURCHASE = "purchase"

    assert evaluate(role="child", kind=Kind.PURCHASE).rule_id == "child_spend"  # type: ignore[arg-type]


# --- approvals disabled by flag --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": "adult", "kind": "purchase", "amount_cents": 9999},
        {"role": "child", "kind": "purchase", "amount_cents": 500},
        {"role": "child", "kind": "booking"},
        {"role": "adult", "kind": "general"},
    ],
)
def test_approvals_disabled_skips_every_rule_except_browser(kwargs: dict) -> None:
    d = evaluate(approvals_enabled=False, **kwargs)
    assert d.requires_approval is False
    assert d.reason == "Approvals disabled by flag"


@pytest.mark.parametrize("role", ["adult", "guardian", "child"])
def test_approvals_disabled_still_requires_approval_for_browser(role: str) -> None:
    d = evaluate(role=role, kind="shopping_list", browser=True, approvals_enabled=False)
    assert d.requires_approval is True
    assert d.rule_id == "browser_action_needs_human_ok"
    assert d.requester_may_approve is (role != "child")


def test_approvals_disabled_browser_ignores_the_disabled_spend_rules() -> None:
    d = evaluate(kind="purchase", amount_cents=9999, browser=True, approvals_enabled=False)
    assert d.requester_may_approve is True


def test_decision_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        evaluate().requires_approval = True  # type: ignore[misc]


# --- merge_model_advice: Snap may only escalate ----------------------------------------------------------------


def test_snap_can_escalate_a_no_approval_decision() -> None:
    base = evaluate(kind="general", amount_cents=2000)
    merged = merge_model_advice(base, "require_approval")
    assert merged.requires_approval is True
    assert merged.rule_id == "model_escalation"
    assert merged.source == "policy+snap"
    assert merged.approver_roles == ("guardian", "adult")
    assert merged.reason
    assert base.requires_approval is False  # input untouched


@pytest.mark.parametrize("advice", ["no_extra_approval", None, "", "approve_everything", "REQUIRE_APPROVAL"])
def test_snap_cannot_remove_an_approval(advice: str | None) -> None:
    base = evaluate(kind="purchase", amount_cents=5000)
    assert merge_model_advice(base, advice) == base


@pytest.mark.parametrize("advice", ["no_extra_approval", None, "", "approve_everything", "REQUIRE_APPROVAL"])
def test_unusable_advice_leaves_a_no_approval_decision_alone(advice: str | None) -> None:
    base = evaluate(kind="general")
    assert merge_model_advice(base, advice) == base


def test_snap_agreeing_with_an_existing_approval_keeps_the_policy_rule() -> None:
    base = evaluate(kind="shopping_list", browser=True)
    merged = merge_model_advice(base, "require_approval")
    assert merged == base
    assert merged.source == "policy"


def test_snap_cannot_escalate_when_approvals_are_disabled() -> None:
    base = evaluate(kind="general", approvals_enabled=False)
    merged = merge_model_advice(base, "require_approval")
    assert merged.requires_approval is False
    assert merged.reason == "Approvals disabled by flag"


@pytest.mark.parametrize("advice", ["require_approval", "no_extra_approval", None, "junk"])
@pytest.mark.parametrize(
    "base",
    [
        evaluate(kind="shopping_list", browser=True),
        evaluate(role="child", kind="purchase"),
        evaluate(kind="purchase", amount_cents=7000),
        evaluate(kind="general"),
    ],
)
def test_merge_never_lowers_requires_approval(base: PolicyDecision, advice: str | None) -> None:
    assert merge_model_advice(base, advice).requires_approval >= base.requires_approval


# --- can_decide ------------------------------------------------------------------------------------------------

ALEX, JORDAN, MAX = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def decide(**overrides: object) -> tuple[bool, str]:
    args: dict = {
        "approval_status": "pending",
        "rule_id": "browser_action_needs_human_ok",
        "requester_may_approve": True,
        "decider_role": "adult",
        "decider_user_id": JORDAN,
        "requested_for_user_id": ALEX,
    }
    args.update(overrides)
    return can_decide(**args)


def test_other_adult_can_decide_a_pending_approval() -> None:
    ok, reason = decide()
    assert ok is True
    assert reason


@pytest.mark.parametrize("role", ["adult", "guardian"])
def test_adult_may_self_approve_a_browser_action(role: str) -> None:
    assert decide(decider_role=role, decider_user_id=ALEX)[0] is True


def test_child_never_decides() -> None:
    ok, reason = decide(decider_role="child", decider_user_id=MAX, requested_for_user_id=ALEX)
    assert ok is False
    assert reason


def test_child_requester_cannot_self_approve() -> None:
    ok, _ = decide(
        decider_role="child",
        decider_user_id=MAX,
        requested_for_user_id=MAX,
        rule_id="child_spend",
        requester_may_approve=False,
    )
    assert ok is False


@pytest.mark.parametrize("role", [None, "", "agent", "visitor"])
def test_only_known_adult_roles_decide(role: str | None) -> None:
    assert decide(decider_role=role)[0] is False


def test_requester_cannot_approve_adult_spend_threshold_even_if_flag_says_so() -> None:
    ok, _ = decide(rule_id="adult_spend_threshold", requester_may_approve=True, decider_user_id=ALEX)
    assert ok is False


def test_different_adult_can_approve_adult_spend_threshold() -> None:
    assert decide(rule_id="adult_spend_threshold", requester_may_approve=False, decider_user_id=JORDAN)[0] is True


def test_requester_cannot_approve_when_requester_may_approve_is_false() -> None:
    assert decide(rule_id="child_spend", requester_may_approve=False, decider_user_id=ALEX)[0] is False


@pytest.mark.parametrize("status", ["approved", "denied", "cancelled", "", "PENDING"])
def test_only_pending_approvals_can_be_decided(status: str) -> None:
    ok, reason = decide(approval_status=status)
    assert ok is False
    assert "pending" in reason.lower()


def test_decider_must_be_a_human_user() -> None:
    assert decide(decider_user_id=None)[0] is False


def test_user_ids_compare_by_value_not_type() -> None:
    ok, _ = decide(
        requester_may_approve=False,
        decider_user_id=str(ALEX),
        requested_for_user_id=ALEX,
    )
    assert ok is False


def test_unknown_rule_id_still_requires_an_adult_role() -> None:
    assert decide(rule_id="something_new", decider_role="child")[0] is False
    assert decide(rule_id="something_new")[0] is True


def test_model_escalation_is_decided_by_a_human_adult() -> None:
    assert decide(rule_id="model_escalation", decider_user_id=ALEX)[0] is True


# --- can_access_conversation -----------------------------------------------------------------------------------


def access(**overrides: object) -> bool:
    args: dict = {
        "same_household": True,
        "visibility": "household",
        "is_participant": False,
        "principal_role": "adult",
        "created_by_role": "adult",
    }
    args.update(overrides)
    return can_access_conversation(**args)


@pytest.mark.parametrize("participant", [True, False])
@pytest.mark.parametrize("visibility", ["household", "private"])
def test_cross_household_is_never_accessible(participant: bool, visibility: str) -> None:
    assert access(same_household=False, is_participant=participant, visibility=visibility) is False


@pytest.mark.parametrize("role", ["adult", "guardian"])
def test_adults_see_household_threads_without_being_participants(role: str) -> None:
    assert access(principal_role=role) is True


def test_private_threads_need_participation() -> None:
    assert access(visibility="private") is False
    assert access(visibility="private", is_participant=True) is True
    assert access(visibility="private", principal_role="guardian") is False


def test_child_cannot_open_adult_created_household_thread_they_are_not_in() -> None:
    assert access(principal_role="child", created_by_role="adult") is False
    assert access(principal_role="child", created_by_role="guardian") is False


def test_child_participant_can_open_adult_created_thread() -> None:
    assert access(principal_role="child", created_by_role="adult", is_participant=True) is True


def test_child_can_open_household_thread_made_by_a_child() -> None:
    assert access(principal_role="child", created_by_role="child") is True


def test_child_with_unknown_creator_role_fails_closed() -> None:
    assert access(principal_role="child", created_by_role=None) is False


def test_unknown_principal_role_fails_closed() -> None:
    assert access(principal_role="visitor") is False
    assert access(principal_role=None, is_participant=True) is False
