"""Task creation and assignment-email delivery for confirmed action items."""

from email.message import EmailMessage
import logging
import os
import smtplib
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.models import (
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    Employee,
    MeetingActionItem,
    MeetingActionItemExtraction,
    Meeting,
    Task,
    TASK_STATUS_PENDING,
)

logger = logging.getLogger(__name__)


def prepare_task_from_confirmed_action_item(
    db: Session,
    item: MeetingActionItem,
    creator_id: str,
) -> tuple[Task, bool]:
    """Prepare an idempotent task in the caller's current transaction."""
    if item.review_status != ACTION_ITEM_REVIEW_STATUS_CONFIRMED:
        raise ValueError("Only confirmed action items can create tasks.")
    if not item.confirmed_employee_id:
        raise ValueError("Confirm an active assignee before creating this task.")
    if item.deadline_at is None:
        raise ValueError("Set a due date before confirming this action item.")

    employee = db.get(Employee, item.confirmed_employee_id)
    if employee is None or not employee.is_active:
        raise ValueError("The confirmed task assignee must be an active employee.")

    extraction = item.extraction
    idempotency_key = f"meeting-action-item:{item.id}"
    task = (
        db.query(Task)
        .filter(Task.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if task is not None:
        assignee_changed = task.assignee_id != employee.id
        task.title = item.task_description
        task.assignee_id = employee.id
        task.due_at = item.deadline_at
        task.priority = item.priority
        task.source_segment_index = item.source_segment_index
        task.source_timestamp_seconds = item.source_start_seconds
        if assignee_changed:
            task.assignment_email_status = "pending"
            task.assignment_email_error = None
        return task, assignee_changed

    task = Task(
        id=str(uuid.uuid4()),
        title=item.task_description,
        assignee_id=employee.id,
        creator_id=creator_id,
        source="meeting_action_item",
        source_action_item_id=item.id,
        idempotency_key=idempotency_key,
        meeting_id=extraction.meeting_id,
        transcript_id=extraction.transcript_id,
        source_segment_index=item.source_segment_index,
        source_timestamp_seconds=item.source_start_seconds,
        due_at=item.deadline_at,
        priority=item.priority,
        status=TASK_STATUS_PENDING,
    )
    db.add(task)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing_task = (
            db.query(Task)
            .filter(Task.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing_task is None:
            raise
        return existing_task, False
    return task, True


def synchronize_confirmed_action_item_tasks(
    db: Session,
    extraction: MeetingActionItemExtraction,
    fallback_creator_id: str,
) -> None:
    """Create missing tasks for complete confirmed items when analysis is rerun."""
    tasks_to_notify: list[str] = []
    has_changes = False
    for item in extraction.items:
        if item.review_status != ACTION_ITEM_REVIEW_STATUS_CONFIRMED:
            continue
        try:
            task, notify_assignee = prepare_task_from_confirmed_action_item(
                db,
                item,
                item.reviewed_by or fallback_creator_id,
            )
        except ValueError as exc:
            logger.warning(
                "Confirmed action item is not ready for task creation "
                "item_id=%s reason=%s",
                item.id,
                str(exc),
            )
            continue
        has_changes = True
        if notify_assignee:
            tasks_to_notify.append(task.id)

    if has_changes:
        db.commit()
    for task_id in tasks_to_notify:
        attempt_assignment_email(db, task_id)


def send_assignment_email(
    task: Task,
    employee: Employee,
    meeting_title: str,
) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM_EMAIL", "").strip()
    if not host or not sender:
        raise ValueError("SMTP_HOST and SMTP_FROM_EMAIL must be configured.")

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer.") from exc

    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        raise ValueError("Configure both SMTP_USERNAME and SMTP_PASSWORD.")

    message = EmailMessage()
    message["Subject"] = f"Task assigned: {task.title}"
    message["From"] = sender
    message["To"] = employee.email
    message.set_content(
        f"Hello {employee.name},\n\n"
        f"You have been assigned: {task.title}\n"
        f"Due: {task.due_at.isoformat()}\n"
        f"Priority: {task.priority}\n"
        f"Source meeting: {meeting_title}\n"
    )

    with smtplib.SMTP(host, port, timeout=10) as smtp:
        if os.getenv("SMTP_USE_STARTTLS", "true").strip().lower() not in {
            "0",
            "false",
            "no",
        }:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)


def attempt_assignment_email(db: Session, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise ValueError("Task not found.")
    if task.assignment_email_status == "sent":
        return task

    employee = db.get(Employee, task.assignee_id)
    meeting = db.get(Meeting, task.meeting_id)
    if employee is None or meeting is None:
        raise ValueError("Task assignee or source meeting no longer exists.")

    try:
        send_assignment_email(task, employee, meeting.title)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        task.assignment_email_status = "failed"
        task.assignment_email_error = str(exc)
        logger.exception(
            "Task assignment email failed task_id=%s assignee_id=%s",
            task.id,
            task.assignee_id,
        )
    else:
        task.assignment_email_status = "sent"
        task.assignment_email_error = None
    db.commit()
    db.refresh(task)
    return task
