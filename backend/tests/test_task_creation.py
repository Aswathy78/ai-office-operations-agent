from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.db.database import get_db
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingAttendanceLog,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    Task,
)

ORGANIZER_ID = "task-creation-organizer"


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = testing_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session: Session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _create_confirmable_action_item(
    db: Session,
) -> tuple[Meeting, MeetingTranscript, Employee, MeetingActionItem]:
    now = datetime.now(timezone.utc)
    meeting_id = str(uuid.uuid4())
    transcript_id = str(uuid.uuid4())
    recording_id = str(uuid.uuid4())
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Maya Employee",
        email="maya.employee@example.test",
        is_active=True,
    )
    meeting = Meeting(
        id=meeting_id,
        organizer_id=ORGANIZER_ID,
        title="Quarterly Planning",
        start_at=now,
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=recording_id,
        meeting_id=meeting_id,
        status=RECORDING_STATUS_COMPLETED,
    )
    transcript = MeetingTranscript(
        id=transcript_id,
        recording_id=recording_id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
        transcript_text="Maya, send the agenda by Friday.",
        segments=[
            {
                "start": 23.25,
                "end": 26.5,
                "text": "Maya, send the agenda by Friday.",
                "speaker": "Organizer",
            }
        ],
        completed_at=now,
    )
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript_id,
        status=ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
        completed_at=now,
    )
    item = MeetingActionItem(
        id=str(uuid.uuid4()),
        owner_name=employee.name,
        confirmed_employee_id=employee.id,
        ownership_status="confirmed",
        task_description="Send the agenda.",
        deadline_at=now + timedelta(days=2),
        deadline_text="by Friday",
        priority="high",
        confidence=0.98,
        source_segment_index=0,
        source_start_seconds=23.25,
        source_end_seconds=26.5,
        source_quote_start_seconds=23.25,
        source_quote_end_seconds=26.5,
        source_quote="Maya, send the agenda by Friday.",
        review_status=ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
    )
    extraction.items.append(item)
    db.add_all([employee, meeting, recording, transcript, extraction])
    db.commit()
    db.refresh(item)
    return meeting, transcript, employee, item


def test_confirmation_creates_linked_task_and_reruns_are_idempotent(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    meeting, transcript, employee, item = _create_confirmable_action_item(db_session)
    sent_to: list[str] = []

    def send_email(task: Task, assigned_employee: Employee, meeting_title: str) -> None:
        assert task.title == "Send the agenda."
        assert meeting_title == meeting.title
        sent_to.append(assigned_employee.email)

    monkeypatch.setattr("app.task_service.send_assignment_email", send_email)
    review_url = (
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id={ORGANIZER_ID}"
    )
    response = client.patch(review_url, json={"action": "confirm"})

    assert response.status_code == 200
    task = db_session.query(Task).one()
    assert task.title == item.task_description
    assert task.assignee_id == employee.id
    assert task.creator_id == ORGANIZER_ID
    assert task.source == "meeting_action_item"
    assert task.source_action_item_id == item.id
    assert task.idempotency_key == f"meeting-action-item:{item.id}"
    assert task.meeting_id == meeting.id
    assert task.transcript_id == transcript.id
    assert task.source_segment_index == item.source_segment_index
    assert task.source_timestamp_seconds == item.source_start_seconds
    assert task.due_at.replace(tzinfo=timezone.utc) == item.deadline_at.replace(
        tzinfo=timezone.utc
    )
    assert task.priority == item.priority
    assert task.status == "pending"
    assert task.assignment_email_status == "sent"
    assert sent_to == [employee.email]

    repeated_confirmation = client.patch(review_url, json={"action": "confirm"})
    assert repeated_confirmation.status_code == 200
    assert db_session.query(Task).count() == 1
    assert sent_to == [employee.email]

    extraction = db_session.query(MeetingActionItemExtraction).one()
    monkeypatch.setattr(
        "app.meeting.router.generate_meeting_action_items",
        lambda _db, _transcript_id: extraction,
    )
    rerun = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}"
    )
    assert rerun.status_code == 200
    assert db_session.query(Task).count() == 1
    assert sent_to == [employee.email]

    dashboard = client.get("/api/v1/tasks/dashboard")
    assert dashboard.status_code == 200
    dashboard_task = dashboard.json()[0]
    assert dashboard_task["meeting_id"] == meeting.id
    assert dashboard_task["transcript_id"] == transcript.id
    assert dashboard_task["source_timestamp_seconds"] == 23.25
    assert dashboard_task["assignee_id"] == employee.id
    assert dashboard_task["assignee_name"] == employee.name

    assigned_tasks = client.get(
        f"/api/v1/tasks/?assignee_email={employee.email}"
    )
    assert assigned_tasks.status_code == 200
    assert [row["id"] for row in assigned_tasks.json()] == [task.id]

    calendar = client.get(
        f"/api/v1/tasks/calendar.ics?assignee_email={employee.email}"
    )
    assert calendar.status_code == 200
    assert calendar.headers["content-type"].startswith("text/calendar")
    assert f"SUMMARY:{task.title}" in calendar.text
    assert "DTSTART:" in calendar.text
    assert meeting.title in calendar.text


