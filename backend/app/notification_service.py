"""Recipient-scoped in-app notifications and deferred/retried e-mail delivery."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import logging
import smtplib
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.plain_errors import is_sign_in_failure, plain_error
from app.meeting.models import (
    AssistantActionApproval,
    AuthAccount,
    EmailDraft,
    EmailDraftEvent,
    Employee,
    NotificationPreference,
    Task,
    TaskNotification,
    TaskReassignmentRequest,
    UserNotification,
)

logger = logging.getLogger(__name__)

UTC_FALLBACK = "UTC"
EMAIL_READY_STATUSES = ("pending", "deferred", "retrying")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _preferences(db: Session, employee_id: str) -> NotificationPreference | None:
    return db.get(NotificationPreference, employee_id)


def email_delivery_policy(
    db: Session,
    recipient_email: str,
    now: datetime | None = None,
) -> tuple[str, datetime | None, str | None]:
    """Return whether an existing email workflow may send, defer, or is disabled."""
    employee = (
        db.query(Employee)
        .filter(Employee.email.ilike(recipient_email.strip()))
        .one_or_none()
    )
    if employee is None:
        return "pending", None, None
    preferences = _preferences(db, employee.id)
    if preferences and not preferences.email_enabled:
        return "disabled", None, "E-mail delivery is disabled by the recipient's preferences."
    try:
        end_at = quiet_hours_end(
            now or _now(),
            preferences.quiet_hours_start if preferences else None,
            preferences.quiet_hours_end if preferences else None,
            employee.timezone_name,
        )
    except (ValueError, KeyError) as exc:
        return "disabled", None, f"Quiet-hours timezone could not be resolved: {exc}"
    if end_at:
        return "deferred", end_at, _channel_error("deferred", end_at, None)
    return "pending", None, None


def _in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    current = now.timetz().replace(tzinfo=None)
    quiet_start = time.fromisoformat(start)
    quiet_end = time.fromisoformat(end)
    if quiet_start < quiet_end:
        return quiet_start <= current < quiet_end
    return current >= quiet_start or current < quiet_end


def quiet_hours_end(
    now: datetime,
    start: str | None,
    end: str | None,
    timezone_name: str | None,
) -> datetime | None:
    """Return the next real instant outside the local quiet interval.

    Scanning UTC instants rather than constructing a local wall-clock endpoint
    handles both nonexistent and repeated local times at DST transitions.
    """
    if not start or not end:
        return None
    try:
        zone = ZoneInfo(timezone_name or UTC_FALLBACK)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "Invalid employee timezone %r; notification quiet hours use UTC.",
            timezone_name,
        )
        zone = ZoneInfo(UTC_FALLBACK)
    now = _as_utc(now)
    local_now = now.astimezone(zone)
    if not _in_quiet_hours(local_now, start, end):
        return None

    inside = now
    outside = now
    for _ in range(26 * 60):
        outside += timedelta(minutes=1)
        if not _in_quiet_hours(outside.astimezone(zone), start, end):
            break
        inside = outside
    else:
        raise ValueError("Quiet hours did not end within the supported 26-hour window.")

    low, high = int(inside.timestamp()), int(outside.timestamp())
    while high - low > 1:
        middle = (low + high) // 2
        candidate = datetime.fromtimestamp(middle, timezone.utc)
        if _in_quiet_hours(candidate.astimezone(zone), start, end):
            low = middle
        else:
            high = middle
    return datetime.fromtimestamp(high, timezone.utc)


def _channel_error(status: str, next_attempt_at: datetime | None, email_error: str | None) -> str | None:
    if status == "deferred" and next_attempt_at:
        return f"Deferred until quiet hours end at {_as_utc(next_attempt_at).isoformat()}."
    if status == "disabled":
        return "E-mail delivery is disabled by the recipient's preferences."
    return email_error


def _sync_task_delivery(db: Session, notification: UserNotification) -> None:
    if not notification.task_notification_id:
        return
    legacy = db.get(TaskNotification, notification.task_notification_id)
    if legacy is None:
        return
    if notification.email_status == "sent":
        legacy.status = "sent"
        legacy.error = None
    elif notification.email_status in ("pending", "deferred", "sending", "retrying"):
        legacy.status = "pending" if notification.email_status != "retrying" else "failed"
        legacy.error = _channel_error(
            notification.email_status,
            notification.email_next_attempt_at,
            notification.email_error,
        )
    elif notification.email_status in ("failed", "disabled"):
        legacy.status = "failed"
        legacy.error = _channel_error(
            notification.email_status,
            notification.email_next_attempt_at,
            notification.email_error,
        )
    task = db.get(Task, legacy.task_id)
    if task is not None and task.assignee_id == legacy.recipient_employee_id:
        from app.task_service import assignment_notification_key

        if assignment_notification_key(task) == legacy.idempotency_key:
            task.assignment_email_status = legacy.status
            task.assignment_email_error = legacy.error


def sync_task_notification_delivery(db: Session, notification: UserNotification) -> None:
    _sync_task_delivery(db, notification)


def create_notification(
    db: Session,
    *,
    recipient_id: str,
    event_type: str,
    summary: str,
    idempotency_key: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    entity_url: str | None = None,
    email_subject: str | None = None,
    email_body: str | None = None,
    task_notification_id: str | None = None,
    email_failure_notification_id: str | None = None,
    external_email_status: str | None = None,
    external_email_error: str | None = None,
    external_email_next_attempt_at: datetime | None = None,
    now: datetime | None = None,
) -> UserNotification:
    """Create one inbox item per recipient; uniqueness makes event retries safe."""
    existing = (
        db.query(UserNotification)
        .filter(UserNotification.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if existing is not None:
        return existing
    employee = db.get(Employee, recipient_id)
    if employee is None:
        raise ValueError(f"Notification recipient {recipient_id} does not exist.")
    preferences = _preferences(db, recipient_id)
    created_at = _as_utc(now or _now())
    in_app_enabled = preferences.in_app_enabled if preferences else True
    email_enabled = preferences.email_enabled if preferences else True
    status = "disabled"
    next_attempt_at = None
    error = external_email_error
    if external_email_status is not None:
        status = external_email_status
        next_attempt_at = external_email_next_attempt_at
    elif email_subject and email_body and email_enabled:
        defer_until = quiet_hours_end(
            created_at,
            preferences.quiet_hours_start if preferences else None,
            preferences.quiet_hours_end if preferences else None,
            employee.timezone_name,
        )
        if defer_until is None:
            status = "pending"
            next_attempt_at = created_at
        else:
            status = "deferred"
            next_attempt_at = defer_until
    notification = UserNotification(
        id=str(uuid.uuid4()),
        recipient_employee_id=recipient_id,
        event_type=event_type,
        summary=summary,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_url=entity_url,
        idempotency_key=idempotency_key,
        in_app_status="delivered" if in_app_enabled else "disabled",
        email_status=status,
        email_subject=email_subject,
        email_body=email_body,
        email_next_attempt_at=next_attempt_at,
        email_error=error,
        task_notification_id=task_notification_id,
        email_failure_notification_id=email_failure_notification_id,
        created_at=created_at,
    )
    try:
        with db.begin_nested():
            db.add(notification)
            db.flush()
    except IntegrityError:
        existing = (
            db.query(UserNotification)
            .filter(UserNotification.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is None:
            raise
        return existing
    _sync_task_delivery(db, notification)
    return notification


def create_task_notification(
    db: Session,
    task_notification: TaskNotification,
    *,
    event_type: str,
    summary: str,
    now: datetime | None = None,
) -> UserNotification:
    """Mirror an existing task e-mail record into the unified inbox."""
    return create_notification(
        db,
        recipient_id=task_notification.recipient_employee_id,
        event_type=event_type,
        summary=summary,
        idempotency_key=f"task-event:{task_notification.idempotency_key or task_notification.id}",
        entity_type="task",
        entity_id=task_notification.task_id,
        entity_url=f"/tasks?task_id={task_notification.task_id}",
        now=now,
        email_subject=task_notification.subject,
        email_body=task_notification.body,
        task_notification_id=task_notification.id,
        external_email_status=(
            task_notification.status
            if task_notification.status in ("sent", "failed")
            else None
        ),
        external_email_error=task_notification.error,
    )


def _employees_with_roles(db: Session, roles: set[str]) -> list[Employee]:
    return (
        db.query(Employee)
        .join(AuthAccount, AuthAccount.employee_id == Employee.id)
        .filter(
            Employee.is_active.is_(True),
            AuthAccount.is_enabled.is_(True),
            AuthAccount.role.in_(roles),
        )
        .all()
    )


def _create_and_deliver(
    db: Session,
    *,
    recipient: Employee,
    event_type: str,
    summary: str,
    idempotency_key: str,
    entity_type: str,
    entity_id: str,
    entity_url: str,
    email_subject: str,
    email_body: str,
) -> UserNotification:
    notification = create_notification(
        db,
        recipient_id=recipient.id,
        event_type=event_type,
        summary=summary,
        idempotency_key=idempotency_key,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_url=entity_url,
        email_subject=email_subject,
        email_body=email_body,
    )
    db.commit()
    if notification.email_status == "pending":
        deliver_notification(db, notification.id)
    return notification


def notify_task_reassignment_approvers(
    db: Session,
    request: TaskReassignmentRequest,
) -> None:
    """Notify authenticated HR/Operations and Admin approvers of a pending move."""
    task = db.get(Task, request.task_id)
    if task is None:
        return
    title = f"Approval required: reassign {task.title}"
    recipients = _employees_with_roles(db, {"hr_operations", "admin"})
    for recipient in recipients:
        _create_and_deliver(
            db,
            recipient=recipient,
            event_type="approval_request",
            summary=title,
            idempotency_key=f"task-reassignment-approval:{request.id}:{recipient.id}",
            entity_type="task_reassignment",
            entity_id=request.id,
            entity_url="/approvals",
            email_subject=title,
            email_body=(
                f"A cross-team task reassignment needs your decision.\n\n"
                f"Task: {task.title}\n"
                f"Requested by: {request.requested_by}\n"
                f"Reason: {request.reason or 'Not provided'}\n"
                "Open Approvals in the Office Operations app to review."
            ),
        )


def notify_task_reassignment_decision(
    db: Session,
    request: TaskReassignmentRequest,
    *,
    outcome: str,
) -> None:
    requester = db.get(Employee, request.requested_by)
    task = db.get(Task, request.task_id)
    if requester is None or not requester.is_active or task is None:
        return
    summary = f"Task reassignment {outcome}: {task.title}"
    _create_and_deliver(
        db,
        recipient=requester,
        event_type="approval_decision",
        summary=summary,
        idempotency_key=f"task-reassignment-decision:{request.id}:{outcome}:{requester.id}",
        entity_type="task_reassignment",
        entity_id=request.id,
        entity_url="/approvals",
        email_subject=summary,
        email_body=f"{summary}\n\nOpen Approvals in the Office Operations app for details.",
    )


def notify_assistant_approval_request(
    db: Session,
    approval: AssistantActionApproval,
) -> None:
    if approval.approver_kind == "requester":
        requester = db.get(Employee, approval.requester_id)
        recipients = [requester] if requester and requester.is_active else []
    else:
        recipients = _employees_with_roles(db, {"hr_operations", "admin"})
        if approval.requester_role == "employee" and approval.requester_team:
            managers = (
                db.query(Employee)
                .join(AuthAccount, AuthAccount.employee_id == Employee.id)
                .filter(
                    Employee.is_active.is_(True),
                    AuthAccount.is_enabled.is_(True),
                    AuthAccount.role == "manager",
                    Employee.team == approval.requester_team,
                )
                .all()
            )
            recipients.extend(managers)
    deduplicated = {recipient.id: recipient for recipient in recipients}
    for recipient in deduplicated.values():
        summary = f"Assistant action needs approval: {approval.summary}"
        _create_and_deliver(
            db,
            recipient=recipient,
            event_type="approval_request",
            summary=summary,
            idempotency_key=f"assistant-approval:{approval.id}:{recipient.id}",
            entity_type="assistant_approval",
            entity_id=approval.id,
            entity_url=(
                "/my-approvals"
                if approval.approver_kind == "requester"
                else "/approvals"
            ),
            email_subject="AI assistant action approval required",
            email_body=(
                f"{approval.summary}\n\n"
                f"Requested by: {approval.requester_id}\n"
                "Open Approvals in the Office Operations app to review."
            ),
        )


def notify_assistant_approval_decision(
    db: Session,
    approval: AssistantActionApproval,
    *,
    outcome: str,
) -> None:
    requester = db.get(Employee, approval.requester_id)
    if requester is None or not requester.is_active:
        return
    summary = f"Assistant action {outcome}: {approval.summary}"
    _create_and_deliver(
        db,
        recipient=requester,
        event_type="workflow_failure" if outcome == "failed" else "approval_decision",
        summary=summary,
        idempotency_key=f"assistant-approval-decision:{approval.id}:{outcome}:{requester.id}",
        entity_type="assistant_approval",
        entity_id=approval.id,
        entity_url=(
            "/my-approvals"
            if approval.requester_role == "employee"
            else "/approvals"
        ),
        email_subject="AI assistant approval update",
        email_body=f"{summary}\n\nOpen Approvals in the Office Operations app for details.",
    )


def notify_email_approval_request(db: Session, draft: EmailDraft) -> None:
    sequence = (
        db.query(func.max(EmailDraftEvent.sequence))
        .filter(
            EmailDraftEvent.draft_id == draft.id,
            EmailDraftEvent.to_status == "approval",
        )
        .scalar()
    )
    if sequence is None:
        raise ValueError(f"Approval transition for e-mail draft {draft.id} was not recorded.")
    recipients = [
        employee
        for employee in _employees_with_roles(db, {"manager", "hr_operations", "admin"})
        if employee.id != draft.created_by_id
    ]
    summary = f"E-mail approval required: {draft.subject or '(no subject)'}"
    for recipient in recipients:
        _create_and_deliver(
            db,
            recipient=recipient,
            event_type="approval_request",
            summary=summary,
            idempotency_key=f"email-draft-approval:{draft.id}:{sequence}:{recipient.id}",
            entity_type="email_draft",
            entity_id=draft.id,
            entity_url="/approvals",
            email_subject=summary,
            email_body=(
                f"{draft.created_by_name} submitted an e-mail for approval.\n\n"
                f"Subject: {draft.subject or '(no subject)'}\n"
                "Open Approvals in the Office Operations app to review."
            ),
        )


def notify_email_approval_decision(db: Session, draft: EmailDraft) -> None:
    requester = db.get(Employee, draft.created_by_id)
    if requester is None or not requester.is_active:
        return
    sequence = (
        db.query(func.max(EmailDraftEvent.sequence))
        .filter(
            EmailDraftEvent.draft_id == draft.id,
            EmailDraftEvent.to_status == "draft",
        )
        .scalar()
    )
    if sequence is None:
        raise ValueError(f"Approval decision for e-mail draft {draft.id} was not recorded.")
    outcome = draft.approval_decision or "updated"
    summary = f"E-mail approval {outcome}: {draft.subject or '(no subject)'}"
    _create_and_deliver(
        db,
        recipient=requester,
        event_type="approval_decision",
        summary=summary,
        idempotency_key=f"email-draft-decision:{draft.id}:{sequence}:{requester.id}",
        entity_type="email_draft",
        entity_id=draft.id,
        entity_url="/chat",
        email_subject=summary,
        email_body=f"{summary}\n\nOpen the AI Assistant in the Office Operations app for details.",
    )


def workflow_alert_recipients(db: Session) -> list[Employee]:
    """Who is told when a workflow run is dead-lettered (Story 8.4): the active
    administrators, or HR / Operations when no administrator account is active."""
    return _employees_with_roles(db, {"admin"}) or _employees_with_roles(
        db, {"hr_operations"}
    )


def notify_workflow_failure(
    db: Session,
    *,
    recipient: Employee,
    summary: str,
    idempotency_key: str,
    entity_type: str,
    entity_id: str,
    entity_url: str,
) -> UserNotification:
    return _create_and_deliver(
        db,
        recipient=recipient,
        event_type="workflow_failure",
        summary=summary,
        idempotency_key=idempotency_key,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_url=entity_url,
        email_subject="Office Operations workflow failure",
        email_body=f"{summary}\n\nOpen the linked item in the Office Operations app for details.",
    )


def record_email_draft_failure(
    db: Session,
    draft,
    failure_notifications: list,
) -> None:
    """Expose existing per-channel email-failure records in each account's inbox."""
    by_email = {
        row.recipient_email.strip().casefold(): row
        for row in failure_notifications
        if row.recipient_email
    }
    candidates = (
        db.query(Employee, AuthAccount.role)
        .join(AuthAccount, AuthAccount.employee_id == Employee.id)
        .filter(Employee.is_active.is_(True), AuthAccount.is_enabled.is_(True))
        .all()
    )
    sequence = failure_notifications[0].failure_event_sequence if failure_notifications else 0
    for employee, role in candidates:
        failure = by_email.get(employee.email.strip().casefold())
        is_requester = employee.id == draft.created_by_id
        if failure is None and not is_requester and role != "admin":
            continue
        if failure is not None:
            status_by_failure = {
                "pending": (
                    "deferred"
                    if failure.next_attempt_at and _as_utc(failure.next_attempt_at) > _now()
                    else "pending"
                ),
                "sent": "sent",
                "failed": "retrying" if failure.next_attempt_at else "failed",
                "recorded": "disabled",
            }
            external_status = status_by_failure.get(failure.status, "failed")
            failure_id = failure.id
            error = failure.error
            next_attempt = failure.next_attempt_at
        else:
            external_status = "disabled"
            failure_id = None
            error = None
            next_attempt = None
        notification = create_notification(
            db,
            recipient_id=employee.id,
            event_type="workflow_failure",
            summary=f"E-mail delivery failed: {draft.subject or '(no subject)'}",
            idempotency_key=f"email-draft-failure:{draft.id}:{sequence}:{employee.id}",
            entity_type="email_draft",
            entity_id=draft.id,
            entity_url="/chat" if is_requester else "/dashboard",
            email_failure_notification_id=failure_id,
            external_email_status=external_status,
            external_email_error=error,
            external_email_next_attempt_at=next_attempt,
        )
        if failure_id is not None:
            _sync_failure_delivery(db, notification)
    db.commit()


