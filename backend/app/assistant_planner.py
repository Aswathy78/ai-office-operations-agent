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
* Story 6.5: every actionable step names the tool it runs (``tool_name``).  Tools
  are executed through ``app.assistant_tools.execute_tool`` (schema validation,
  scopes, idempotency, timeout, retries).  Medium/high-risk write steps wait for
  the user's confirmation before they run.
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
from datetime import datetime, time as Time, timedelta, timezone
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


def search_company_knowledge(query: str, limit: int = 3) -> list[dict]:
    """Return up to ``limit`` policy paragraphs ranked by how many query words they contain.

    Same keyword-matching adapter as ``retrieve_company_knowledge`` (no vector
    search), used by the ``document.search`` tool (Story 6.5). Each result is
    ``{"snippet", "score", "source"}``; the list is empty when nothing matches
    or the knowledge base is missing.
    """
    if not _KNOWLEDGE_BASE_PATH.exists():
        logger.warning(
            "Company policy knowledge base not found at %s; search skipped.",
            _KNOWLEDGE_BASE_PATH,
        )
        return []
    try:
        content = _KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read knowledge base: %s", exc)
        return []

    query_tokens = {token.lower().strip(".,;:!?\"'") for token in query.split() if len(token) > 3}
    if not query_tokens:
        return []

    ranked: list[tuple[int, int, str]] = []
    for position, paragraph in enumerate(content.split("\n\n")):
        paragraph = paragraph.strip()
        if not paragraph or paragraph.startswith("#"):
            continue
        lower_para = paragraph.lower()
        score = sum(1 for token in query_tokens if token in lower_para)
        if score:
            ranked.append((-score, position, paragraph))
    ranked.sort()
    return [
        {
            "snippet": paragraph[:_MAX_RETRIEVAL_SNIPPET_CHARS],
            "score": -negative_score,
            "source": _KNOWLEDGE_BASE_PATH.name,
        }
        for negative_score, _, paragraph in ranked[:limit]
    ]


# ---------------------------------------------------------------------------
# Plan data model
# ---------------------------------------------------------------------------

StepStatus = Literal[
    "pending", "running", "done", "failed", "skipped", "awaiting_confirmation"
]
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
    # Story 6.5 — the tool this step runs and how.
    tool_name: str | None = None
    tool_arguments: dict | None = None  # bound just before the step runs
    requires_confirmation: bool = False
    idempotency_key: str | None = None  # set for write steps when confirmation is requested
    result: dict | None = None  # the tool's result once the step has run


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
    pending_confirmation
                    Story 6.5: set while write steps wait for the user to confirm —
                    ``{confirmation_id, created_at, understanding, results}``.
    """

    plan_steps: list[dict]
    completed_steps: list[dict]
    current_step: int
    total_tokens_estimated: int
    halted_reason: str | None
    started_at: str
    pending_confirmation: dict | None = None


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

    Each actionable step names the tool it runs (Story 6.5); the tool, not the
    step, decides the required scopes, risk tier, timeout and retry policy:
      meeting_scheduling → directory.lookup_employees, calendar.check_availability,
                           meeting.schedule
      task_assignment    → directory.lookup_employees, task.create
      send_email         → directory.lookup_employees, email.preview,
                           document.search, email.send

    An extra "summarise_response" step is appended to every plan to produce
    the final plain-language response streamed back to the user.
    """
    if understanding.status != "understood":
        raise ValueError(
            "build_execution_plan called with non-understood intent "
            f"(status={understanding.status!r}).  Run clarification first."
        )

    steps: list[PlanStep] = []

    def add(description: str, action_type: ActionType, tool_name: str, estimated: int,
            retrieval_query: str | None = None) -> None:
        steps.append(PlanStep(
            index=len(steps),
            description=description,
            action_type=action_type,
            tool_name=tool_name,
            requires_confirmation=_tool_requires_confirmation(tool_name),
            retrieval_query=retrieval_query,
            estimated_tokens=estimated,
        ))

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

        add(
            f"Look up {attendee} in the employee directory",
            "schedule_meeting", "directory.lookup_employees",
            estimate_tokens(f"Look up {attendee}"),
        )
        add(
            f"Check attendee availability for {attendee} on {date_str} at {time_str}",
            "schedule_meeting", "calendar.check_availability",
            estimate_tokens(f"Check availability {attendee} {date_str} {time_str}"),
        )
        add(
            f"Schedule '{title}' with {attendee} on {date_str} at {time_str}{duration} "
            "and send calendar invitations",
            "schedule_meeting", "meeting.schedule",
            estimate_tokens(f"Schedule meeting {attendee} {date_str} {time_str} {title}"),
        )

    elif understanding.intent == "task_assignment":
        entities = understanding.entities
        task = entities.task or "the specified task"
        assignee = entities.people[0] if entities.people else "the specified assignee"
        due = (
            f" by {entities.date.isoformat()}" if entities.date else ""
        )

        add(
            f"Verify that '{assignee}' is an active employee",
            "assign_task", "directory.lookup_employees",
            estimate_tokens(f"Verify employee {assignee}"),
        )
        add(
            f"Create task '{task}' assigned to {assignee}{due} "
            "and send the assignment notification e-mail",
            "assign_task", "task.create",
            estimate_tokens(f"Assign task {task} {assignee} {due}"),
        )

    elif understanding.intent == "send_email":
        entities = understanding.entities
        recipients = entities.groups or entities.people
        recipient = recipients[0] if recipients else "the specified recipient"
        topic = entities.topic or "the requested topic"

        add(
            f"Resolve recipients for '{recipient}'",
            "draft_email", "directory.lookup_employees",
            estimate_tokens(f"Resolve recipients {recipient}"),
        )
        add(
            f"Draft email to {recipient} about \"{topic}\" "
            "and run sensitive-data scan",
            "draft_email", "email.preview",
            estimate_tokens(f"Draft email {recipient} {topic}"),
        )
        add(
            "Check if approval is required (threshold: >10 recipients "
            "or external addresses per company email approval policy)",
            "draft_email", "document.search", 80,
            retrieval_query="email approval threshold policy",
        )
        add(
            "Queue email draft for review or delivery",
            "draft_email", "email.send", 50,
        )

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
    pending_confirmation: dict | None = None,
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
        pending_confirmation=pending_confirmation,
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
            pending_confirmation=data.get("pending_confirmation"),
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


