import logging

import httpx

from app.errors import ProviderUnavailable
from app.providers import ProviderStatus, config_status, guarded_call, mark_unavailable, provider_statuses
from app.providers.subq.client import SnapClient
from app.providers.subq.models import DecisionResult
from app.settings import Settings

logger = logging.getLogger(__name__)

NAME = "subq"

# The Decisions API needs `instructions` per question; callers only know the id (spec sections 10 and 20).
INSTRUCTIONS: dict[str, str] = {
    "thread_routing": (
        "Decide whether the new request belongs to one of the listed open conversation threads or needs a new "
        "thread. Pick a continue option only when the request is about the same list or topic as that thread; "
        "otherwise pick new_thread."
    ),
    "approval_escalation": (
        "Decide whether this household task needs an extra human approval on top of the fixed rules. Pick "
        "require_approval when it spends money, contacts someone outside the household, or acts for a child; "
        "otherwise pick no_extra_approval."
    ),
    "context_sufficiency": (
        "Decide whether the short thread summary and its recent items are enough context to act on the new "
        "request, or whether the full conversation transcript is needed first."
    ),
    "browser_automation": (
        "Decide whether fulfilling the request means operating a web page in a browser (adding, ordering or "
        "booking something online) or can be done without a browser."
    ),
    "verify_screenshot": (
        "Look at the screenshot after the last browser step. Pick verified only when every requested item is "
        "visibly present; not_verified when something is missing or unclear; needs_human when the page shows a "
        "login, CAPTCHA, two-factor or payment step that a person must handle."
    ),
}


class SnapService:
    """Bounded structured decisions from SubQ Snap. It advises; the policy engine decides."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._client = SnapClient(settings, transport)

    @property
    def status(self) -> ProviderStatus:
        return provider_statuses(self._settings)[NAME]

    def decide(
        self,
        question_id: str,
        state: dict,
        choices: dict[str, str],
        *,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> DecisionResult:
        """Never raises. Anything but status "ok" carries `choice=None`: the caller uses its deterministic fallback.

        `image` (PNG, JPEG or WebP bytes, at most 4 MiB, `image_mime` must match) goes to SubQ as a documented
        `images` entry. `verify_screenshot` is a question about a picture, so it is never asked without one: a
        text-only "verified" would be a guess.
        """
        config = config_status(self._settings, NAME)
        if config.status == "disabled":
            return DecisionResult.fallback(question_id, "disabled", config.detail)
        if config.status == "misconfigured":
            return DecisionResult.fallback(question_id, "unavailable", config.detail)
        instructions = INSTRUCTIONS.get(question_id)
        if instructions is None:  # our bug, not a provider outage: do not call out and do not mark Snap unavailable
            return DecisionResult.fallback(question_id, "unavailable", f"no instructions for question {question_id!r}")
        if question_id == "verify_screenshot" and image is None:
            return DecisionResult.fallback(question_id, "unavailable", "verify_screenshot needs a screenshot image")
        # The no-image call keeps the original four positional arguments; the image goes as keywords only when present.
        extra = {} if image is None else {"image": image, "image_mime": image_mime}
        try:
            return guarded_call(
                self._settings, NAME, self._client.decide, question_id, instructions, state, choices, **extra
            )
        except ProviderUnavailable as exc:
            logger.info("snap unavailable: %s", exc.reason)
            return DecisionResult.fallback(question_id, "unavailable", exc.reason)
        except Exception as exc:  # Snap is advisory: no failure here may break a request
            reason = f"unexpected {type(exc).__name__}"
            logger.warning("snap failed: %s", reason)
            mark_unavailable(NAME, reason)
            return DecisionResult.fallback(question_id, "unavailable", reason)
