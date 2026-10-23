"""Wire layer for SubQ's Decisions API. Only reached when ENABLE_SUBQ_SNAP is on and a key is present.

One `decide` is one `POST {subq_decisions_base_url}/systemone` carrying one `choice` question (plus optional images).
Everything about the wire format comes from docs.subq.ai/endpoints/decisions-api and
docs/research/2026-10-22-subq-facts.md. The answer is validated fail-closed: anything that is not a clean, confident
choice among the options we offered comes back as a no-decision result, never as a guess.

Neither the key, the state, the images nor the provider's error messages ever reach a log line or a result detail.
SubQ's `detail.message` can echo fragments of the request, so only its `error_type` (a short token) is surfaced.
"""

import base64
import json
import logging
import math
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.errors import ProviderUnavailable
from app.providers.subq.models import DecisionResult
from app.settings import Settings

logger = logging.getLogger(__name__)

PROVIDER = "subq"
TIMEOUT_SECONDS = 10.0  # httpx applies it per phase (connect, write, read); a healthy call takes ~0.4 s
RETRY_BACKOFF_SECONDS = 0.5  # one retry only, for 429 / 5xx / timeout
MAX_QUESTION_ID_CHARS = 100
MIN_CHOICES = 2
MAX_CHOICES = 255
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # decoded, per image
MAX_REQUEST_BYTES = 13 * 1024 * 1024
IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp")
PROBABILITY_SUM_TOLERANCE = 0.01  # SubQ's own sums drift by ~1e-7

# Statuses that mean "fix your key / credits / model access", not "try again". Static text: never the provider's.
_CONFIG_REASONS = {
    401: "HTTP 401 invalid or missing SUBQ_API_KEY",
    402: "HTTP 402 insufficient SubQ credits",
    403: "HTTP 403 key cannot use this model",
}
_ERROR_TYPE = re.compile(r"[a-z][a-z0-9_]{0,39}")


def _sleep(seconds: float) -> None:
    """Indirection so tests can run the retry path without waiting."""
    time.sleep(seconds)


class _BadRequest(Exception):
    """Our own request is invalid. Nothing is sent and the provider is not blamed."""


class _Malformed(Exception):
    """SubQ answered 200 but the answer is not one we may act on."""


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _image_bytes_match(media_type: str, data: bytes) -> bool:
    """SubQ answers 422 when the bytes disagree with `mediaType`; catch it before spending a call."""
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _check_request(question_id: object, choices: object, image: bytes | None, image_mime: str) -> None:
    if not isinstance(question_id, str) or not 1 <= len(question_id) <= MAX_QUESTION_ID_CHARS:
        raise _BadRequest(f"question_id must be 1-{MAX_QUESTION_ID_CHARS} characters")
    if not isinstance(choices, Mapping) or not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
        raise _BadRequest(f"choices must be a mapping of {MIN_CHOICES}-{MAX_CHOICES} options")
    for key, description in choices.items():
        if not isinstance(key, str) or not key:
            raise _BadRequest("every choice key must be a non-empty string")
        if description is not None and not isinstance(description, str):
            raise _BadRequest("every choice description must be a string or None")
    if image is None:
        return
    if len(image) == 0:
        raise _BadRequest("image is empty")
    if len(image) > MAX_IMAGE_BYTES:
        raise _BadRequest("image exceeds 4 MiB")
    if image_mime not in IMAGE_MEDIA_TYPES:
        raise _BadRequest("image mediaType must be image/png, image/jpeg or image/webp")
    if not _image_bytes_match(image_mime, image):
        raise _BadRequest("image bytes do not match the declared mediaType")


def _validate_choice(
    body: object, question_id: str, choices: Mapping[str, Any], *, model: str, min_confidence: float
) -> tuple[str, float, dict[str, float]]:
    """The fail-closed validator from the facts file. Messages are fixed text: they never echo the provider's body."""
    if not isinstance(body, dict):
        raise _Malformed("response body is not an object")
    if "detail" in body:
        raise _Malformed("response carries an error detail")
    if body.get("model") != model:
        raise _Malformed("response model does not match the request")
    answers = body.get("answers")
    if not isinstance(answers, dict) or set(answers) != {question_id}:
        raise _Malformed("answers do not match the requested question")
    answer = answers[question_id]
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise _Malformed("answer is not a choice")
    choice, probabilities, confidence = answer.get("choice"), answer.get("probabilities"), answer.get("confidence")
    if not isinstance(choice, str) or choice not in choices:
        raise _Malformed("choice is not one of the offered options")
    if not isinstance(probabilities, dict) or set(probabilities) != set(choices):
        raise _Malformed("probabilities do not cover exactly the offered options")
    if not all(_is_number(p) and 0 <= p <= 1 for p in probabilities.values()):
        raise _Malformed("a probability is out of range")
    if abs(sum(probabilities.values()) - 1) > PROBABILITY_SUM_TOLERANCE:
        raise _Malformed("probabilities do not sum to 1")
    if probabilities[choice] < max(probabilities.values()):
        raise _Malformed("choice is not the most probable option")
    if not _is_number(confidence) or not 0 <= confidence <= 1:
        raise _Malformed("confidence is missing or out of range")
    if confidence < min_confidence:
        raise _Malformed(f"low confidence ({confidence:.2f} is below the minimum {min_confidence:.2f})")
    return choice, float(confidence), {key: float(probabilities[key]) for key in choices}


