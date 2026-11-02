"""Concurrent background workers for persistent Epic 8 workflow runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import threading
import time

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    Meeting,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    Employee,
    Task,
    WorkflowRun,
    WorkflowStep,
)
from app.workflow_service import (
    CALENDAR_MONITOR_WORKFLOW,
    MEETING_WORKFLOW,
    TASK_REMINDER_WORKFLOW,
    claim_workflow,
    get_workflow_steps,
    recover_stale_workflows,
)

logger = logging.getLogger(__name__)
_stop_event: threading.Event | None = None
_threads: list[threading.Thread] = []
_claim_lock = threading.Lock()
_idle_seconds = 1.0
_retry_seconds = 5


class WorkflowNotReady(RuntimeError):
    """A prerequisite is still progressing in its owning background service."""


def worker_count() -> int:
    try:
        return min(8, max(1, int(os.getenv("WORKFLOW_CONCURRENCY", "2"))))
    except ValueError:
        logger.warning("Invalid WORKFLOW_CONCURRENCY; using 2 workers.")
        return 2


def _begin_step(db: Session, run: WorkflowRun, step: WorkflowStep) -> None:
    now = datetime.now(timezone.utc)
    step.status = "processing"
    step.started_at = now
    step.attempt_count += 1
    step.last_error = None
    step.updated_at = now
    run.updated_at = now
    db.commit()


def _verify_step(db: Session, run: WorkflowRun, step: WorkflowStep) -> dict:
    if run.workflow_type != MEETING_WORKFLOW or run.meeting_id is None:
        if step.step_key == "task_reminders":
            from app.task_reminder_service import run_task_reminders

            return run_task_reminders(db).as_dict()
        if step.step_key == "calendar_monitor":
            from app.task_calendar_sync_service import run_sync_pass

            result = run_sync_pass(db, poll=bool(run.payload.get("poll")))
            return result.as_dict()
        raise ValueError(f"Unsupported workflow step: {step.step_key}")

    meeting = db.get(Meeting, run.meeting_id)
    if meeting is None:
        raise ValueError("The workflow meeting no longer exists.")
    if step.step_key == "transcription":
        if meeting.status != "completed":
            raise WorkflowNotReady("Waiting for the meeting to end.")
        recording = (
            db.query(MeetingRecording)
            .filter(
                MeetingRecording.meeting_id == meeting.id,
                MeetingRecording.status == "completed",
                MeetingRecording.participant_id.is_(None),
                MeetingRecording.connection_id.is_(None),
            )
            .order_by(MeetingRecording.completed_at.desc())
            .first()
        )
        if recording is None:
            raise WorkflowNotReady("Waiting for a completed meeting recording.")
        transcript = (
            db.query(MeetingTranscript)
            .filter(MeetingTranscript.recording_id == recording.id)
            .one_or_none()
        )
        if transcript is None or transcript.status in ("pending", "processing"):
            raise WorkflowNotReady("Waiting for the recording transcription.")
        if transcript.status != "completed":
            raise RuntimeError(
                transcript.error_message or "The meeting transcription failed."
            )
        return {"recording_id": recording.id, "transcript_id": transcript.id}

    transcript_id = (step.checkpoint or {}).get("transcript_id")
    if not transcript_id:
        previous = (
            db.query(WorkflowStep)
            .filter(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == "transcription",
                WorkflowStep.status == "completed",
            )
            .one_or_none()
        )
        transcript_id = (previous.checkpoint or {}).get("transcript_id") if previous else None
    if not transcript_id:
        raise WorkflowNotReady("The transcription checkpoint is not available.")

    if step.step_key == "summary":
        from app.meeting.summary_service import generate_meeting_summary

        summary = generate_meeting_summary(db, transcript_id)
        db.expire_all()
        verified = (
            db.query(MeetingSummary)
            .filter(MeetingSummary.transcript_id == transcript_id)
            .one_or_none()
        )
        if (
            summary.status != "completed"
            or verified is None
            or verified.status != "completed"
            or not (verified.overview or "").strip()
        ):
            raise RuntimeError("Meeting summary read-back verification failed.")
        return {"summary_id": verified.id, "status": verified.status}

    if step.step_key == "action_items":
        from app.meeting.action_item_service import generate_meeting_action_items

        extraction = generate_meeting_action_items(db, transcript_id)
        db.expire_all()
        verified = (
            db.query(MeetingActionItemExtraction)
            .filter(MeetingActionItemExtraction.transcript_id == transcript_id)
            .one_or_none()
        )
        if (
            extraction.status != ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
            or verified is None
            or verified.status != ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
        ):
            raise RuntimeError("Action-item read-back verification failed.")
        return {"extraction_id": verified.id, "status": verified.status}

    if step.step_key == "task_creation":
        from app.meeting.action_item_service import get_task_eligible_action_items
        from app.task_service import synchronize_confirmed_action_item_tasks

        extraction = (
            db.query(MeetingActionItemExtraction)
            .filter(MeetingActionItemExtraction.transcript_id == transcript_id)
            .one_or_none()
        )
        if extraction is None or extraction.status != ACTION_ITEM_EXTRACTION_STATUS_COMPLETED:
            raise WorkflowNotReady("Waiting for completed action-item extraction.")
        synchronize_confirmed_action_item_tasks(db, extraction, meeting.organizer_id)
        eligible = get_task_eligible_action_items(db, meeting.id)
        eligible_ids = [item.id for item in eligible]
        verified_tasks = (
            db.query(Task)
            .filter(Task.source_action_item_id.in_(eligible_ids))
            .all()
            if eligible_ids
            else []
        )
        task_ids = {task.source_action_item_id for task in verified_tasks}
        missing_ids = set(eligible_ids) - task_ids
        if missing_ids:
            raise RuntimeError(
                "Task read-back verification failed for confirmed action items: "
                + ", ".join(sorted(missing_ids))
            )
        return {"task_ids": [task.id for task in verified_tasks]}

    raise ValueError(f"Unsupported meeting workflow step: {step.step_key}")


def _notify_meeting_result(db: Session, run: WorkflowRun, *, failed: bool) -> None:
    if run.workflow_type != MEETING_WORKFLOW or run.meeting_id is None:
        return
    meeting = db.get(Meeting, run.meeting_id)
    if meeting is None or db.get(Employee, meeting.organizer_id) is None:
        logger.warning(
            "Workflow notification recipient is unavailable meeting_id=%s",
            run.meeting_id,
        )
        return
    from app.notification_service import create_notification, deliver_notification

    event_type = "meeting_workflow_failed" if failed else "meeting_workflow_completed"
    summary = (
        f"Follow-up workflow failed for {meeting.title}."
        if failed
        else f"Follow-up workflow completed for {meeting.title}."
    )
    notification = create_notification(
        db,
        recipient_id=meeting.organizer_id,
        event_type=event_type,
        summary=summary,
        idempotency_key=f"workflow-result:{run.id}:{event_type}",
        entity_type="meeting",
        entity_id=meeting.id,
        entity_url=f"/meetings/{meeting.id}/report",
        email_subject=summary,
        email_body=f"{summary}\n\nOpen the meeting report: /meetings/{meeting.id}/report",
    )
    db.commit()
    if notification.email_status == "pending":
        deliver_notification(db, notification.id)


def _finish_step(db: Session, run: WorkflowRun, step: WorkflowStep, checkpoint: dict) -> None:
    db.expire_all()
    verified_step = db.get(WorkflowStep, step.id)
    if verified_step is None or verified_step.status != "processing":
        raise RuntimeError("Workflow step lease was lost before checkpoint persistence.")
    now = datetime.now(timezone.utc)
    verified_step.checkpoint = checkpoint
    verified_step.status = "completed"
    verified_step.completed_at = now
    verified_step.last_error = None
    verified_step.updated_at = now
    run = db.get(WorkflowRun, run.id)
    if run is None:
        raise RuntimeError("Workflow run disappeared before checkpoint persistence.")
    run.updated_at = now
    db.commit()


def _defer_run(db: Session, run: WorkflowRun, step: WorkflowStep, error: Exception) -> None:
    now = datetime.now(timezone.utc)
    run = db.get(WorkflowRun, run.id)
    step = db.get(WorkflowStep, step.id)
    if run is None or step is None:
        raise RuntimeError("Workflow state disappeared while deferring a run.")
    step.status = "retrying"
    step.last_error = str(error)[:4000] or type(error).__name__
    step.updated_at = now
    run.status = "retrying"
    run.claimed_at = None
    run.next_attempt_at = now + timedelta(seconds=_retry_seconds)
    run.last_error = step.last_error
    run.updated_at = now
    db.commit()


def _fail_step(db: Session, run: WorkflowRun, step: WorkflowStep, error: Exception) -> None:
    now = datetime.now(timezone.utc)
    run = db.get(WorkflowRun, run.id)
    step = db.get(WorkflowStep, step.id)
    if run is None or step is None:
        raise RuntimeError("Workflow state disappeared while recording a failure.")
    message = str(error)[:4000] or type(error).__name__
    step.last_error = message
    step.updated_at = now
    run.attempt_count += 1
    run.last_error = message
    run.updated_at = now
    run.claimed_at = None
    if run.attempt_count < run.max_attempts:
        step.status = "retrying"
        run.status = "retrying"
        run.next_attempt_at = now + timedelta(seconds=min(300, _retry_seconds * 2 ** (run.attempt_count - 1)))
    else:
        step.status = "failed"
        step.completed_at = now
        run.status = "failed"
        run.next_attempt_at = None
        run.completed_at = now
    db.commit()
    logger.exception(
        "Workflow step failed run_id=%s step=%s attempt=%s",
        run.id,
        step.step_key,
        run.attempt_count,
        exc_info=error,
    )
    if run.status == "failed":
        _notify_meeting_result(db, run, failed=True)


def process_workflow(db: Session, run_id: str) -> None:
    run = db.get(WorkflowRun, run_id)
    if run is None or run.status != "processing":
        return
    for step in get_workflow_steps(db, run.id):
        if step.status == "completed":
            continue
        _begin_step(db, run, step)
        try:
            checkpoint = _verify_step(db, run, step)
        except WorkflowNotReady as error:
            _defer_run(db, run, step, error)
            return
        except Exception as error:
            db.rollback()
            _fail_step(db, run, step, error)
            return
        _finish_step(db, run, step, checkpoint)
        run = db.get(WorkflowRun, run_id)
        if run is None:
            raise RuntimeError(f"Workflow run {run_id} disappeared during execution.")

    run = db.get(WorkflowRun, run_id)
    if run is None:
        return
    now = datetime.now(timezone.utc)
    run.status = "completed"
    run.completed_at = now
    run.claimed_at = None
    run.next_attempt_at = None
    run.last_error = None
    run.updated_at = now
    db.commit()
    _notify_meeting_result(db, run, failed=False)


def _worker_loop(stop: threading.Event, worker_index: int) -> None:
    logger.info("Workflow worker %s started", worker_index)
    while not stop.is_set():
        db = SessionLocal()
        try:
            with _claim_lock:
                run = claim_workflow(db)
            if run is None:
                stop.wait(_idle_seconds)
                continue
            try:
                process_workflow(db, run.id)
            except Exception:
                db.rollback()
                logger.exception("Workflow worker failed run_id=%s", run.id)
        except Exception:
            db.rollback()
            logger.exception("Workflow worker could not claim a job")
            stop.wait(_retry_seconds)
        finally:
            db.close()


def start_workflow_workers() -> bool:
    global _stop_event, _threads
    if _threads and any(thread.is_alive() for thread in _threads):
        return True
    count = worker_count()
    stop = threading.Event()
    _stop_event = stop
    with SessionLocal() as db:
        recover_stale_workflows(db)
    _threads = [
        threading.Thread(
            target=_worker_loop,
            args=(stop, index + 1),
            name=f"workflow-worker-{index + 1}",
            daemon=True,
        )
        for index in range(count)
    ]
    for thread in _threads:
        thread.start()
    logger.info("Started %s workflow worker(s)", count)
    return True


def stop_workflow_workers() -> None:
    global _threads
    if _stop_event is not None:
        _stop_event.set()
    for thread in _threads:
        thread.join(timeout=10)
    _threads = []
