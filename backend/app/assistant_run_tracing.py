"""Durable, privacy-filtered traces for interactive assistant runs."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import uuid

from sqlalchemy.orm import Session

from app.meeting.models import (
    AssistantActionApproval,
    AssistantRun,
    AssistantRunStep,
    AssistantToolExecution,
)

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "email",
    "phone",
    "mobile",
    "address",
    "recipient",
    "attendee",
    "assignee",
    "person",
    "employee",
    "name",
    "title",
    "subject",
    "body",
    "content",
    "prompt",
    "query",
    "snippet",
    "transcript",
    "document",
    "message",
    "description",
    "comment",
    "note",
    "text",
)
_SAFE_STRING_KEYS = frozenset({
    "status",
    "outcome",
    "risk_level",
    "tool_name",
    "action_type",
    "response_mode",
})
_SAFE_NUMBER_KEYS = frozenset({"count", "attempts", "attempt_count", "retry_count", "duration_ms"})
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)")
_MAX_TRACE_DEPTH = 6
_MAX_TRACE_ITEMS = 50
_MAX_SAFE_STRING = 80


def redact_trace_data(value, *, _key: str = "", _depth: int = 0):
    """Keep only compact operational metadata; do not retain free-form user/document text."""
    normalized_key = re.sub(r"[^a-z0-9_]", "", _key.casefold())
    if normalized_key not in _SAFE_STRING_KEYS and any(
        fragment in normalized_key for fragment in _SENSITIVE_KEYS
    ):
        return _REDACTED
    if _depth >= _MAX_TRACE_DEPTH:
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(key)[:80]: redact_trace_data(item, _key=str(key), _depth=_depth + 1)
            for key, item in list(value.items())[:_MAX_TRACE_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_trace_data(item, _key=_key, _depth=_depth + 1)
            for item in list(value)[:_MAX_TRACE_ITEMS]
        ]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if normalized_key in _SAFE_NUMBER_KEYS else _REDACTED
    if isinstance(value, str) and normalized_key in _SAFE_STRING_KEYS:
        safe = value[:_MAX_SAFE_STRING]
        safe = _EMAIL.sub(_REDACTED, safe)
        safe = _BEARER.sub(_REDACTED, safe)
        return _LONG_DIGITS.sub(_REDACTED, safe)
    return _REDACTED


def create_assistant_run(
    db: Session,
    *,
    user_id: str,
    conversation_id: str,
    run_id: str | None = None,
) -> AssistantRun:
    now = datetime.now(timezone.utc)
    run = AssistantRun(
        id=run_id or str(uuid.uuid4()),
        user_id=user_id,
        conversation_id=conversation_id,
        status="running",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def resume_assistant_run(db: Session, run_id: str) -> AssistantRun | None:
    run = db.get(AssistantRun, run_id)
    if run is None:
        return None
    run.status = "running"
    run.ended_at = None
    run.updated_at = datetime.now(timezone.utc)
    db.commit()
    return run


def record_assistant_run_step(
    db: Session,
    *,
    run_id: str,
    step_order: int,
    step_type: str,
    step_name: str,
    status: str,
    input_data: dict | None = None,
    output_data: dict | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    duration_ms: int | None = None,
    estimated_tokens: int | None = None,
    tool_execution_id: str | None = None,
    approval_id: str | None = None,
) -> AssistantRunStep:
    step = (
        db.query(AssistantRunStep)
        .filter(
            AssistantRunStep.run_id == run_id,
            AssistantRunStep.step_order == step_order,
        )
        .one_or_none()
    )
    now = datetime.now(timezone.utc)
    if step is None:
        step = AssistantRunStep(
            id=str(uuid.uuid4()),
            run_id=run_id,
            step_order=step_order,
            step_type=step_type[:60],
            step_name=step_name[:100],
            status=status,
            started_at=started_at,
            created_at=now,
        )
        db.add(step)
    step.step_type = step_type[:60]
    step.step_name = step_name[:100]
    step.status = status
    if input_data is not None:
        step.input_data = redact_trace_data(input_data)
    if output_data is not None:
        step.output_data = redact_trace_data(output_data)
    if started_at is not None and step.started_at is None:
        step.started_at = started_at
    if ended_at is not None:
        step.ended_at = ended_at
    if duration_ms is not None:
        step.duration_ms = max(0, duration_ms)
    if estimated_tokens is not None:
        step.estimated_tokens = max(0, estimated_tokens)
    if tool_execution_id is not None:
        step.tool_execution_id = tool_execution_id
        execution = db.get(AssistantToolExecution, tool_execution_id)
        if execution is not None:
            step.attempt_count = execution.attempts or 0
            step.retry_count = max(0, step.attempt_count - 1)
    elif status in ("completed", "failed", "escalated"):
        step.attempt_count = max(step.attempt_count or 0, 1)
        step.retry_count = max(0, step.attempt_count - 1)
    if approval_id is not None:
        step.approval_id = approval_id
    step.updated_at = now
    run = db.get(AssistantRun, run_id)
    if run is not None:
        run.updated_at = now
    db.commit()
    db.refresh(step)
    return step


def ensure_assistant_run_step(
    db: Session,
    *,
    run_id: str,
    step_order: int,
    step_type: str,
    step_name: str,
) -> AssistantRunStep:
    step = (
        db.query(AssistantRunStep)
        .filter(
            AssistantRunStep.run_id == run_id,
            AssistantRunStep.step_order == step_order,
        )
        .one_or_none()
    )
    if step is not None:
        return step
    return record_assistant_run_step(
        db,
        run_id=run_id,
        step_order=step_order,
        step_type=step_type,
        step_name=step_name,
        status="pending",
    )


def set_assistant_run_status(db: Session, run_id: str, status: str) -> AssistantRun | None:
    run = db.get(AssistantRun, run_id)
    if run is None:
        return None
    now = datetime.now(timezone.utc)
    run.status = status
    run.ended_at = now if status in ("completed", "failed", "cancelled") else None
    run.updated_at = now
    db.commit()
    return run


def resolve_run_for_approval(
    db: Session,
    approval: AssistantActionApproval,
    *,
    status: str,
    execution_id: str | None = None,
    output_data: dict | None = None,
    finalize_run: bool = True,
) -> None:
    step = (
        db.query(AssistantRunStep)
        .filter(AssistantRunStep.approval_id == approval.id)
        .one_or_none()
    )
    if step is None:
        return
    now = datetime.now(timezone.utc)
    step.status = status
    step.ended_at = now
    if step.started_at is not None:
        started_at = step.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        step.duration_ms = max(0, int((now - started_at).total_seconds() * 1000))
    if execution_id:
        step.tool_execution_id = execution_id
        execution = db.get(AssistantToolExecution, execution_id)
        if execution is not None:
            step.attempt_count = execution.attempts or 0
            step.retry_count = max(0, step.attempt_count - 1)
    if output_data is not None:
        step.output_data = redact_trace_data(output_data)
    step.updated_at = now
    db.commit()

    # A requester confirmation can continue through the same chat runner; approval
    # callbacks otherwise resolve the paused trace.
    if finalize_run:
        terminal_status = {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(status)
        if terminal_status:
            set_assistant_run_status(db, step.run_id, terminal_status)
