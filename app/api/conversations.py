"""Conversation routes (spec section 14). Thin: validate, call the service as the Principal, commit once."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.auth.dependencies import get_principal
from app.db.session import get_db
from app.providers.primitive.service import PrimitiveService
from app.providers.subq.service import SnapService
from app.schemas.conversations import ConversationDetailOut, ConversationOut, MessageIn, MessageResultOut
from app.services import conversation_service, views
from app.services.providers import get_primitive, get_snap

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("")
def list_conversations(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[ConversationOut]:
    return views.conversation_outs(db, conversation_service.list_for(db, principal))


@router.post("/messages")
def post_message(
    body: MessageIn,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    snap: SnapService = Depends(get_snap),
    primitive: PrimitiveService = Depends(get_primitive),
) -> MessageResultOut:
    """Route the text to a thread, post it, and raise a task (+ approval) when it asks for one."""
    payload = conversation_service.handle_message(
        db, principal, body.text, conversation_id=body.conversation_id, source="rest", snap=snap, primitive=primitive
    )
    db.commit()
    return MessageResultOut.model_validate(payload)


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> ConversationDetailOut:
    conversation = conversation_service.get_for(db, principal, conversation_id)
    summary = views.conversation_out(db, conversation)
    return ConversationDetailOut(**dict(summary), messages=views.message_outs(db, conversation.messages))
