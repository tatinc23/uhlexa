"""Policy engine: pure functions, final authority over approvals and conversation access."""

from app.policy.engine import (
    PolicyDecision,
    can_access_conversation,
    can_decide,
    evaluate_task,
    merge_model_advice,
)
from app.policy.rules import (
    ADULT_SPEND_THRESHOLD,
    BROWSER_ACTION,
    CHILD_EXTERNAL_ACTION,
    CHILD_SPEND,
    MODEL_ESCALATION,
)

__all__ = [
    "ADULT_SPEND_THRESHOLD",
    "BROWSER_ACTION",
    "CHILD_EXTERNAL_ACTION",
    "CHILD_SPEND",
    "MODEL_ESCALATION",
    "PolicyDecision",
    "can_access_conversation",
    "can_decide",
    "evaluate_task",
    "merge_model_advice",
]
