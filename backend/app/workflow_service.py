"""Persistent queue operations and checkpoint helpers for Epic 8 workflows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import random
import uuid

from sqlalchemy import func, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.models import (
    Meeting,
    MeetingRecording,
    WorkflowDeadLetter,
    WorkflowEvent,
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

# Story 8.4: a transient failure is retried with exponential backoff and random
# jitter, at most WORKFLOW_MAX_ATTEMPTS attempts in total; then the run is moved to
# the dead-letter queue as Failed and an administrator is told.
WORKFLOW_MAX_ATTEMPTS = 5
WORKFLOW_BACKOFF_CAP_SECONDS = 300
# The wait is the exponential delay scaled into [1 - ratio, 1], so retries from many
# runs do not all fire together while the delay still doubles each time.
WORKFLOW_JITTER_RATIO = 0.5

MAX_WORKFLOW_STEPS = 12
DEFAULT_WORKFLOW_COST_CAP_USD = 5.0
DEFAULT_STEP_COST_USD = 0.25


def workflow_cost_cap_usd() -> float:
    raw = os.getenv("WORKFLOW_COST_CAP_USD", "").strip()
    if not raw:
        return DEFAULT_WORKFLOW_COST_CAP_USD
    try:
        val = float(raw)
        return val if val > 0 else DEFAULT_WORKFLOW_COST_CAP_USD
    except ValueError:
        return DEFAULT_WORKFLOW_COST_CAP_USD


WORKFLOW_LABELS = {
    MEETING_WORKFLOW: "Meeting follow-up workflow",
    TASK_REMINDER_WORKFLOW: "Task reminder workflow",
    CALENDAR_MONITOR_WORKFLOW: "Calendar monitor workflow",
}


class PermanentWorkflowError(RuntimeError):
    """A failure retrying cannot fix (for example, the meeting no longer exists).

    The run goes straight to the dead-letter queue instead of burning its attempts.
    """


class DeadLetterError(Exception):
    """An administrator action on the dead-letter queue could not be applied."""


class DeadLetterNotFound(DeadLetterError):
    pass


class DeadLetterConflict(DeadLetterError):
    pass


def _jitter_factor() -> float:
    return random.uniform(1.0 - WORKFLOW_JITTER_RATIO, 1.0)


def backoff_delay_seconds(failed_attempts: int, base_seconds: float) -> float:
    """Seconds to wait after the ``failed_attempts``-th failure: base * 2^(n-1),
    capped, with random jitter."""
    exponential = min(
        WORKFLOW_BACKOFF_CAP_SECONDS,
        base_seconds * 2 ** max(0, failed_attempts - 1),
    )
    return exponential * _jitter_factor()


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
    max_attempts: int = WORKFLOW_MAX_ATTEMPTS,
) -> WorkflowRun:
    max_attempts = max(1, min(max_attempts, WORKFLOW_MAX_ATTEMPTS))
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
    cutoff = _now() - timedelta(minutes=max(0, stale_minutes))
    stale_query = db.query(WorkflowRun).filter(WorkflowRun.status == "processing")
    if stale_minutes > 0:
        stale_query = stale_query.filter(WorkflowRun.claimed_at < cutoff)
    stale = stale_query.all()
    now = _now()
    failed_steps: dict[str, str | None] = {}
    for run in stale:
        run.attempt_count += 1
        exhausted = run.attempt_count >= min(run.max_attempts, WORKFLOW_MAX_ATTEMPTS)
        run.status = "failed" if exhausted else "retrying"
        run.next_attempt_at = None if exhausted else now
        run.last_error = "Workflow worker lease expired; the job was recovered."
        run.claimed_at = None
        run.updated_at = now
        if run.status == "failed":
            run.completed_at = now
        failed_steps[run.id] = None
        for step in db.query(WorkflowStep).filter(
            WorkflowStep.workflow_run_id == run.id,
            WorkflowStep.status == "processing",
        ):
            step.status = "retrying" if run.status == "retrying" else "failed"
            step.last_error = run.last_error
            step.updated_at = now
            failed_steps[run.id] = step.step_key
        record_event(
            db,
            run.id,
            "lease_expired",
            step_key=failed_steps[run.id],
            attempt_number=run.attempt_count,
            error=run.last_error,
            message="The worker stopped responding; the run was recovered.",
            next_attempt_at=run.next_attempt_at,
        )
    db.commit()
    for run in stale:
        if run.status == "failed":
            dead_letter_run(
                db,
                run.id,
                step_key=failed_steps.get(run.id),
                error_message=run.last_error or "Workflow worker lease expired.",
            )
            if run.workflow_type == MEETING_WORKFLOW:
                from app.workflow_worker import _notify_meeting_result

                _notify_meeting_result(db, run, failed=True)
    if stale:
        logger.warning("Recovered %d stale workflow run(s)", len(stale))
    return len(stale)


def recover_crashed_workflows(db: Session, max_lease_age_seconds: int = 0) -> int:
    """Immediately recover runs that crashed while in processing state."""
    return recover_stale_workflows(db, stale_minutes=max(0, max_lease_age_seconds // 60))


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
        "max_attempts": run.max_attempts,
        "next_attempt_at": run.next_attempt_at,
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


# ---------------------------------------------------------------------------
# Execution history
# ---------------------------------------------------------------------------


def record_event(
    db: Session,
    run_id: str,
    event_type: str,
    *,
    step_key: str | None = None,
    attempt_number: int | None = None,
    message: str | None = None,
    error: str | None = None,
    retry_delay_seconds: float | None = None,
    next_attempt_at: datetime | None = None,
    actor_id: str | None = None,
) -> WorkflowEvent:
    """Append to a run's history. The caller commits with its own change."""
    db.flush()  # count events added earlier in this transaction too
    # Deduplicate lifecycle events for same run, step, attempt, and event_type
    if event_type in ("step_completed", "run_completed", "delivery_confirmed", "notification_sent", "retry_scheduled"):
        existing_event = (
            db.query(WorkflowEvent)
            .filter(
                WorkflowEvent.workflow_run_id == run_id,
                WorkflowEvent.step_key == step_key,
                WorkflowEvent.attempt_number == attempt_number,
                WorkflowEvent.event_type == event_type,
            )
            .first()
        )
        if existing_event is not None:
            return existing_event
    sequence = (
        db.query(func.coalesce(func.max(WorkflowEvent.sequence), 0))
        .filter(WorkflowEvent.workflow_run_id == run_id)
        .scalar()
        + 1
    )
    event = WorkflowEvent(
        id=str(uuid.uuid4()),
        workflow_run_id=run_id,
        sequence=sequence,
        step_key=step_key,
        attempt_number=attempt_number,
        event_type=event_type,
        message=message,
        error=error,
        retry_delay_seconds=retry_delay_seconds,
        next_attempt_at=next_attempt_at,
        actor_id=actor_id,
        created_at=_now(),
    )
    db.add(event)
    return event


