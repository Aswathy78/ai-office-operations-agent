"""Task reassignment and due-date changes (Story 3.6 / FR-17).

Rules:
- Employees cannot reassign tasks or change due dates.
- A manager may act only on tasks whose current assignee is in the manager's
  team. Reassigning to someone in the same team is applied immediately;
  reassigning outside the team creates a request that HR / Operations or an
  Admin must approve.
- HR / Operations and Admin may reassign across teams directly and are the
  only roles that can approve or reject cross-team requests.
- Every attempt is written to the task audit log, including denials, and the
  affected employees are notified by e-mail.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import uuid

from sqlalchemy.orm import Session

from app.meeting.models import (
    Employee,
    Meeting,
    Task,
    TaskAuditLog,
    TaskNotification,
    TaskReassignmentRequest,
    UserNotification,
    TASK_AUDIT_OUTCOME_APPLIED,
    TASK_AUDIT_OUTCOME_DENIED,
    TASK_AUDIT_OUTCOME_PENDING_APPROVAL,
    TASK_AUDIT_OUTCOME_REJECTED,
    TASK_REASSIGNMENT_STATUS_APPROVED,
    TASK_REASSIGNMENT_STATUS_PENDING,
    TASK_REASSIGNMENT_STATUS_REJECTED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
)
from app.task_service import (
    assignment_notification_key,
    render_assignment_email,
)

logger = logging.getLogger(__name__)

ROLE_EMPLOYEE = "employee"
ROLE_MANAGER = "manager"
ROLE_HR_OPERATIONS = "hr_operations"
ROLE_ADMIN = "admin"
APPROVER_ROLES = {ROLE_HR_OPERATIONS, ROLE_ADMIN}

ACTION_REASSIGN = "reassign"
ACTION_CHANGE_DUE_DATE = "change_due_date"
ACTION_DECIDE_REASSIGNMENT = "decide_reassignment"
ACTION_CHANGE_STATUS = "change_status"

NOTIFICATION_REASSIGNED_AWAY = "reassigned_away"
NOTIFICATION_REASSIGNED_TO = "reassigned_to"
NOTIFICATION_DUE_DATE_CHANGED = "due_date_changed"


@dataclass(frozen=True)
class TaskActor:
    id: str
    role: str
    team: str | None = None
    # Signed-in user's e-mail; lets an employee act on tasks assigned to them.
    email: str | None = None


class TaskChangeDenied(Exception):
    """The actor is not allowed to make this change (HTTP 403)."""


class TaskChangeConflict(Exception):
    """The change conflicts with the task's current state (HTTP 409)."""


@dataclass
class TaskChangeResult:
    outcome: str
    message: str
    task: Task
    reassignment_request: TaskReassignmentRequest | None = None
    notifications: list[TaskNotification] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_due(value: datetime) -> str:
    return _as_utc(value).strftime("%d %b %Y %H:%M UTC")


def _team_label(team: str | None) -> str:
    return team or "no team"


def _same_team(left: str | None, right: str | None) -> bool:
    return bool(left) and bool(right) and left.casefold() == right.casefold()


def _audit(
    db: Session,
    task: Task,
    actor: TaskActor,
    action: str,
    outcome: str,
    detail: str,
) -> TaskAuditLog:
    entry = TaskAuditLog(
        id=str(uuid.uuid4()),
        task_id=task.id,
        actor_id=actor.id,
        actor_role=actor.role,
        action=action,
        outcome=outcome,
        detail=detail,
        created_at=_now(),
    )
    db.add(entry)
    return entry


def _deny(db: Session, task: Task, actor: TaskActor, action: str, reason: str):
    """Record the denied attempt and raise with the explanation shown to the user."""
    db.rollback()
    _audit(db, task, actor, action, TASK_AUDIT_OUTCOME_DENIED, reason)
    db.commit()
    logger.warning(
        "Task change denied task_id=%s actor_id=%s action=%s",
        task.id,
        actor.id,
        action,
    )
    raise TaskChangeDenied(reason)


