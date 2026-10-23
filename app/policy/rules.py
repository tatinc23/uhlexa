"""Ordered approval rules (spec section 9). Pure predicates over normalized facts: no I/O, no app imports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

GUARDIAN = "guardian"
ADULT = "adult"
CHILD = "child"
ROLES: tuple[str, ...] = (GUARDIAN, ADULT, CHILD)
APPROVER_ROLES: tuple[str, ...] = (GUARDIAN, ADULT)

BROWSER_ACTION = "browser_action_needs_human_ok"
CHILD_SPEND = "child_spend"
CHILD_EXTERNAL_ACTION = "child_external_action"
ADULT_SPEND_THRESHOLD = "adult_spend_threshold"
MODEL_ESCALATION = "model_escalation"

# Every rule today is decided by a guardian or an adult; kept as data so can_decide stays rule-driven.
RULE_APPROVER_ROLES: dict[str, tuple[str, ...]] = {
    rule_id: APPROVER_ROLES
    for rule_id in (BROWSER_ACTION, CHILD_SPEND, CHILD_EXTERNAL_ACTION, ADULT_SPEND_THRESHOLD, MODEL_ESCALATION)
}

THRESHOLD_SETTING = "adult_spend_approval_threshold_cents"
DEFAULT_SPEND_THRESHOLD_CENTS = 5000

ADVICE_REQUIRE_APPROVAL = "require_approval"
ADVICE_NO_EXTRA_APPROVAL = "no_extra_approval"


@dataclass(frozen=True, slots=True)
class TaskFacts:
    requester_role: str
    kind: str
    amount_cents: int
    will_use_browser: bool
    spend_threshold_cents: int

    @property
    def is_child(self) -> bool:
        return self.requester_role == CHILD

    @property
    def uses_browser(self) -> bool:
        # A browser-kind task drives a browser even if the caller forgot the flag: fail closed.
        return self.will_use_browser or self.kind == "browser"


@dataclass(frozen=True, slots=True)
class RuleHit:
    rule_id: str
    reason: str
    approver_roles: tuple[str, ...]
    requester_may_approve: bool


Rule = Callable[[TaskFacts], RuleHit | None]


def _dollars(cents: int) -> str:
    whole, rest = divmod(cents, 100)
    return f"${whole}.{rest:02d}"


def browser_action_needs_human_ok(f: TaskFacts) -> RuleHit | None:
    """A human, never the model, must OK anything that drives a Maritime browser. Adults may OK their own."""
    if not f.uses_browser:
        return None
    return RuleHit(
        BROWSER_ACTION,
        "Driving a real browser needs a person's OK first.",
        APPROVER_ROLES,
        requester_may_approve=not f.is_child,
    )


def child_spend(f: TaskFacts) -> RuleHit | None:
    if f.is_child and (f.kind == "purchase" or f.amount_cents > 0):
        return RuleHit(
            CHILD_SPEND,
            "A child's purchase or spending request needs a grown-up's approval.",
            APPROVER_ROLES,
            requester_may_approve=False,
        )
    return None


def child_external_action(f: TaskFacts) -> RuleHit | None:
    if f.is_child and (f.kind == "booking" or f.uses_browser):
        return RuleHit(
            CHILD_EXTERNAL_ACTION,
            "A child's booking or browser request needs a grown-up's approval.",
            APPROVER_ROLES,
            requester_may_approve=False,
        )
    return None


def adult_spend_threshold(f: TaskFacts) -> RuleHit | None:
    if not f.is_child and f.amount_cents > 0 and f.amount_cents >= f.spend_threshold_cents:
        return RuleHit(
            ADULT_SPEND_THRESHOLD,
            f"Spending {_dollars(f.spend_threshold_cents)} or more needs approval from a different adult.",
            APPROVER_ROLES,
            requester_may_approve=False,
        )
    return None


# Evaluation order is the spec's rule order; the first hit names the decision.
TASK_RULES: tuple[Rule, ...] = (
    browser_action_needs_human_ok,
    child_spend,
    child_external_action,
    adult_spend_threshold,
)

# With ENABLE_APPROVALS=false only the browser rule stays on (we never drive a browser unapproved).
ALWAYS_ON_RULES: tuple[Rule, ...] = (browser_action_needs_human_ok,)
