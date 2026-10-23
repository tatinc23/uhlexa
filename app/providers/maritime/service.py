import uuid
from collections.abc import Callable
from urllib.parse import quote

from app.providers import ProviderStatus, guarded_call, provider_statuses
from app.providers.maritime.client import MaritimeClient
from app.providers.maritime.models import RunOutcome, StepRecord, TakeoverMode, TakeoverState, Verifier
from app.settings import Settings

NAME = "maritime"


class MaritimeService:
    """One Maritime computer per user, always reached through a URL pinned server-side from the authenticated user.

    No method accepts a URL, a header or free text that could change who the connection belongs to, and the computers
    key stays inside the client. Disabled or misconfigured raises `ProviderUnavailable("maritime", reason)`.

    `verifier` reads a screenshot (`(image_bytes, question) -> "verified" | "not_verified" | "needs_human"`); the
    service layer wires it to SubQ Snap `verify_screenshot`. Without one nothing is ever reported as verified. It can
    be given here, set later with `service.verifier = ...`, or passed to one `run_list_add` / `run_sign_in_check` call.
    """

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[str], MaritimeClient] | None = None,
        *,
        verifier: Verifier | None = None,
    ) -> None:
        self._settings = settings
        self.verifier = verifier
        self._client_factory = client_factory or (
            lambda pinned_url: MaritimeClient(settings, pinned_url, verifier=self.verifier)
        )

    @property
    def status(self) -> ProviderStatus:
        return provider_statuses(self._settings)[NAME]

    def pinned_mcp_url(self, user_id: uuid.UUID) -> str:
        """`{MARITIME_MCP_URL}/u/{user_id}`. Pure and ungated; the id must come from the authenticated Principal."""
        if not isinstance(user_id, uuid.UUID):
            raise TypeError("pinned_mcp_url takes the authenticated user's uuid.UUID, nothing else")
        return f"{self._settings.maritime_mcp_url.rstrip('/')}/u/{quote(str(user_id), safe='')}"

    def _call[T](self, user_id: uuid.UUID, operation: Callable[[MaritimeClient], T]) -> T:
        # The client is built inside the guard so a disabled provider never constructs one.
        return guarded_call(self._settings, NAME, lambda: operation(self._client_factory(self.pinned_mcp_url(user_id))))

    def ensure_computer(self, user_id: uuid.UUID) -> str:
        return self._call(user_id, lambda client: client.get_computer())

    def run_list_add(
        self,
        user_id: uuid.UUID,
        computer_id: str,
        target_url: str,
        items: list[str],
        on_step: Callable[[StepRecord], None],
        *,
        verifier: Verifier | None = None,
    ) -> RunOutcome:
        # The 4-argument client call is kept when no per-call verifier is given: a client built by a custom factory
        # (tests, a replacement client) then only has to implement the original `run_list_add` signature.
        extra = {} if verifier is None else {"verifier": verifier}
        return self._call(user_id, lambda client: client.run_list_add(computer_id, target_url, items, on_step, **extra))

    def run_sign_in_check(
        self,
        user_id: uuid.UUID,
        computer_id: str,
        page_url: str,
        on_step: Callable[[StepRecord], None],
        *,
        verifier: Verifier | None = None,
    ) -> RunOutcome:
        """Load `page_url` (our own sign-in wall page); a sign-in form on screen requests a takeover. Never types."""
        extra = {} if verifier is None else {"verifier": verifier}
        return self._call(user_id, lambda client: client.run_sign_in_check(computer_id, page_url, on_step, **extra))

    def request_takeover(self, user_id: uuid.UUID, computer_id: str, reason: str) -> str:
        """The control link: a secret. Show it only to the owning user, never log it."""
        return self._call(user_id, lambda client: client.request_takeover(computer_id, reason))

    def takeover_status(self, user_id: uuid.UUID, computer_id: str) -> TakeoverMode:
        return self._call(user_id, lambda client: client.takeover_status(computer_id))

    def takeover_state(self, user_id: uuid.UUID, computer_id: str) -> TakeoverState:
        """Mode plus how the last takeover ended (`failed` / `expired`: the person did not finish, do not retry)."""
        return self._call(user_id, lambda client: client.takeover_state(computer_id))

    def close_computer(self, user_id: uuid.UUID, computer_id: str) -> None:
        """End the session now (cancel path). Plans close the computer themselves, except while a takeover is open."""
        return self._call(user_id, lambda client: client.close_computer(computer_id))