def _sync_failure_delivery(db: Session, notification: UserNotification) -> None:
    if not notification.email_failure_notification_id:
        return
    from app.meeting.models import EmailFailureNotification

    failure = db.get(EmailFailureNotification, notification.email_failure_notification_id)
    if failure is None:
        return
    failure_status = failure.status
    if failure_status == "sent":
        notification.email_status = "sent"
        notification.email_sent_at = failure.sent_at
        notification.email_error = None
        notification.email_next_attempt_at = None
    elif failure_status == "pending":
        notification.email_status = (
            "deferred"
            if failure.next_attempt_at and _as_utc(failure.next_attempt_at) > _now()
            else "pending"
        )
        notification.email_next_attempt_at = failure.next_attempt_at
        notification.email_error = failure.error
    elif failure_status == "failed":
        notification.email_status = "retrying" if failure.next_attempt_at else "failed"
        notification.email_error = failure.error
        notification.email_next_attempt_at = failure.next_attempt_at
    else:
        notification.email_status = "disabled"
    notification.email_attempts = failure.attempts
    notification.email_provider = failure.provider


def sync_email_failure_notification(db: Session, failure_id: str) -> None:
    notifications = (
        db.query(UserNotification)
        .filter(UserNotification.email_failure_notification_id == failure_id)
        .all()
    )
    for notification in notifications:
        _sync_failure_delivery(db, notification)
    if notifications:
        db.commit()