def clear_pending_confirmation(
    db: Session,
    conversation: AssistantConversation,
) -> None:
    """Drop write steps that were waiting for confirmation (Story 6.5).

    The steps are marked "skipped" so a later "confirm" can never run them.
    The rest of the checkpoint is kept for the record. Never raises; logs on failure.
    """
    checkpoint = load_execution_checkpoint(conversation)
    if checkpoint is None or not checkpoint.pending_confirmation:
        return
    for step in checkpoint.plan_steps:
        if step.get("status") == "awaiting_confirmation":
            step["status"] = "skipped"
    checkpoint.pending_confirmation = None
    try:
        conversation.execution_state = json.dumps(
            asdict(checkpoint), separators=(",", ":"), default=str
        )
        conversation.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to clear pending confirmation: %s", exc, exc_info=True)


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


def _tool_requires_confirmation(tool_name: str) -> bool:
    from app.assistant_tools import get_registry

    return get_registry().get(tool_name).requires_confirmation


def assistant_email_draft_mode() -> str:
    """Drafting mode for chat e-mails: "template" (default) or "auto" (LLM first)."""
    mode = os.environ.get("ASSISTANT_EMAIL_DRAFT_MODE", "template").strip().casefold()
    return mode if mode in ("template", "auto") else "template"


DEFAULT_MEETING_MINUTES = 30
DEFAULT_TASK_DUE_DAYS = 7
TASK_DUE_LOCAL_TIME = (17, 0)


def _organization_datetime(day, hour: int, minute: int) -> datetime:
    from app.meeting.scheduling_service import get_organization_timezone

    local = datetime.combine(day, Time(hour, minute), tzinfo=get_organization_timezone())
    return local.astimezone(timezone.utc)


def _task_due_at(understanding: AssistantUnderstanding) -> datetime:
    """The requested date at 17:00 organization time, or a week from today."""
    from app.meeting.scheduling_service import get_organization_timezone

    day = understanding.entities.date
    if day is None:
        today = datetime.now(timezone.utc).astimezone(get_organization_timezone()).date()
        day = today + timedelta(days=DEFAULT_TASK_DUE_DAYS)
    return _organization_datetime(day, *TASK_DUE_LOCAL_TIME)


