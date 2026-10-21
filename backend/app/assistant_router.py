"""FastAPI Router for AI Assistant streaming chat and conversation persistence (Epic 6.1 / FR-30)."""

from datetime import datetime
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    AssistantConversation,
)
from app import assistant_service
from app.assistant_service import (
    delete_conversation,
    get_conversation_history,
    get_or_create_conversation,
    list_user_conversations,
    persist_message,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant", tags=["AI Assistant"])


class ChatMessageItem(BaseModel):
    id: str | None = None
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime | None = None


class ChatStreamRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = None
    history: list[ChatMessageItem] = []


class ConversationSummaryResponse(BaseModel):
    id: str
    title: str | None
    user_id: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int


class ConversationDetailResponse(BaseModel):
    id: str
    title: str | None
    user_id: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageItem]


@router.post("/chat/stream")
def stream_assistant_chat(
    req: ChatStreamRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Progressive SSE streaming endpoint for AI Assistant chat.

    Requires an authenticated principal.
    Accepts a user prompt and optional conversation ID / history.
    Enforces authorization: users cannot append to or read another user's conversation.
    Streams incoming tokens as Server-Sent Events (`data: {"token": "..."}\n\n`).
    Persists user prompt and full assistant response in the database.
    """
    if req.conversation_id:
        conv = db.get(AssistantConversation, req.conversation_id)
        if conv is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found.",
            )
        if conv.user_id != principal.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access to this conversation is denied.",
            )
    else:
        conv = get_or_create_conversation(
            db,
            conversation_id=None,
            user_id=principal.user_id,
            title=req.message[:50],
        )

    conv_id = conv.id

    # Persist the incoming user message
    persist_message(db, conv_id, role="user", content=req.message)

    # Build messages context for LLM
    context_messages: list[dict[str, str]] = []
    if req.history:
        for item in req.history[-10:]:
            context_messages.append({"role": item.role, "content": item.content})
    else:
        history = get_conversation_history(db, conv_id)
        for h in history[:-1][-10:]:
            context_messages.append({"role": h.role, "content": h.content})

    context_messages.append({"role": "user", "content": req.message})

    def sse_event_stream():
        init_payload = json.dumps({"conversation_id": conv_id, "role": "assistant"})
        yield f"data: {init_payload}\n\n"

        accumulated_chunks: list[str] = []

        try:
            for token in assistant_service.stream_chat_completion(context_messages):
                if token:
                    accumulated_chunks.append(token)
                    token_payload = json.dumps({"token": token})
                    yield f"data: {token_payload}\n\n"
        except Exception as exc:
            logger.error(f"Error in stream_assistant_chat generator: {exc}", exc_info=True)
            err_payload = json.dumps({"error": "An error occurred while generating the response."})
            yield f"data: {err_payload}\n\n"
            yield "data: [DONE]\n\n"
            return

        full_response = "".join(accumulated_chunks).strip()
        if full_response:
            try:
                engine = db.get_bind()
                with Session(bind=engine) as write_db:
                    persist_message(
                        write_db,
                        conversation_id=conv_id,
                        role="assistant",
                        content=full_response,
                    )
            except Exception as save_err:
                logger.error(f"Failed to persist assistant reply: {save_err}", exc_info=True)

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations", response_model=list[ConversationSummaryResponse])
def get_user_conversations(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Retrieve list of conversation sessions for the active user."""
    conversations = list_user_conversations(db, principal.user_id, limit=limit)
    return [
        ConversationSummaryResponse(
            id=c.id,
            title=c.title,
            user_id=c.user_id,
            created_at=c.created_at,
            updated_at=c.updated_at,
            message_count=len(c.messages),
        )
        for c in conversations
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation_detail(
    conversation_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Retrieve conversation messages history by ID."""
    conv = db.get(AssistantConversation, conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    if conv.user_id != principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to this conversation is denied.",
        )

    messages = get_conversation_history(db, conversation_id)
    return ConversationDetailResponse(
        id=conv.id,
        title=conv.title,
        user_id=conv.user_id,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=[
            ChatMessageItem(
                id=m.id,
                role=m.role,  # type: ignore[arg-type]
                content=m.content,
                created_at=m.created_at,
            )
            for m in messages
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_conversation(
    conversation_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Delete a conversation thread."""
    conv = db.get(AssistantConversation, conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    if conv.user_id != principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to this conversation is denied.",
        )

    delete_conversation(db, conversation_id, user_id=principal.user_id)
    return None