def test_assigned_employee_can_open_only_their_task_source_context(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    meeting, transcript, employee, item = _create_confirmable_action_item(db_session)
    unrelated_employee = Employee(
        id=str(uuid.uuid4()),
        name="Unrelated Employee",
        email="unrelated@example.test",
        is_active=True,
    )
    db_session.add(unrelated_employee)
    db_session.add(
        MeetingAttendanceLog(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            participant_name="Meeting Participant",
            participant_email="participant@example.test",
            joined_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.task_service.send_assignment_email",
        lambda *_args: None,
    )

    confirmation = client.patch(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id={ORGANIZER_ID}",
        json={"action": "confirm"},
    )
    assert confirmation.status_code == 200

    assigned_report = client.get(
        f"/api/v1/meetings/{meeting.id}/report"
        f"?organizer_id=employee-user&employee_email={employee.email}"
    )
    assert assigned_report.status_code == 200
    assert assigned_report.json()["meeting_id"] == meeting.id
    assert assigned_report.json()["attendance"] == []

    organizer_report = client.get(
        f"/api/v1/meetings/{meeting.id}/report?organizer_id={ORGANIZER_ID}"
    )
    assert organizer_report.status_code == 200
    assert len(organizer_report.json()["attendance"]) == 1

    assigned_transcript = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript"
        f"?organizer_id=employee-user&transcript_id={transcript.id}"
        f"&employee_email={employee.email}"
    )
    assert assigned_transcript.status_code == 200
    assert assigned_transcript.json()["id"] == transcript.id
    assert assigned_transcript.json()["segments"][0]["text"] == item.source_quote

    unrelated_report = client.get(
        f"/api/v1/meetings/{meeting.id}/report"
        f"?organizer_id=unrelated-user&employee_email={unrelated_employee.email}"
    )
    assert unrelated_report.status_code == 403

    unrelated_transcript = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript"
        f"?organizer_id=unrelated-user&transcript_id={transcript.id}"
        f"&employee_email={unrelated_employee.email}"
    )
    assert unrelated_transcript.status_code == 403

    wrong_transcript = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript"
        f"?organizer_id=employee-user&transcript_id=not-the-task-transcript"
        f"&employee_email={employee.email}"
    )
    assert wrong_transcript.status_code == 403