def prepare_tool_arguments(
    step: PlanStep,
    understanding: AssistantUnderstanding,
    results: dict[str, dict],
) -> dict:
    """Bind a step's tool arguments from the understanding and earlier tool results.

    ``results`` maps tool names to the results of steps that already ran, so for
    example the meeting attendees are the employees the directory step resolved.
    """
    entities = understanding.entities
    tool = step.tool_name
    if tool == "directory.lookup_employees":
        arguments: dict = {}
        if entities.people:
            arguments["names"] = list(dict.fromkeys(entities.people))
        if entities.groups:
            arguments["groups"] = list(dict.fromkeys(entities.groups))
        return arguments

    resolved = results.get("directory.lookup_employees", {}).get("employees", [])
    named_emails = [
        employee["email"] for employee in resolved
        if str(employee.get("matched_by", "")).startswith("name:")
    ]

    if tool in ("calendar.check_availability", "meeting.schedule"):
        start = _organization_datetime(entities.date, entities.time.hour, entities.time.minute)
        duration = entities.duration_minutes or DEFAULT_MEETING_MINUTES
        emails = list(dict.fromkeys(employee["email"] for employee in resolved))
        if tool == "calendar.check_availability":
            return {
                "attendee_emails": emails,
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(minutes=duration)).isoformat(),
            }
        return {
            "title": entities.meeting or "Meeting",
            "start_at": start.isoformat(),
            "duration_minutes": duration,
            "attendee_emails": emails,
            "agenda": "Scheduled via the AI Office Assistant.",
        }

    if tool == "task.create":
        if not resolved:
            raise StepExecutionError("The task assignee was not resolved in the directory.")
        return {
            "assignee_email": resolved[0]["email"],
            "title": entities.task,
            "due_at": _task_due_at(understanding).isoformat(),
            "priority": "medium",
        }

    if tool in ("email.preview", "email.send"):
        arguments = {}
        if named_emails:
            arguments["recipient_emails"] = named_emails
        if entities.groups:
            arguments["groups"] = list(dict.fromkeys(entities.groups))
        if tool == "email.preview":
            return {**arguments, "topic": entities.topic, "mode": assistant_email_draft_mode()}
        preview = results.get("email.preview")
        if not preview:
            raise StepExecutionError("The e-mail has not been drafted yet.")
        return {
            **arguments,
            "subject": preview["subject"],
            "body": preview["body"],
            "request": f"E-mail about {entities.topic}",
        }

    if tool == "document.search":
        return {"query": step.retrieval_query or step.description, "limit": 3}

    raise StepExecutionError(f"No argument binding is defined for the tool {tool!r}.")


def _names(people: list[dict]) -> str:
    return ", ".join(person["name"] for person in people) or "nobody"


