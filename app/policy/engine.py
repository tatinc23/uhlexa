"""Policy engine: final authority on approvals and conversation access (spec section 9).

Pure functions over plain values: no DB, no I/O, no imports from the rest of the app. Model advice (SubQ Snap)
is merged here but can only ever add an approval, never remove one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any
from uuid import UUID

from app.policy.rules import (
    ADULT_SPEND_THRESHOLD,
    ADVICE_REQUIRE_APPROVAL,
    ALWAYS_ON_RULES,
    APPROVER_ROLES,
    CHILD,
    DEFAULT_SPEND_THRESHOLD_CENTS,
    MODEL_ESCALATION,
    ROLES,
    RULE_APPROVER_ROLES,
    TASK_RULES,
    THRESHOLD_SETTING,
    TaskFacts,
)

SOURCE_POLICY = "policy"
SOURCE_POLICY_SNAP = "policy+snap"
APPROVALS_DISABLED_REASON = "Approvals disabled by flag"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    requires_approval: bool
    rule_id: str | None
    reason: str
    approver_roles: tuple[str, ...]
    requester_may_approve: bool
    source: str  # "policy" | "policy+snap"
    # Echo of ENABLE_APPROVALS, so merge_model_advice cannot escalate on a deployment that switched approvals off.
    approvals_enabled: bool = True


def _kind(kind: object) -> str:
    return str(kind.value if isinstance(kind, Enum) else kind)


def _spend_threshold(household_settings: Mapping[str, Any] | None) -> int:
    raw = (household_settings or {}).get(THRESHOLD_SETTING, DEFAULT_SPEND_THRESHOLD_CENTS)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else DEFAULT_SPEND_THRESHOLD_CENTS


def evaluate_task(
    *,
    requester_role: str,
    kind: str,
    amount_cents: int | None,
    will_use_browser: bool,
    household_settings: Mapping[str, Any] | None,
    approvals_enabled: bool,
) -> PolicyDecision:
    """First matching rule names the decision; when several match, the strictest constraints apply."""
    facts = TaskFacts(
        requester_role=requester_role if requester_role in ROLES else CHILD,  # unknown role: least privilege
        kind=_kind(kind),
        amount_cents=max(amount_cents or 0, 0),
        will_use_browser=will_use_browser,
        spend_threshold_cents=_spend_threshold(household_settings),
    )
    hits = [hit for rule in (TASK_RULES if approvals_enabled else ALWAYS_ON_RULES) if (hit := rule(facts))]
    if not hits:
        reason = "No approval required." if approvals_enabled else APPROVALS_DISABLED_REASON
        return PolicyDecision(False, None, reason, (), False, SOURCE_POLICY, approvals_enabled)

    first = hits[0]
    return PolicyDecision(
        requires_approval=True,
        rule_id=first.rule_id,
        reason=first.reason,
        approver_roles=tuple(r for r in first.approver_roles if all(r in h.approver_roles for h in hits)),
        requester_may_approve=all(h.requester_may_approve for h in hits),
        source=SOURCE_POLICY,
        approvals_enabled=approvals_enabled,
    )


def merge_model_advice(decision: PolicyDecision, advice_choice: str | None) -> PolicyDecision:
    """Snap may only escalate no-approval to approval. Anything else (failure, junk, "no_extra_approval") is ignored."""
    if advice_choice != ADVICE_REQUIRE_APPROVAL or decision.requires_approval or not decision.approvals_enabled:
        return decision
    return replace(
        decision,
        requires_approval=True,
        rule_id=MODEL_ESCALATION,
        reason="The assistant's safety check asked for a person's OK first.",
        approver_roles=APPROVER_ROLES,
        requester_may_approve=True,  # a human adult still has to click; the model cannot
        source=SOURCE_POLICY_SNAP,
    )


def can_decide(
    *,
    approval_status: str,
    rule_id: str | None,
    requester_may_approve: bool,
    decider_role: str | None,
    decider_user_id: UUID | str | None,
    requested_for_user_id: UUID | str | None,
) -> tuple[bool, str]:
    """Whether this human principal may approve or deny the approval. Returns (allowed, reason)."""
    if approval_status != "pending":
        return False, "This approval is no longer pending."
    if decider_user_id is None:
        return False, "Approvals are decided by a person, not an agent."
    allowed_roles = RULE_APPROVER_ROLES.get(rule_id or "", APPROVER_ROLES)
    if decider_role not in allowed_roles:
        return False, "Only a guardian or adult can decide this."
    is_requester = requested_for_user_id is not None and str(decider_user_id) == str(requested_for_user_id)
    if is_requester and (not requester_may_approve or rule_id == ADULT_SPEND_THRESHOLD):
        return False, "Someone other than the requester has to decide this."
    return True, "Allowed."


def can_access_conversation(
    *,
    same_household: bool,
    visibility: str,
    is_participant: bool,
    principal_role: str | None,
    created_by_role: str | None,
) -> bool:
    """Same household AND (participant OR a household-visible thread). Children only see child-made threads."""
    if not same_household or principal_role not in ROLES:
        return False
    if is_participant:
        return True
    if visibility != "household":
        return False
    return principal_role != CHILD or created_by_role == CHILD