def _rejection_detail(status: int, response: httpx.Response) -> str:
    """`HTTP 422 validation_error`: the error_type token only; the message may echo the request."""
    error_type = ""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError, RecursionError):
        detail = None
    if isinstance(detail, dict):
        candidate = detail.get("error_type")
        if isinstance(candidate, str) and _ERROR_TYPE.fullmatch(candidate):
            error_type = f" {candidate}"
    return f"request rejected: HTTP {status}{error_type}"


class SnapClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    def decide(
        self,
        question_id: str,
        instructions: str,
        state: dict,
        choices: dict[str, str],
        *,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> DecisionResult:
        """One `choice` question in one `POST {subq_decisions_base_url}/systemone`; fail closed on anything odd.

        Raise `ProviderUnavailable` for transport and credential failures (timeout, 429/5xx after one retry,
        401/402/403 and other unexpected statuses); return `status="malformed"` for an answer that fails validation or
        a 400/422 rejection; return `status="unavailable"` without calling out when our own request is invalid.
        Never return a guess.
        """
        try:
            _check_request(question_id, choices, image, image_mime)
            payload = self._encode(question_id, instructions, state, choices, image, image_mime)
        except _BadRequest as exc:
            return DecisionResult.fallback(question_id, "unavailable", f"invalid request: {exc}")
        started = time.monotonic()
        response = self._post(payload)
        result = self._interpret(response, question_id, choices)
        logger.info("snap %s -> %s in %.2fs", question_id, result.status, time.monotonic() - started)
        return result

    def _encode(
        self,
        question_id: str,
        instructions: str,
        state: dict,
        choices: dict[str, str],
        image: bytes | None,
        image_mime: str,
    ) -> bytes:
        body: dict[str, Any] = {
            "model": self._settings.subq_snap_model,
            "state": state,
            "questions": {
                question_id: {"type": "choice", "instructions": instructions, "criteria": dict(choices)},
            },
        }
        if image is not None:
            # Raw base64, no `data:` prefix, one line (docs: Images).
            body["images"] = [{"mediaType": image_mime, "base64": base64.b64encode(image).decode("ascii")}]
        try:
            # default=str: ids such as UUIDs and timestamps are fine in `state`. NaN/Infinity are not valid JSON.
            data = json.dumps(body, default=str, allow_nan=False, separators=(",", ":")).encode()
        except (TypeError, ValueError, RecursionError):
            raise _BadRequest("state cannot be encoded as JSON") from None
        if len(data) > MAX_REQUEST_BYTES:
            raise _BadRequest("request body exceeds 13 MiB")
        return data

    def _post(self, payload: bytes) -> httpx.Response:
        """POST once; 429, 5xx and timeouts get one retry after a short back-off, everything else is final."""
        url = self._settings.subq_decisions_base_url.rstrip("/") + "/systemone"
        headers = {
            "Authorization": f"Bearer {self._settings.subq_api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        reason = "unknown"
        with httpx.Client(transport=self._transport, timeout=httpx.Timeout(TIMEOUT_SECONDS)) as http:
            for attempt in (1, 2):
                try:
                    response = http.post(url, content=payload, headers=headers)
                except httpx.TimeoutException:
                    reason = "timeout"
                except httpx.HTTPError as exc:  # DNS, refused, TLS, reset: the exception text can name hosts, drop it
                    raise ProviderUnavailable(PROVIDER, f"connection error ({type(exc).__name__})") from None
                else:
                    if response.status_code != 429 and response.status_code < 500:
                        return response
                    reason = f"HTTP {response.status_code}"
                if attempt == 1:
                    _sleep(RETRY_BACKOFF_SECONDS)
        raise ProviderUnavailable(PROVIDER, f"{reason} (after one retry)")

    def _interpret(self, response: httpx.Response, question_id: str, choices: Mapping[str, Any]) -> DecisionResult:
        status = response.status_code
        if status == 200:
            return self._validated(response, question_id, choices)
        if status in (400, 422):  # our request was wrong; a retry cannot change that and the provider is fine
            return DecisionResult.fallback(question_id, "malformed", _rejection_detail(status, response))
        if status in _CONFIG_REASONS:
            raise ProviderUnavailable(PROVIDER, _CONFIG_REASONS[status])
        if 200 <= status < 300:
            return DecisionResult.fallback(question_id, "malformed", f"unexpected HTTP {status}")
        raise ProviderUnavailable(PROVIDER, f"HTTP {status}")

    def _validated(self, response: httpx.Response, question_id: str, choices: Mapping[str, Any]) -> DecisionResult:
        try:
            body = response.json()
        except (ValueError, RecursionError):
            return DecisionResult.fallback(question_id, "malformed", "response is not valid JSON")
        try:
            choice, confidence, probabilities = _validate_choice(
                body,
                question_id,
                choices,
                model=self._settings.subq_snap_model,
                min_confidence=self._settings.subq_min_confidence,
            )
        except _Malformed as exc:
            return DecisionResult.fallback(question_id, "malformed", str(exc))
        return DecisionResult(question_id, choice, confidence, probabilities, "snap", "ok", "")
