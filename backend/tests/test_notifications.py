"""Story 8.1 notification inbox, channel delivery, and quiet-hours behavior."""

from datetime import datetime, timedelta, timezone
import logging
import smtplib
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_service import AuthenticatedPrincipal
from app.meeting.models import Base, Employee, NotificationPreference, UserNotification
from app.notification_router import (
    NotificationPreferencesUpdate,
    get_preferences,
    list_notifications,
    read_notification,
    update_preferences,
)
from app.notification_service import (
    create_notification,
    deliver_notification,
    process_due_notification_emails,
    quiet_hours_end,
)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _employee(db: Session, employee_id: str) -> Employee:
    employee = Employee(
        id=employee_id,
        name=f"Employee {employee_id}",
        email=f"{employee_id}@example.test",
        is_active=True,
    )
    db.add(employee)
    db.commit()
    return employee


def _principal(employee: Employee) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=employee.id,
        account_id=f"account-{employee.id}",
        email=employee.email,
        name=employee.name,
        role="employee",
        team=None,
        jti=f"session-{employee.id}",
    )


def test_notification_is_idempotent_and_email_waits_until_quiet_hours_end(
    db_session: Session,
    monkeypatch,
):
    employee = _employee(db_session, "quiet-user")
    employee.timezone_name = "UTC"
    db_session.add(
        NotificationPreference(
            employee_id=employee.id,
            in_app_enabled=True,
            email_enabled=True,
            quiet_hours_start="22:00",
            quiet_hours_end="06:00",
        )
    )
    db_session.commit()
    now = datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.notification_service._now", lambda: now)
    sent: list[str] = []
    monkeypatch.setattr("app.task_service.send_email", lambda address, *_: sent.append(address))

    notification = create_notification(
        db_session,
        recipient_id=employee.id,
        event_type="task_assignment",
        summary="New task assigned",
        idempotency_key="task-assignment:task-1:version-1:quiet-user",
        entity_type="task",
        entity_id="task-1",
        entity_url="/tasks?task_id=task-1",
        email_subject="New task",
        email_body="A task was assigned.",
    )
    duplicate = create_notification(
        db_session,
        recipient_id=employee.id,
        event_type="task_assignment",
        summary="New task assigned",
        idempotency_key="task-assignment:task-1:version-1:quiet-user",
        email_subject="New task",
        email_body="A task was assigned.",
    )
    db_session.commit()

    assert duplicate.id == notification.id
    assert notification.email_status == "deferred"
    assert notification.email_next_attempt_at == datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    assert db_session.query(UserNotification).count() == 1
    assert deliver_notification(db_session, notification.id, now=now).email_status == "deferred"
    assert sent == []

    delivered = deliver_notification(
        db_session,
        notification.id,
        now=datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc),
    )
    assert delivered.email_status == "sent"
    assert sent == [employee.email]


def test_smtp_send_disconnect_is_staged_and_notification_retry_waits_for_backoff(
    db_session: Session,
    monkeypatch,
    caplog,
):
    employee = _employee(db_session, "smtp-retry-user")
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.notification_service._now", lambda: now)
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "office@example.test")
    monkeypatch.setenv("SMTP_USE_STARTTLS", "true")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("FALLBACK_SMTP_HOST", raising=False)
    monkeypatch.setenv("EMAIL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("EMAIL_RETRY_BACKOFF_MINUTES", "1,5,15")
    send_attempts: list[int] = []

    class DisconnectingSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def starttls(self):
            pass

        def send_message(self, _message):
            send_attempts.append(1)
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

        def close(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", DisconnectingSMTP)
    notification = create_notification(
        db_session,
        recipient_id=employee.id,
        event_type="task_assignment",
        summary="New task assigned",
        idempotency_key="task-assignment:smtp-retry-user:1",
        email_subject="New task",
        email_body="A task was assigned.",
    )
    duplicate = create_notification(
        db_session,
        recipient_id=employee.id,
        event_type="task_assignment",
        summary="New task assigned",
        idempotency_key="task-assignment:smtp-retry-user:1",
        email_subject="New task",
        email_body="A task was assigned.",
    )
    assert duplicate.id == notification.id
    assert notification.recipient_employee_id == employee.id
    assert notification.event_type == "task_assignment"
    assert notification.in_app_status == "delivered"

    with caplog.at_level(logging.WARNING, logger="app.email_providers"):
        first_attempt = deliver_notification(db_session, notification.id, now=now)

    assert first_attempt.email_status == "retrying"
    assert first_attempt.email_attempts == 1
    assert first_attempt.email_next_attempt_at.replace(tzinfo=timezone.utc) == (
        now + timedelta(minutes=1)
    )
    assert "operation=sendmail/response handling" in caplog.text
    assert employee.email not in caplog.text
    assert db_session.query(UserNotification).count() == 1
    assert db_session.query(UserNotification).filter_by(event_type="workflow_failure").count() == 0

    assert process_due_notification_emails(db_session, now=now) == 0
    assert send_attempts == [1]

    retry_at = now + timedelta(minutes=1)
    assert process_due_notification_emails(db_session, now=retry_at) == 1
    db_session.refresh(notification)
    assert notification.email_status == "retrying"
    assert notification.email_attempts == 2
    assert notification.email_next_attempt_at.replace(tzinfo=timezone.utc) == (
        retry_at + timedelta(minutes=5)
    )
    assert process_due_notification_emails(db_session, now=retry_at) == 0
    assert send_attempts == [1, 1]
    assert db_session.query(UserNotification).count() == 1


def test_quiet_hours_end_handles_midnight_and_dst_transitions():
    crossing_midnight = quiet_hours_end(
        datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc),
        "22:00",
        "07:00",
        "Asia/Kolkata",
    )
    assert crossing_midnight == datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)

    spring_forward = quiet_hours_end(
        datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc),
        "01:00",
        "03:30",
        "America/New_York",
    )
    assert spring_forward == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)

    fall_back = quiet_hours_end(
        datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc),
        "01:30",
        "02:30",
        "America/New_York",
    )
    assert fall_back == datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)


def test_inbox_and_read_state_are_scoped_to_the_authenticated_employee(db_session: Session):
    first = _employee(db_session, "first-user")
    second = _employee(db_session, "second-user")
    item = UserNotification(
        id=str(uuid.uuid4()),
        recipient_employee_id=first.id,
        event_type="task_reminder",
        summary="Reminder",
        idempotency_key="reminder:first-user:1",
        in_app_status="delivered",
        email_status="disabled",
    )
    db_session.add(item)
    db_session.commit()

    assert [row.id for row in list_notifications(False, 50, _principal(first), db_session)] == [item.id]
    assert list_notifications(False, 50, _principal(second), db_session) == []
    with pytest.raises(HTTPException) as exc_info:
        read_notification(item.id, _principal(second), db_session)
    assert exc_info.value.status_code == 404


def test_preferences_update_only_the_authenticated_employee(db_session: Session):
    first = _employee(db_session, "settings-user")
    second = _employee(db_session, "other-settings-user")
    payload = NotificationPreferencesUpdate(
        in_app_enabled=False,
        email_enabled=True,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        timezone_name="America/Los_Angeles",
    )

    response = update_preferences(payload, _principal(first), db_session)

    assert response.timezone_name == "America/Los_Angeles"
    assert get_preferences(_principal(first), db_session).in_app_enabled is False
    assert db_session.get(NotificationPreference, second.id) is None
