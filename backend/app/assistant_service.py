"""AI Assistant Streaming and Conversation Persistence Service (Epic 6.1 / FR-30).

Integrates with the existing Groq LLM client (with OpenAI fallback) to provide
progressive streaming chat completions. Manages conversation threads and durable
message persistence using the application's SQLAlchemy database layer.
"""

from collections.abc import Generator
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import uuid

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.meeting.models import (
    AssistantConversation,
    AssistantMessage,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the AI Office Operations Assistant for NorthStar Operations. "
    "You assist employees with meeting management, task tracking, email drafting, "
    "and calendar scheduling. "
    "Keep answers concise, direct, helpful, and professional. "
    "Important constraints: You provide recommendations and answers. You do not "
    "claim to have executed or finalized external actions (such as sending emails or "
    "deleting records) unless explicitly instructed and confirmed. Never reveal internal "
    "API keys, encryption keys, or system passwords."
)


def _load_env() -> None:
    """Ensure backend environment variables are loaded."""
    load_dotenv(
        dotenv_path=Path(__file__).resolve().parents[1] / ".env",
        override=False,
    )


def get_llm_client():
    """Resolve and return configured LLM client (Groq first, OpenAI fallback)."""
    _load_env()

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            model = os.environ.get(
                "GROQ_ASSISTANT_MODEL",
                os.environ.get("GROQ_SUMMARY_MODEL", "llama-3.3-70b-versatile"),
            )
            return client, model, "groq"
        except ImportError:
            logger.warning("groq library not installed; falling back.")

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            model = os.environ.get("OPENAI_ASSISTANT_MODEL", "gpt-4o-mini")
            return client, model, "openai"
        except ImportError:
            logger.warning("openai library not installed.")

    return None, None, None


def stream_chat_completion(
    messages: list[dict[str, str]],
    temperature: float = 0.3,
) -> Generator[str, None, None]:
    """Stream token chunks from the configured LLM client.

    Yields individual string tokens as they are received.
    If no LLM provider is configured (e.g. offline dev/testing), yields a
    clean informational notice without crashing.
    """
    client, model, provider = get_llm_client()

    if client is None:
        yield "AI Assistant is running in local mode. Please configure GROQ_API_KEY in backend/.env for live model responses."
        return

    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # Append conversation messages
    for msg in messages:
        if msg.get("role") in ("user", "assistant", "system") and msg.get("content"):
            full_messages.append({"role": msg["role"], "content": msg["content"]})

    try:
        if provider == "groq":
            stream = client.chat.completions.create(
                model=model,
                messages=full_messages,
                temperature=temperature,
                stream=True,
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta:
                    content = chunk.choices[0].delta.content
                    if content:
                        yield content
        elif provider == "openai":
            stream = client.chat.completions.create(
                model=model,
                messages=full_messages,
                temperature=temperature,
                stream=True,
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta:
                    content = chunk.choices[0].delta.content
                    if content:
                        yield content
    except Exception as exc:
        logger.error(f"Error during LLM streaming: {exc}", exc_info=True)
        # Re-raise or yield safe error message without leaking internal details
        raise RuntimeError(f"Model generation error: {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------
# Conversation Persistence Layer
# ---------------------------------------------------------------------------

def get_or_create_conversation(
    db: Session,
    conversation_id: str | None = None,
    user_id: str | None = None,
    title: str | None = None,
) -> AssistantConversation:
    """Retrieve an existing conversation or create a new one."""
    if conversation_id:
        conv = db.get(AssistantConversation, conversation_id)
        if conv is not None:
            return conv

    new_id = conversation_id or str(uuid.uuid4())
    conv = AssistantConversation(
        id=new_id,
        user_id=user_id,
        title=title or "New Chat",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def persist_message(
    db: Session,
    conversation_id: str,
    role: str,
    content: str,
) -> AssistantMessage:
    """Append a message to the specified conversation."""
    conv = db.get(AssistantConversation, conversation_id)
    if conv is None:
        conv = get_or_create_conversation(db, conversation_id=conversation_id)

    msg = AssistantMessage(
        id=str(uuid.uuid4()),
        conversation_id=conv.id,
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg


def get_conversation_history(
    db: Session,
    conversation_id: str,
) -> list[AssistantMessage]:
    """Retrieve chronologically ordered messages for a conversation."""
    return (
        db.query(AssistantMessage)
        .filter(AssistantMessage.conversation_id == conversation_id)
        .order_by(AssistantMessage.created_at.asc())
        .all()
    )


def list_user_conversations(
    db: Session,
    user_id: str,
    limit: int = 50,
) -> list[AssistantConversation]:
    """List recent conversation sessions for a user."""
    return (
        db.query(AssistantConversation)
        .filter(AssistantConversation.user_id == user_id)
        .order_by(AssistantConversation.updated_at.desc())
        .limit(limit)
        .all()
    )


def delete_conversation(
    db: Session,
    conversation_id: str,
    user_id: str | None = None,
) -> bool:
    """Delete a conversation and all its messages."""
    query = db.query(AssistantConversation).filter(AssistantConversation.id == conversation_id)
    if user_id:
        query = query.filter(AssistantConversation.user_id == user_id)
    conv = query.one_or_none()
    if conv is None:
        return False
    db.delete(conv)
    db.commit()
    return True
