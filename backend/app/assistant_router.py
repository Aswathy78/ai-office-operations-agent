"""FastAPI Router for AI Assistant streaming chat and conversation persistence.

Stories covered:
  6.1 — Streaming chat, conversation persistence (FR-30).
  6.2 — Intent and entity understanding.
  6.3 — Clarification handling (pending_clarification state).
  6.4 — Plan & Retrieve: ordered execution plan, conditional company-knowledge
         retrieval, per-step checkpoints, 12-step / token-budget limits.

Two-phase design for Story 6.4
-------------------------------
When the assistant understands a supported intent (status == "understood") the
router FIRST emits the full execution plan as an SSE ``plan`` event so that the
frontend can render the ordered steps BEFORE any action begins.  Then it emits
``step_start``, optionally ``retrieval``, ``step_done`` / ``step_error``, and
finally ``token`` events carrying the plain-language summary.

This guarantees the plan is always displayed before execution.  The frontend
can choose to gate execution behind a user confirmation button; the backend
does not require that — it starts executing immediately after emitting the plan.
If the user-confirmation gate is desired, implement a separate
``/api/v1/assistant/plan`` endpoint in a future story.

New SSE event fields (additive — old clients ignore unknown fields):
  plan        list[dict]          full ordered plan
  step_start  {index, label}      step is about to run
  retrieval   {query, snippet}    company-knowledge lookup result
  step_done   {index, summary}    step completed successfully
  step_error  {index, message}    step failed; earlier checkpoints preserved
  halted      str                 "step_limit" | "cost_cap"

Existing fields remain unchanged:
  conversation_id, role, understanding, clarification_count  (init payload)
  token                                                      (text chunk)
  error                                                      (stream error)
"""

