"""Typed Keiki failures. Messages carry the status and a fixed reason only: never the key, never a response body."""

from app.errors import ProviderUnavailable


class KeikiError(ProviderUnavailable):
    def __init__(self, reason: str, status: int | None = None) -> None:
        self.status = status
        super().__init__("keiki", f"{reason} (HTTP {status})" if status else reason)


class KeikiAuthError(KeikiError):
    """401 bad key, 403 missing `agents:chat` scope or gateway off, 404 no such enabled agent. Not retried."""


class KeikiBusy(KeikiError):
    """409: a turn is already running for this conversation (after the backoff retries)."""


class KeikiTurnFailed(KeikiError):
    """The turn failed or may have run: 500, in-stream error, truncated stream, tool_calls, network error."""


class KeikiBadRequest(KeikiError):
    """400: a bug on our side (for example an unknown reasoning_effort). Not retried."""
