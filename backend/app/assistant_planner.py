"""Planning, conditional document retrieval, step execution, and checkpointing
for the AI Office Operations Agent assistant (Stories 6.4 and 7.3).

Terminology
-----------
plan        Ordered list of PlanStep objects produced from an AssistantUnderstanding.
retrieval   Semantic search of indexed company documents for assistant questions;
            the legacy keyword lookup remains for policy-check plan steps.
checkpoint  JSON blob stored in AssistantConversation.execution_state that records
            the plan, every completed step's result and token usage, the current step
            index, total token usage so far, and the termination reason when halted.

Design constraints
------------------
* No new external services or vector databases. Document retrieval reuses Story 7.2
  embeddings, vectors, version checks, and ACL-filtered semantic search.
* No duplicate service implementations.  Step execution delegates exclusively to the
  existing scheduling_service, task_service, and email_draft_service modules.
* Story 6.5: every actionable step names the tool it runs (``tool_name``).  Tools
  are executed through ``app.assistant_tools.execute_tool`` (schema validation,
  scopes, idempotency, timeout, retries).  Medium/high-risk write steps wait for
  the user's confirmation before they run.
* Semantic retrieval is limited to document-focused questions and plan steps explicitly
  marked for retrieval. Greetings and routine scheduling do not trigger document search.
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
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as Time, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from app import rag_citations
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
# Assistant knowledge retrieval
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
    "sensitive",
    "security",
    "data policy",
    "leave",
    "leaves",
    "holiday",
    "holidays",
    "vacation",
    "attendance",
    "wfh",
    "remote work",
    "work from home",
    "probation",
    "notice",
    "expense",
    "travel",
    "procurement",
    "laptop",
    "asset",
    "sla",
    "document",
    "ocr",
    "contract",
    "handbook",
    "allowance",
    "reimbursement",
    "working hours",
    "onboarding",
    "offboarding",
})

_MAX_RETRIEVAL_SNIPPET_CHARS = 600  # keep token usage predictable
DOCUMENT_SEARCH_RETRIEVAL_LIMIT = 8
DOCUMENT_SEARCH_CONTEXT_LIMIT = 4
_DOCUMENT_QUERY_TERMS = re.compile(
    r"\b(?:casual|maternity|time\s+off|working\s+hours|core\s+hours?|human\s+resources|"
    r"entitlements?|benefits?)\b",
    re.IGNORECASE,
)


def needs_retrieval(step_description: str) -> bool:
    """Return True when the description references company-document concepts.

    Ordinary meeting/task requests do not trigger document search unnecessarily.
    """
    lower = step_description.lower()
    return any(kw in lower for kw in _RETRIEVAL_KEYWORDS) or bool(
        re.search(r"\b(?:hr|sops?|projects?)\b", lower)
        or _DOCUMENT_QUERY_TERMS.search(lower)
    )


_QUESTION_START = re.compile(
    r"^\s*(?:please\s+)?(?:what|when|where|which|who|whom|whose|why|how|can|could|do|does|"
    r"did|is|are|am|will|would|should|may|tell\s+me|explain)\b",
    re.IGNORECASE,
)


def looks_like_question(message: str) -> bool:
    """True for a plain question, whatever words it uses."""
    return "?" in message or bool(_QUESTION_START.search(message))


def should_search_documents(message: str) -> bool:
    """A question might be answered by a company document even without a policy keyword.

    "How many remote work days are allowed per year?" has no entry in the keyword
    list, yet only the uploaded documents can answer it, so any question is searched.
    """
    return needs_retrieval(message) or looks_like_question(message)


def is_document_plan_request(message: str) -> bool:
    """Identify document-grounded plan requests that belong in the chat flow."""
    return needs_retrieval(message) and bool(
        re.search(
            r"\b(?:create|make|build|draft|prepare|develop)\b.{0,80}\bplan\b",
            message,
            re.IGNORECASE,
        )
    )


_PLAN_REQUEST_WORDS = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+)?(?:create|make|build|draft|prepare|develop)\s+"
    r"(?:(?:a|an|the|my|our)\s+)?|\bplan\b|\b(?:based\s+on|using|from|according\s+to)\b",
    re.IGNORECASE,
)


def document_search_query(message: str) -> str:
    """The question to search documents with (Story 7.3).

    A plan request ("Create an onboarding plan based on the HR SOP") is searched
    by its subject; the instruction words would only dilute the match.
    """
    if not is_document_plan_request(message):
        return message
    subject = re.sub(r"\s+", " ", _PLAN_REQUEST_WORDS.sub(" ", message)).strip(" .!?")
    return subject or message


def retrieve_semantic_document_context(
    db: Session,
    *,
    actor_id: str,
    query: str,
) -> list[dict]:
    """Retrieve eight ACL-eligible semantic matches and pass only the best four."""
    from app.documents.indexing import search_documents

    retrieved = search_documents(
        db,
        actor_id=actor_id,
        query=query,
        limit=DOCUMENT_SEARCH_RETRIEVAL_LIMIT,
    )
    context = []
    for result in retrieved[:DOCUMENT_SEARCH_CONTEXT_LIMIT]:
        # Story 7.5: retrieved text is data. Instruction-like sentences are removed
        # before the passage reaches any prompt, draft or citation.
        snippet, redacted = rag_citations.sanitize_passage(result.content)
        if redacted:
            logger.warning(
                "Removed %d instruction-like sentence(s) from a passage of document %s.",
                redacted,
                result.document_id,
            )
        context.append(
            {
                "source": result.filename,
                "snippet": snippet,
                "score": result.score,
                "document_id": result.document_id,
                "heading": result.heading,
                "document_version": result.document_version,
                "page_label": result.page_label,
                "chunk_index": result.chunk_index,
                "redacted_instructions": redacted,
            }
        )
    return context


def search_company_knowledge(
    query: str,
    limit: int = 3,
    db: Session | None = None,
    actor_id: str | None = None,
) -> list[dict]:
    """Return relevant document passages and static policy paragraphs.

    Uploaded documents are searched only through
    ``retrieve_semantic_document_context`` so version and ACL checks remain
    consistent with Story 7.2. The static company policy remains a keyword
    fallback for legacy policy-check plan steps.

    Each result is ``{"snippet", "score", "source"}``.
    """
    query_tokens = [
        token.lower().strip(".,;:!?\"'()[]{}")
        for token in query.split()
        if len(token.strip(".,;:!?\"'()[]{}")) > 2
    ]
    if not query_tokens:
        return []

    # Check for 2-word phrases from query for high-relevance boosting
    raw_words = [w.lower().strip(".,;:!?\"'") for w in query.split() if w.strip(".,;:!?\"'")]
    phrases = [
        f"{raw_words[i]} {raw_words[i+1]}"
        for i in range(len(raw_words) - 1)
        if len(raw_words[i]) > 2 and len(raw_words[i+1]) > 2
    ]

    if db is not None and actor_id:
        semantic_results = retrieve_semantic_document_context(
            db,
            actor_id=actor_id,
            query=query,
        )
        if semantic_results:
            return semantic_results[:limit]
    if re.search(r"\bworking\s+hours?\b", query, re.IGNORECASE):
        return []

    corpus_items: list[tuple[str, str]] = []  # (source_name, paragraph)

    # Only use the static policy file when indexed search has no relevant result.
    if _KNOWLEDGE_BASE_PATH.exists():
        try:
            content = _KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
            for para in content.split("\n\n"):
                clean_para = para.strip()
                if clean_para and not clean_para.startswith("#"):
                    corpus_items.append((_KNOWLEDGE_BASE_PATH.name, clean_para))
        except OSError as exc:
            logger.error("Failed to read knowledge base file %s: %s", _KNOWLEDGE_BASE_PATH, exc)

    if not corpus_items:
        return []

    ranked: list[tuple[int, int, str, str]] = []
    for position, (source, paragraph) in enumerate(corpus_items):
        lower_para = paragraph.lower()
        score = sum(1 for token in query_tokens if token in lower_para)
        # Boost exact phrase matches
        for phrase in phrases:
            if phrase in lower_para:
                score += 8
        if score > 0:
            ranked.append((-score, position, source, paragraph))

    ranked.sort()
    return [
        {
            "snippet": paragraph[:_MAX_RETRIEVAL_SNIPPET_CHARS],
            "score": -negative_score,
            "source": source,
        }
        for negative_score, _, source, paragraph in ranked[:limit]
    ]


def retrieve_company_knowledge(
    query: str,
    db: Session | None = None,
    actor_id: str | None = None,
) -> str | None:
    """Return the first matching paragraph from the company policy or uploaded documents.

    Maintains full backward compatibility with Story 6.4 while enabling RAG
    retrieval across uploaded documents when `db` is available.
    """
    results = search_company_knowledge(query, limit=1, db=db, actor_id=actor_id)
    if results:
        return results[0]["snippet"]
    return None


# Story 7.3: answers come only from documents the employee may read. When the
# search finds nothing relevant the assistant says so instead of guessing.
# Story 7.5: insufficient evidence is answered with "Not found", never a guess.
DOCUMENT_NOT_FOUND_MESSAGE = rag_citations.NOT_FOUND_MESSAGE
DOCUMENT_SEARCH_FAILED_MESSAGE = (
    "I couldn't search the company documents right now, so I won't guess an answer. "
    "Please try again in a moment."
)
EMAIL_DOCUMENT_NOT_FOUND_NOTICE = (
    "I couldn't find relevant information in the indexed company documents for this "
    "e-mail, so the draft contains no company-specific facts; add them before sending."
)
_EMAIL_QUERY_NOISE = re.compile(
    r"\b(?:the\s+)?(?:company|organi[sz]ation)['’]s\s*"
    r"|\s+(?:according\s+to|based\s+on|using|from|in)\s+(?:the\s+)?"
    r"(?:hr\s+)?(?:handbook|polic(?:y|ies)|sops?|documents?)\b.*$",
    re.IGNORECASE,
)


def email_document_query(topic: str) -> str:
    """The search query for an e-mail topic, without where-to-look phrasing."""
    return re.sub(r"\s+", " ", _EMAIL_QUERY_NOISE.sub(" ", topic)).strip() or topic


def retrieve_email_document_context(
    db: Session,
    *,
    actor_id: str,
    topic: str,
) -> list[dict]:
    """Company-document facts for an e-mail the assistant drafts (Story 7.3).

    The ACL-filtered semantic search (top 8, best 4) comes first; the static
    company policy is the same fallback the plan's policy check uses.
    """
    query = email_document_query(topic)
    results = retrieve_semantic_document_context(db, actor_id=actor_id, query=query)
    if results:
        return results
    snippet = retrieve_company_knowledge(query, db=db, actor_id=actor_id)
    if snippet:
        return [{"source": _KNOWLEDGE_BASE_PATH.name, "snippet": snippet}]
    return []


def _is_public_policy_text(snippet: str) -> bool:
    """True when ``snippet`` really comes from the static company policy file,
    which every employee may read (it lives in the repository, not the ACL store)."""
    text = " ".join((snippet or "").split())
    if not text or not _KNOWLEDGE_BASE_PATH.exists():
        return False
    try:
        policy = " ".join(_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8").split())
    except OSError:
        return False
    return text in policy


def authorize_document_context(
    db: Session,
    *,
    actor_id: str,
    context: list[dict],
) -> list[dict]:
    """Re-check retrieved or cached document context for ``actor_id`` (Story 7.4).

    Called right before context is used to answer, draft or send, and again for
    context carried over from an earlier turn or another workflow. An item is kept
    only when it names a document the actor may read now, at its current version,
    or when its text comes from the public static policy file. Anything that
    cannot be verified is dropped (secure default), so neither its content nor its
    citation reaches the model or the user.
    """
    from app.documents.service import accessible_document_versions

    if not context or not actor_id:
        return []
    allowed = accessible_document_versions(
        db, actor_id, {item.get("document_id") for item in context}
    )
    kept = []
    for item in context:
        document_id = item.get("document_id")
        if document_id:
            version = item.get("document_version")
            if document_id in allowed and (version is None or version == allowed[document_id]):
                kept.append(item)
        elif item.get("source") == _KNOWLEDGE_BASE_PATH.name and _is_public_policy_text(
            item.get("snippet", "")
        ):
            kept.append(item)
    if len(kept) < len(context):
        logger.info(
            "Dropped %d document passage(s) the requester may not read.",
            len(context) - len(kept),
        )
    return kept


# ---------------------------------------------------------------------------
# Plan data model
# ---------------------------------------------------------------------------

StepStatus = Literal[
    "pending", "running", "done", "failed", "escalated", "skipped", "awaiting_confirmation",
    "approval_required",
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
    # Story 6.6 — set when the write guard paused the step for approval.
    approval_id: str | None = None
    risk_level: str | None = None


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
        has_explicit_assignee = bool(entities.people)
        assignee = entities.people[0] if has_explicit_assignee else "yourself"
        due = (
            f" by {entities.date.isoformat()}" if entities.date else ""
        )

        if has_explicit_assignee:
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


class StepApprovalRequired(Exception):
    """The write guard paused the step (Story 6.6); ``approval`` is the pending request."""

    def __init__(self, approval: dict, risk_reasons: list[str]):
        super().__init__(approval["summary"])
        self.approval = approval
        self.risk_reasons = risk_reasons


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
    """The requested date/time in organization time, or the existing defaults."""
    from app.meeting.scheduling_service import get_organization_timezone

    day = understanding.entities.date
    if day is None:
        today = datetime.now(timezone.utc).astimezone(get_organization_timezone()).date()
        day = today + timedelta(days=DEFAULT_TASK_DUE_DAYS)
    due_time = understanding.entities.time or Time(*TASK_DUE_LOCAL_TIME)
    return _organization_datetime(day, due_time.hour, due_time.minute)


def prepare_tool_arguments(
    step: PlanStep,
    understanding: AssistantUnderstanding,
    results: dict[str, dict],
    principal=None,
) -> dict:
    """Bind a step's tool arguments from the understanding and earlier tool results.

    ``results`` maps tool names to the results of steps that already ran, so for
    example the meeting attendees are the employees the directory step resolved.
    ``principal`` is the logged-in user; used as the default task assignee when
    no explicit person was named in a create-task command.
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
        if resolved:
            assignee_email = resolved[0]["email"]
        elif principal is not None and hasattr(principal, "email"):
            # create-task with no explicit assignee — default to the logged-in user
            assignee_email = principal.email
        else:
            raise StepExecutionError("The task assignee was not resolved in the directory.")
        return {
            "assignee_email": assignee_email,
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
            document_context = results.get("document.context")
            if document_context is not None:
                context_results = document_context.get("results", [])
                if context_results:
                    context_text = "\n\n".join(
                        f"[{item['source']}]\n{item['snippet']}"
                        for item in context_results
                    )
                    arguments["document_context"] = "\n\n".join(
                        item["snippet"] for item in context_results
                    )[:10000]
                    instructions = (
                        "Use only the following retrieved company-document facts "
                        "for factual claims; do not add unsupported company details:\n"
                        f"{context_text}"
                    )
                else:
                    instructions = (
                        "No relevant indexed company documents were found. Do not "
                        "make factual company-policy or project claims; ask the "
                        "user to provide the needed information."
                    )
                arguments["instructions"] = instructions[:2000]
            return {**arguments, "topic": entities.topic, "mode": assistant_email_draft_mode()}
        preview = results.get("email.preview")
        if not preview:
            raise StepExecutionError("The e-mail has not been drafted yet.")
        if principal is None:
            raise StepExecutionError("The authenticated sender is required to send an e-mail.")
        from app.email_draft_service import finalize_draft_body

        document_context = results.get("document.context") or {}
        context_results = document_context.get("results", [])
        retrieved_context = "\n\n".join(
            item["snippet"] for item in context_results if item.get("snippet")
        )[:10000]
        body = finalize_draft_body(
            preview["body"],
            sender=principal.name,
            retrieved_context=retrieved_context,
            recipient_names=[
                recipient["name"]
                for recipient in preview.get("recipients", [])
                if recipient.get("name")
            ],
        )
        sent = {
            **arguments,
            "subject": preview["subject"],
            "body": body,
            "request": f"E-mail about {entities.topic}",
        }
        # Story 7.4: the documents this e-mail quotes, re-checked when it is sent.
        document_ids = list(dict.fromkeys(
            item["document_id"] for item in context_results if item.get("document_id")
        ))
        if document_ids:
            sent["document_ids"] = document_ids[:DOCUMENT_SEARCH_RETRIEVAL_LIMIT]
        return sent

    if tool == "document.search":
        return {"query": step.retrieval_query or step.description, "limit": 3}

    raise StepExecutionError(f"No argument binding is defined for the tool {tool!r}.")


def _names(people: list[dict]) -> str:
    return ", ".join(person["name"] for person in people) or "nobody"


def _format_assistant_datetime(value: str | datetime) -> str:
    """Format a stored UTC timestamp in the configured organization timezone."""
    from app.meeting.scheduling_service import get_organization_timezone

    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(get_organization_timezone())
    return (
        f"{local.strftime('%A, %B')} {local.day} at "
        f"{local.strftime('%I:%M %p').lstrip('0')}"
    )


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
            slots = ", ".join(
                _format_assistant_datetime(slot["start_at"])
                for slot in result["alternative_slots"]
            )
            raise StepExecutionError(
                f"{busy}. " + (f"Free slots: {slots}." if slots else "No free slot was found.")
            )
        return f"All {len(result['attendee_emails'])} attendee(s) are free."

    if tool_name == "meeting.schedule":
        summary = (
            f"Scheduled '{result['title']}' at "
            f"{_format_assistant_datetime(result['start_at'])} "
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
        delivery_results = result.get("delivery_results", [])
        sent_recipients = {
            item["email"].casefold()
            for item in delivery_results
            if item.get("status") == "sent" and item.get("email")
        }
        recipients = {
            email.casefold() for email in result.get("recipients", []) if email
        }
        if (
            result.get("recipient_count", 0) > 0
            and len(sent_recipients) >= result["recipient_count"]
            and (not recipients or recipients <= sent_recipients)
        ):
            return (
                f"E-mail \"{result['subject']}\" sent successfully to "
                f"{result['recipient_count']} recipient(s) (draft {result['draft_id']})."
            )
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
            f"Task: {result['title']}. Assignee: {assignee}. "
            f"Due: {_format_assistant_datetime(result['due_at'])}. "
            f"Assignment e-mail: {result['assignment_email_status']}."
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
) -> str:
    """Execute one plan step and return a short human-readable summary.

    Story 6.5: a step that names a tool runs it through
    ``app.assistant_tools.execute_tool`` as ``principal``, so the change really
    happens (a task row is created, a meeting is booked, an e-mail is queued) and
    is recorded in the tool-execution ledger.  The tool result is stored in
    ``step.result`` and in ``results`` for later steps.

    Story 6.6: writes pass the write guard.  A denied or conflicting write raises
    StepExecutionError with the reason; a medium/high-risk write raises
    StepApprovalRequired and runs nothing until it is approved.

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
            step.tool_arguments = prepare_tool_arguments(step, understanding, results, principal)
        try:
            outcome = execute_tool(
                db,
                principal,
                step.tool_name,
                step.tool_arguments,
                idempotency_key=step.idempotency_key,
                conversation_id=conversation_id,
            )
        except ToolError as exc:
            raise StepExecutionError(exc.message) from exc
        step.risk_level = outcome.risk_level
        if outcome.status == "approval_required":
            step.approval_id = outcome.approval["id"]
            raise StepApprovalRequired(outcome.approval, outcome.risk_reasons)
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
        assignee = entities.people[0] if entities.people else "yourself"
        due = (
            f" by {entities.date.isoformat()}" if entities.date else ""
        )
        return f"Validated: create task '{task}' assigned to {assignee}{due}."

    if step.action_type == "draft_email":
        recipients = entities.groups or entities.people
        if not recipients:
            raise StepExecutionError("Email recipient is missing from the understanding.")
        topic = entities.topic or "(no topic)"
        return f"Validated: draft email to {recipients[0]} about \"{topic}\"."

    raise StepExecutionError(
        f"Unknown action_type {step.action_type!r}. No handler available."
    )