def test_failed_assignment_email_is_reported_and_can_be_retried(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    meeting, _, employee, item = _create_confirmable_action_item(db_session)
    attempts = 0

    def flaky_send_email(
        _task: Task,
        _assigned_employee: Employee,
        _meeting_title: str,
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("SMTP unavailable")

    monkeypatch.setattr("app.task_service.send_assignment_email", flaky_send_email)
    review_url = (
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id={ORGANIZER_ID}"
    )
    confirmed = client.patch(review_url, json={"action": "confirm"})
    assert confirmed.status_code == 200

    task = db_session.query(Task).one()
    assert task.assignment_email_status == "failed"
    assert task.assignment_email_error == "SMTP unavailable"

    retried = client.post(
        f"/api/v1/tasks/{task.id}/assignment-email/retry"
    )
    assert retried.status_code == 200
    assert retried.json()["assignment_email_status"] == "sent"
    assert retried.json()["assignee_id"] == employee.id
    assert attempts == 2


def test_extraction_rerun_creates_missing_confirmed_task_once(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    meeting, transcript, employee, item = _create_confirmable_action_item(db_session)
    item.review_status = "confirmed"
    item.reviewed_by = ORGANIZER_ID
    db_session.commit()
    extraction = db_session.query(MeetingActionItemExtraction).one()
    sent_to: list[str] = []
    monkeypatch.setattr(
        "app.task_service.send_assignment_email",
        lambda _task, assigned_employee, _meeting_title: sent_to.append(
            assigned_employee.email
        ),
    )
    monkeypatch.setattr(
        "app.meeting.router.generate_meeting_action_items",
        lambda _db, _transcript_id: extraction,
    )
    extract_url = (
        f"/api/v1/meetings/{meeting.id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}"
    )

    first_rerun = client.post(extract_url)
    assert first_rerun.status_code == 200
    task = db_session.query(Task).one()
    assert task.assignee_id == employee.id
    assert task.meeting_id == meeting.id
    assert task.transcript_id == transcript.id
    assert task.source_timestamp_seconds == item.source_start_seconds
    assert sent_to == [employee.email]

    second_rerun = client.post(extract_url)
    assert second_rerun.status_code == 200
    assert db_session.query(Task).count() == 1
    assert sent_to == [employee.email]


def test_confirmation_requires_assignee_and_due_date_without_partial_task(
    client: TestClient,
    db_session: Session,
):
    meeting, _, _, item = _create_confirmable_action_item(db_session)
    item.deadline_at = None
    db_session.commit()

    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id={ORGANIZER_ID}",
        json={"action": "confirm"},
    )

    assert response.status_code == 400
    assert "due date" in response.json()["detail"]
    assert db_session.query(Task).count() == 0
    db_session.refresh(item)
    assert item.review_status == ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW


def test_assignment_email_uses_configured_smtp(
    monkeypatch: pytest.MonkeyPatch,
):
    import smtplib

    from app.task_service import send_assignment_email

    sent_messages: list[EmailMessage] = []
    tls_started = False
    logged_in = False

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int):
            assert host == "smtp.example.test"
            assert port == 2525
            assert timeout == 10

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def starttls(self):
            nonlocal tls_started
            tls_started = True

        def login(self, username: str, password: str):
            nonlocal logged_in
            assert username == "mailer"
            assert password == "secret"
            logged_in = True

        def send_message(self, message: EmailMessage):
            sent_messages.append(message)

    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "tasks@example.test")
    monkeypatch.setenv("SMTP_USERNAME", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_USE_STARTTLS", "true")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    task = Task(
        id=str(uuid.uuid4()),
        title="Send the agenda",
        due_at=datetime.now(timezone.utc) + timedelta(days=1),
        priority="high",
    )
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Maya",
        email="maya@example.test",
        is_active=True,
    )
    send_assignment_email(task, employee, "Quarterly Planning")

    assert tls_started
    assert logged_in
    assert len(sent_messages) == 1
    assert sent_messages[0]["To"] == employee.email
    assert sent_messages[0]["Subject"] == "Task assigned: Send the agenda"
    assert "Quarterly Planning" in sent_messages[0].get_content()
