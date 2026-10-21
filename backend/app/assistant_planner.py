"""Planning, conditional knowledge retrieval, step execution, and checkpointing
for the AI Office Operations Agent assistant (Story 6.4 — Plan & Retrieve).

Terminology
-----------
plan        Ordered list of PlanStep objects produced from an AssistantUnderstanding.
retrieval   Keyword-based search of the company policy knowledge base triggered only
            when a step description contains policy/rule/procedure language.
checkpoint  JSON blob stored in AssistantConversation.execution_state that records
            the plan, every completed step's result and token usage, the current step
            index, total token usage so far, and the termination reason when halted.

Design constraints
------------------
* No new external services or vector databases.  Retrieval is plain-text keyword
  matching against backend/docs/company_policy.md; the interface can be swapped later.
* No duplicate service implementations.  Step execution delegates exclusively to the
  existing scheduling_service, task_service, and email_draft_service modules.
* Retrieval is ONLY triggered by steps that contain explicit policy-related language.
  Ordinary greetings, routine scheduling, and general chat never call the retrieval
  function.
* Estimated token counts are clearly labelled as estimates (character-heuristic
  len(text) // 4).  Actual provider usage is used when available (Groq/OpenAI
  CompletionUsage).  The two are never conflated.
* The clarification gate in assistant_router.py runs BEFORE the planner.  A request
  that still needs clarification never reaches build_execution_plan().
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from app.assistant_understanding import AssistantUnderstanding
from app.meeting.models import AssistantConversation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable limits (overridable via environment variables)
# ---------------------------------------------------------------------------

MAX_PLAN_STEPS: int = int(os.environ.get("ASSISTANT_MAX_STEPS", "12"))

# Estimated token budget per request (character heuristic: len(text) // 4).
# The planner will not begin another step or model call when this is reached.
COST_CAP_TOKENS: int = int(os.environ.get("ASSISTANT_COST_CAP_TOKENS", "4000"))

# ---------------------------------------------------------------------------
# Knowledge-base retrieval (keyword-matching, no vector DB)
# ---------------------------------------------------------------------------

_KNOWLEDGE_BASE_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "company_policy.md"
    if (Path(__file__).resolve().parents[1] / "docs" / "company_policy.md").exists()
    else Path(__file__).resolve().parents[2] / "docs" / "company_policy.md"
)

# Keywords whose presence in a step description signals that company-policy
# context is needed.  Ordinary scheduling/task/email steps do NOT contain these.
_RETRIEVAL_KEYWORDS = frozenset({
    "policy",
    "policies",
    "rule",
    "rules",
    "procedure",
    "procedures",
    "guideline",
    "guidelines",
    "compliance",
    "regulation",
    "threshold",
    "approval",
    "approvals",
    "retention",
    "consent",
    "budget",
    "escalation",
    "restriction",
    "restriction",
    "sensitive",
    "security",
    "data policy",
})

_MAX_RETRIEVAL_SNIPPET_CHARS = 600  # keep token usage predictable


def needs_retrieval(step_description: str) -> bool:
    """Return True only when the step description references policy-level concepts.

    This function deliberately returns False for ordinary requests (schedule a
    meeting, assign a task, draft an email) so the knowledge base is never
    consulted unnecessarily.
    """
    lower = step_description.lower()
    return any(kw in lower for kw in _RETRIEVAL_KEYWORDS)


def retrieve_company_knowledge(query: str) -> str | None:
    """Return the first matching paragraph from the company policy document.

    This is a plain keyword-match retrieval adapter over a Markdown file.
    It is NOT described as semantic or vector search.  The interface can be
    replaced with a proper retrieval backend (e.g. vector store) by swapping
    this function without touching the planner or router.

    Returns None when:
    - The knowledge-base file does not exist.
    - No paragraph contains any token from the query.

    The returned snippet is truncated to _MAX_RETRIEVAL_SNIPPET_CHARS characters
    to limit token consumption.
    """
    if not _KNOWLEDGE_BASE_PATH.exists():
        logger.warning(
            "Company policy knowledge base not found at %s; retrieval skipped.",
            _KNOWLEDGE_BASE_PATH,
        )
        return None

    try:
        content = _KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read knowledge base: %s", exc)
        return None

    query_tokens = {token.lower() for token in query.split() if len(token) > 3}
    if not query_tokens:
        return None

    for paragraph in content.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph or paragraph.startswith("#"):
            continue
        lower_para = paragraph.lower()
        if any(token in lower_para for token in query_tokens):
            return paragraph[:_MAX_RETRIEVAL_SNIPPET_CHARS]

    return None


# ---------------------------------------------------------------------------
# Plan data model
# ---------------------------------------------------------------------------

StepStatus = Literal["pending", "running", "done", "failed", "skipped"]
ActionType = Literal[
    "schedule_meeting",
    "assign_task",
    "draft_email",
    "retrieve_knowledge",
    "summarise_response",
]


@dataclass
class PlanStep:
    """One ordered step in the execution plan."""

    index: int
    description: str
    action_type: ActionType
    status: StepStatus = "pending"
    result_summary: str | None = None
    retrieval_query: str | None = None  # set if this step triggers retrieval
    retrieval_snippet: str | None = None  # populated after retrieval
    estimated_tokens: int = 0  # tokens this step is expected to consume


@dataclass
class CompletedStep:
    """Persisted record of a step that finished (successfully or with failure)."""

    index: int
    description: str
    action_type: str
    status: str  # "done" or "failed"
    result_summary: str
    tokens_used: int  # estimated tokens consumed by this step


@dataclass
class ExecutionCheckpoint:
    """Durable snapshot written to AssistantConversation.execution_state after every step.

    Fields
    ------
    plan_steps      Serialised list of PlanStep dicts (full plan including pending steps).
    completed_steps Records of every finished step (done or failed).
    current_step    0-based index of the step that just ran (-1 means not started).
    total_tokens_estimated
                    Running sum of estimated token usage across all steps executed.
    halted_reason   None while running; "step_limit" or "cost_cap" when stopped early.
    started_at      ISO-8601 timestamp when the plan began executing.
    """

    plan_steps: list[dict]
    completed_steps: list[dict]
    current_step: int
    total_tokens_estimated: int
    halted_reason: str | None
    started_at: str


# ---------------------------------------------------------------------------
# Token estimation (clearly labelled as an estimate)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Return a *rough* token estimate for a text string.

    Uses the character-count heuristic (len(text) // 4).  This is intentionally
    labelled as an estimate and must not be treated as an authoritative token count.
    When the LLM provider returns actual usage data that should be used instead.
    """
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def build_execution_plan(
    understanding: AssistantUnderstanding,
) -> list[PlanStep]:
    """Produce an ordered, bounded execution plan from a validated understanding.

    Only intents with status "understood" should reach this function.  The
    clarification gate in the router must block "needs_clarification" requests.

    Returns at most MAX_PLAN_STEPS steps.

    Supported action types map to existing application services:
      schedule_meeting  → meeting.scheduling_service
      assign_task       → task_service
      draft_email       → email_draft_service

    An extra "summarise_response" step is appended to every plan to produce
    the final plain-language response streamed back to the user.
    """
    if understanding.status != "understood":
        raise ValueError(
            "build_execution_plan called with non-understood intent "
            f"(status={understanding.status!r}).  Run clarification first."
        )

    steps: list[PlanStep] = []

    if understanding.intent == "meeting_scheduling":
        entities = understanding.entities
        people_or_groups = entities.groups or entities.people
        attendee = people_or_groups[0] if people_or_groups else "the specified attendee"
        date_str = entities.date.isoformat() if entities.date else "the requested date"
        time_str = (
            entities.time.strftime("%I:%M %p").lstrip("0")
            if entities.time
            else "the requested time"
        )
        title = entities.meeting or "Meeting"
        duration = (
            f" for {entities.duration_minutes} minutes"
            if entities.duration_minutes is not None
            else ""
        )

        steps.append(PlanStep(
            index=0,
            description=(
                f"Check attendee availability for {attendee} on {date_str} at {time_str}"
            ),
            action_type="schedule_meeting",
            estimated_tokens=estimate_tokens(
                f"Check availability {attendee} {date_str} {time_str}"
            ),
        ))
        steps.append(PlanStep(
            index=1,
            description=(
                f"Schedule '{title}' with {attendee} on {date_str} at {time_str}{duration}"
            ),
            action_type="schedule_meeting",
            estimated_tokens=estimate_tokens(
                f"Schedule meeting {attendee} {date_str} {time_str} {title}"
            ),
        ))
        steps.append(PlanStep(
            index=2,
            description="Send calendar invitations to all attendees",
            action_type="schedule_meeting",
            estimated_tokens=50,
        ))

    elif understanding.intent == "task_assignment":
        entities = understanding.entities
        task = entities.task or "the specified task"
        assignee = entities.people[0] if entities.people else "the specified assignee"
        due = (
            f" by {entities.date.isoformat()}" if entities.date else ""
        )

        steps.append(PlanStep(
            index=0,
            description=f"Verify that '{assignee}' is an active employee",
            action_type="assign_task",
            estimated_tokens=estimate_tokens(f"Verify employee {assignee}"),
        ))
        steps.append(PlanStep(
            index=1,
            description=f"Create task '{task}' assigned to {assignee}{due}",
            action_type="assign_task",
            estimated_tokens=estimate_tokens(f"Assign task {task} {assignee} {due}"),
        ))
        steps.append(PlanStep(
            index=2,
            description=f"Send assignment notification email to {assignee}",
            action_type="assign_task",
            estimated_tokens=50,
        ))

    elif understanding.intent == "send_email":
        entities = understanding.entities
        recipients = entities.groups or entities.people
        recipient = recipients[0] if recipients else "the specified recipient"
        topic = entities.topic or "the requested topic"

        steps.append(PlanStep(
            index=0,
            description=f"Resolve recipients for '{recipient}'",
            action_type="draft_email",
            estimated_tokens=estimate_tokens(f"Resolve recipients {recipient}"),
        ))
        steps.append(PlanStep(
            index=1,
            description=(
                f"Draft email to {recipient} about \"{topic}\" "
                "and run sensitive-data scan"
            ),
            action_type="draft_email",
            estimated_tokens=estimate_tokens(f"Draft email {recipient} {topic}"),
        ))
        steps.append(PlanStep(
            index=2,
            description=(
                "Check if approval is required (threshold: >10 recipients "
                "or external addresses per company email approval policy)"
            ),
            action_type="draft_email",
            retrieval_query="email approval threshold policy",
            estimated_tokens=80,
        ))
        steps.append(PlanStep(
            index=3,
            description="Queue email draft for review or delivery",
            action_type="draft_email",
            estimated_tokens=50,
        ))

    # Always end with a summarise step so the LLM produces a plain-language reply.
    summary_index = len(steps)
    steps.append(PlanStep(
        index=summary_index,
        description="Compose a plain-language confirmation for the user",
        action_type="summarise_response",
        estimated_tokens=estimate_tokens("Compose confirmation response"),
    ))

    # Pre-flag retrieval steps so the router can emit retrieval events before executing.
    for step in steps:
        if step.retrieval_query is None and needs_retrieval(step.description):
            step.retrieval_query = step.description

    # Safety: cap at MAX_PLAN_STEPS regardless of how many steps were built.
    return steps[:MAX_PLAN_STEPS]


