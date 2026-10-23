"""Primitive (primitive.dev): email-thread transcript and org-scoped memory assistance. Never authorization."""

from app.providers.primitive.errors import (
    MemoryConflict,
    NotRepliable,
    PrimitiveNotFound,
    PrimitiveRejection,
    RecipientNotAllowed,
    ThreadChanged,
)
from app.providers.primitive.models import PrimitiveEvent
from app.providers.primitive.service import PrimitiveService
from app.providers.primitive.webhook import parse_event, verify_signature

__all__ = [
    "MemoryConflict",
    "NotRepliable",
    "PrimitiveEvent",
    "PrimitiveNotFound",
    "PrimitiveRejection",
    "PrimitiveService",
    "RecipientNotAllowed",
    "ThreadChanged",
    "parse_event",
    "verify_signature",
]