def _claim_notification(db: Session, notification: UserNotification, now: datetime) -> bool:
    result = db.execute(
        update(UserNotification)
        .where(
            UserNotification.id == notification.id,
            UserNotification.email_status.in_(EMAIL_READY_STATUSES),
            or_(
                UserNotification.email_next_attempt_at.is_(None),
                UserNotification.email_next_attempt_at <= now,
            ),
        )
        .values(
            email_status="sending",
            email_started_at=now,
            email_next_attempt_at=None,
            email_attempts=UserNotification.email_attempts + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    db.refresh(notification)
    return True


def deliver_notification(
    db: Session,
    notification_id: str,
    *,
    now: datetime | None = None,
    force_retry: bool = False,
) -> UserNotification | None:
    """Deliver one due e-mail channel; failures retry without spawning notices."""
    now = _as_utc(now or _now())
    notification = db.get(UserNotification, notification_id)
    if notification is None:
        return None
    if notification.email_failure_notification_id is not None:
        return notification
    if force_retry and notification.email_status in ("failed", "retrying"):
        notification.email_status = "pending"
        notification.email_next_attempt_at = now
        notification.email_attempts = 0
        notification.email_error = None
        db.commit()
    if notification.email_status not in EMAIL_READY_STATUSES:
        return notification
    if notification.email_next_attempt_at and _as_utc(notification.email_next_attempt_at) > now:
        return notification
    employee = db.get(Employee, notification.recipient_employee_id)
    if employee is None or not employee.email:
        notification.email_status = "failed"
        notification.email_error = "The notification recipient has no e-mail address."
        notification.email_next_attempt_at = None
        _sync_task_delivery(db, notification)
        db.commit()
        return notification
    if notification.task_notification_id:
        legacy = db.get(TaskNotification, notification.task_notification_id)
        task = db.get(Task, legacy.task_id) if legacy else None
        if legacy and task:
            stale_assignment = (
                legacy.kind in ("assignment", "reassigned_to")
                and task.assignee_id != legacy.recipient_employee_id
            )
            obsolete_reminder = (
                legacy.kind in ("reminder_24h", "reminder_2h", "reminder_overdue", "escalation")
                and task.status == "completed"
            )
            if stale_assignment or obsolete_reminder:
                notification.email_status = "disabled"
                notification.email_error = (
                    "Delivery cancelled because the task assignment or reminder is no longer current."
                )
                notification.email_next_attempt_at = None
                _sync_task_delivery(db, notification)
                db.commit()
                return notification
    preferences = _preferences(db, employee.id)
    if preferences and not preferences.email_enabled:
        notification.email_status = "disabled"
        notification.email_next_attempt_at = None
        notification.email_error = "E-mail delivery is disabled by the recipient's preferences."
        _sync_task_delivery(db, notification)
        db.commit()
        return notification
    try:
        defer_until = quiet_hours_end(
            now,
            preferences.quiet_hours_start if preferences else None,
            preferences.quiet_hours_end if preferences else None,
            employee.timezone_name,
        )
    except (ValueError, KeyError) as exc:
        notification.email_status = "failed"
        notification.email_error = f"Invalid quiet-hours timezone: {exc}"[:1000]
        notification.email_next_attempt_at = None
        _sync_task_delivery(db, notification)
        db.commit()
        return notification
    if defer_until is not None:
        notification.email_status = "deferred"
        notification.email_next_attempt_at = defer_until
        notification.email_error = _channel_error("deferred", defer_until, None)
        _sync_task_delivery(db, notification)
        db.commit()
        return notification
    if not _claim_notification(db, notification, now):
        return db.get(UserNotification, notification_id)

    sign_in_rejected = False
    try:
        from app.task_service import send_email

        receipt = send_email(
            employee.email,
            notification.email_subject or notification.summary,
            notification.email_body or notification.summary,
        )
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        from app.email_delivery_service import max_attempts, retry_backoff_seconds

        notification.email_error = plain_error(exc, email=True)[:1000]
        # A rejected mail login cannot succeed on retry, and every retry is another
        # failed login that makes the mail provider lock the account for longer.
        sign_in_rejected = is_sign_in_failure(exc)
        if not sign_in_rejected and notification.email_attempts < max_attempts():
            notification.email_status = "retrying"
            notification.email_next_attempt_at = now + timedelta(
                seconds=retry_backoff_seconds(notification.email_attempts)
            )
        else:
            notification.email_status = "failed"
            notification.email_next_attempt_at = None
        logger.warning(
            "Notification e-mail failed operation=%s error_type=%s detail=%s",
            getattr(exc, "smtp_operation", "unknown"),
            type(exc).__name__,
            str(exc)[:300],
        )
    except Exception as exc:  # noqa: BLE001 - prevent a stuck Sending channel on unexpected provider errors
        from app.email_delivery_service import max_attempts, retry_backoff_seconds

        notification.email_error = plain_error(exc, email=True)[:1000]
        if notification.email_attempts < max_attempts():
            notification.email_status = "retrying"
            notification.email_next_attempt_at = now + timedelta(
                seconds=retry_backoff_seconds(notification.email_attempts)
            )
        else:
            notification.email_status = "failed"
            notification.email_next_attempt_at = None
        logger.error(
            "Unexpected notification e-mail failure operation=%s error_type=%s detail=%s",
            getattr(exc, "smtp_operation", "unknown"),
            type(exc).__name__,
            str(exc)[:300],
        )
    else:
        notification.email_status = "sent"
        notification.email_provider = getattr(receipt, "provider", None)
        notification.email_sent_at = _now()
        notification.email_error = None
        notification.email_next_attempt_at = None
    _sync_task_delivery(db, notification)
    db.commit()
    if sign_in_rejected:
        alert_mail_sign_in_rejected(db, now)
    return notification


MAIL_SIGN_IN_ALERT = (
    "E-mail sending is failing because the mail account's sign-in was rejected or blocked. "
    "Check the e-mail username and app password (SMTP_USERNAME and SMTP_PASSWORD in "
    "backend/.env), then restart the API. If the password is new and sign-in is still "
    "refused, Google may have blocked sign-ins for a while after repeated failures: wait "
    "about an hour. Until then no e-mail can be sent."
)


def alert_mail_sign_in_rejected(db: Session, now: datetime | None = None) -> int:
    """Tell the administrators, once a day, that the system's mail login is rejected.

    The alert is in-app only: sending is what is broken. Returns how many people were
    alerted. Never raises, so a problem here cannot hide the delivery failure itself.
    """
    day = _as_utc(now or _now()).date().isoformat()
    try:
        recipients = workflow_alert_recipients(db)
    except Exception:  # noqa: BLE001
        logger.exception("Could not look up who to tell that the mail sign-in was rejected")
        return 0
    alerted = 0
    for recipient in recipients:
        try:
            create_notification(
                db,
                recipient_id=recipient.id,
                event_type="workflow_failure",
                summary=MAIL_SIGN_IN_ALERT,
                idempotency_key=f"mail-sign-in-rejected:{day}:{recipient.id}",
                entity_type="system",
                entity_id="mail",
                entity_url="/notifications",
            )
            db.commit()
            alerted += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("Could not alert %s that the mail sign-in was rejected", recipient.id)
    return alerted


def process_due_notification_emails(
    db: Session,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    now = _as_utc(now or _now())
    stuck_before = now - timedelta(minutes=5)
    stuck = (
        db.query(UserNotification)
        .filter(
            UserNotification.email_status == "sending",
            UserNotification.email_started_at < stuck_before,
        )
        .all()
    )
    for notification in stuck:
        notification.email_status = "retrying"
        notification.email_next_attempt_at = now
        notification.email_error = "Delivery was interrupted and will be retried."
    if stuck:
        db.commit()
    due = (
        db.query(UserNotification.id)
        .filter(UserNotification.email_status.in_(EMAIL_READY_STATUSES))
        .filter(UserNotification.email_failure_notification_id.is_(None))
        .filter(
            (UserNotification.email_next_attempt_at.is_(None))
            | (UserNotification.email_next_attempt_at <= now)
        )
        .order_by(UserNotification.email_next_attempt_at, UserNotification.created_at)
        .limit(limit)
        .all()
    )
    attempted = 0
    for (notification_id,) in due:
        row = deliver_notification(db, notification_id, now=now)
        if row and row.email_status in ("sent", "retrying", "failed"):
            attempted += 1
    return attempted


def mark_notification_read(db: Session, employee_id: str, notification_id: str) -> UserNotification | None:
    notification = (
        db.query(UserNotification)
        .filter(
            UserNotification.id == notification_id,
            UserNotification.recipient_employee_id == employee_id,
            UserNotification.in_app_status == "delivered",
        )
        .one_or_none()
    )
    if notification is None:
        return None
    if notification.read_at is None:
        notification.read_at = _now()
        db.commit()
        db.refresh(notification)
    return notification
