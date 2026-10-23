"""FastAPI Router for AI Assistant streaming chat and conversation persistence.

Stories covered:
  6.1 — Streaming chat, conversation persistence (FR-30).
  6.2 — Intent and entity understanding.
  6.3 — Clarification handling (pending_clarification state).
  6.4 — Plan & Retrieve: ordered execution plan, conditional company-knowledge
         retrieval, per-step checkpoints, 12-step / token-budget limits.
  6.5 — Select tools & execute: every plan step runs a registered tool
         (app.assistant_tools); read tools run at once, medium/high-risk writes
         wait for the user's "confirm" and then really execute.

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
  awaiting_confirmation {index, label, tool_name, risk_tier, arguments}
                                  (6.5) write step waiting for "confirm"
  confirmed   {confirmation_id}   (6.5) the user confirmed; waiting steps now run

Existing fields remain unchanged:
  conversation_id, role, understanding, clarification_count  (init payload)
  token                                                      (text chunk)
  error                                                      (stream error)
"""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
from typing import Literal
import uuid

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
    PlanStep,
    StepExecutionError,
    clear_execution_checkpoint,
    estimate_tokens,
    execute_plan_step,
    load_execution_checkpoint,
    prepare_tool_arguments,
    save_execution_checkpoint,
)
from app.assistant_tools import ToolError, get_registry, validate_tool_call

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
# Story 6.5: confirmation replies and the plan runner
# ---------------------------------------------------------------------------

CONFIRM_REPLIES = frozenset({
    "confirm",
    "confirmed",
    "yes",
    "yes confirm",
    "yes please",
    "go ahead",
    "proceed",
    "do it",
})
CANCEL_REPLIES = frozenset({
    "cancel",
    "cancel request",
    "never mind",
    "nevermind",
    "start over",
    "no",
})


def _normalized_reply(message: str) -> str:
    return re.sub(r"\s+", " ", message.casefold().replace(",", " ")).strip(" .!")


def _confirmation_ttl() -> timedelta:
    try:
        minutes = int(os.environ.get("ASSISTANT_CONFIRMATION_TTL_MINUTES", "30"))
    except ValueError:
        minutes = 30
    return timedelta(minutes=max(1, minutes))


