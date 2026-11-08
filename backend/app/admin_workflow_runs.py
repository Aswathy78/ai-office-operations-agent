"""Privacy-limited administrator views of background workflow runs."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.meeting.models import Employee, Meeting, WorkflowRun, WorkflowStep


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _elapsed_ms(started_at: datetime | None, ended_at: datetime | None) -> int | None:
    if started_at is None:
        return None
    start = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    end = ended_at or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds() * 1000))


def _user_filter(query, user: str):
    pattern = "%" + user.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return query.filter(
        or_(
            Meeting.organizer_id.ilike(pattern, escape="\\"),
            Employee.name.ilike(pattern, escape="\\"),
            Employee.email.ilike(pattern, escape="\\"),
        )
    )


def _run_attempt_count(run: WorkflowRun) -> int:
    if run.status in ("processing", "completed"):
        return run.attempt_count + 1
    return run.attempt_count


def _run_retry_count(run: WorkflowRun) -> int:
    if run.status in ("failed", "cancelled"):
        return max(0, run.attempt_count - 1)
    return run.attempt_count


def _summary(
    run: WorkflowRun,
    user_id: str | None,
    user_name: str | None,
    first_step_started_at: datetime | None = None,
) -> dict:
    started_at = run.claimed_at or first_step_started_at
    return {
        "id": run.id,
        "run_type": "background_workflow",
        "workflow_type": run.workflow_type,
        "meeting_id": run.meeting_id,
        "user_id": user_id,
        "user_name": user_name,
        "status": run.status,
        "queued_at": _iso(run.created_at),
        "started_at": _iso(started_at),
        "ended_at": _iso(run.completed_at),
        "duration_ms": _elapsed_ms(started_at, run.completed_at),
        "attempt_count": _run_attempt_count(run),
        "retry_count": _run_retry_count(run),
        "max_attempts": run.max_attempts,
        "estimated_tokens": None,
        "provider_usage": None,
        "cost": None,
    }


def list_workflow_runs(
    db: Session,
    *,
    status: str | None = None,
    user: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    query = (
        db.query(WorkflowRun, Meeting.organizer_id, Employee.name)
        .outerjoin(Meeting, Meeting.id == WorkflowRun.meeting_id)
        .outerjoin(Employee, Employee.id == Meeting.organizer_id)
    )
    if status:
        query = query.filter(WorkflowRun.status == status)
    if user:
        query = _user_filter(query, user.strip())
    total = query.count()
    rows = (
        query.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    run_ids = [run.id for run, _, _ in rows]
    first_step_starts = {}
    if run_ids:
        first_step_starts = dict(
            db.query(
                WorkflowStep.workflow_run_id,
                func.min(WorkflowStep.started_at),
            )
            .filter(
                WorkflowStep.workflow_run_id.in_(run_ids),
                WorkflowStep.started_at.is_not(None),
            )
            .group_by(WorkflowStep.workflow_run_id)
            .all()
        )
    return {
        "runs": [
            _summary(run, user_id, user_name, first_step_starts.get(run.id))
            for run, user_id, user_name in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


def workflow_run_detail(db: Session, run_id: str) -> dict | None:
    row = (
        db.query(WorkflowRun, Meeting.organizer_id, Employee.name)
        .outerjoin(Meeting, Meeting.id == WorkflowRun.meeting_id)
        .outerjoin(Employee, Employee.id == Meeting.organizer_id)
        .filter(WorkflowRun.id == run_id)
        .one_or_none()
    )
    if row is None:
        return None
    run, user_id, user_name = row
    steps = (
        db.query(WorkflowStep)
        .filter(WorkflowStep.workflow_run_id == run.id)
        .order_by(WorkflowStep.step_order.asc(), WorkflowStep.id.asc())
        .all()
    )
    first_step_started_at = min(
        (step.started_at for step in steps if step.started_at is not None),
        default=None,
    )
    return {
        **_summary(run, user_id, user_name, first_step_started_at),
        "steps": [
            {
                "id": step.id,
                "step_order": step.step_order,
                "step_name": step.step_key,
                "status": step.status,
                "started_at": _iso(step.started_at),
                "ended_at": _iso(step.completed_at),
                "duration_ms": _elapsed_ms(step.started_at, step.completed_at),
                "attempt_count": step.attempt_count,
                "retry_count": max(0, step.attempt_count - 1),
            }
            for step in steps
        ],
        "pending_approvals": [],
    }
