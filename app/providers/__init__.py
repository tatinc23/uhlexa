"""Provider status shared by the SubQ Snap, Primitive, Maritime, Keiki and Inkbox adapters (spec section 5, 21.6).

A provider is `ok` only when its ENABLE_* flag is on AND its credential is present (Keiki also needs its agent ids).
Otherwise it is `disabled` (flag off) or `misconfigured` (flag on, credential missing). `unavailable` is a runtime
overlay: a live call failed, and it clears on the next success. Nothing here ever holds or returns a secret value.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.errors import ProviderUnavailable
from app.settings import Settings

PROVIDER_NAMES = ("primitive", "subq", "maritime", "keiki", "inkbox")
ProviderState = Literal["disabled", "misconfigured", "ok", "unavailable"]

_MAX_STORED_REASON_CHARS = 4000  # generous: redaction must see a whole secret before the display cut
_MAX_SHOWN_REASON_CHARS = 200
_MIN_REDACTED_SECRET_CHARS = 6


@dataclass(frozen=True)
class _Spec:
    flag: str  # Settings attribute holding the ENABLE_* flag
    env_flag: str
    credential: str  # Settings attribute holding the credential
    env_credential: str
    # Further settings that must be non-empty before the provider counts as configured: (attribute, env name, what).
    also_required: tuple[tuple[str, str, str], ...] = ()


_SPECS = {
    "primitive": _Spec("enable_primitive", "ENABLE_PRIMITIVE", "primitive_api_key", "PRIMITIVE_API_KEY"),
    "subq": _Spec("enable_subq_snap", "ENABLE_SUBQ_SNAP", "subq_api_key", "SUBQ_API_KEY"),
    "maritime": _Spec("enable_maritime", "ENABLE_MARITIME", "maritime_computers_key", "MARITIME_COMPUTERS_KEY"),
    "keiki": _Spec(
        "enable_keiki",
        "ENABLE_KEIKI",
        "keiki_gateway_key",
        "KEIKI_GATEWAY_KEY",
        also_required=(("keiki_agent_ids", "KEIKI_AGENT_IDS", "is empty"),),
    ),
    "inkbox": _Spec("enable_inkbox", "ENABLE_INKBOX", "inkbox_api_key", "INKBOX_API_KEY"),
}
_SECRET_FIELDS = (
    "primitive_api_key",
    "primitive_webhook_secret",
    "subq_api_key",
    "maritime_computers_key",
    "keiki_gateway_key",
    "keiki_api_key",
    "inkbox_api_key",
    "inkbox_webhook_auth_token",
    "secret_encryption_key",
    "app_session_secret",
)
_SECRET_MAP_FIELDS = ("inkbox_signing_keys",)  # {handle: secret}: the values are redacted


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    status: ProviderState
    enabled: bool
    configured: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        """The `GET /api/providers` entry. `name` is the dict key, so it is not repeated."""
        return {
            "status": self.status,
            "enabled": self.enabled,
            "configured": self.configured,
            "detail": self.detail,
        }


# Runtime overlay, process-wide: the API request threads and the embedded worker thread share it.
_unavailable: dict[str, str] = {}
_lock = threading.Lock()


def _check_name(name: str) -> _Spec:
    try:
        return _SPECS[name]
    except KeyError:
        raise ValueError(f"unknown provider {name!r}; expected one of {PROVIDER_NAMES}") from None


def mark_unavailable(name: str, reason: str) -> None:
    """Record that a live call to `name` failed. Shown as `unavailable` until `mark_available`."""
    _check_name(name)
    cleaned = " ".join(reason.split())[:_MAX_STORED_REASON_CHARS] or "unknown error"
    with _lock:
        _unavailable[name] = cleaned


def mark_available(name: str) -> None:
    _check_name(name)
    with _lock:
        _unavailable.pop(name, None)


def reset_runtime_state() -> None:
    """Forget every runtime failure (tests, `/demo/reset`)."""
    with _lock:
        _unavailable.clear()


def config_status(settings: Settings, name: str) -> ProviderStatus:
    """Status from configuration alone: `disabled`, `misconfigured` or `ok`. Services gate their calls on this."""
    spec = _check_name(name)
    enabled = bool(getattr(settings, spec.flag))
    problems = []
    if not getattr(settings, spec.credential).strip():
        problems.append(f"{spec.env_credential} is not set")
    problems.extend(f"{env} {what}" for attr, env, what in spec.also_required if not getattr(settings, attr))
    configured = not problems
    if not enabled:
        return ProviderStatus(name, "disabled", enabled, configured, f"Disabled ({spec.env_flag}=false)")
    if not configured:
        detail = f"{spec.env_flag} is on but " + "; ".join(problems)
        return ProviderStatus(name, "misconfigured", enabled, configured, detail)
    return ProviderStatus(name, "ok", enabled, configured, "Enabled and configured")


def _redact(text: str, settings: Settings) -> str:
    for field in _SECRET_FIELDS:
        value = getattr(settings, field, "")
        if isinstance(value, str) and len(value) >= _MIN_REDACTED_SECRET_CHARS:
            text = text.replace(value, "[redacted]")
    for field in _SECRET_MAP_FIELDS:
        for value in (getattr(settings, field, None) or {}).values():
            if isinstance(value, str) and len(value) >= _MIN_REDACTED_SECRET_CHARS:
                text = text.replace(value, "[redacted]")
    return text


def provider_statuses(settings: Settings) -> dict[str, ProviderStatus]:
    """Every provider, in `PROVIDER_NAMES` order. Safe to serialise: it carries no secret values."""
    with _lock:
        runtime = dict(_unavailable)
    statuses: dict[str, ProviderStatus] = {}
    for name in PROVIDER_NAMES:
        status = config_status(settings, name)
        if status.status == "ok" and name in runtime:
            reason = _redact(runtime[name], settings)[:_MAX_SHOWN_REASON_CHARS]
            status = ProviderStatus(name, "unavailable", status.enabled, status.configured, f"Unavailable: {reason}")
        statuses[name] = status
    return statuses


def guarded_call[T](settings: Settings, name: str, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run one provider call: refuse when disabled or misconfigured, keep the availability overlay honest.

    The wire layer should raise `ProviderUnavailable` itself; a stray transport error is converted here so a provider
    outage can never surface as a 500. Only the exception type is kept, never its message (it may echo a key or a URL).
    """
    status = config_status(settings, name)
    if status.status != "ok":
        raise ProviderUnavailable(name, status.detail)
    try:
        result = fn(*args, **kwargs)
    except ProviderUnavailable as exc:
        answered = getattr(exc, "provider_answered", None)
        if answered is None:
            mark_unavailable(name, exc.reason)  # an outage
        elif answered:
            mark_available(name)  # a typed refusal (not found, recipient gate, conflict): the provider replied
        raise  # answered is False: refused before anything was sent, so the badge says nothing new
    except (httpx.HTTPError, OSError) as exc:
        reason = type(exc).__name__
        mark_unavailable(name, reason)
        raise ProviderUnavailable(name, reason) from exc
    mark_available(name)
    return result


__all__ = [
    "PROVIDER_NAMES",
    "ProviderState",
    "ProviderStatus",
    "config_status",
    "guarded_call",
    "mark_available",
    "mark_unavailable",
    "provider_statuses",
    "reset_runtime_state",
]
