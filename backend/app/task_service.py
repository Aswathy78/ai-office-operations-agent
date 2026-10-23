"""Task creation and assignment-email delivery for confirmed action items."""

from datetime import datetime, timezone
import logging
import os
import smtplib
from urllib.parse import quote, urlencode
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
    TaskNotification,
    TASK_STATUS_PENDING,
)

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
        task.due_at = _as_utc(item.deadline_at)
        task.priority = item.priority
        task.source_segment_index = item.source_segment_index
        task.source_timestamp_seconds = item.source_start_seconds
        if assignee_changed:
            task.assignment_version = (task.assignment_version or 1) + 1
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
        due_at=_as_utc(item.deadline_at),
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
            task, _ = prepare_task_from_confirmed_action_item(
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
        tasks_to_notify.append(task.id)

    if has_changes:
        db.commit()
    for task_id in tasks_to_notify:
        attempt_assignment_email(db, task_id)


def send_email(recipient: str, subject: str, body: str) -> None:
    """Deliver one plain-text e-mail through the configured SMTP server."""
    deliver_email(recipient, subject, body)


def deliver_email(recipient: str, subject: str, body: str) -> str:
    """Hand one plain-text e-mail to the mail provider and return its Message-ID.

    Returns only once a provider has accepted the message for the recipient
    (falling back to ``FALLBACK_SMTP_*`` when the primary is unavailable); any
    refusal raises instead. See ``app.email_providers``.
    """
    from app import email_providers

    return email_providers.send_email(recipient, subject, body).message_id


def assignment_notification_key(task: Task) -> str:
    return (
        f"assignment:{task.id}:{task.assignment_version or 1}:"
        f"{task.assignee_id}"
    )


def render_assignment_email(
    task: Task,
    employee: Employee,
    meeting_title: str,
    assignment_context: str | None = None,
) -> tuple[str, str]:
    frontend_base_url = os.getenv(
        "FRONTEND_BASE_URL",
        "http://localhost:5173",
    ).strip().rstrip("/")
    if not frontend_base_url:
        raise ValueError("FRONTEND_BASE_URL must not be empty.")
    task_url = (
        f"{frontend_base_url}/tasks?"
        f"{urlencode({'task_id': task.id})}"
    )
    if task.meeting_id:
        meeting_query = urlencode(
            {
                "transcript_id": task.transcript_id,
                "segment": task.source_segment_index,
            }
        )
        meeting_url = (
            f"{frontend_base_url}/meetings/{quote(task.meeting_id, safe='')}/report"
            f"?{meeting_query}"
        )
        source = (
            f"Source meeting: {meeting_title}\n"
            f"Meeting link: {meeting_url}\n"
        )
    else:
        # Tasks the AI assistant created directly have no source meeting.
        source = "Source: assigned through the AI Office Assistant\n"
    subject = f"Task assigned: {task.title}"
    context = f"{assignment_context.strip()}\n" if assignment_context else ""
    body = (
        f"Hello {employee.name},\n\n"
        f"{context}"
        f"You have been assigned: {task.title}\n"
        f"Due: {task.due_at.isoformat()}\n"
        f"Priority: {task.priority}\n"
        f"Task: {task_url}\n"
        f"{source}"
    )
    return subject, body


def send_assignment_email(
    task: Task,
    employee: Employee,
    meeting_title: str,
) -> None:
    """Send an assignment message rendered from the reusable email template."""
    subject, body = render_assignment_email(task, employee, meeting_title)
    send_email(employee.email, subject, body)


def attempt_assignment_email(
    db: Session,
    task_id: str,
    *,
    retry_failed: bool = False,
) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise ValueError("Task not found.")
    employee = db.get(Employee, task.assignee_id)
    meeting = db.get(Meeting, task.meeting_id) if task.meeting_id else None
    if employee is None or (task.meeting_id and meeting is None):
        raise ValueError("Task assignee or source meeting no longer exists.")

    idempotency_key = assignment_notification_key(task)
    notification = (
        db.query(TaskNotification)
        .filter(TaskNotification.idempotency_key == idempotency_key)
        .one_or_none()
    )
    notification_created = notification is None
    if notification_created:
        subject, body = render_assignment_email(task, employee, meeting.title if meeting else "")
        notification = TaskNotification(
            id=str(uuid.uuid4()),
            task_id=task.id,
            recipient_employee_id=employee.id,
            kind="assignment",
            subject=subject,
            body=body,
            idempotency_key=idempotency_key,
            status=task.assignment_email_status,
            error=task.assignment_email_error,
        )
        db.add(notification)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            task = db.get(Task, task_id)
            if task is None:
                raise ValueError("Task not found.") from None
            idempotency_key = assignment_notification_key(task)
            notification = (
                db.query(TaskNotification)
                .filter(TaskNotification.idempotency_key == idempotency_key)
                .one_or_none()
            )
            if notification is None:
                raise
            notification_created = False
    if notification is None:
        raise RuntimeError("Assignment notification could not be persisted.")
    if notification.status == "sent":
        task.assignment_email_status = "sent"
        task.assignment_email_error = None
        db.commit()
        db.refresh(task)
        return task

    if not notification_created and not retry_failed:
        task.assignment_email_status = notification.status
        task.assignment_email_error = notification.error
        db.commit()
        db.refresh(task)
        return task
    if notification.status == "failed" and not retry_failed:
        task.assignment_email_status = "failed"
        task.assignment_email_error = notification.error
        db.commit()
        db.refresh(task)
        return task

    task.assignment_email_status = "pending"
    task.assignment_email_error = None
    db.commit()
    db.refresh(notification)

    try:
        send_email(employee.email, notification.subject, notification.body)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        notification.status = "failed"
        notification.error = str(exc)
        task.assignment_email_status = "failed"
        task.assignment_email_error = str(exc)
        logger.exception(
            "Task assignment email failed task_id=%s assignee_id=%s",
            task.id,
            task.assignee_id,
        )
    else:
        notification.status = "sent"
        notification.error = None
        task.assignment_email_status = "sent"
        task.assignment_email_error = None
    db.commit()
    db.refresh(task)
    return task
