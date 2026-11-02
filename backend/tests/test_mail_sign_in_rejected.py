"""A rejected mail login is the administrator's to fix; retrying only makes Google lock the account.

Every failed e-mail used to be retried, so one wrong app password produced dozens of
failed logins, then Google closed every connection. Nobody but the recipient of each
failed notice was told.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import smtplib
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import notification_service
from app.meeting.models import AuthAccount, Base, Employee, UserNotification
from app.notification_service import (
    MAIL_SIGN_IN_ALERT,
    alert_mail_sign_in_rejected,
    create_notification,
    deliver_notification,
)
from app.plain_errors import SIGN_IN_REJECTED


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _person(db: Session, employee_id: str, role: str = "employee", *, enabled: bool = True) -> Employee:
    employee = Employee(id=employee_id, name=f"Person {employee_id}", email=f"{employee_id}@example.test",
                        is_active=True)
    db.add(employee)
    db.add(AuthAccount(id=f"acc-{employee_id}", employee_id=employee_id,
                       email_normalized=employee.email, password_hash="x", role=role, is_enabled=enabled))
    db.commit()
    return employee


def _pending(db: Session, recipient: str = "worker") -> UserNotification:
    return create_notification(
        db, recipient_id=recipient, event_type="task_assignment", summary="New task assigned",
        idempotency_key=f"signin-{uuid.uuid4()}", email_subject="New task", email_body="A task was assigned.",
    )


def _failing_with(monkeypatch, error: Exception) -> list[int]:
    attempts: list[int] = []

    def send(*args, **kwargs):
        attempts.append(1)
        raise error

    monkeypatch.setattr("app.task_service.send_email", send)
    return attempts


def _alerts(db: Session) -> list[UserNotification]:
    db.expire_all()
    return db.query(UserNotification).filter_by(entity_type="system", entity_id="mail").all()


def test_a_rejected_login_is_not_retried(db_session, monkeypatch):
    _person(db_session, "worker")
    attempts = _failing_with(monkeypatch, smtplib.SMTPAuthenticationError(535, b"5.7.8 not accepted"))
    notification = _pending(db_session)

    result = deliver_notification(db_session, notification.id)

    assert result.email_status == "failed"
    assert result.email_attempts == 1 and result.email_next_attempt_at is None
    assert result.email_error == SIGN_IN_REJECTED
    assert attempts == [1], "the mail provider was contacted once, not once per retry"


@pytest.mark.parametrize("error", [
    smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
    OSError("[Errno 11001] getaddrinfo failed"),
    smtplib.SMTPDataError(421, b"try later"),
])
def test_other_mail_problems_are_still_retried(db_session, monkeypatch, error):
    _person(db_session, "worker")
    _failing_with(monkeypatch, error)
    notification = _pending(db_session)

    result = deliver_notification(db_session, notification.id)

    assert result.email_status == "retrying" and result.email_next_attempt_at is not None
    assert _alerts(db_session) == [], "no alert for a problem that may clear by itself"


def test_every_active_administrator_is_told_in_the_app_and_not_by_e_mail(db_session, monkeypatch):
    _person(db_session, "worker")
    _person(db_session, "admin-1", "admin")
    _person(db_session, "admin-2", "admin")
    _person(db_session, "admin-off", "admin", enabled=False)
    _person(db_session, "manager-1", "manager")
    _failing_with(monkeypatch, smtplib.SMTPAuthenticationError(535, b"5.7.8 not accepted"))

    deliver_notification(db_session, _pending(db_session).id)

    alerts = _alerts(db_session)
    assert sorted(a.recipient_employee_id for a in alerts) == ["admin-1", "admin-2"]
    assert all(a.summary == MAIL_SIGN_IN_ALERT and a.event_type == "workflow_failure" for a in alerts)
    assert all(a.in_app_status == "delivered" and a.email_status == "disabled" for a in alerts)
    assert "SMTP_PASSWORD" in MAIL_SIGN_IN_ALERT and "restart" in MAIL_SIGN_IN_ALERT


def test_the_alert_is_sent_once_a_day_however_many_e_mails_fail(db_session, monkeypatch):
    _person(db_session, "worker")
    _person(db_session, "admin-1", "admin")
    _failing_with(monkeypatch, smtplib.SMTPAuthenticationError(535, b"5.7.8 not accepted"))
    today = datetime.now(timezone.utc) + timedelta(minutes=1)  # after the notices were created

    for _ in range(4):
        deliver_notification(db_session, _pending(db_session).id, now=today)
    assert len(_alerts(db_session)) == 1

    deliver_notification(db_session, _pending(db_session).id, now=today + timedelta(days=1))
    assert len(_alerts(db_session)) == 2, "still broken tomorrow, so the administrator hears again"


def test_hr_operations_are_told_when_there_is_no_administrator(db_session, monkeypatch):
    _person(db_session, "worker")
    _person(db_session, "hr-1", "hr_operations")
    _failing_with(monkeypatch, smtplib.SMTPAuthenticationError(535, b"5.7.8 not accepted"))

    deliver_notification(db_session, _pending(db_session).id)

    assert [a.recipient_employee_id for a in _alerts(db_session)] == ["hr-1"]


def test_nobody_to_tell_is_not_an_error(db_session):
    assert alert_mail_sign_in_rejected(db_session) == 0


def test_a_problem_raising_the_alert_never_hides_the_delivery_failure(db_session, monkeypatch):
    _person(db_session, "worker")
    _person(db_session, "admin-1", "admin")
    notification = _pending(db_session)  # made with the real create_notification
    _failing_with(monkeypatch, smtplib.SMTPAuthenticationError(535, b"5.7.8 not accepted"))

    def broken(*args, **kwargs):
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr(notification_service, "create_notification", broken)

    result = deliver_notification(db_session, notification.id)

    assert result.email_status == "failed" and result.email_error == SIGN_IN_REJECTED
    assert alert_mail_sign_in_rejected(db_session) == 0


# ---------------------------------------------------------------------------
# The server hanging up while signing in (Google blocking sign-ins after many failures)
# ---------------------------------------------------------------------------


def _hung_up_during(operation: str) -> smtplib.SMTPServerDisconnected:
    error = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    error.smtp_operation = operation  # set by the mail code on every failed step
    return error


def test_a_hang_up_during_sign_in_is_explained_as_a_sign_in_block_not_a_network_blip():
    from app.plain_errors import SIGN_IN_BLOCKED, UNREACHABLE, is_sign_in_failure, plain_error

    assert plain_error(_hung_up_during("login"), email=True) == SIGN_IN_BLOCKED
    assert "wait an hour" in SIGN_IN_BLOCKED.lower() and "app password" in SIGN_IN_BLOCKED
    # A hang-up at any other step is an ordinary connection problem.
    assert plain_error(_hung_up_during("connect"), email=True) == UNREACHABLE
    assert plain_error(smtplib.SMTPServerDisconnected("closed"), email=True) == UNREACHABLE
    assert is_sign_in_failure(_hung_up_during("login"))
    assert is_sign_in_failure(smtplib.SMTPAuthenticationError(535, b"no"))
    assert not is_sign_in_failure(_hung_up_during("sendmail/response handling"))
    assert not is_sign_in_failure(OSError("down"))


def test_a_hang_up_during_sign_in_is_not_retried_and_alerts_the_administrator(db_session, monkeypatch):
    from app.plain_errors import SIGN_IN_BLOCKED

    _person(db_session, "worker")
    _person(db_session, "admin-1", "admin")
    attempts = _failing_with(monkeypatch, _hung_up_during("login"))

    result = deliver_notification(db_session, _pending(db_session).id)

    assert result.email_status == "failed" and result.email_next_attempt_at is None
    assert result.email_error == SIGN_IN_BLOCKED
    assert attempts == [1], "Google was contacted once; retrying would deepen the block"
    assert [a.recipient_employee_id for a in _alerts(db_session)] == ["admin-1"]


def test_a_hang_up_while_sending_is_still_retried(db_session, monkeypatch):
    _person(db_session, "worker")
    _failing_with(monkeypatch, _hung_up_during("sendmail/response handling"))

    result = deliver_notification(db_session, _pending(db_session).id)

    assert result.email_status == "retrying"
    assert _alerts(db_session) == []


def test_a_draft_whose_sign_in_was_blocked_is_not_retried_or_bounced():
    from app import email_delivery_service as delivery

    assert delivery.is_transient(_hung_up_during("login")) is False
    assert delivery.is_transient(smtplib.SMTPAuthenticationError(535, b"no")) is False
    assert delivery.is_transient(_hung_up_during("connect")) is True
    assert delivery.is_transient(smtplib.SMTPServerDisconnected("closed")) is True
