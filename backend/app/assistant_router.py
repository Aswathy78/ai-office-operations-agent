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
  awaiting_confirmation {index, label, tool_name, approval_id, risk_level,
                         risk_reasons, approver_kind, arguments, summary}
                                  (6.5/6.6) medium-risk write waiting for "confirm"
  approval_required {same fields} (6.6) high-risk write waiting for an approver
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
import time
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
from app.assistant_run_tracing import (
    create_assistant_run,
    ensure_assistant_run_step,
    record_assistant_run_step,
    resume_assistant_run,
    set_assistant_run_status,
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
import app.assistant_planner as assistant_planner
from app.assistant_approvals import cancel_approval, decide_approval
from app.assistant_planner import (
    CompletedStep,
    PlanStep,
    StepApprovalRequired,
    StepExecutionError,
    clear_execution_checkpoint,
    estimate_tokens,
    execute_plan_step,
    load_execution_checkpoint,
    save_execution_checkpoint,
    summarize_tool_result,
)
from app.assistant_tools import ToolError, get_registry

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

    Every write goes through the write guard (Story 6.6): low-risk writes within
    the user's permissions run at once; a medium-risk write pauses until the user
    replies "confirm" (``confirmed=True`` turn approves and runs it); a high-risk
    write pauses until someone else approves it on the Approvals page. Denied or
    conflicting writes fail with the reason. Steps after a paused step wait. A
    failed step skips the remaining action steps but the summary still runs.
    """

    def __init__(
        self,
        *,
        plan_steps: list[PlanStep],
        understanding: AssistantUnderstanding,
        principal: AuthenticatedPrincipal,
        conversation_id: str,
        run_id: str,
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
        self.run_id = run_id
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
        self.turn_id = uuid.uuid4().hex
        self.turn_summaries: list[str] = []
        self.writes_done: list[str] = []
        # Story 7.3: things the user should know that are not failures.
        self.notices: list[str] = []
        self._reasons: dict[int, list[str]] = {}
        self.chunks: list[str] = []
        self.last_index = -1
        self._step_started_at: dict[int, datetime] = {}
        self._step_started_monotonic: dict[int, float] = {}

    # -- persistence -------------------------------------------------------

    def save(self, pending_confirmation: dict | None = None) -> None:
        if pending_confirmation is not None:
            pending_confirmation = {**pending_confirmation, "run_id": self.run_id}
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

    def _trace_step(self, step: PlanStep, status_: str, tokens: int | None = None) -> None:
        now = datetime.now(timezone.utc)
        if status_ == "running":
            self._step_started_at.setdefault(step.index, now)
            self._step_started_monotonic.setdefault(step.index, time.monotonic())
        terminal = status_ in {"completed", "failed", "skipped", "escalated", "cancelled"}
        started_at = self._step_started_at.get(step.index)
        duration_ms = None
        if terminal and step.index in self._step_started_monotonic:
            duration_ms = int((time.monotonic() - self._step_started_monotonic[step.index]) * 1000)
        input_data = {"tool_name": step.tool_name, "arguments": step.tool_arguments}
        output_data = {"status": status_}
        if step.result:
            output_data["result"] = step.result
        record_assistant_run_step(
            self.run_db,
            run_id=self.run_id,
            step_order=step.index,
            step_type=step.action_type,
            step_name=step.tool_name or step.action_type,
            status=status_,
            input_data=input_data,
            output_data=output_data,
            started_at=started_at,
            ended_at=now if terminal else None,
            duration_ms=duration_ms,
            estimated_tokens=tokens if tokens is not None else step.estimated_tokens,
            tool_execution_id=step.tool_execution_id,
            approval_id=step.approval_id,
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
        trace_status = {
            "done": "completed",
            "failed": "failed",
            "escalated": "escalated",
            "skipped": "skipped",
        }.get(status_, "completed")
        self._trace_step(step, trace_status, tokens)
        self.save()

    def _fail(self, step: PlanStep, message: str):
        step.status = "failed"
        self.failure = message
        self._record(step, "failed", message, step.estimated_tokens)
        yield _sse({"step_error": {"index": step.index, "message": message}})

    # -- steps -------------------------------------------------------------

    def _pause(self, step: PlanStep, exc: StepApprovalRequired):
        """The write guard paused this step: show why and who must approve it."""
        approval = exc.approval
        step.approval_id = approval["id"]
        step.risk_level = approval["risk_level"]
        by_requester = approval["approver_kind"] == "requester"
        step.status = "awaiting_confirmation" if by_requester else "approval_required"
        self._reasons[step.index] = approval["risk_reasons"][1:] or approval["risk_reasons"]
        self.awaiting.append(step)
        self.last_index = step.index
        self._trace_step(step, "awaiting_approval")
        self.save()
        payload = {
            "index": step.index,
            "label": step.description,
            "tool_name": step.tool_name,
            "approval_id": approval["id"],
            "risk_level": approval["risk_level"],
            "risk_reasons": approval["risk_reasons"],
            "approver_kind": approval["approver_kind"],
            "arguments": step.tool_arguments,
            "summary": approval["summary"],
        }
        # Medium risk: the requester confirms in chat. High risk: someone else approves.
        yield _sse({"awaiting_confirmation" if by_requester else "approval_required": payload})

    def _run_approved(self, step: PlanStep) -> str:
        """Confirm turn: approve the requester's own medium-risk request and run it."""
        result = decide_approval(
            self.run_db,
            step.approval_id,
            self.principal,
            "approve",
            continue_run=True,
        )
        if result.outcome is None:
            raise StepExecutionError(result.message)
        step.tool_execution_id = result.outcome.execution_id
        step.result = result.outcome.result or {}
        self.results[step.tool_name] = step.result
        return summarize_tool_result(step.tool_name, step.result)

    def _email_document_context(self):
        """Story 7.3: find company-document facts before the assistant drafts an e-mail.

        Stored as ``results["document.context"]``, which binds the preview's
        instructions and fills the e-mail body (``prepare_tool_arguments``).
        """
        topic = self.understanding.entities.topic or ""
        if "document.context" in self.results or not assistant_planner.needs_retrieval(topic):
            return
        try:
            context = assistant_planner.retrieve_email_document_context(
                self.run_db, actor_id=self.principal.user_id, topic=topic
            )
            # Story 7.4: only facts the requester may read go into the draft.
            context = assistant_planner.authorize_document_context(
                self.run_db, actor_id=self.principal.user_id, context=context
            )
        except Exception as exc:  # noqa: BLE001 - draft without facts rather than fail
            logger.warning("Document retrieval for an e-mail failed: %s", exc)
            context = []
        self.results["document.context"] = {
            "query": assistant_planner.email_document_query(topic),
            "results": context,
        }
        if context:
            snippet = context[0]["snippet"]
            self.total_tokens_estimated += estimate_tokens(snippet)
            yield _sse({"retrieval": {
                "query": topic,
                "snippet": snippet,
                "source": context[0].get("source"),
            }})
        else:
            self.notices.append(assistant_planner.EMAIL_DOCUMENT_NOT_FOUND_NOTICE)

    def _execute(self, step: PlanStep):
        self._trace_step(step, "running")
        if step.tool_name == "email.preview":
            yield from self._email_document_context()
        # Conditional retrieval for steps that are not themselves the search tool.
        if step.retrieval_query is not None and step.tool_name != "document.search":
            snippet = assistant_planner.retrieve_company_knowledge(
                step.retrieval_query,
                db=self.run_db,
                actor_id=self.principal.user_id,
            )
            if snippet:
                step.retrieval_snippet = snippet
                self.total_tokens_estimated += estimate_tokens(snippet)
                # Do NOT log snippet content (sensitive policy details).
                yield _sse({"retrieval": {"query": step.retrieval_query, "snippet": snippet}})
        if step.tool_name and step.idempotency_key is None:
            spec = get_registry().get(step.tool_name)
            if spec.is_write:
                step.idempotency_key = f"chat:{self.conversation_id}:{self.turn_id}:{step.index}"
        try:
            if self.confirmed and step.status == "awaiting_confirmation" and step.approval_id:
                summary = self._run_approved(step)
            else:
                summary = execute_plan_step(
                    step,
                    self.understanding,
                    self.run_db,
                    user_id=self.principal.user_id,
                    principal=self.principal,
                    results=self.results,
                    conversation_id=self.conversation_id,
                )
        except StepApprovalRequired as exc:
            yield from self._pause(step, exc)
            return
        except ToolError as exc:
            yield from self._fail(step, exc.message)
            return
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

        verification = (step.result or {}).get("verification")
        if verification and verification.get("status") == "escalated":
            step.status = "escalated"
            link = verification.get("link", {})
            escalation_notifications = verification.get("notifications", [])
            sent_count = sum(
                notification.get("status") == "sent"
                for notification in escalation_notifications
            )
            failed_count = sum(
                notification.get("status") == "failed"
                for notification in escalation_notifications
            )
            message = (
                "The action may have completed, but its saved result did not match the request "
                "after a retry and re-plan."
            )
            if sent_count:
                message += f" Review notifications were sent to {sent_count} recipient(s)."
            if failed_count:
                message += f" {failed_count} review notification(s) could not be delivered."
            if not sent_count and not failed_count:
                message += " No review notification could be delivered; check the audit log."
            if link.get("url"):
                message += f" Review the record: {link['url']}"
            self.failure = message
            self._record(step, "escalated", message, step.estimated_tokens)
            yield _sse({
                "step_error": {
                    "index": step.index,
                    "status": "escalated",
                    "message": message,
                    "links": (step.result or {}).get("links", {}),
                }
            })
            return

        step.status = "done"
        tokens = step.estimated_tokens + estimate_tokens(summary)
        self.total_tokens_estimated += tokens
        self.turn_summaries.append(summary)
        if step.tool_name and get_registry().get(step.tool_name).is_write:
            record_link = ((step.result or {}).get("links") or {}).get("record", {})
            link_text = (
                f" Review: {record_link['url']}"
                if step.tool_name != "task.create" and record_link.get("url")
                else ""
            )
            self.writes_done.append(summary + link_text)
        self._record(step, "done", summary, tokens)
        yield _sse({
            "step_done": {
                "index": step.index,
                "summary": summary,
                "status": step.status,
                "links": (step.result or {}).get("links", {}),
            }
        })

    def _summary_text(self) -> str:
        parts: list[str] = []
        if self.writes_done:
            parts.append("Done. " + " ".join(self.writes_done))
        elif not self.confirmed:
            parts.append(explain_understanding(self.understanding))
        for step in self.awaiting:
            approval_reasons = "; ".join(
                reason for reason in self._reasons.get(step.index, []) if reason
            )
            why = f" ({step.risk_level} risk: {approval_reasons})" if approval_reasons else ""
            if step.status == "awaiting_confirmation":
                parts.append(
                    f"Approval required{why}: {step.description}. "
                    "Reply \"confirm\" to go ahead or \"cancel\" to discard it."
                )
            else:
                parts.append(
                    f"Approval required{why}: {step.description}. I sent it for approval "
                    "to HR / Operations, an admin, or your team's manager (request "
                    f"{step.approval_id}); it will run with your permissions once approved. "
                    "You can follow it on the Approvals page."
                )
        if self.failure:
            parts.append(
                ("I couldn't finish: " if self.writes_done else "I couldn't continue: ")
                + self.failure
            )
        parts.extend(self.notices)
        return " ".join(parts) or "Nothing was run."

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
        for step in self.plan_steps:
            ensure_assistant_run_step(
                self.run_db,
                run_id=self.run_id,
                step_order=step.index,
                step_type=step.action_type,
                step_name=step.tool_name or step.action_type,
            )
        yield _sse({"plan": [asdict(step) for step in self.plan_steps]})
        for step in self.plan_steps:
            if step.status in ("done", "failed", "escalated", "skipped", "approval_required"):
                continue  # finished earlier, or waiting for someone else's approval
            is_summary = step.action_type == "summarise_response"

            if assistant_planner.check_step_limit(step.index):
                self.halted_reason = "step_limit"
                yield _sse({"halted": self.halted_reason})
                break
            if not is_summary and self.failure is not None:
                step.status = "skipped"
                self._trace_step(step, "skipped")
                continue
            if not is_summary and self.awaiting:
                continue  # runs after the paused step is approved
            if assistant_planner.check_cost_cap(self.total_tokens_estimated, step.estimated_tokens):
                self.halted_reason = "cost_cap"
                yield _sse({"halted": self.halted_reason})
                break

            yield _sse({"step_start": {"index": step.index, "label": step.description}})
            if is_summary:
                self._trace_step(step, "running")
                yield from self._summarise(step)
                if self.halted_reason:
                    break
                continue
            yield from self._execute(step)

        pending = None
        confirmable = [s for s in self.awaiting if s.status == "awaiting_confirmation"]
        if confirmable and not self.halted_reason:
            pending = {
                "confirmation_id": self.turn_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "understanding": self.understanding.model_dump(mode="json"),
                "results": self.results,
                "approval_ids": [s.approval_id for s in confirmable],
            }
        self.save(pending)
        final_status = (
            "awaiting_approval" if self.awaiting
            else "failed" if self.failure
            else "completed"
        )
        set_assistant_run_status(self.run_db, self.run_id, final_status)

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
    clarification_resolved_notice: str | None = None
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
            for approval_id in checkpoint.pending_confirmation.get("approval_ids") or []:
                try:
                    cancel_approval(db, approval_id, principal)
                except ToolError:
                    pass  # already decided, expired or cancelled
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
                clarification_resolved_notice = (
                    "All required details are resolved. "
                    + explain_understanding(understanding)
                )
                forced_response = None
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
            if clarification_resolved_notice is not None:
                forced_response = clarification_resolved_notice

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
    existing_run_id = (
        confirmed_checkpoint.pending_confirmation.get("run_id")
        if confirmed_checkpoint is not None
        else None
    )
    trace_run = resume_assistant_run(db, existing_run_id) if existing_run_id else None
    if trace_run is None:
        trace_run = create_assistant_run(
            db,
            user_id=principal.user_id,
            conversation_id=conv_id,
        )
    trace_run_id = trace_run.id

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
                    run_id=trace_run_id,
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
            if clarification_resolved_notice is not None:
                yield _sse({"token": "All required details are resolved. "})
            with Session(bind=engine) as run_db:
                yield from _PlanRunner(
                    plan_steps=plan_steps,
                    understanding=understanding,
                    principal=principal,
                    conversation_id=conv_id,
                    run_id=trace_run_id,
                    run_db=run_db,
                    engine=engine,
                    confirmed=False,
                    started_at=datetime.now(timezone.utc).isoformat(),
                ).run()
            return

        # ================================================================
        # STORY 6.1 / 6.3 / 7.3 PATH (forced_response, document question, or chat)
        # ================================================================
        # Story 7.3: a policy / SOP / project question (or a request for a plan
        # based on those documents) is answered only from the best four of eight
        # ACL-filtered semantic matches. Nothing relevant: say so; never guess.
        effective_forced = forced_response or clarification_resolved_notice
        response_step_started = datetime.now(timezone.utc)
        response_step_clock = time.monotonic()
        with Session(bind=engine) as trace_db:
            record_assistant_run_step(
                trace_db,
                run_id=trace_run_id,
                step_order=0,
                step_type="assistant_response",
                step_name="assistant_response",
                status="running",
                input_data={"response_mode": "assistant"},
                started_at=response_step_started,
                estimated_tokens=assistant_planner.estimate_tokens(json.dumps(context_messages)),
            )
        policy_question = assistant_planner.needs_retrieval(req.message)
        document_question = (
            effective_forced is None
            and assistant_planner.should_search_documents(req.message)
            and (
                understanding is None
                or assistant_planner.is_document_plan_request(req.message)
            )
        )
        rag_matches: list[dict] = []
        document_answer: str | None = None
        if document_question:
            try:
                with Session(bind=engine) as query_db:
                    rag_matches = assistant_planner.retrieve_semantic_document_context(
                        query_db,
                        actor_id=principal.user_id,
                        query=assistant_planner.document_search_query(req.message),
                    )
                    # Story 7.4: re-check permissions at answer time, before any
                    # passage or citation reaches the model or the reply.
                    rag_matches = assistant_planner.authorize_document_context(
                        query_db, actor_id=principal.user_id, context=rag_matches
                    )
            except Exception as rag_err:  # noqa: BLE001 - answer without guessing
                logger.warning("Document retrieval failed: %s", rag_err)
                document_answer = assistant_planner.DOCUMENT_SEARCH_FAILED_MESSAGE
            else:
                if rag_matches:
                    try:
                        grounded = assistant_service.answer_from_documents(
                            context_messages,
                            rag_matches,
                            question=req.message,
                        )
                    except Exception as llm_err:  # noqa: BLE001 - answer without guessing
                        logger.warning("Grounded answer failed: %s", llm_err)
                        grounded = None
                        document_answer = assistant_planner.DOCUMENT_SEARCH_FAILED_MESSAGE
                    if grounded is not None and not grounded.found:
                        # Story 7.5: not enough evidence, so no guess and no citation.
                        document_answer = assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
                    elif grounded is not None:
                        document_answer = grounded.rendered
                        yield _sse({
                            "retrieval": {
                                "query": req.message,
                                "snippet": grounded.citations[0]["quote"],
                                "source": grounded.citations[0]["source"],
                                "sources": list(dict.fromkeys(
                                    citation["source"] for citation in grounded.citations
                                )),
                            }
                        })
                        yield _sse({"citations": grounded.citations})
                elif policy_question:
                    document_answer = assistant_planner.DOCUMENT_NOT_FOUND_MESSAGE
                # Any other question with no readable match continues as ordinary
                # chat, which is told not to invent company facts (see the service).

        try:
            if effective_forced is not None:
                response_stream = iter([effective_forced])
            elif document_answer is not None:
                response_stream = iter([document_answer])
            elif understanding is not None:
                response_stream = iter([explain_understanding(understanding)])
            else:
                response_stream = assistant_service.stream_chat_completion(context_messages)
            for token in response_stream:
                if token:
                    accumulated_chunks.append(token)
                    token_payload = json.dumps({"token": token})
                    yield f"data: {token_payload}\n\n"
        except Exception as exc:
            logger.error(
                "Error in stream_assistant_chat generator: %s", exc, exc_info=True
            )
            ended_at = datetime.now(timezone.utc)
            with Session(bind=engine) as trace_db:
                record_assistant_run_step(
                    trace_db,
                    run_id=trace_run_id,
                    step_order=0,
                    step_type="assistant_response",
                    step_name="assistant_response",
                    status="failed",
                    output_data={"status": "failed"},
                    started_at=response_step_started,
                    ended_at=ended_at,
                    duration_ms=int((time.monotonic() - response_step_clock) * 1000),
                )
                set_assistant_run_status(trace_db, trace_run_id, "failed")
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

        ended_at = datetime.now(timezone.utc)
        with Session(bind=engine) as trace_db:
            record_assistant_run_step(
                trace_db,
                run_id=trace_run_id,
                step_order=0,
                step_type="assistant_response",
                step_name="assistant_response",
                status="completed",
                output_data={"status": "completed"},
                started_at=response_step_started,
                ended_at=ended_at,
                duration_ms=int((time.monotonic() - response_step_clock) * 1000),
                estimated_tokens=(
                    assistant_planner.estimate_tokens(json.dumps(context_messages))
                    + assistant_planner.estimate_tokens(full_response)
                    if full_response
                    else assistant_planner.estimate_tokens(json.dumps(context_messages))
                ),
            )
            set_assistant_run_status(trace_db, trace_run_id, "completed")

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
