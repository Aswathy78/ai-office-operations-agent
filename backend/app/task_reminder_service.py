"""Automatic task reminders and manager escalation (Story 4.4 / FR-21).

Schedule, relative to a task's due date:

- ``T-24h``: reminder to the assignee.
- ``T-2h``: second reminder to the assignee.
- From the due date on, while the task is still open: one overdue reminder per
  day (day 1 at the due time, day 2 at +24h, ...).
- Escalation: once two reminders have been *missed*, the assignee's line
  manager gets one escalation e-mail listing the task, assignee and due date.
  A pre-due reminder counts as missed once the due date passes with the task
  still open; overdue reminder N counts as missed once overdue reminder N+1 is
  due. In the normal flow the escalation goes out at the due time.

Nothing is sent for completed tasks, and the status is re-read immediately
before every e-mail.

Duplicate prevention: each e-mail is recorded in ``task_notifications`` with a
unique idempotency key built from the task, its assignment version, its due
date version and the reminder stage. The row is committed *before* the e-mail
is sent, so a second scheduler run (or a concurrent process) hits the unique
key and skips it. Reassigning a task or changing its due date starts a new
cycle. If the scheduler was down, only the reminder for the current stage is
sent; stages that were missed in the meantime are not sent late.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import smtplib
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import task_service
from app.meeting.models import (
    Employee,
    Meeting,
    Task,
    TaskNotification,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
)

logger = logging.getLogger(__name__)

FIRST_REMINDER_BEFORE = timedelta(hours=24)
SECOND_REMINDER_BEFORE = timedelta(hours=2)
OVERDUE_INTERVAL = timedelta(hours=24)
MISSED_REMINDERS_BEFORE_ESCALATION = 2

KIND_REMINDER_24H = "reminder_24h"
KIND_REMINDER_2H = "reminder_2h"
KIND_REMINDER_OVERDUE = "reminder_overdue"
KIND_ESCALATION = "escalation"
REMINDER_KINDS = (KIND_REMINDER_24H, KIND_REMINDER_2H, KIND_REMINDER_OVERDUE)

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class ReminderStage:
    kind: str
    # Overdue day number (1 = the due date itself); 0 for pre-due reminders.
    day: int = 0

    @property
    def key(self) -> str:
        if self.kind == KIND_REMINDER_24H:
            return "t24h"
        if self.kind == KIND_REMINDER_2H:
            return "t2h"
        return f"overdue{self.day}"


@dataclass
class ReminderRunSummary:
    checked: int = 0
    reminders_sent: int = 0
    escalations_sent: int = 0
    failed: int = 0
    skipped_no_manager: int = 0
    notifications: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "reminders_sent": self.reminders_sent,
            "escalations_sent": self.escalations_sent,
            "failed": self.failed,
            "skipped_no_manager": self.skipped_no_manager,
            "notification_ids": list(self.notifications),
        }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_due(value: datetime) -> str:
    return _as_utc(value).strftime("%d %b %Y %H:%M UTC")


def current_stage(due_at: datetime, now: datetime) -> ReminderStage | None:
    """The reminder that applies at ``now``; earlier stages are never sent late."""
    due_at, now = _as_utc(due_at), _as_utc(now)
    if now < due_at - FIRST_REMINDER_BEFORE:
        return None
    if now < due_at - SECOND_REMINDER_BEFORE:
        return ReminderStage(KIND_REMINDER_24H)
    if now < due_at:
        return ReminderStage(KIND_REMINDER_2H)
    day = int((now - due_at) / OVERDUE_INTERVAL) + 1
    return ReminderStage(KIND_REMINDER_OVERDUE, day)


def cycle_key(task: Task) -> str:
    """Identifies one assignee + due-date cycle; reassignment or a new due date starts a new one."""
    return f"{task.id}:a{task.assignment_version or 1}:d{task.calendar_sequence or 0}"


def reminder_key(task: Task, stage: ReminderStage) -> str:
    return f"task-reminder:{cycle_key(task)}:{stage.key}"


def escalation_key(task: Task) -> str:
    return f"task-escalation:{cycle_key(task)}"


def _stage_from_key(key: str) -> ReminderStage | None:
    suffix = key.rsplit(":", 1)[-1]
    if suffix == "t24h":
        return ReminderStage(KIND_REMINDER_24H)
    if suffix == "t2h":
        return ReminderStage(KIND_REMINDER_2H)
    if suffix.startswith("overdue") and suffix[7:].isdigit():
        return ReminderStage(KIND_REMINDER_OVERDUE, int(suffix[7:]))
    return None


def count_missed_reminders(db: Session, task: Task, now: datetime) -> int:
    """Delivered reminders in the current cycle whose response window has closed."""
    due_at, now = _as_utc(task.due_at), _as_utc(now)
    prefix = f"task-reminder:{cycle_key(task)}:"
    sent = (
        db.query(TaskNotification.idempotency_key)
        .filter(
            TaskNotification.task_id == task.id,
            TaskNotification.status == STATUS_SENT,
            TaskNotification.idempotency_key.like(f"{prefix}%"),
        )
        .all()
    )
    missed = 0
    for (key,) in sent:
        stage = _stage_from_key(key)
        if stage is None:
            continue
        if stage.kind == KIND_REMINDER_OVERDUE:
            window_closes = due_at + OVERDUE_INTERVAL * stage.day
        else:
            window_closes = due_at
        if now >= window_closes:
            missed += 1
    return missed


# ---------------------------------------------------------------------------
# E-mail content
# ---------------------------------------------------------------------------


def _time_until(due_at: datetime, now: datetime) -> str:
    hours = max(0, round((_as_utc(due_at) - _as_utc(now)).total_seconds() / 3600))
    if hours < 1:
        return "in less than an hour"
    return f"in about {hours} hour{'s' if hours != 1 else ''}"


def render_reminder(
    task: Task,
    assignee: Employee,
    meeting: Meeting | None,
    stage: ReminderStage,
    now: datetime,
) -> tuple[str, str]:
    due = _format_due(task.due_at)
    if stage.kind == KIND_REMINDER_OVERDUE:
        days = stage.day - 1
        overdue = "is now overdue" if days == 0 else f"is overdue by {days} day{'s' if days != 1 else ''}"
        subject = f"Overdue task: {task.title}"
        opening = f"Your task {overdue}. It was due on {due}."
        closing = (
            "Please complete it as soon as possible and mark it as Completed, or "
            "talk to your manager if the due date needs to change."
        )
    else:
        subject = f"Reminder: {task.title} is due {_time_until(task.due_at, now)}"
        opening = f"This is a reminder that your task is due {_time_until(task.due_at, now)}, on {due}."
        closing = "When you are done, mark the task as Completed to stop further reminders."

    source = f"Source meeting: {meeting.title}\n" if meeting else ""
    body = (
        f"Hello {assignee.name},\n\n"
        f"{opening}\n\n"
        f"Task: {task.title}\n"
        f"Due: {due}\n"
        f"Priority: {task.priority}\n"
        f"{source}\n"
        f"{closing}\n\n"
        "This message was sent automatically by the Office Operations system.\n"
    )
    return subject, body


def render_escalation(
    task: Task,
    assignee: Employee,
    manager: Employee,
    meeting: Meeting | None,
    missed: int,
    now: datetime,
) -> tuple[str, str]:
    due = _format_due(task.due_at)
    overdue_hours = max(0, int((_as_utc(now) - _as_utc(task.due_at)).total_seconds() // 3600))
    overdue_text = (
        f"{overdue_hours // 24} day(s) {overdue_hours % 24} hour(s)" if overdue_hours else "less than an hour"
    )
    source = f"Source meeting: {meeting.title}\n" if meeting else ""
    subject = f"Escalation: {assignee.name} has not completed \"{task.title}\""
    body = (
        f"Hello {manager.name},\n\n"
        f"A task assigned to someone on your team is overdue and {missed} reminders "
        "have gone unanswered.\n\n"
        f"Task: {task.title}\n"
        f"Assignee: {assignee.name} <{assignee.email}>\n"
        f"Due date: {due}\n"
        f"Overdue by: {overdue_text}\n"
        f"Priority: {task.priority}\n"
        f"{source}\n"
        "Please follow up with the assignee. Reminders will stop automatically "
        "once the task is marked Completed.\n\n"
        "This message was sent automatically by the Office Operations system.\n"
    )
    return subject, body


# ---------------------------------------------------------------------------
# Delivery with duplicate prevention
# ---------------------------------------------------------------------------


def _is_open(db: Session, task: Task) -> bool:
    db.refresh(task)
    return task.status != TASK_STATUS_COMPLETED


def _deliver_once(
    db: Session,
    task: Task,
    *,
    recipient: Employee,
    kind: str,
    key: str,
    subject: str,
    body: str,
    now: datetime,
) -> TaskNotification | None:
    """Send one e-mail at most once per idempotency key.

    Returns the notification when an e-mail was attempted, ``None`` when it was
    skipped (already sent / claimed by another run, or the task is completed).
    A *failed* attempt for the same key is retried on the next run.
    """
    notification = (
        db.query(TaskNotification).filter(TaskNotification.idempotency_key == key).one_or_none()
    )
    if notification is not None and notification.status != STATUS_FAILED:
        return None

    if not _is_open(db, task):
        return None

    if notification is None:
        notification = TaskNotification(
            id=str(uuid.uuid4()),
            task_id=task.id,
            recipient_employee_id=recipient.id,
            kind=kind,
            subject=subject,
            body=body,
            idempotency_key=key,
            status=STATUS_PENDING,
            created_at=now,
        )
        db.add(notification)
    else:
        notification.status = STATUS_PENDING
        notification.subject = subject
        notification.body = body
        notification.error = None
    try:
        # Claim the key before sending so concurrent runs cannot both send.
        db.commit()
    except IntegrityError:
        db.rollback()
        return None

    try:
        task_service.send_email(recipient.email, subject, body)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        notification.status = STATUS_FAILED
        notification.error = str(exc)[:1000]
        logger.warning("Task %s e-mail failed task_id=%s to=%s: %s", kind, task.id, recipient.email, exc)
    else:
        notification.status = STATUS_SENT
    db.commit()
    return notification


def process_task(db: Session, task: Task, now: datetime, summary: ReminderRunSummary) -> None:
    if task.status == TASK_STATUS_COMPLETED:
        return
    assignee = db.get(Employee, task.assignee_id)
    if assignee is None or not assignee.email:
        return
    meeting = db.get(Meeting, task.meeting_id)

    stage = current_stage(task.due_at, now)
    if stage is not None:
        subject, body = render_reminder(task, assignee, meeting, stage, now)
        notification = _deliver_once(
            db, task, recipient=assignee, kind=stage.kind, key=reminder_key(task, stage),
            subject=subject, body=body, now=now,
        )
        if notification is not None:
            if notification.status == STATUS_SENT:
                summary.reminders_sent += 1
            else:
                summary.failed += 1
            summary.notifications.append(notification.id)

    missed = count_missed_reminders(db, task, now)
    if missed < MISSED_REMINDERS_BEFORE_ESCALATION:
        return
    already = (
        db.query(TaskNotification.status)
        .filter(TaskNotification.idempotency_key == escalation_key(task))
        .scalar()
    )
    if already in (STATUS_SENT, STATUS_PENDING):
        return
    manager = db.get(Employee, assignee.manager_id) if assignee.manager_id else None
    if manager is None or not manager.is_active or not manager.email:
        summary.skipped_no_manager += 1
        logger.warning("No active manager to escalate task_id=%s assignee=%s", task.id, assignee.email)
        return
    subject, body = render_escalation(task, assignee, manager, meeting, missed, now)
    notification = _deliver_once(
        db, task, recipient=manager, kind=KIND_ESCALATION, key=escalation_key(task),
        subject=subject, body=body, now=now,
    )
    if notification is not None:
        if notification.status == STATUS_SENT:
            summary.escalations_sent += 1
        else:
            summary.failed += 1
        summary.notifications.append(notification.id)


def run_task_reminders(db: Session, now: datetime | None = None) -> ReminderRunSummary:
    """One scheduler pass over every open task that is within 24 hours of, or past, its due date."""
    now = _as_utc(now or datetime.now(timezone.utc))
    summary = ReminderRunSummary()
    tasks = (
        db.query(Task)
        .filter(Task.status == TASK_STATUS_PENDING)
        .filter(Task.due_at <= now + FIRST_REMINDER_BEFORE)
        .order_by(Task.due_at)
        .all()
    )
    for task in tasks:
        summary.checked += 1
        try:
            process_task(db, task, now, summary)
        except Exception:  # noqa: BLE001 - one bad task must not stop the run
            db.rollback()
            summary.failed += 1
            logger.exception("Reminder processing failed task_id=%s", task.id)
    return summary
