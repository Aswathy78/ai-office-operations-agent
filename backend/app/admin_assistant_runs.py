"""Read-only, privacy-filtered administrator views of assistant runs."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.assistant_run_tracing import redact_trace_data
from app.meeting.models import (
    ACTION_APPROVAL_STATUS_PENDING,
    AssistantActionApproval,
    AssistantRun,
    AssistantRunStep,
    Employee,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _duration_ms(run: AssistantRun) -> int | None:
    if run.started_at is None:
        return None
    ended_at = run.ended_at or datetime.now(timezone.utc)
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)
    started_at = run.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0, int((ended_at - started_at).total_seconds() * 1000))


def _escaped_like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def assistant_run_summary(run: AssistantRun, user_name: str | None = None) -> dict:
    return {
        "id": run.id,
        "user_id": run.user_id,
        "user_name": user_name,
        "conversation_id": run.conversation_id,
        "status": run.status,
        "started_at": _iso(run.started_at),
        "ended_at": _iso(run.ended_at),
        "duration_ms": _duration_ms(run),
        "retry_count": 0,
        "estimated_tokens": None,
        "provider_usage": {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        },
    }


def list_assistant_runs(
    db: Session,
    *,
    status: str | None = None,
    user: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    query = db.query(AssistantRun).outerjoin(Employee, Employee.id == AssistantRun.user_id)
    if status:
        query = query.filter(AssistantRun.status == status)
    if user:
        pattern = _escaped_like(user.strip())
        query = query.filter(
            or_(
                AssistantRun.user_id.ilike(pattern, escape="\\"),
                Employee.name.ilike(pattern, escape="\\"),
                Employee.email.ilike(pattern, escape="\\"),
            )
        )
    total = query.count()
    rows = (
        query.add_columns(Employee.name.label("user_name"))
        .order_by(AssistantRun.started_at.desc(), AssistantRun.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    run_ids = [run.id for run, _ in rows]
    step_totals = {}
    if run_ids:
        step_totals = {
            row.run_id: {
                "retry_count": row.retry_count or 0,
                "estimated_tokens": row.estimated_tokens,
                "provider_usage": {
                    "prompt_tokens": row.prompt_tokens,
                    "completion_tokens": row.completion_tokens,
                    "total_tokens": row.total_tokens,
                },
            }
            for row in (
                db.query(
                    AssistantRunStep.run_id.label("run_id"),
                    func.sum(AssistantRunStep.retry_count).label("retry_count"),
                    func.sum(AssistantRunStep.estimated_tokens).label("estimated_tokens"),
                    func.sum(AssistantRunStep.provider_prompt_tokens).label("prompt_tokens"),
                    func.sum(AssistantRunStep.provider_completion_tokens).label("completion_tokens"),
                    func.sum(AssistantRunStep.provider_total_tokens).label("total_tokens"),
                )
                .filter(AssistantRunStep.run_id.in_(run_ids))
                .group_by(AssistantRunStep.run_id)
                .all()
            )
        }
    summaries = []
    for run, user_name in rows:
        summary = assistant_run_summary(run, user_name)
        summary.update(step_totals.get(run.id, {}))
        summaries.append(summary)
    return {
        "runs": summaries,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(summaries) < total,
    }


def assistant_run_detail(db: Session, run_id: str) -> dict | None:
    run = db.get(AssistantRun, run_id)
    if run is None:
        return None
    steps = (
        db.query(AssistantRunStep)
        .filter(AssistantRunStep.run_id == run.id)
        .order_by(AssistantRunStep.step_order.asc(), AssistantRunStep.id.asc())
        .all()
    )
    approval_ids = [step.approval_id for step in steps if step.approval_id]
    pending_approvals = (
        db.query(AssistantActionApproval)
        .filter(
            AssistantActionApproval.id.in_(approval_ids),
            AssistantActionApproval.status == ACTION_APPROVAL_STATUS_PENDING,
        )
        .order_by(AssistantActionApproval.created_at.asc(), AssistantActionApproval.id.asc())
        .all()
        if approval_ids
        else []
    )
    step_data = [
        {
            "id": step.id,
            "step_order": step.step_order,
            "step_type": step.step_type,
            "step_name": step.step_name,
            "status": step.status,
            "input": redact_trace_data(step.input_data) if step.input_data is not None else None,
            "output": redact_trace_data(step.output_data) if step.output_data is not None else None,
            "started_at": _iso(step.started_at),
            "ended_at": _iso(step.ended_at),
            "duration_ms": step.duration_ms,
            "attempt_count": step.attempt_count,
            "retry_count": step.retry_count,
            "estimated_tokens": step.estimated_tokens,
            "provider_usage": {
                "prompt_tokens": step.provider_prompt_tokens,
                "completion_tokens": step.provider_completion_tokens,
                "total_tokens": step.provider_total_tokens,
            },
            "tool_execution_id": step.tool_execution_id,
            "approval_id": step.approval_id,
        }
        for step in steps
    ]
    summary = assistant_run_summary(
        run,
        db.query(Employee.name).filter(Employee.id == run.user_id).scalar()
        if run.user_id
        else None,
    )
    summary["retry_count"] = sum(step["retry_count"] for step in step_data)
    estimates = [step["estimated_tokens"] for step in step_data if step["estimated_tokens"] is not None]
    summary["estimated_tokens"] = sum(estimates) if estimates else None
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [
            step["provider_usage"][field]
            for step in step_data
            if step["provider_usage"][field] is not None
        ]
        summary["provider_usage"][field] = sum(values) if values else None
    return {
        **summary,
        "steps": step_data,
        "pending_approvals": [
            {
                "id": approval.id,
                "tool_name": approval.tool_name,
                "status": approval.status,
                "risk_level": approval.risk_level,
                "approver_kind": approval.approver_kind,
                "created_at": _iso(approval.created_at),
                "expires_at": _iso(approval.expires_at),
            }
            for approval in pending_approvals
        ],
    }
