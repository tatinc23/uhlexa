from dataclasses import dataclass
from typing import Literal

DecisionSource = Literal["snap", "fallback"]
DecisionStatus = Literal["ok", "disabled", "unavailable", "malformed"]


@dataclass(frozen=True)
class DecisionResult:
    """One Snap answer, or the honest absence of one. Snap is advice: callers still run policy afterwards.

    Any status other than "ok" means `choice is None` and the caller must use its deterministic fallback.
    """

    question_id: str
    choice: str | None
    confidence: float | None
    probabilities: dict[str, float]
    source: DecisionSource
    status: DecisionStatus
    detail: str

    @classmethod
    def fallback(cls, question_id: str, status: DecisionStatus, detail: str) -> "DecisionResult":
        return cls(question_id, None, None, {}, "fallback", status, detail)