from dataclasses import asdict
from datetime import datetime, timezone
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
from app.meeting.models import AssistantConversation
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
import app.assistant_planner as assistant_planner
from app.assistant_planner import (
    CompletedStep,
    StepExecutionError,
    clear_execution_checkpoint,
    estimate_tokens,
    execute_plan_step,
    save_execution_checkpoint,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant", tags=["AI Assistant"])
MAX_CLARIFICATION_QUESTIONS = 2


# ---------------------------------------------------------------------------
# Request / response schemas (unchanged from 6.1–6.3)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helper: emit a single SSE data line
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Main streaming endpoint
# ---------------------------------------------------------------------------

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

    Story 6.1/6.3 path (general chat and clarification):
      Streams incoming tokens as Server-Sent Events.
      Persists user prompt and full assistant response in the database.

    Story 6.4 path (understood supported intent):
      1. Emits `plan` SSE event with the full ordered plan.
      2. Iterates steps, checking step-limit and token-budget BEFORE each step.
      3. For retrieval-flagged steps, emits `retrieval` event.
      4. Emits `step_start`, executes step, then `step_done` or `step_error`.
      5. Saves checkpoint after every step (success or failure).
      6. On budget/limit exhaustion emits `halted` and stops.
      7. Streams the final plain-language summary via `token` events.
    """
    # ------------------------------------------------------------------
    # Authentication / conversation lookup (unchanged from 6.1)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Story 6.3: clarification gate — must run before the planner
    # ------------------------------------------------------------------
    pending_state = load_pending_clarification(conv)
    forced_response: str | None = None
    clarification_count = 0
    understanding: AssistantUnderstanding | None = None

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

    # ------------------------------------------------------------------
    # Story 6.4: build the plan when the intent is fully understood
    # (and no forced_response has been set by the clarification path)
    # ------------------------------------------------------------------
    plan_steps = None
    if (
        forced_response is None
        and understanding is not None
        and understanding.status == "understood"
        and understanding.intent != "unsupported"
    ):
        try:
            plan_steps = assistant_planner.build_execution_plan(understanding)
        except Exception as exc:
            logger.error("Failed to build execution plan: %s", exc, exc_info=True)
            plan_steps = None  # fall back to explain_understanding

    # ------------------------------------------------------------------
    # Persist the incoming user message
    # ------------------------------------------------------------------
    persist_message(db, conv_id, role="user", content=req.message)

    # ------------------------------------------------------------------
    # Build LLM context for the fallback / summary path
    # ------------------------------------------------------------------
    context_messages: list[dict[str, str]] = []
    if req.history:
        for item in req.history[-10:]:
            context_messages.append({"role": item.role, "content": item.content})
    else:
        history = get_conversation_history(db, conv_id)
        for h in history[:-1][-10:]:
            context_messages.append({"role": h.role, "content": h.content})

    context_messages.append({"role": "user", "content": req.message})

    # ------------------------------------------------------------------
    # SSE generator
    # ------------------------------------------------------------------
    def sse_event_stream():  # noqa: C901  (acceptable complexity for SSE handler)
        # ---- Init payload (Story 6.1/6.2/6.3 fields preserved) ----
        init_payload = {
            "conversation_id": conv_id,
            "role": "assistant",
            "understanding": (
                understanding.model_dump(mode="json")
                if understanding is not None
                else None
            ),
            "clarification_count": clarification_count,
        }
        yield _sse(init_payload)

        accumulated_chunks: list[str] = []

        # ================================================================
        # STORY 6.4 PLAN & RETRIEVE PATH
        # ================================================================
        if plan_steps is not None:
            started_at = datetime.now(timezone.utc).isoformat()
            completed_steps: list[CompletedStep] = []
            total_tokens_estimated = 0
            halted_reason: str | None = None

            # --- Phase 1: Emit the full plan BEFORE any step executes ---
            serialised_plan = [asdict(s) for s in plan_steps]
            yield _sse({"plan": serialised_plan})

            # --- Phase 2: Execute steps with limits and checkpointing ---
            engine = db.get_bind()
            for step in plan_steps:
                step_index = step.index

                # Limit guard: step count
                if assistant_planner.check_step_limit(step_index):
                    halted_reason = "step_limit"
                    yield _sse({"halted": halted_reason})
                    break

                # Limit guard: token budget (pre-flight estimate)
                if assistant_planner.check_cost_cap(total_tokens_estimated, step.estimated_tokens):
                    halted_reason = "cost_cap"
                    yield _sse({"halted": halted_reason})
                    break

                # --- Emit step_start ---
                yield _sse({"step_start": {"index": step_index, "label": step.description}})

                # --- Conditional retrieval ---
                if step.retrieval_query is not None:
                    snippet = assistant_planner.retrieve_company_knowledge(step.retrieval_query)
                    # Only emit the event when retrieval returns a result.
                    if snippet:
                        step.retrieval_snippet = snippet
                        retrieval_tokens = estimate_tokens(snippet)
                        total_tokens_estimated += retrieval_tokens
                        # Emit retrieval event — do NOT log snippet content
                        # to prevent sensitive policy details appearing in logs.
                        yield _sse({
                            "retrieval": {
                                "query": step.retrieval_query,
                                "snippet": snippet,
                            }
                        })

                # --- Handle summarise_response inline (streams via token events) ---
                if step.action_type == "summarise_response":
                    summary_text = explain_understanding(understanding)
                    summary_tokens_estimated = estimate_tokens(summary_text)
                    if assistant_planner.check_cost_cap(total_tokens_estimated, summary_tokens_estimated):
                        halted_reason = "cost_cap"
                        yield _sse({"halted": halted_reason})
                        break

                    accumulated_chunks.append(summary_text)
                    yield _sse({"token": summary_text})
                    step_summary = "Summary produced."
                    step.status = "done"
                    total_tokens_estimated += summary_tokens_estimated

                    completed_steps.append(CompletedStep(
                        index=step_index,
                        description=step.description,
                        action_type=step.action_type,
                        status=step.status,
                        result_summary=step_summary,
                        tokens_used=summary_tokens_estimated,
                    ))
                    with Session(bind=engine) as write_db:
                        write_conv = write_db.get(AssistantConversation, conv_id)
                        if write_conv:
                            save_execution_checkpoint(
                                write_db,
                                write_conv,
                                plan_steps,
                                completed_steps,
                                step_index,
                                total_tokens_estimated,
                                halted_reason,
                                started_at,
                            )
                    yield _sse({"step_done": {"index": step_index, "summary": step_summary}})
                    continue

                # --- Execute non-summary steps ---
                try:
                    result_summary = execute_plan_step(
                        step, understanding, db, user_id=principal.user_id
                    )
                    step.status = "done"
                    step_tokens = estimate_tokens(result_summary)
                    total_tokens_estimated += step.estimated_tokens + step_tokens

                    completed_steps.append(CompletedStep(
                        index=step_index,
                        description=step.description,
                        action_type=step.action_type,
                        status="done",
                        result_summary=result_summary,
                        tokens_used=step.estimated_tokens + step_tokens,
                    ))

                    # Save checkpoint AFTER successful step
                    with Session(bind=engine) as write_db:
                        write_conv = write_db.get(AssistantConversation, conv_id)
                        if write_conv:
                            save_execution_checkpoint(
                                write_db,
                                write_conv,
                                plan_steps,
                                completed_steps,
                                step_index,
                                total_tokens_estimated,
                                halted_reason,
                                started_at,
                            )

                    yield _sse({"step_done": {"index": step_index, "summary": result_summary}})

                except StepExecutionError as exc:
                    error_msg = str(exc)
                    step.status = "failed"
                    completed_steps.append(CompletedStep(
                        index=step_index,
                        description=step.description,
                        action_type=step.action_type,
                        status="failed",
                        result_summary=error_msg,
                        tokens_used=step.estimated_tokens,
                    ))

                    # Save checkpoint AFTER failed step (preserves earlier checkpoints)
                    with Session(bind=engine) as write_db:
                        write_conv = write_db.get(AssistantConversation, conv_id)
                        if write_conv:
                            save_execution_checkpoint(
                                write_db,
                                write_conv,
                                plan_steps,
                                completed_steps,
                                step_index,
                                total_tokens_estimated,
                                None,  # not halted — a step failed but we may continue
                                started_at,
                            )

                    yield _sse({"step_error": {"index": step_index, "message": error_msg}})
                    # Stop plan execution on step failure.
                    break

                except Exception as exc:
                    logger.error(
                        "Unexpected error in step %d: %s", step_index, exc, exc_info=True
                    )
                    step.status = "failed"
                    error_msg = f"Unexpected error: {type(exc).__name__}"
                    completed_steps.append(CompletedStep(
                        index=step_index,
                        description=step.description,
                        action_type=step.action_type,
                        status="failed",
                        result_summary=error_msg,
                        tokens_used=step.estimated_tokens,
                    ))
                    with Session(bind=engine) as write_db:
                        write_conv = write_db.get(AssistantConversation, conv_id)
                        if write_conv:
                            save_execution_checkpoint(
                                write_db,
                                write_conv,
                                plan_steps,
                                completed_steps,
                                step_index,
                                total_tokens_estimated,
                                None,
                                started_at,
                            )
                    yield _sse({"step_error": {"index": step_index, "message": error_msg}})
                    break

            # If no summary step ran (e.g. execution was halted), emit a token fallback.
            if not accumulated_chunks and halted_reason:
                fallback = (
                    f"Execution stopped: {halted_reason.replace('_', ' ')}. "
                    f"{len(completed_steps)} of {len(plan_steps)} steps completed. "
                    "Your checkpoint has been saved."
                )
                accumulated_chunks.append(fallback)
                yield _sse({"token": fallback})

            # Persist the final assistant message (plan execution summary)
            full_response = "".join(accumulated_chunks).strip()
            if full_response:
                try:
                    with Session(bind=engine) as write_db:
                        persist_message(
                            write_db,
                            conversation_id=conv_id,
                            role="assistant",
                            content=full_response,
                        )
                except Exception as save_err:
                    logger.error(
                        "Failed to persist assistant reply: %s", save_err, exc_info=True
                    )

            yield "data: [DONE]\n\n"
            return

        # ================================================================
        # STORY 6.1 / 6.3 PATH (forced_response or general chat)
        # ================================================================
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
            logger.error(
                "Error in stream_assistant_chat generator: %s", exc, exc_info=True
            )
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
                logger.error(
                    "Failed to persist assistant reply: %s", save_err, exc_info=True
                )

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


def _build_summary_prompt(
    understanding: AssistantUnderstanding,
    completed_steps: list[CompletedStep],
) -> str:
    """Build the prompt sent to the LLM for the final plain-language summary step."""
    done = [s for s in completed_steps if s.status == "done"]
    failed = [s for s in completed_steps if s.status == "failed"]
    lines = [
        "Summarise the following completed plan steps in one or two sentences "
        "for the user. Be concise and factual. Do not claim actions were "
        "actually executed — say they have been validated and queued.",
        "",
        f"Intent: {understanding.intent}",
    ]
    if done:
        lines.append("Completed steps:")
        for s in done:
            lines.append(f"  - {s.description}: {s.result_summary}")
    if failed:
        lines.append("Failed steps:")
        for s in failed:
            lines.append(f"  - {s.description}: {s.result_summary}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conversation management endpoints (unchanged from 6.1)
# ---------------------------------------------------------------------------

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
