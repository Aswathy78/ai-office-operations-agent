"""Technical failures are shown to people in plain language.

A rejected mail login used to reach the screen as
``SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted ...')``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import smtplib
import socket
import ssl
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import email_delivery_service as delivery
from app import plain_errors
from app.meeting.models import Base, Employee, WorkflowEvent, WorkflowRun
from app.meeting.schemas import TaskCalendarSyncResponse, TaskNotificationResponse
from app.notification_router import NotificationResponse
from app.notification_service import create_notification, deliver_notification
from app.email_draft_router import DeliveryResult
from app.plain_errors import (
    BUSY,
    DATABASE,
    GENERIC,
    MAIL_SETUP,
    RECIPIENT_LATER,
    RECIPIENT_REFUSED,
    REJECTED,
    SENDER_REFUSED,
    SERVICE_BUSY,
    SERVICE_REJECTED,
    SERVICE_UNREACHABLE,
    SIGN_IN_REJECTED,
    TOO_LARGE,
    UNREACHABLE,
    plain_error,
)

# What a person actually saw (Gmail rejecting the app password), word for word.
GMAIL_REJECTION = (
    "SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted. For more "
    "information, go to\n5.7.8 https://support.google.com/mail/?p=BadCredentials "
    "a92af1059eb24-1ac1a13652csm39335297c88.17 - gsmtp')"
)
TECHNICAL_MARKERS = ("SMTP", "535", "b'", "Traceback", "Errno", "gsmtp", "0x")


# ---------------------------------------------------------------------------
# The wording itself
# ---------------------------------------------------------------------------


def test_the_gmail_rejection_people_saw_becomes_a_sentence_they_can_act_on():
    assert plain_error(GMAIL_REJECTION) == SIGN_IN_REJECTED
    assert plain_error(smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")) == SIGN_IN_REJECTED
    assert "administrator" in SIGN_IN_REJECTED and "app password" in SIGN_IN_REJECTED
    assert not any(marker in SIGN_IN_REJECTED for marker in TECHNICAL_MARKERS)


@pytest.mark.parametrize(("error", "expected"), [
    (smtplib.SMTPRecipientsRefused({"a@x.test": (550, b"5.1.1 No such user")}), RECIPIENT_REFUSED),
    (smtplib.SMTPRecipientsRefused({"a@x.test": (450, b"try later")}), RECIPIENT_LATER),
    (smtplib.SMTPSenderRefused(553, b"bad sender", "me@x.test"), SENDER_REFUSED),
    (smtplib.SMTPDataError(421, b"too many connections"), BUSY),
    (smtplib.SMTPDataError(552, b"message too large"), TOO_LARGE),
    (smtplib.SMTPDataError(554, b"rejected as spam"), REJECTED),
    (smtplib.SMTPResponseException(530, b"auth required"), SIGN_IN_REJECTED),
    (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), UNREACHABLE),
    (smtplib.SMTPConnectError(421, b"no service"), UNREACHABLE),
    (smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported"), MAIL_SETUP),
    (smtplib.SMTPException("something odd"), REJECTED),
    (ConnectionRefusedError(10061, "refused"), UNREACHABLE),
    (TimeoutError("timed out"), UNREACHABLE),
    (socket.gaierror(11001, "getaddrinfo failed"), UNREACHABLE),
    (ssl.SSLError("handshake failed"), UNREACHABLE),
])
def test_each_kind_of_mail_failure_gets_its_own_plain_sentence(error, expected):
    assert plain_error(error, email=True) == expected


def test_a_generic_os_error_is_only_called_a_network_problem_when_it_reads_like_one():
    assert plain_error(OSError("[Errno 11001] getaddrinfo failed"), email=True) == UNREACHABLE
    assert plain_error(OSError("Connection reset by peer"), email=True) == UNREACHABLE
    assert plain_error(OSError("SMTP unavailable"), email=True) == "SMTP unavailable"
    # Outside e-mail, a file error is not a mail-server problem.
    assert plain_error(PermissionError("cannot write the report")) == "cannot write the report"


def test_online_service_and_database_failures_are_explained_too():
    httpx = pytest.importorskip("httpx")
    assert plain_error(httpx.ConnectTimeout("slow")) == SERVICE_UNREACHABLE
    groq = pytest.importorskip("groq")
    request = httpx.Request("POST", "https://api.groq.test")
    response = httpx.Response(401, request=request)
    assert plain_error(groq.AuthenticationError("bad key", response=response, body=None)) == SERVICE_REJECTED
    response = httpx.Response(429, request=request)
    assert plain_error(groq.RateLimitError("slow down", response=response, body=None)) == SERVICE_BUSY
    assert plain_error(OperationalError("SELECT 1", {}, Exception("database is locked"))) == DATABASE


def test_mail_setup_mistakes_point_at_the_settings():
    assert plain_error(ValueError("SMTP_HOST is not configured"), email=True) == MAIL_SETUP


def test_messages_that_are_already_readable_are_left_alone():
    for message in (
        "Meeting summary read-back verification failed.",
        "provider timed out",
        "The workflow meeting no longer exists.",
        "E-mail delivery is disabled by the recipient's preferences.",
    ):
        assert plain_error(RuntimeError(message)) == message
        assert plain_error(message) == message


def test_an_unrecognised_technical_message_is_never_shown_raw():
    for error in (Exception("(500, b'oops')"), RuntimeError("[Errno 2] No such file 0x7ffd12345678")):
        assert plain_error(error) == GENERIC
    assert plain_error("SomethingWeirdError: (999, b'x')") == GENERIC


def test_a_class_name_prefix_is_dropped_but_the_message_kept():
    assert plain_error("Unexpected RuntimeError: the queue is paused") == "the queue is paused"
    assert plain_error("ValueError: the date is in the past") == "the date is in the past"


def test_old_stored_text_is_reworded_too():
    assert plain_error("OSError: SMTP unavailable") == "SMTP unavailable"
    assert plain_error("TimeoutError: timed out") == UNREACHABLE
    assert plain_error("Recipient refused by the mail server: 550 5.1.1 No such user") == RECIPIENT_REFUSED
    assert plain_error("Mail server replied 421: try again later") == BUSY
    assert plain_error("2 of 3 recipient(s) not delivered. " + GMAIL_REJECTION) == (
        "2 of 3 recipient(s) not delivered. " + SIGN_IN_REJECTED
    )


def test_nothing_comes_out_empty_or_technical_and_rewording_twice_changes_nothing():
    samples = [GMAIL_REJECTION, "OSError: SMTP unavailable", "Unexpected KeyError: 'x'",
               "Mail server replied 552: too big", "plain words", "TimeoutError: timed out"]
    for text in samples:
        once = plain_error(text)
        assert once and plain_error(once) == once
        assert not any(marker in once for marker in ("b'", "Traceback", "Errno", "gsmtp"))
    assert plain_error(None) is None and plain_error("   ") is None


# ---------------------------------------------------------------------------
# Where it is used
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(Employee(id="emp-1", name="Evan Cole", email="evan@example.test", is_active=True))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _notification(db: Session):
    return create_notification(
        db, recipient_id="emp-1", event_type="task_assignment", summary="New task assigned",
        idempotency_key=f"plain-{uuid.uuid4()}", email_subject="New task", email_body="A task was assigned.",
    )


def test_a_rejected_mail_login_is_stored_on_the_notification_in_plain_words(db_session, monkeypatch, caplog):
    def rejected(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr("app.task_service.send_email", rejected)
    notification = _notification(db_session)

    with caplog.at_level("WARNING", logger="app.notification_service"):
        result = deliver_notification(db_session, notification.id)

    assert result.email_status == "failed", "a rejected login is never retried"
    assert result.email_error == SIGN_IN_REJECTED
    assert not any(marker in result.email_error for marker in TECHNICAL_MARKERS)
    # The server's own words are not lost: they are in the log for an administrator.
    assert "Username and Password not accepted" in caplog.text


def test_an_unexpected_failure_while_notifying_never_shows_its_class_name(db_session, monkeypatch):
    monkeypatch.setattr("app.task_service.send_email", lambda *a, **k: (_ for _ in ()).throw(KeyError("subject")))
    notification = _notification(db_session)

    result = deliver_notification(db_session, notification.id)

    assert result.email_error == GENERIC
    assert "KeyError" not in result.email_error


def test_a_failed_delivery_says_why_in_plain_words_but_a_bounce_keeps_the_servers_reason():
    refused = smtplib.SMTPRecipientsRefused({"a@x.test": (550, b"5.1.1 No such user")})

    assert delivery._describe(refused) == RECIPIENT_REFUSED
    assert delivery._describe(smtplib.SMTPAuthenticationError(535, b"nope")) == SIGN_IN_REJECTED
    # An administrator reading a bounce still needs what the server said.
    assert "550 5.1.1 No such user" in delivery._server_reply(refused)


def test_the_api_rewords_errors_saved_before_this_change():
    stored = GMAIL_REJECTION
    notification = NotificationResponse(
        id="n1", event_type="task_assignment", summary="s", entity_type=None, entity_id=None,
        entity_url=None, in_app_status="delivered", email_status="failed", email_attempts=5,
        email_next_attempt_at=None, email_error=stored, created_at=datetime.now(timezone.utc), read_at=None,
    )
    result = DeliveryResult(email="a@x.test", status="failed", error=stored)
    task_notification = TaskNotificationResponse(
        id="t1", task_id="task", recipient_employee_id="e", recipient_name="E", kind="assignment",
        subject="s", status="failed", error=stored, created_at=datetime.now(timezone.utc),
    ) if "task_id" in TaskNotificationResponse.model_fields else None

    assert notification.email_error == SIGN_IN_REJECTED
    assert result.error == SIGN_IN_REJECTED
    if task_notification is not None:
        assert task_notification.error == SIGN_IN_REJECTED
    assert "last_error" in TaskCalendarSyncResponse.model_fields
    assert NotificationResponse.model_fields["email_error"].metadata  # the validator is attached


def test_a_workflow_run_failed_by_a_mail_error_shows_plain_words_and_keeps_the_trace_in_the_log(db_session):
    from app.workflow_service import enqueue_workflow
    from app.meeting.models import WorkflowStep
    from app.workflow_worker import _fail_step

    run = enqueue_workflow(db_session, workflow_type="t", idempotency_key="plain-wf", steps=("send",))
    step = db_session.query(WorkflowStep).filter_by(workflow_run_id=run.id).one()
    run.status = step.status = "processing"
    db_session.commit()

    _fail_step(db_session, run, step, smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"))

    db_session.expire_all()
    run = db_session.get(WorkflowRun, run.id)
    event = db_session.query(WorkflowEvent).filter_by(workflow_run_id=run.id, event_type="retry_scheduled").one()
    assert run.last_error == SIGN_IN_REJECTED and event.error == SIGN_IN_REJECTED
    assert db_session.query(WorkflowStep).one().last_error == SIGN_IN_REJECTED


def test_the_module_has_no_import_cycles():
    # It is imported by the schemas and routers; it must depend on nothing of the app's.
    assert not [name for name in vars(plain_errors) if name.startswith("app")]


def test_the_other_raw_messages_found_in_a_real_database_read_plainly_too():
    assert plain_error("gaierror: [Errno 11001] getaddrinfo failed") == UNREACHABLE
    assert plain_error(
        "SilentRecordingError: The recording contains no sound, so it could not be transcribed."
    ) == "The recording contains no sound, so it could not be transcribed."


def test_every_error_message_the_meeting_api_returns_is_reworded():
    from pydantic import BaseModel

    from app.meeting import schemas

    carriers = [
        model for model in vars(schemas).values()
        if isinstance(model, type) and issubclass(model, BaseModel) and "error_message" in model.model_fields
    ]
    assert len(carriers) >= 6
    for model in carriers:
        assert model.model_fields["error_message"].metadata, f"{model.__name__} does not reword errors"
