"""Opt-in demo data for Story 8.4: put a failed workflow run in the dead-letter queue.

Nothing fails on demand in a healthy system, so a demo needs a run that already ran
out of retries. This creates one through the same code the worker uses (attempt
history, dead-lettering and the administrator notification), so what you see is the
real behaviour, not a mock-up.

Run from ``backend/``::

    python -m app.db.demo_dead_letter            # add a failed run to the queue
    python -m app.db.demo_dead_letter --remove   # delete the demo runs again

The demo run is a task-reminder workflow. Retrying or resuming it really runs a
reminder pass, which is safe to repeat.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import uuid

from sqlalchemy.orm import Session

from app.meeting.models import (
    WorkflowDeadLetter,
    WorkflowEvent,
    WorkflowRun,
    WorkflowStep,
)
from app.workflow_service import (
    TASK_REMINDER_WORKFLOW,
    WORKFLOW_MAX_ATTEMPTS,
    dead_letter_run,
    enqueue_workflow,
    record_event,
)

DEMO_KEY_PREFIX = "demo-dlq:"
DEMO_ERROR = "SMTP server smtp.example.test timed out after 30 seconds (demo failure)"


def seed_demo_dead_letter(db: Session) -> WorkflowRun:
    run = enqueue_workflow(
        db,
        workflow_type=TASK_REMINDER_WORKFLOW,
        idempotency_key=f"{DEMO_KEY_PREFIX}{uuid.uuid4()}",
        steps=("task_reminders",),
        payload={"demo": True},
    )
    step = db.query(WorkflowStep).filter_by(workflow_run_id=run.id).one()
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=2)
    for attempt, delay in enumerate((4.3, 8.9, 17.2, 31.6), start=1):
        record_event(
            db,
            run.id,
            "retry_scheduled",
            step_key=step.step_key,
            attempt_number=attempt,
            error=DEMO_ERROR,
            retry_delay_seconds=delay,
            next_attempt_at=started + timedelta(seconds=attempt * 20),
        )
    run.status = "failed"
    run.attempt_count = WORKFLOW_MAX_ATTEMPTS
    run.last_error = DEMO_ERROR
    run.completed_at = now
    run.updated_at = now
    step.status = "failed"
    step.attempt_count = WORKFLOW_MAX_ATTEMPTS
    step.last_error = DEMO_ERROR
    step.completed_at = now
    db.commit()
    dead_letter_run(db, run.id, step_key=step.step_key, error_message=DEMO_ERROR)
    return run


def remove_demo_dead_letters(db: Session) -> int:
    runs = db.query(WorkflowRun).filter(WorkflowRun.idempotency_key.like(f"{DEMO_KEY_PREFIX}%")).all()
    for run in runs:
        for model in (WorkflowEvent, WorkflowDeadLetter, WorkflowStep):
            db.query(model).filter(model.workflow_run_id == run.id).delete()
        db.delete(run)
    db.commit()
    return len(runs)


def main(argv: list[str]) -> int:
    from app.db.database import SessionLocal

    with SessionLocal() as db:
        if "--remove" in argv:
            print(f"Removed {remove_demo_dead_letters(db)} demo workflow run(s).")
            return 0
        run = seed_demo_dead_letter(db)
        entry = db.query(WorkflowDeadLetter).filter_by(workflow_run_id=run.id).one()
        print(f"Added failed demo run {run.id} to the dead-letter queue.")
        print(f"Administrators notified: {entry.notified_count}")
        print("Open the Workflow queue page as an administrator to retry, resume or cancel it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