def workflow_events(db: Session, run_id: str) -> list[WorkflowEvent]:
    return (
        db.query(WorkflowEvent)
        .filter(WorkflowEvent.workflow_run_id == run_id)
        .order_by(WorkflowEvent.sequence, WorkflowEvent.created_at)
        .all()
    )


# ---------------------------------------------------------------------------
# Dead-letter queue (Story 8.4)
# ---------------------------------------------------------------------------


def _open_dead_letter(db: Session, run_id: str) -> WorkflowDeadLetter | None:
    return (
        db.query(WorkflowDeadLetter)
        .filter(
            WorkflowDeadLetter.workflow_run_id == run_id,
            WorkflowDeadLetter.status == "open",
        )
        .one_or_none()
    )


def _notify_administrators(db: Session, entry: WorkflowDeadLetter, run: WorkflowRun) -> int:
    """Tell a human that a run was given up on. Never raises: a notification problem
    must not hide the failure, which is already recorded in the queue."""
    try:
        from app.notification_service import (
            notify_workflow_failure,
            workflow_alert_recipients,
        )

        recipients = workflow_alert_recipients(db)
    except Exception:  # noqa: BLE001
        logger.exception("Could not look up administrators for dead-letter %s", entry.id)
        return 0
    if not recipients:
        logger.error(
            "Workflow run %s is in the dead-letter queue but no administrator can be notified.",
            run.id,
        )
        return 0
    label = WORKFLOW_LABELS.get(run.workflow_type, run.workflow_type)
    reason = (entry.error_message or "unknown error").strip()
    summary = (
        f"{label} failed after {entry.attempt_count} attempt(s) and was moved to the "
        f"dead-letter queue: {reason[:200]}"
    )
    notified = 0
    for recipient in recipients:
        try:
            notify_workflow_failure(
                db,
                recipient=recipient,
                summary=summary,
                idempotency_key=f"workflow-dlq:{entry.id}:{recipient.id}",
                entity_type="workflow_run",
                entity_id=run.id,
                entity_url="/admin/workflows",
            )
            notified += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "Could not notify %s about dead-letter %s", recipient.id, entry.id
            )
    return notified


