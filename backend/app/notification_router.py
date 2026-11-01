"""Authenticated inbox and notification-preference APIs."""

from datetime import datetime, time, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    EmailFailureNotification,
    Employee,
    NotificationPreference,
    UserNotification,
)
from app.notification_service import (
    mark_notification_read,
    quiet_hours_end,
    sync_task_notification_delivery,
    sync_email_failure_notification,
)

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class NotificationResponse(BaseModel):
    id: str
    event_type: str
    summary: str
    entity_type: str | None
    entity_id: str | None
    entity_url: str | None
    in_app_status: str
    email_status: str
    email_attempts: int
    email_next_attempt_at: datetime | None
    email_error: str | None
    created_at: datetime
    read_at: datetime | None


class UnreadCountResponse(BaseModel):
    unread_count: int


class NotificationPreferencesResponse(BaseModel):
    in_app_enabled: bool
    email_enabled: bool
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    timezone_name: str
    timezone_fallback: bool


class NotificationPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_app_enabled: bool
    email_enabled: bool
    quiet_hours_start: str | None = Field(default=None, max_length=5)
    quiet_hours_end: str | None = Field(default=None, max_length=5)
    timezone_name: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_preferences(self):
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            raise ValueError("Set both quiet-hours times, or leave both empty.")
        if self.quiet_hours_start is not None:
            for value in (self.quiet_hours_start, self.quiet_hours_end):
                if not value or not _TIME_PATTERN.fullmatch(value):
                    raise ValueError("Quiet-hours values must use 24-hour HH:MM format.")
                time.fromisoformat(value)
            if self.quiet_hours_start == self.quiet_hours_end:
                raise ValueError("Quiet-hours start and end must be different.")
        if self.timezone_name:
            try:
                ZoneInfo(self.timezone_name)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Use a valid IANA time-zone name.") from exc
        return self


def _preference_response(
    employee: Employee,
    preferences: NotificationPreference | None,
) -> NotificationPreferencesResponse:
    return NotificationPreferencesResponse(
        in_app_enabled=preferences.in_app_enabled if preferences else True,
        email_enabled=preferences.email_enabled if preferences else True,
        quiet_hours_start=preferences.quiet_hours_start if preferences else None,
        quiet_hours_end=preferences.quiet_hours_end if preferences else None,
        timezone_name=employee.timezone_name or "UTC",
        timezone_fallback=not bool(employee.timezone_name),
    )


@router.get("", response_model=list[NotificationResponse])
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    query = db.query(UserNotification).filter(
        UserNotification.recipient_employee_id == principal.user_id,
        UserNotification.in_app_status == "delivered",
    )
    if unread_only:
        query = query.filter(UserNotification.read_at.is_(None))
    return query.order_by(UserNotification.created_at.desc()).limit(limit).all()


@router.get("/unread-count", response_model=UnreadCountResponse)
def get_unread_count(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    count = (
        db.query(UserNotification.id)
        .filter(
            UserNotification.recipient_employee_id == principal.user_id,
            UserNotification.in_app_status == "delivered",
            UserNotification.read_at.is_(None),
        )
        .count()
    )
    return UnreadCountResponse(unread_count=count)


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
def read_notification(
    notification_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    notification = mark_notification_read(db, principal.user_id, notification_id)
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found.",
        )
    return notification


@router.get("/preferences", response_model=NotificationPreferencesResponse)
def get_preferences(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, principal.user_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found.")
    return _preference_response(
        employee,
        db.get(NotificationPreference, principal.user_id),
    )


@router.put("/preferences", response_model=NotificationPreferencesResponse)
def update_preferences(
    payload: NotificationPreferencesUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, principal.user_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found.")
    now = datetime.now(timezone.utc)
    employee.timezone_name = payload.timezone_name or None
    preferences = db.get(NotificationPreference, principal.user_id)
    if preferences is None:
        preferences = NotificationPreference(employee_id=principal.user_id)
        db.add(preferences)
    preferences.in_app_enabled = payload.in_app_enabled
    preferences.email_enabled = payload.email_enabled
    preferences.quiet_hours_start = payload.quiet_hours_start
    preferences.quiet_hours_end = payload.quiet_hours_end
    preferences.updated_at = now

    pending = (
        db.query(UserNotification)
        .filter(
            UserNotification.recipient_employee_id == principal.user_id,
            UserNotification.email_status.in_(("pending", "deferred", "retrying")),
        )
        .all()
    )
    for notification in pending:
        if not payload.email_enabled:
            notification.email_status = "disabled"
            notification.email_error = "E-mail delivery is disabled by the recipient's preferences."
            notification.email_next_attempt_at = None
            sync_task_notification_delivery(db, notification)
            if notification.email_failure_notification_id:
                failure = db.get(EmailFailureNotification, notification.email_failure_notification_id)
                if failure is not None:
                    failure.status = "recorded"
                    failure.error = notification.email_error
                    failure.next_attempt_at = None
        elif notification.email_status == "deferred":
            end_at = quiet_hours_end(
                now,
                payload.quiet_hours_start,
                payload.quiet_hours_end,
                payload.timezone_name,
            )
            notification.email_status = "deferred" if end_at else "pending"
            notification.email_next_attempt_at = end_at or now
            notification.email_error = (
                f"Deferred until quiet hours end at {end_at.isoformat()}." if end_at else None
            )
    db.commit()
    for notification in pending:
        if notification.email_failure_notification_id:
            sync_email_failure_notification(db, notification.email_failure_notification_id)
    db.refresh(employee)
    db.refresh(preferences)
    return _preference_response(employee, preferences)
