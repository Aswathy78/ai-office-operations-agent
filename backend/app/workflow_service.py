"""Persistent queue operations and checkpoint helpers for Epic 8 workflows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.models import (
    Meeting,
    MeetingRecording,
    WorkflowRun,
    WorkflowStep,
)

logger = logging.getLogger(__name__)

MEETING_WORKFLOW = "meeting_follow_up"
TASK_REMINDER_WORKFLOW = "task_reminders"
CALENDAR_MONITOR_WORKFLOW = "calendar_monitor"
MEETING_STEPS = (
    "transcription",
    "summary",
    "action_items",
    "task_creation",
)
WORKFLOW_RETRY_BASE_SECONDS = 5
WORKFLOW_STALE_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def concurrency_limit() -> int:
    try:
        return min(8, max(1, int(os.getenv("WORKFLOW_CONCURRENCY", "2"))))
    except ValueError:
        logger.warning("Invalid WORKFLOW_CONCURRENCY; using 2 workers.")
        return 2


def enqueue_workflow(
    db: Session,
    *,
    workflow_type: str,
    idempotency_key: str,
    meeting_id: str | None = None,
    priority: int = 0,
    payload: dict | None = None,
    steps: tuple[str, ...],
    max_attempts: int = 5,
) -> WorkflowRun:
    existing = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if existing is not None:
        return existing

    now = _now()
    run = WorkflowRun(
        id=str(uuid.uuid4()),
        workflow_type=workflow_type,
        meeting_id=meeting_id,
        idempotency_key=idempotency_key,
        priority=priority,
        status="pending",
        payload=payload or {},
        attempt_count=0,
        max_attempts=max_attempts,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
            db.add_all(
                WorkflowStep(
                    id=str(uuid.uuid4()),
                    workflow_run_id=run.id,
                    step_key=step_key,
                    step_order=order,
                    status="pending",
                    attempt_count=0,
                    updated_at=now,
                )
                for order, step_key in enumerate(steps)
            )
            db.flush()
    except IntegrityError:
        existing = (
            db.query(WorkflowRun)
            .filter(WorkflowRun.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is None:
            raise
        return existing
    db.commit()
    db.refresh(run)
    return run


def enqueue_meeting_workflow(db: Session, meeting_id: str) -> WorkflowRun | None:
    """Queue once a primary recording is complete; meeting end may arrive first."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        return None
    recording_exists = (
        db.query(MeetingRecording.id)
        .filter(
            MeetingRecording.meeting_id == meeting_id,
            MeetingRecording.status == "completed",
            MeetingRecording.participant_id.is_(None),
            MeetingRecording.connection_id.is_(None),
        )
        .first()
        is not None
    )
    if not recording_exists:
        return None
    return enqueue_workflow(
        db,
        workflow_type=MEETING_WORKFLOW,
        meeting_id=meeting_id,
        idempotency_key=f"meeting-follow-up:{meeting_id}",
        priority=100,
        steps=MEETING_STEPS,
    )


def enqueue_task_reminders(db: Session, *, interval_seconds: int) -> WorkflowRun:
    interval = max(1, interval_seconds)
    bucket = int(_now().timestamp()) // interval
    return enqueue_workflow(
        db,
        workflow_type=TASK_REMINDER_WORKFLOW,
        idempotency_key=f"task-reminders:{bucket}",
        priority=20,
        steps=("task_reminders",),
        max_attempts=3,
    )


def enqueue_calendar_monitor(
    db: Session,
    *,
    interval_seconds: int,
    poll: bool,
) -> WorkflowRun:
    interval = max(1, interval_seconds)
    bucket = int(_now().timestamp()) // interval
    return enqueue_workflow(
        db,
        workflow_type=CALENDAR_MONITOR_WORKFLOW,
        idempotency_key=f"calendar-monitor:{bucket}",
        priority=10,
        payload={"poll": poll},
        steps=("calendar_monitor",),
        max_attempts=3,
    )


def claim_workflow(db: Session) -> WorkflowRun | None:
    now = _now()
    claimed_id = db.execute(
        text(
            """
            UPDATE workflow_runs
            SET status = 'processing', claimed_at = :now, updated_at = :now
            WHERE id = (
                SELECT candidate.id
                FROM workflow_runs AS candidate
                WHERE candidate.status IN ('pending', 'retrying')
                  AND (candidate.next_attempt_at IS NULL OR candidate.next_attempt_at <= :now)
                  AND (
                    SELECT COUNT(*) FROM workflow_runs AS active
                    WHERE active.status = 'processing'
                  ) < :concurrency
                ORDER BY candidate.priority DESC, candidate.created_at, candidate.id
                LIMIT 1
            )
              AND status IN ('pending', 'retrying')
            RETURNING id
            """
        ),
        {"now": now, "concurrency": concurrency_limit()},
    )
    run_id = claimed_id.scalar_one_or_none()
    db.commit()
    return db.get(WorkflowRun, run_id) if run_id is not None else None


def recover_stale_workflows(db: Session, stale_minutes: int = WORKFLOW_STALE_MINUTES) -> int:
    cutoff = _now() - timedelta(minutes=max(1, stale_minutes))
    stale = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.status == "processing", WorkflowRun.claimed_at < cutoff)
        .all()
    )
    now = _now()
    for run in stale:
        run.attempt_count += 1
        run.status = "retrying" if run.attempt_count < run.max_attempts else "failed"
        run.next_attempt_at = now if run.status == "retrying" else None
        run.last_error = "Workflow worker lease expired; the job was recovered."
        run.claimed_at = None
        run.updated_at = now
        if run.status == "failed":
            run.completed_at = now
        for step in db.query(WorkflowStep).filter(
            WorkflowStep.workflow_run_id == run.id,
            WorkflowStep.status == "processing",
        ):
            step.status = "retrying" if run.status == "retrying" else "failed"
            step.last_error = run.last_error
            step.updated_at = now
    db.commit()
    for run in stale:
        if run.status == "failed" and run.workflow_type == MEETING_WORKFLOW:
            from app.workflow_worker import _notify_meeting_result

            _notify_meeting_result(db, run, failed=True)
    if stale:
        logger.warning("Recovered %d stale workflow run(s)", len(stale))
    return len(stale)


def get_workflow_steps(db: Session, run_id: str) -> list[WorkflowStep]:
    return (
        db.query(WorkflowStep)
        .filter(WorkflowStep.workflow_run_id == run_id)
        .order_by(WorkflowStep.step_order)
        .all()
    )


def workflow_response(db: Session, run: WorkflowRun) -> dict:
    steps = get_workflow_steps(db, run.id)
    return {
        "id": run.id,
        "workflow_type": run.workflow_type,
        "meeting_id": run.meeting_id,
        "priority": run.priority,
        "status": run.status,
        "attempt_count": run.attempt_count,
        "last_error": run.last_error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "completed_at": run.completed_at,
        "steps": [
            {
                "id": step.id,
                "step_key": step.step_key,
                "status": step.status,
                "attempt_count": step.attempt_count,
                "checkpoint": step.checkpoint,
                "last_error": step.last_error,
                "updated_at": step.updated_at,
                "completed_at": step.completed_at,
            }
            for step in steps
        ],
    }