def dead_letter_run(
    db: Session,
    run_id: str,
    *,
    step_key: str | None,
    error_message: str,
    permanent: bool = False,
) -> WorkflowDeadLetter | None:
    """Move a run that cannot continue into the dead-letter queue.

    The run must already be ``failed``. Safe to call twice for the same failure: the
    open entry is reused and administrators are only told once.
    """
    run = db.get(WorkflowRun, run_id)
    if run is None or run.status != "failed":
        return None
    existing = _open_dead_letter(db, run_id)
    if existing is not None:
        return existing
    entry = WorkflowDeadLetter(
        id=str(uuid.uuid4()),
        workflow_run_id=run.id,
        workflow_type=run.workflow_type,
        meeting_id=run.meeting_id,
        failed_step_key=step_key,
        error_message=error_message,
        attempt_count=run.attempt_count,
        max_attempts=run.max_attempts,
        payload={
            k: v for k, v in (run.payload or {}).items()
            if k not in ("steps_executed", "cost_usd")
        },
        status="open",
        notified_count=0,
        created_at=_now(),
    )
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
    except IntegrityError:
        return _open_dead_letter(db, run_id)
    record_event(
        db,
        run.id,
        "dead_lettered",
        step_key=step_key,
        attempt_number=run.attempt_count,
        error=error_message,
        message=(
            "Permanent failure: retrying cannot fix it."
            if permanent
            else f"Retries exhausted after {run.attempt_count} attempt(s)."
        ),
    )
    db.commit()
    entry.notified_count = _notify_administrators(db, entry, run)
    db.commit()
    return entry


_RESOLUTIONS = {"retry": "retried", "resume": "resumed", "cancel": "cancelled"}


def _reset_step(step: WorkflowStep, now: datetime, *, keep_checkpoint: bool = False) -> None:
    step.status = "pending"
    step.attempt_count = 0
    step.last_error = None
    step.started_at = None
    step.completed_at = None
    step.updated_at = now
    if not keep_checkpoint:
        step.checkpoint = None


def resolve_dead_letter(
    db: Session,
    run_id: str,
    *,
    action: str,
    actor_id: str,
    note: str | None = None,
) -> WorkflowRun:
    """Administrator action on a failed run in the dead-letter queue.

    * ``retry``  - run it again from the first step, with a fresh attempt budget.
    * ``resume`` - continue from the last checkpoint: completed steps stay done and
      only the failed and later steps run again.
    * ``cancel`` - give up for good; the run ends as ``cancelled``.

    The same run is reused (no second job is created) and the switch from ``failed``
    is one conditional update, so two clicks cannot start it twice.
    """
    if action not in _RESOLUTIONS:
        raise ValueError(f"Unknown dead-letter action: {action}")
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise DeadLetterNotFound("Workflow run not found.")
    now = _now()
    if action == "cancel":
        values = {
            "status": "cancelled",
            "completed_at": now,
            "next_attempt_at": None,
            "claimed_at": None,
            "updated_at": now,
        }
    else:
        values = {
            "status": "pending",
            "attempt_count": 0,
            "completed_at": None,
            "next_attempt_at": now,
            "claimed_at": None,
            "updated_at": now,
        }
    switched = db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.status == "failed")
        .values(**values)
    )
    if switched.rowcount != 1:
        db.rollback()
        raise DeadLetterConflict(
            "Only a failed workflow run can be retried, resumed or cancelled; "
            "this one has already been handled or is running."
        )

    steps = get_workflow_steps(db, run_id)
    failed_step = next((step.step_key for step in steps if step.status == "failed"), None)
    if action == "retry":
        for step in steps:
            _reset_step(step, now)
    elif action == "resume":
        for step in steps:
            if step.status != "completed":
                _reset_step(step, now, keep_checkpoint=True)

    entry = (
        db.query(WorkflowDeadLetter)
        .filter(WorkflowDeadLetter.workflow_run_id == run_id)
        .order_by(WorkflowDeadLetter.created_at.desc())
        .first()
    )
    if entry is None or entry.status != "open":
        # A failed run that predates the queue: record it so the history is complete.
        db.expire_all()
        run = db.get(WorkflowRun, run_id)
        entry = WorkflowDeadLetter(
            id=str(uuid.uuid4()),
            workflow_run_id=run_id,
            workflow_type=run.workflow_type,
            meeting_id=run.meeting_id,
            failed_step_key=failed_step,
            error_message=run.last_error,
            attempt_count=run.max_attempts,
            max_attempts=run.max_attempts,
            payload=dict(run.payload or {}),
            status="open",
            notified_count=0,
            created_at=now,
        )
        db.add(entry)
    entry.status = _RESOLUTIONS[action]
    entry.resolved_at = now
    entry.resolved_by = actor_id
    entry.resolution_note = (note or "").strip() or None
    record_event(
        db,
        run_id,
        f"manual_{action}",
        step_key=failed_step,
        message=(note or "").strip() or None,
        actor_id=actor_id,
    )
    db.commit()
    db.expire_all()
    return db.get(WorkflowRun, run_id)