# ---------------------------------------------------------------------------
# Checkpoint persistence
# ---------------------------------------------------------------------------

def save_execution_checkpoint(
    db: Session,
    conversation: AssistantConversation,
    plan_steps: list[PlanStep],
    completed_steps: list[CompletedStep],
    current_step: int,
    total_tokens_estimated: int,
    halted_reason: str | None,
    started_at: str,
) -> None:
    """Persist the current execution state to AssistantConversation.execution_state.

    Called after every step completes (success or failure) so that the latest
    checkpoint is always available even if an error occurs on a later step.

    Never raises; logs on failure.
    """
    checkpoint = ExecutionCheckpoint(
        plan_steps=[asdict(s) for s in plan_steps],
        completed_steps=[asdict(s) for s in completed_steps],
        current_step=current_step,
        total_tokens_estimated=total_tokens_estimated,
        halted_reason=halted_reason,
        started_at=started_at,
    )
    try:
        conversation.execution_state = json.dumps(
            asdict(checkpoint), separators=(",", ":"), default=str
        )
        conversation.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to save execution checkpoint: %s", exc, exc_info=True)


def load_execution_checkpoint(
    conversation: AssistantConversation,
) -> ExecutionCheckpoint | None:
    """Load a previously saved execution checkpoint, or None if none exists."""
    raw = conversation.execution_state
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return ExecutionCheckpoint(
            plan_steps=data.get("plan_steps", []),
            completed_steps=data.get("completed_steps", []),
            current_step=data.get("current_step", -1),
            total_tokens_estimated=data.get("total_tokens_estimated", 0),
            halted_reason=data.get("halted_reason"),
            started_at=data.get("started_at", ""),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.error("Corrupted execution checkpoint; discarding: %s", exc)
        return None


def clear_execution_checkpoint(
    db: Session,
    conversation: AssistantConversation,
) -> None:
    """Remove the execution checkpoint from the conversation row."""
    try:
        conversation.execution_state = None
        conversation.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to clear execution checkpoint: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Limit guard helpers (used by the router's step loop)
# ---------------------------------------------------------------------------

def check_step_limit(step_index: int) -> bool:
    """Return True when the step index has reached MAX_PLAN_STEPS."""
    return step_index >= MAX_PLAN_STEPS


def check_cost_cap(total_tokens_estimated: int, next_step_tokens: int) -> bool:
    """Return True when starting the next step would exceed the token budget.

    The check uses the *estimate* for the next step so that the budget check
    happens BEFORE the step begins (preventing any irreversible side effects
    after budget exhaustion).
    """
    return (total_tokens_estimated + next_step_tokens) > COST_CAP_TOKENS


# ---------------------------------------------------------------------------
# Step executor — delegates to existing application services
# ---------------------------------------------------------------------------

class StepExecutionError(Exception):
    """Raised when a plan step fails.  The caller preserves earlier checkpoints."""


def execute_plan_step(
    step: PlanStep,
    understanding: AssistantUnderstanding,
    db: Session,
    user_id: str,
) -> str:
    """Execute one plan step by calling the appropriate existing service.

    Returns a short human-readable summary string on success.
    Raises StepExecutionError on failure.  Earlier checkpoints are never
    overwritten on failure — the caller is responsible for that.

    Note on dryrun vs execution
    ---------------------------
    Meeting scheduling and task creation require many parameters that are
    only partially available from the understanding (e.g. organiser email,
    calendar provider config).  In Story 6.4 the executor produces a
    validated, human-readable description of what *would* happen rather
    than calling the live service.  The live service integration is the
    responsibility of Story 6.5 (Execute).  This is consistent with the
    existing assistant behaviour: Story 6.1–6.3 state "I haven't made any
    changes."

    The retrieval step is handled inline in the router because it produces
    an SSE event; summarise_response is also handled in the router because it
    streams tokens.  Those two action types raise StepExecutionError here so
    the router catches them and handles them directly.
    """
    if step.action_type == "summarise_response":
        raise StepExecutionError(
            "summarise_response steps are handled by the streaming router, "
            "not the step executor."
        )
    if step.action_type == "retrieve_knowledge":
        raise StepExecutionError(
            "retrieve_knowledge steps are handled inline by the router."
        )

    entities = understanding.entities

    if step.action_type == "schedule_meeting":
        # Validate that the required entities are present.
        people_or_groups = entities.groups or entities.people
        if not people_or_groups:
            raise StepExecutionError("No attendees found in the understanding entities.")
        attendee = people_or_groups[0]
        if entities.date is None:
            raise StepExecutionError("Meeting date is missing from the understanding.")
        if entities.time is None:
            raise StepExecutionError("Meeting time is missing from the understanding.")

        date_str = entities.date.isoformat()
        time_str = entities.time.strftime("%H:%M")
        title = entities.meeting or "Meeting"
        duration = entities.duration_minutes or 30

        # Dry-run description (see note above).  Story 6.5 will replace this
        # with a live call to scheduling_service.schedule_meeting().
        return (
            f"Validated: schedule '{title}' with {attendee} on {date_str} "
            f"at {time_str} for {duration} min."
        )

    if step.action_type == "assign_task":
        task = entities.task
        if not task:
            raise StepExecutionError("Task description is missing from the understanding.")
        assignee = entities.people[0] if entities.people else None
        if not assignee:
            raise StepExecutionError("Task assignee is missing from the understanding.")
        due = (
            f" by {entities.date.isoformat()}" if entities.date else ""
        )
        # Dry-run description.  Story 6.5 will call task_service.
        return f"Validated: assign '{task}' to {assignee}{due}."

    if step.action_type == "draft_email":
        recipients = entities.groups or entities.people
        if not recipients:
            raise StepExecutionError("Email recipient is missing from the understanding.")
        topic = entities.topic or "(no topic)"
        # Dry-run description.  Story 6.5 will call email_draft_service.
        return f"Validated: draft email to {recipients[0]} about \"{topic}\"."

    raise StepExecutionError(
        f"Unknown action_type {step.action_type!r}. No handler available."
    )