def _confirmation_expired(pending: dict) -> bool:
    try:
        created = datetime.fromisoformat(pending["created_at"])
    except (KeyError, TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - created > _confirmation_ttl()


class _PlanRunner:
    """Runs plan steps for one chat turn and yields the SSE events.

    First turn (``confirmed=False``): read-only tool steps run immediately; the
    first medium/high-risk write step is bound and validated, then it and every
    later action step wait for the user's "confirm". Confirm turn
    (``confirmed=True``): the waiting steps run with the arguments the user saw.
    A failed step skips the remaining action steps but the summary still runs.
    """

    def __init__(
        self,
        *,
        plan_steps: list[PlanStep],
        understanding: AssistantUnderstanding,
        principal: AuthenticatedPrincipal,
        conversation_id: str,
        run_db: Session,
        engine,
        confirmed: bool,
        started_at: str,
        completed_steps: list[CompletedStep] | None = None,
        total_tokens_estimated: int = 0,
        results: dict[str, dict] | None = None,
    ):
        self.plan_steps = plan_steps
        self.understanding = understanding
        self.principal = principal
        self.conversation_id = conversation_id
        self.run_db = run_db
        self.engine = engine
        self.confirmed = confirmed
        self.started_at = started_at
        self.completed_steps = completed_steps or []
        self.total_tokens_estimated = total_tokens_estimated
        self.results = results or {}
        self.halted_reason: str | None = None
        self.failure: str | None = None
        self.awaiting: list[PlanStep] = []
        self.confirmation_id: str | None = None
        self.turn_summaries: list[str] = []
        self.chunks: list[str] = []
        self.last_index = -1

    # -- persistence -------------------------------------------------------

    def save(self, pending_confirmation: dict | None = None) -> None:
        with Session(bind=self.engine) as write_db:
            write_conv = write_db.get(AssistantConversation, self.conversation_id)
            if write_conv:
                save_execution_checkpoint(
                    write_db,
                    write_conv,
                    self.plan_steps,
                    self.completed_steps,
                    self.last_index,
                    self.total_tokens_estimated,
                    self.halted_reason,
                    self.started_at,
                    pending_confirmation,
                )

    def _record(self, step: PlanStep, status_: str, summary: str, tokens: int) -> None:
        self.completed_steps.append(CompletedStep(
            index=step.index,
            description=step.description,
            action_type=step.action_type,
            status=status_,
            result_summary=summary,
            tokens_used=tokens,
        ))
        self.last_index = step.index
        self.save()

    def _fail(self, step: PlanStep, message: str):
        step.status = "failed"
        self.failure = message
        self._record(step, "failed", message, step.estimated_tokens)
        yield _sse({"step_error": {"index": step.index, "message": message}})

    # -- steps -------------------------------------------------------------

    def _await_confirmation(self, step: PlanStep):
        if not self.awaiting:
            # Bind and validate now, so the user confirms exactly what will run.
            try:
                step.tool_arguments = prepare_tool_arguments(
                    step, self.understanding, self.results
                )
                validate_tool_call(
                    self.principal, step.tool_name, step.tool_arguments, require_key=False
                )
            except ToolError as exc:
                yield from self._fail(step, exc.message)
                return
            except StepExecutionError as exc:
                yield from self._fail(step, str(exc))
                return
        elif step.tool_name is not None:
            try:
                step.tool_arguments = prepare_tool_arguments(
                    step, self.understanding, self.results
                )
            except (StepExecutionError, KeyError):
                step.tool_arguments = None  # depends on an earlier write; bound on confirm
        self.confirmation_id = self.confirmation_id or uuid.uuid4().hex
        step.idempotency_key = f"chat:{self.conversation_id}:{self.confirmation_id}:{step.index}"
        step.status = "awaiting_confirmation"
        self.awaiting.append(step)
        risk_tier = (
            get_registry().get(step.tool_name).risk_tier if step.tool_name else None
        )
        yield _sse({
            "awaiting_confirmation": {
                "index": step.index,
                "label": step.description,
                "tool_name": step.tool_name,
                "risk_tier": risk_tier,
                "arguments": step.tool_arguments,
            }
        })

    def _execute(self, step: PlanStep):
        # Conditional retrieval for steps that are not themselves the search tool.
        if step.retrieval_query is not None and step.tool_name != "document.search":
            snippet = assistant_planner.retrieve_company_knowledge(step.retrieval_query)
            if snippet:
                step.retrieval_snippet = snippet
                self.total_tokens_estimated += estimate_tokens(snippet)
                # Do NOT log snippet content (sensitive policy details).
                yield _sse({"retrieval": {"query": step.retrieval_query, "snippet": snippet}})
        try:
            summary = execute_plan_step(
                step,
                self.understanding,
                self.run_db,
                user_id=self.principal.user_id,
                principal=self.principal,
                results=self.results,
                conversation_id=self.conversation_id,
                confirmed=self.confirmed,
            )
        except StepExecutionError as exc:
            yield from self._fail(step, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in step %d: %s", step.index, exc, exc_info=True)
            yield from self._fail(step, f"Unexpected error: {type(exc).__name__}")
            return

        if step.tool_name == "document.search" and step.result and step.result.get("results"):
            snippet = step.result["results"][0]["snippet"]
            step.retrieval_snippet = snippet
            self.total_tokens_estimated += estimate_tokens(snippet)
            yield _sse({"retrieval": {"query": step.retrieval_query, "snippet": snippet}})

        step.status = "done"
        tokens = step.estimated_tokens + estimate_tokens(summary)
        self.total_tokens_estimated += tokens
        self.turn_summaries.append(summary)
        self._record(step, "done", summary, tokens)
        yield _sse({"step_done": {"index": step.index, "summary": summary}})

    def _summary_text(self) -> str:
        if self.confirmed:
            parts = []
            if self.turn_summaries:
                parts.append("Done. " + " ".join(self.turn_summaries))
            if self.failure:
                parts.append(f"I couldn't finish: {self.failure}")
            return " ".join(parts) or "Nothing was run."
        text = explain_understanding(self.understanding)
        if self.awaiting:
            actions = "; ".join(
                step.description for step in self.awaiting if step.requires_confirmation
            )
            return (
                f"{text} Ready to run: {actions}. "
                "Reply \"confirm\" to go ahead or \"cancel\" to discard it."
            )
        if self.failure:
            return f"{text} I couldn't continue: {self.failure}"
        return text

    def _summarise(self, step: PlanStep):
        summary_text = self._summary_text()
        summary_tokens = estimate_tokens(summary_text)
        if assistant_planner.check_cost_cap(self.total_tokens_estimated, summary_tokens):
            self.halted_reason = "cost_cap"
            yield _sse({"halted": self.halted_reason})
            return
        self.chunks.append(summary_text)
        yield _sse({"token": summary_text})
        step.status = "done"
        self.total_tokens_estimated += summary_tokens
        self._record(step, "done", "Summary produced.", summary_tokens)
        yield _sse({"step_done": {"index": step.index, "summary": "Summary produced."}})

    # -- main loop ---------------------------------------------------------

    def run(self):
        yield _sse({"plan": [asdict(step) for step in self.plan_steps]})
        for step in self.plan_steps:
            if step.status in ("done", "failed", "skipped"):
                continue  # finished in an earlier turn
            is_summary = step.action_type == "summarise_response"

            if assistant_planner.check_step_limit(step.index):
                self.halted_reason = "step_limit"
                yield _sse({"halted": self.halted_reason})
                break
            if not is_summary and self.failure is not None:
                step.status = "skipped"
                continue
            if not is_summary and (
                self.awaiting
                or (not self.confirmed and step.tool_name and step.requires_confirmation)
            ):
                yield from self._await_confirmation(step)
                continue
            if assistant_planner.check_cost_cap(self.total_tokens_estimated, step.estimated_tokens):
                self.halted_reason = "cost_cap"
                yield _sse({"halted": self.halted_reason})
                break

            yield _sse({"step_start": {"index": step.index, "label": step.description}})
            if is_summary:
                yield from self._summarise(step)
                if self.halted_reason:
                    break
                continue
            yield from self._execute(step)

        pending = None
        if self.awaiting and not self.halted_reason:
            pending = {
                "confirmation_id": self.confirmation_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "understanding": self.understanding.model_dump(mode="json"),
                "results": self.results,
            }
        self.save(pending)

        if not self.chunks and self.halted_reason:
            done = sum(1 for s in self.completed_steps if s.status == "done")
            fallback = (
                f"Execution stopped: {self.halted_reason.replace('_', ' ')}. "
                f"{done} of {len(self.plan_steps)} steps completed. "
                "Your checkpoint has been saved."
            )
            self.chunks.append(fallback)
            yield _sse({"token": fallback})

        full_response = "".join(self.chunks).strip()
        if full_response:
            try:
                with Session(bind=self.engine) as write_db:
                    persist_message(
                        write_db,
                        conversation_id=self.conversation_id,
                        role="assistant",
                        content=full_response,
                    )
            except Exception as save_err:  # noqa: BLE001
                logger.error("Failed to persist assistant reply: %s", save_err, exc_info=True)
        yield "data: [DONE]\n\n"


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

    pending_state = load_pending_clarification(conv)
    forced_response: str | None = None
    clarification_count = 0
    understanding: AssistantUnderstanding | None = None

    # ------------------------------------------------------------------
    # Story 6.5: a reply to write steps that are waiting for confirmation
    # ------------------------------------------------------------------
    checkpoint = load_execution_checkpoint(conv) if pending_state is None else None
    confirmed_checkpoint = None
    if checkpoint is not None and checkpoint.pending_confirmation:
        reply = _normalized_reply(req.message)
        if reply in CONFIRM_REPLIES and not _confirmation_expired(checkpoint.pending_confirmation):
            confirmed_checkpoint = checkpoint
            understanding = AssistantUnderstanding.model_validate(
                checkpoint.pending_confirmation["understanding"]
            )
        else:
            # Cancelled, expired, or the user moved on: the waiting steps never run.
            assistant_planner.clear_pending_confirmation(db, conv)
            if reply in CONFIRM_REPLIES:
                forced_response = (
                    "That confirmation expired, so nothing was run. "
                    "Send the request again to start over."
                )
            elif reply in CANCEL_REPLIES:
                forced_response = "I canceled the pending action. No operation was performed."

    # ------------------------------------------------------------------
    # Story 6.3: clarification gate — must run before the planner
    # ------------------------------------------------------------------
    if confirmed_checkpoint is not None or forced_response is not None:
        pass
    elif pending_state is not None:
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
        and confirmed_checkpoint is None
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
    engine = db.get_bind()

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
        # STORY 6.5 CONFIRM PATH — run the steps the user just confirmed
        # ================================================================
        if confirmed_checkpoint is not None:
            pending = confirmed_checkpoint.pending_confirmation
            restored_steps = [PlanStep(**step) for step in confirmed_checkpoint.plan_steps]
            for step in restored_steps:
                if step.action_type == "summarise_response":
                    step.status = "pending"  # summarise what this turn did
            yield _sse({"confirmed": {"confirmation_id": pending.get("confirmation_id")}})
            with Session(bind=engine) as run_db:
                yield from _PlanRunner(
                    plan_steps=restored_steps,
                    understanding=understanding,
                    principal=principal,
                    conversation_id=conv_id,
                    run_db=run_db,
                    engine=engine,
                    confirmed=True,
                    started_at=confirmed_checkpoint.started_at,
                    completed_steps=[
                        CompletedStep(**step) for step in confirmed_checkpoint.completed_steps
                    ],
                    total_tokens_estimated=confirmed_checkpoint.total_tokens_estimated,
                    results=pending.get("results") or {},
                ).run()
            return

        # ================================================================
        # STORY 6.4 / 6.5 PLAN & EXECUTE PATH
        # ================================================================
        if plan_steps is not None:
            with Session(bind=engine) as run_db:
                yield from _PlanRunner(
                    plan_steps=plan_steps,
                    understanding=understanding,
                    principal=principal,
                    conversation_id=conv_id,
                    run_db=run_db,
                    engine=engine,
                    confirmed=False,
                    started_at=datetime.now(timezone.utc).isoformat(),
                ).run()
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