def dead_letter_response(
    entry: WorkflowDeadLetter,
    run: WorkflowRun | None,
    meeting_title: str | None = None,
) -> dict:
    return {
        "id": entry.id,
        "run_id": entry.workflow_run_id,
        "workflow_type": entry.workflow_type,
        "workflow_label": WORKFLOW_LABELS.get(entry.workflow_type, entry.workflow_type),
        "meeting_id": entry.meeting_id,
        "meeting_title": meeting_title,
        "failed_step_key": entry.failed_step_key,
        "error_message": entry.error_message,
        "attempt_count": entry.attempt_count,
        "max_attempts": entry.max_attempts,
        "status": entry.status,
        "run_status": run.status if run is not None else None,
        "notified_count": entry.notified_count,
        "created_at": entry.created_at,
        "resolved_at": entry.resolved_at,
        "resolved_by": entry.resolved_by,
        "resolution_note": entry.resolution_note,
    }


def list_dead_letters(
    db: Session,
    *,
    status: str | None = "open",
    limit: int = 100,
) -> list[dict]:
    query = db.query(WorkflowDeadLetter)
    if status:
        query = query.filter(WorkflowDeadLetter.status == status)
    entries = (
        query.order_by(WorkflowDeadLetter.created_at.desc(), WorkflowDeadLetter.id)
        .limit(max(1, min(limit, 500)))
        .all()
    )
    rows = []
    for entry in entries:
        run = db.get(WorkflowRun, entry.workflow_run_id)
        meeting = db.get(Meeting, entry.meeting_id) if entry.meeting_id else None
        rows.append(dead_letter_response(entry, run, meeting.title if meeting else None))
    return rows


def dead_letter_counts(db: Session) -> dict[str, int]:
    counts = {status: 0 for status in ("open", "retried", "resumed", "cancelled")}
    for entry_status, total in (
        db.query(WorkflowDeadLetter.status, func.count(WorkflowDeadLetter.id))
        .group_by(WorkflowDeadLetter.status)
        .all()
    ):
        counts[entry_status] = total
    return counts


def event_response(event: WorkflowEvent) -> dict:
    return {
        "id": event.id,
        "step_key": event.step_key,
        "attempt_number": event.attempt_number,
        "event_type": event.event_type,
        "message": event.message,
        "error": event.error,
        "retry_delay_seconds": event.retry_delay_seconds,
        "next_attempt_at": event.next_attempt_at,
        "actor_id": event.actor_id,
        "created_at": event.created_at,
    }


def dead_letter_detail(db: Session, run_id: str) -> dict:
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise DeadLetterNotFound("Workflow run not found.")
    meeting = db.get(Meeting, run.meeting_id) if run.meeting_id else None
    entries = (
        db.query(WorkflowDeadLetter)
        .filter(WorkflowDeadLetter.workflow_run_id == run_id)
        .order_by(WorkflowDeadLetter.created_at.desc())
        .all()
    )
    return {
        "workflow": workflow_response(db, run),
        "payload": run.payload or {},
        "idempotency_key": run.idempotency_key,
        "meeting_title": meeting.title if meeting else None,
        "dead_letters": [
            dead_letter_response(entry, run, meeting.title if meeting else None)
            for entry in entries
        ],
        "events": [event_response(event) for event in workflow_events(db, run_id)],
    }