def summarize_tool_result(tool_name: str, result: dict) -> str:
    """Turn a tool result into a one-line step summary.

    Raises StepExecutionError when the result means the plan cannot go on
    (an unknown attendee, a busy calendar, unresolved e-mail recipients).
    """
    if tool_name == "directory.lookup_employees":
        if result.get("ambiguous"):
            name, candidates = next(iter(result["ambiguous"].items()))
            raise StepExecutionError(
                f"Several employees match '{name}': {', '.join(candidates)}."
            )
        if result.get("unresolved"):
            raise StepExecutionError(
                "Not found in the employee directory: " + ", ".join(result["unresolved"]) + "."
            )
        return f"Found {len(result['employees'])} employee(s): {_names(result['employees'])}."

    if tool_name == "calendar.check_availability":
        if not result["available"]:
            busy = "; ".join(
                f"{c['attendee_name'] or c['attendee_email']} is busy with "
                f"'{c['conflicting_event_title']}'"
                for c in result["conflicts"]
            )
            slots = ", ".join(slot["start_at"] for slot in result["alternative_slots"])
            raise StepExecutionError(
                f"{busy}. " + (f"Free slots: {slots}." if slots else "No free slot was found.")
            )
        return f"All {len(result['attendee_emails'])} attendee(s) are free."

    if tool_name == "meeting.schedule":
        summary = (
            f"Scheduled '{result['title']}' at {result['start_at']} "
            f"(meeting {result['meeting_id']}); invitations sent to "
            f"{len(result['invitations_sent'])} attendee(s)."
        )
        if result["invitation_failures"]:
            summary += " Invitations failed for: " + ", ".join(result["invitation_failures"]) + "."
        return summary

    if tool_name == "email.preview":
        if result["unresolved_recipients"]:
            raise StepExecutionError(
                "Could not resolve e-mail recipients: "
                + ", ".join(result["unresolved_recipients"]) + "."
            )
        summary = f"Drafted \"{result['subject']}\" for {len(result['recipients'])} recipient(s)."
        if result["approval_required"]:
            summary += " Approval will be required: " + " ".join(result["approval_reasons"])
        if result["sensitive_findings"]:
            labels = sorted({finding["label"] for finding in result["sensitive_findings"]})
            summary += " Possible sensitive data: " + ", ".join(labels) + "."
        return summary

    if tool_name == "document.search":
        count = result["count"]
        return (
            f"Found {count} matching policy passage(s)." if count
            else "No matching policy passage was found."
        )

    if tool_name == "email.send":
        outcome = result["outcome"]
        if outcome == "queued":
            return (
                f"E-mail \"{result['subject']}\" queued for delivery to "
                f"{result['recipient_count']} recipient(s) (draft {result['draft_id']})."
            )
        if outcome == "pending_approval":
            return (
                f"E-mail \"{result['subject']}\" submitted for approval "
                f"(draft {result['draft_id']}): " + " ".join(result["approval_reasons"])
            )
        if outcome == "needs_review":
            return (
                f"E-mail \"{result['subject']}\" saved as a draft for your review because it "
                "may contain sensitive data: " + ", ".join(result["sensitive_labels"]) + "."
            )
        return f"E-mail \"{result['subject']}\" is {result['status']} (draft {result['draft_id']})."

    if tool_name == "task.create":
        assignee = result["assignee"]["name"] if result.get("assignee") else "the assignee"
        return (
            f"Created task '{result['title']}' for {assignee}, due {result['due_at']} "
            f"(task {result['task_id']}). Assignment e-mail: {result['assignment_email_status']}."
        )

    return f"Completed {tool_name}."


def execute_plan_step(
    step: PlanStep,
    understanding: AssistantUnderstanding,
    db: Session,
    user_id: str,
    *,
    principal=None,
    results: dict[str, dict] | None = None,
    conversation_id: str | None = None,
    confirmed: bool = False,
) -> str:
    """Execute one plan step and return a short human-readable summary.

    Story 6.5: a step that names a tool runs it through
    ``app.assistant_tools.execute_tool`` as ``principal``, so the change really
    happens (a task row is created, a meeting is booked, an e-mail is queued) and
    is recorded in the tool-execution ledger.  The tool result is stored in
    ``step.result`` and in ``results`` for later steps.

    Steps without a tool (e.g. plans built by older code) are only validated
    against the understanding and report "Validated: ..." — they change nothing.

    The retrieval step is handled inline in the router because it produces
    an SSE event; summarise_response is also handled in the router because it
    streams tokens.  Those two action types raise StepExecutionError here so
    the router catches them and handles them directly.

    Raises StepExecutionError on failure.  Earlier checkpoints are never
    overwritten on failure — the caller is responsible for that.
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

    if step.tool_name is not None and principal is not None:
        from app.assistant_tools import ToolError, execute_tool

        results = results if results is not None else {}
        if step.tool_arguments is None:
            step.tool_arguments = prepare_tool_arguments(step, understanding, results)
        try:
            outcome = execute_tool(
                db,
                principal,
                step.tool_name,
                step.tool_arguments,
                idempotency_key=step.idempotency_key,
                conversation_id=conversation_id,
                confirmed=confirmed,
            )
        except ToolError as exc:
            raise StepExecutionError(exc.message) from exc
        step.result = outcome.result or {}
        results[step.tool_name] = step.result
        return summarize_tool_result(step.tool_name, step.result)

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
        return f"Validated: assign '{task}' to {assignee}{due}."

    if step.action_type == "draft_email":
        recipients = entities.groups or entities.people
        if not recipients:
            raise StepExecutionError("Email recipient is missing from the understanding.")
        topic = entities.topic or "(no topic)"
        return f"Validated: draft email to {recipients[0]} about \"{topic}\"."

    raise StepExecutionError(
        f"Unknown action_type {step.action_type!r}. No handler available."
    )