def _load_task(db: Session, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise LookupError("Task not found.")
    return task


def _load_assignee(db: Session, employee_id: str) -> Employee:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise ValueError("The selected employee does not exist.")
    if not employee.is_active:
        raise ValueError(f"{employee.name} is inactive and cannot be assigned tasks.")
    return employee


def _pending_request(db: Session, task_id: str) -> TaskReassignmentRequest | None:
    return (
        db.query(TaskReassignmentRequest)
        .filter(
            TaskReassignmentRequest.task_id == task_id,
            TaskReassignmentRequest.status == TASK_REASSIGNMENT_STATUS_PENDING,
        )
        .one_or_none()
    )


def _require_manager_scope(
    db: Session,
    task: Task,
    actor: TaskActor,
    action: str,
    verb: str,
) -> None:
    """Deny employees, and managers acting on tasks outside their own team."""
    if actor.role == ROLE_EMPLOYEE:
        _deny(
            db,
            task,
            actor,
            action,
            f"Employees cannot {verb}. Ask your manager or HR / Operations to make this change.",
        )
    if actor.role in APPROVER_ROLES:
        return
    if actor.role != ROLE_MANAGER:
        _deny(db, task, actor, action, f"The role '{actor.role}' cannot {verb}.")

    current = db.get(Employee, task.assignee_id)
    current_team = current.team if current else None
    if not _same_team(current_team, actor.team):
        _deny(
            db,
            task,
            actor,
            action,
            f"This task belongs to {current.name if current else 'another employee'} "
            f"({_team_label(current_team)}). Managers can only {verb} for their own "
            f"team ({_team_label(actor.team)}).",
        )


def _queue_notification(
    db: Session,
    task: Task,
    recipient: Employee,
    kind: str,
    subject: str,
    body: str,
    idempotency_key: str | None = None,
) -> TaskNotification:
    if idempotency_key:
        existing = (
            db.query(TaskNotification)
            .filter(TaskNotification.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            return existing
    notification = TaskNotification(
        id=str(uuid.uuid4()),
        task_id=task.id,
        recipient_employee_id=recipient.id,
        kind=kind,
        subject=subject,
        body=body,
        idempotency_key=idempotency_key,
        status="pending",
        created_at=_now(),
    )
    db.add(notification)
    from app.notification_service import create_task_notification

    event_type = {
        NOTIFICATION_REASSIGNED_AWAY: "task_reassignment",
        NOTIFICATION_REASSIGNED_TO: "task_assignment",
        NOTIFICATION_DUE_DATE_CHANGED: "task_due_date_changed",
    }.get(kind, "task_update")
    create_task_notification(
        db,
        notification,
        event_type=event_type,
        summary=subject,
    )
    return notification


def _deliver_notifications(
    db: Session,
    notifications: list[TaskNotification],
) -> list[TaskNotification]:
    """Deliver queued notification e-mails after the task change is committed."""
    from app.notification_service import deliver_notification

    for notification in notifications:
        inbox_item = (
            db.query(UserNotification)
            .filter(UserNotification.task_notification_id == notification.id)
            .one_or_none()
        )
        if inbox_item is not None and notification.status != "sent":
            deliver_notification(db, inbox_item.id)
    return notifications


def _apply_reassignment(
    db: Session,
    task: Task,
    old_assignee: Employee,
    new_assignee: Employee,
    actor: TaskActor,
    detail: str,
) -> list[TaskNotification]:
    task.assignee_id = new_assignee.id
    task.assignment_version = (task.assignment_version or 1) + 1
    task.assignment_email_status = "pending"
    task.assignment_email_error = None
    task.updated_at = _now()
    task.calendar_sequence = (task.calendar_sequence or 0) + 1
    _audit(db, task, actor, ACTION_REASSIGN, TASK_AUDIT_OUTCOME_APPLIED, detail)

    meeting = db.get(Meeting, task.meeting_id) if task.meeting_id else None
    source = f"Source meeting: {meeting.title}\n" if meeting else ""
    assigned_subject, assigned_body = render_assignment_email(
        task,
        new_assignee,
        meeting.title if meeting else "",
        assignment_context=(
            f'The task "{task.title}" was reassigned to you '
            f"from {old_assignee.name}."
        ),
    )
    return [
        _queue_notification(
            db,
            task,
            old_assignee,
            NOTIFICATION_REASSIGNED_AWAY,
            f"Task reassigned: {task.title}",
            f"Hello {old_assignee.name},\n\n"
            f"The task \"{task.title}\" has been reassigned to {new_assignee.name}. "
            "It is no longer on your task list or calendar.\n"
            f"{source}",
            f"reassigned-away:{task.id}:a{task.assignment_version}:{old_assignee.id}",
        ),
        _queue_notification(
            db,
            task,
            new_assignee,
            NOTIFICATION_REASSIGNED_TO,
            assigned_subject,
            assigned_body,
            assignment_notification_key(task),
        ),
    ]


def reassign_task(
    db: Session,
    task_id: str,
    actor: TaskActor,
    new_assignee_id: str,
    reason: str | None = None,
) -> TaskChangeResult:
    task = _load_task(db, task_id)
    _require_manager_scope(db, task, actor, ACTION_REASSIGN, "reassign tasks")

    new_assignee = _load_assignee(db, new_assignee_id)
    old_assignee = db.get(Employee, task.assignee_id)
    if old_assignee is None:
        raise ValueError("The task's current assignee no longer exists.")
    if new_assignee.id == old_assignee.id:
        raise ValueError(f"This task is already assigned to {new_assignee.name}.")
    if _pending_request(db, task.id) is not None:
        raise TaskChangeConflict(
            "A reassignment for this task is already awaiting approval."
        )

    reason_text = f" Reason: {reason.strip()}" if reason and reason.strip() else ""
    cross_team = not _same_team(new_assignee.team, actor.team)

    if actor.role == ROLE_MANAGER and cross_team:
        request = TaskReassignmentRequest(
            id=str(uuid.uuid4()),
            task_id=task.id,
            from_assignee_id=old_assignee.id,
            to_assignee_id=new_assignee.id,
            requested_by=actor.id,
            requester_role=actor.role,
            requester_team=actor.team,
            reason=reason.strip() if reason and reason.strip() else None,
            status=TASK_REASSIGNMENT_STATUS_PENDING,
            created_at=_now(),
        )
        db.add(request)
        _audit(
            db,
            task,
            actor,
            ACTION_REASSIGN,
            TASK_AUDIT_OUTCOME_PENDING_APPROVAL,
            f"Requested reassignment from {old_assignee.name} to {new_assignee.name} "
            f"({_team_label(new_assignee.team)}), outside the requester's team "
            f"({_team_label(actor.team)}).{reason_text}",
        )
        db.commit()
        db.refresh(task)
        from app.notification_service import notify_task_reassignment_approvers

        notify_task_reassignment_approvers(db, request)
        return TaskChangeResult(
            outcome=TASK_AUDIT_OUTCOME_PENDING_APPROVAL,
            message=(
                f"{new_assignee.name} is in {_team_label(new_assignee.team)}, outside "
                f"your team, so this reassignment was sent to HR / Operations for "
                f"approval. {old_assignee.name} keeps the task until it is approved."
            ),
            task=task,
            reassignment_request=request,
            notifications=[],
        )

    notifications = _apply_reassignment(
        db,
        task,
        old_assignee,
        new_assignee,
        actor,
        f"Reassigned from {old_assignee.name} to {new_assignee.name}.{reason_text}",
    )
    db.commit()
    _deliver_notifications(db, notifications)
    db.refresh(task)
    return TaskChangeResult(
        outcome=TASK_AUDIT_OUTCOME_APPLIED,
        message=(
            f"Task reassigned from {old_assignee.name} to {new_assignee.name}. "
            "Both have been notified."
        ),
        task=task,
        notifications=notifications,
    )


def decide_reassignment_request(
    db: Session,
    request_id: str,
    actor: TaskActor,
    decision: str,
    note: str | None = None,
) -> TaskChangeResult:
    request = db.get(TaskReassignmentRequest, request_id)
    if request is None:
        raise LookupError("Reassignment request not found.")
    task = _load_task(db, request.task_id)

    if actor.role not in APPROVER_ROLES:
        _deny(
            db,
            task,
            actor,
            ACTION_DECIDE_REASSIGNMENT,
            "Only HR / Operations or an Admin can approve or reject cross-team "
            "reassignments.",
        )
    if request.status != TASK_REASSIGNMENT_STATUS_PENDING:
        raise TaskChangeConflict(f"This request has already been {request.status}.")

    old_assignee = db.get(Employee, request.from_assignee_id)
    new_assignee = db.get(Employee, request.to_assignee_id)
    note_text = f" Note: {note.strip()}" if note and note.strip() else ""
    request.decided_by = actor.id
    request.decided_at = _now()
    request.decision_note = note.strip() if note and note.strip() else None

    if decision == "reject":
        request.status = TASK_REASSIGNMENT_STATUS_REJECTED
        _audit(
            db,
            task,
            actor,
            ACTION_DECIDE_REASSIGNMENT,
            TASK_AUDIT_OUTCOME_REJECTED,
            f"Rejected reassignment from {old_assignee.name} to {new_assignee.name} "
            f"requested by {request.requested_by}.{note_text}",
        )
        db.commit()
        from app.notification_service import notify_task_reassignment_decision

        notify_task_reassignment_decision(db, request, outcome="rejected")
        db.refresh(task)
        return TaskChangeResult(
            outcome=TASK_AUDIT_OUTCOME_APPLIED,
            message=f"Request rejected. {old_assignee.name} keeps the task.",
            task=task,
            reassignment_request=request,
            notifications=[],
        )

    if task.assignee_id != request.from_assignee_id:
        raise TaskChangeConflict(
            "The task was reassigned after this request was made; reject it and "
            "submit a new request if still needed."
        )
    if new_assignee is None or not new_assignee.is_active:
        raise ValueError("The requested assignee is no longer an active employee.")

    request.status = TASK_REASSIGNMENT_STATUS_APPROVED
    notifications = _apply_reassignment(
        db,
        task,
        old_assignee,
        new_assignee,
        actor,
        f"Approved cross-team reassignment from {old_assignee.name} to "
        f"{new_assignee.name} requested by {request.requested_by}.{note_text}",
    )
    db.commit()
    _deliver_notifications(db, notifications)
    from app.notification_service import notify_task_reassignment_decision

    notify_task_reassignment_decision(db, request, outcome="approved")
    db.refresh(task)
    return TaskChangeResult(
        outcome=TASK_AUDIT_OUTCOME_APPLIED,
        message=(
            f"Approved. Task reassigned from {old_assignee.name} to "
            f"{new_assignee.name}; both have been notified."
        ),
        task=task,
        reassignment_request=request,
        notifications=notifications,
    )


def change_task_due_date(
    db: Session,
    task_id: str,
    actor: TaskActor,
    due_at: datetime,
    reason: str | None = None,
) -> TaskChangeResult:
    task = _load_task(db, task_id)
    _require_manager_scope(db, task, actor, ACTION_CHANGE_DUE_DATE, "change due dates")

    new_due = _as_utc(due_at)
    old_due = _as_utc(task.due_at)
    if new_due == old_due:
        raise ValueError("The task already has this due date.")
    if new_due <= _now():
        raise ValueError("The new due date must be in the future.")

    assignee = db.get(Employee, task.assignee_id)
    if assignee is None:
        raise ValueError("The task's assignee no longer exists.")

    reason_text = f" Reason: {reason.strip()}" if reason and reason.strip() else ""
    task.due_at = new_due
    task.updated_at = _now()
    task.calendar_sequence = (task.calendar_sequence or 0) + 1
    _audit(
        db,
        task,
        actor,
        ACTION_CHANGE_DUE_DATE,
        TASK_AUDIT_OUTCOME_APPLIED,
        f"Due date changed from {_format_due(old_due)} to {_format_due(new_due)}."
        f"{reason_text}",
    )
    notification = _queue_notification(
        db,
        task,
        assignee,
        NOTIFICATION_DUE_DATE_CHANGED,
        f"Due date changed: {task.title}",
        f"Hello {assignee.name},\n\n"
        f"The due date for \"{task.title}\" has changed.\n"
        f"Previous due date: {_format_due(old_due)}\n"
        f"New due date: {_format_due(new_due)}\n"
        f"{('Reason: ' + reason.strip() + chr(10)) if reason and reason.strip() else ''}"
        "Your task calendar feed has been updated with the new deadline.\n",
        f"due-date-changed:{task.id}:s{task.calendar_sequence}:{assignee.id}",
    )
    db.commit()
    _deliver_notifications(db, [notification])
    db.refresh(task)
    return TaskChangeResult(
        outcome=TASK_AUDIT_OUTCOME_APPLIED,
        message=(
            f"Due date changed to {_format_due(new_due)}. The calendar entry was "
            f"updated and {assignee.name} has been notified."
        ),
        task=task,
        notifications=[notification],
    )


def set_task_status(db: Session, task_id: str, actor: TaskActor, new_status: str) -> TaskChangeResult:
    """Mark a task Completed (or reopen it). Completing stops reminders and escalations.

    The assignee may change their own task; otherwise the manager-scope rules apply.
    """
    if new_status not in (TASK_STATUS_PENDING, TASK_STATUS_COMPLETED):
        raise ValueError("Status must be 'pending' or 'completed'.")
    task = _load_task(db, task_id)
    assignee = db.get(Employee, task.assignee_id)
    verb = "complete tasks" if new_status == TASK_STATUS_COMPLETED else "reopen tasks"
    is_assignee = bool(
        actor.email and assignee and actor.email.strip().casefold() == assignee.email.casefold()
    )
    if not is_assignee:
        _require_manager_scope(db, task, actor, ACTION_CHANGE_STATUS, verb)

    if task.status == new_status:
        label = "completed" if new_status == TASK_STATUS_COMPLETED else "open"
        return TaskChangeResult(outcome=TASK_AUDIT_OUTCOME_APPLIED, message=f"The task is already {label}.", task=task)

    now = _now()
    task.status = new_status
    task.completed_at = now if new_status == TASK_STATUS_COMPLETED else None
    task.updated_at = now
    message = (
        "Task marked as Completed. No further reminders will be sent."
        if new_status == TASK_STATUS_COMPLETED
        else "Task reopened. Reminders will resume based on its due date."
    )
    _audit(db, task, actor, ACTION_CHANGE_STATUS, TASK_AUDIT_OUTCOME_APPLIED,
           f"Status changed to {new_status}.")
    db.commit()
    db.refresh(task)
    return TaskChangeResult(outcome=TASK_AUDIT_OUTCOME_APPLIED, message=message, task=task)
