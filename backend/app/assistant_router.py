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
    load_pending_clarification,
    persist_message,
    save_pending_clarification,
)
from app.assistant_understanding import (
    AssistantUnderstanding,
    clarification_question,
    clarification_target,
    explain_understanding,
    merge_clarification_answer,
    understand_assistant_command,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant", tags=["AI Assistant"])
MAX_CLARIFICATION_QUESTIONS = 2


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
    Recognized operation requests include validated intent/entity metadata and are
    explained without executing task, meeting, or e-mail actions.
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
    pending_state = load_pending_clarification(conv)
    forced_response: str | None = None
    clarification_count = 0

    if pending_state is not None:
        if req.message.strip().casefold() in {
            "cancel",
            "cancel request",
            "never mind",
            "nevermind",
            "start over",
        }:
            save_pending_clarification(db, conv, None)
            understanding = None
            forced_response = "I canceled the pending request. No operation was performed."
        else:
            prior_understanding = AssistantUnderstanding.model_validate(
                pending_state["understanding"]
            )
            target = pending_state["pending_field"]
            understanding = merge_clarification_answer(
                db,
                prior_understanding,
                target,
                req.message,
                pending_state["original_message"],
            )
            clarification_count = pending_state["clarification_count"]

            if understanding.status == "needs_clarification":
                next_target = clarification_target(understanding)
                if clarification_count < MAX_CLARIFICATION_QUESTIONS:
                    clarification_count += 1
                    forced_response = clarification_question(understanding)
                else:
                    forced_response = (
                        "I still need "
                        f"{', '.join(understanding.missing_entities)}. "
                        "I have reached the two-question limit for this request. "
                        "Send the remaining detail in your next message, or say "
                        "'cancel request' to reset it. No operation was performed."
                    )
                save_pending_clarification(
                    db,
                    conv,
                    {
                        "original_message": pending_state["original_message"],
                        "understanding": understanding.model_dump(mode="json"),
                        "clarification_count": clarification_count,
                        "pending_field": next_target,
                    },
                )
            else:
                save_pending_clarification(db, conv, None)
                forced_response = (
                    "All required details are resolved. "
                    + explain_understanding(understanding)
                )
    else:
        understanding = understand_assistant_command(db, req.message)
        if understanding is not None and understanding.status == "needs_clarification":
            clarification_count = 1
            forced_response = clarification_question(understanding)
            save_pending_clarification(
                db,
                conv,
                {
                    "original_message": req.message,
                    "understanding": understanding.model_dump(mode="json"),
                    "clarification_count": clarification_count,
                    "pending_field": clarification_target(understanding),
                },
            )

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
        init_payload = json.dumps({
            "conversation_id": conv_id,
            "role": "assistant",
            "understanding": (
                understanding.model_dump(mode="json")
                if understanding is not None
                else None
            ),
            "clarification_count": clarification_count,
        })
        yield f"data: {init_payload}\n\n"

        accumulated_chunks: list[str] = []

        try:
            response_stream = (
                iter([forced_response])
                if forced_response is not None
                else iter([explain_understanding(understanding)])
                if understanding is not None
                else assistant_service.stream_chat_completion(context_messages)
            )
            for token in response_stream:
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
