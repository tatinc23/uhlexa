from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

RunStatus = Literal["succeeded", "needs_takeover", "failed"]
TakeoverMode = Literal["agent", "human", "unknown"]
# What a screenshot check answers. `needs_human` means a login, CAPTCHA, 2FA or payment step is on screen.
Verdict = Literal["verified", "not_verified", "needs_human"]
# `(image_bytes, question) -> Verdict`. The service layer wires this to SubQ Snap `verify_screenshot`.
Verifier = Callable[[bytes, str], Verdict]


class ForbiddenOperation(RuntimeError):
    """A tool, action or argument outside the allowlists. This is a bug in the caller, never a provider outage."""


class HumanInControl(Exception):
    """A person holds the desktop (takeover in progress): every action is refused until control returns to the agent."""


class ActionRefused(Exception):
    """Maritime answered an action with an error or a block (not an outage): the plan stops and reports `failed`."""


def default_verifier(image: bytes, question: str) -> Verdict:
    """Nothing can read a screenshot here, so nothing is ever claimed as verified (honest default)."""
    return "not_verified"


@dataclass(frozen=True)
class StepRecord:
    """One entry of a run's step log (`browser_tasks.steps`). `index` counts from 0."""

    index: int
    action: str
    ok: bool
    note: str = ""
    screenshot_ref: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    """How a plan ended.

    `succeeded` means every step ran without an error; `verified` says whether the final screenshot was confirmed by the
    verifier. `succeeded` with `verified=False` is "done but not visually confirmed" and must be shown as such.
    """

    status: RunStatus
    verified: bool
    # The takeover link lets anyone holding it control the desktop: show it only to the owning user, never log it.
    takeover_url: str | None = field(default=None, repr=False)
    last_screenshot: bytes | None = field(default=None, repr=False)
    detail: str = ""


@dataclass(frozen=True)
class Frame:
    """One screenshot returned by the `computer` tool. Coordinates in the next action refer to this frame."""

    image: bytes = field(repr=False)
    mime: str = "image/jpeg"
    frame_id: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class TakeoverState:
    """`takeover_status` in full. `result` is how the LAST takeover ended; `failed` or `expired` means do not retry."""

    mode: TakeoverMode
    result: Literal["success", "failed", "expired"] | None = None
    note: str | None = field(default=None, repr=False)
    expires_at: str | None = None
