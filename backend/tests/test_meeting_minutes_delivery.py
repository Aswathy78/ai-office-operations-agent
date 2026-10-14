"""Phase 3 tests: meeting-minutes email delivery and per-recipient tracking.

Tests cover:
- Approval enforcement (only approved minutes can be sent)
- Organizer-only sending restriction (API layer)
- Successful delivery to all recipients
- Email content includes summary, decisions, action items
- Per-recipient failure handling (partial failure)
- Already-sent recipients are skipped on repeat calls
- Failed recipients skip by default; retry_failed=True re-attempts
- Delivery-status retrieval endpoint
- Unauthorized user cannot send or read delivery status
- Nonexistent meeting returns 404
"""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.meeting.minutes_service import (
    render_minutes_email,
    send_meeting_minutes_to_recipients,
)
from app.meeting.models import (
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingAttendanceLog,
    MeetingMinutes,
    MeetingMinutesRecipient,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    MINUTES_DELIVERY_STATUS_FAILED,
    MINUTES_DELIVERY_STATUS_PENDING,
    MINUTES_DELIVERY_STATUS_SENT,
    MINUTES_POLICY_MANUAL_APPROVAL,
    MINUTES_STATUS_APPROVED,
    MINUTES_STATUS_PENDING_APPROVAL,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
)
from main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _create_full_setup(
    db: Session,
    organizer_id: str = "org-delivery-test",
    num_attendees: int = 2,
) -> dict:
    """Create a meeting with summary, action items and attendees."""
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    meeting = Meeting(
        id=meeting_id,
        organizer_id=organizer_id,
        title="Q4 Planning Meeting",
        start_at=now - timedelta(hours=1),
        end_at=now,
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        status=RECORDING_STATUS_COMPLETED,
    )
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
    )
    summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript.id,
        status=SUMMARY_STATUS_COMPLETED,
        overview="Discussed roadmap priorities and resource allocation.",
        decisions=["Prioritise mobile first", "Hire two engineers by Q1"],
    )
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript.id,
        status="completed",
    )
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Alex River",
        email="alex@corp.example",
        is_active=True,
    )
    action_item = MeetingActionItem(
        id=str(uuid.uuid4()),
        extraction_id=extraction.id,
        task_description="Draft hiring plan",
        owner_name="Alex",
        confirmed_employee_id=employee.id,
        deadline_at=now + timedelta(days=7),
        deadline_text="next week",
        priority="high",
        confidence=0.95,
        source_segment_index=0,
        source_start_seconds=10.0,
        source_end_seconds=20.0,
        review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    )

    attendees = []
    for i in range(num_attendees):
        att = MeetingAttendanceLog(
            id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            participant_name=f"Attendee {i + 1}",
            participant_email=f"attendee{i + 1}@corp.example",
            joined_at=now - timedelta(minutes=55 - i),
        )
        attendees.append(att)

    db.add_all(
        [meeting, recording, transcript, summary, extraction, employee, action_item]
        + attendees
    )
    db.commit()
    return {
        "meeting": meeting,
        "summary": summary,
        "employee": employee,
        "action_item": action_item,
        "attendees": attendees,
    }


def _generate_and_approve(
    client: TestClient,
    meeting_id: str,
    organizer_id: str,
) -> None:
    """Helper: generate minutes then approve via API."""
    gen = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/generate?organizer_id={organizer_id}"
    )
    assert gen.status_code == 200
    approve = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/approve?organizer_id={organizer_id}"
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"


# ---------------------------------------------------------------------------
# Service-layer tests (no HTTP)
# ---------------------------------------------------------------------------


def test_render_minutes_email_contains_all_sections(db_session: Session):
    ctx = _create_full_setup(db_session)
    meeting = ctx["meeting"]
    summary = ctx["summary"]

    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        summary_id=summary.id,
        status=MINUTES_STATUS_APPROVED,
        policy=MINUTES_POLICY_MANUAL_APPROVAL,
        content_summary="Discussed roadmap priorities and resource allocation.",
        content_decisions=["Prioritise mobile first", "Hire two engineers by Q1"],
        content_action_items=[
            {
                "task_description": "Draft hiring plan",
                "owner_name": "Alex",
                "deadline_text": "next week",
                "deadline_at": None,
                "priority": "high",
            }
        ],
    )
    db_session.add(minutes)
    db_session.commit()

    subject, body = render_minutes_email(meeting.title, minutes, "Alice")

    assert "Q4 Planning Meeting" in subject
    assert "Meeting minutes:" in subject
    assert "Hello Alice," in body
    assert "Discussed roadmap priorities" in body
    assert "Prioritise mobile first" in body
    assert "Hire two engineers by Q1" in body
    assert "Draft hiring plan" in body
    assert "next week" in body


def test_send_raises_if_minutes_not_approved(db_session: Session, monkeypatch):
    ctx = _create_full_setup(db_session)
    meeting = ctx["meeting"]

    # Generate but do NOT approve
    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=MINUTES_STATUS_PENDING_APPROVAL,
        policy=MINUTES_POLICY_MANUAL_APPROVAL,
        content_summary=None,
        content_decisions=[],
        content_action_items=[],
    )
    db_session.add(minutes)
    db_session.commit()

    monkeypatch.setattr("app.task_service.send_email", lambda *_: None)

    with pytest.raises(ValueError, match="approved"):
        send_meeting_minutes_to_recipients(db_session, meeting.id)


def test_successful_delivery_marks_all_recipients_sent(db_session: Session, monkeypatch):
    ctx = _create_full_setup(db_session, num_attendees=2)
    meeting = ctx["meeting"]

    sent_to: list[str] = []

    def mock_send(recipient: str, subject: str, body: str) -> None:
        sent_to.append(recipient)

    monkeypatch.setattr("app.task_service.send_email", mock_send)

    # Create approved minutes
    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=MINUTES_STATUS_APPROVED,
        policy=MINUTES_POLICY_MANUAL_APPROVAL,
        content_summary="Summary text.",
        content_decisions=["Decision A"],
        content_action_items=[],
    )
    db_session.add(minutes)
    db_session.flush()

    rec1 = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Attendee 1",
        recipient_email="attendee1@corp.example",
        delivery_status=MINUTES_DELIVERY_STATUS_PENDING,
        idempotency_key=f"minutes:{minutes.id}:attendee1@corp.example",
    )
    rec2 = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Attendee 2",
        recipient_email="attendee2@corp.example",
        delivery_status=MINUTES_DELIVERY_STATUS_PENDING,
        idempotency_key=f"minutes:{minutes.id}:attendee2@corp.example",
    )
    db_session.add_all([rec1, rec2])
    db_session.commit()

    results = send_meeting_minutes_to_recipients(db_session, meeting.id)

    assert len(results) == 2
    assert all(r.delivery_status == MINUTES_DELIVERY_STATUS_SENT for r in results)
    assert all(r.sent_at is not None for r in results)
    assert all(r.error_message is None for r in results)
    assert set(sent_to) == {"attendee1@corp.example", "attendee2@corp.example"}


def test_partial_failure_tracks_each_recipient_independently(db_session: Session, monkeypatch):
    ctx = _create_full_setup(db_session, num_attendees=2)
    meeting = ctx["meeting"]

    call_count = 0

    def flaky_send(recipient: str, subject: str, body: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("SMTP connection refused")

    monkeypatch.setattr("app.task_service.send_email", flaky_send)

    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=MINUTES_STATUS_APPROVED,
        policy=MINUTES_POLICY_MANUAL_APPROVAL,
        content_summary=None,
        content_decisions=[],
        content_action_items=[],
    )
    db_session.add(minutes)
    db_session.flush()

    rec1 = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Attendee 1",
        recipient_email="attendee1@corp.example",
        delivery_status=MINUTES_DELIVERY_STATUS_PENDING,
        idempotency_key=f"minutes:{minutes.id}:attendee1@corp.example",
    )
    rec2 = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Attendee 2",
        recipient_email="attendee2@corp.example",
        delivery_status=MINUTES_DELIVERY_STATUS_PENDING,
        idempotency_key=f"minutes:{minutes.id}:attendee2@corp.example",
    )
    db_session.add_all([rec1, rec2])
    db_session.commit()

    results = send_meeting_minutes_to_recipients(db_session, meeting.id)

    statuses = {r.recipient_email: r.delivery_status for r in results}
    # First attempt failed, second succeeded (or vice versa — depends on order)
    assert MINUTES_DELIVERY_STATUS_FAILED in statuses.values()
    assert MINUTES_DELIVERY_STATUS_SENT in statuses.values()

    failed_rec = next(r for r in results if r.delivery_status == MINUTES_DELIVERY_STATUS_FAILED)
    assert failed_rec.error_message == "SMTP connection refused"
    assert failed_rec.sent_at is None

    sent_rec = next(r for r in results if r.delivery_status == MINUTES_DELIVERY_STATUS_SENT)
    assert sent_rec.sent_at is not None
    assert sent_rec.error_message is None


def test_already_sent_recipients_are_skipped_on_repeat(db_session: Session, monkeypatch):
    ctx = _create_full_setup(db_session, num_attendees=1)
    meeting = ctx["meeting"]

    call_count = 0

    def mock_send(recipient: str, subject: str, body: str) -> None:
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr("app.task_service.send_email", mock_send)

    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=MINUTES_STATUS_APPROVED,
        policy=MINUTES_POLICY_MANUAL_APPROVAL,
        content_summary=None,
        content_decisions=[],
        content_action_items=[],
    )
    db_session.add(minutes)
    db_session.flush()

    rec = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Attendee 1",
        recipient_email="attendee1@corp.example",
        delivery_status=MINUTES_DELIVERY_STATUS_PENDING,
        idempotency_key=f"minutes:{minutes.id}:attendee1@corp.example",
    )
    db_session.add(rec)
    db_session.commit()

    # First call
    results_1 = send_meeting_minutes_to_recipients(db_session, meeting.id)
    assert all(r.delivery_status == MINUTES_DELIVERY_STATUS_SENT for r in results_1)
    assert call_count == 1

    # Second call — must skip already-sent recipient
    results_2 = send_meeting_minutes_to_recipients(db_session, meeting.id)
    assert all(r.delivery_status == MINUTES_DELIVERY_STATUS_SENT for r in results_2)
    assert call_count == 1  # No new SMTP call


def test_retry_failed_reattempts_failed_recipients(db_session: Session, monkeypatch):
    ctx = _create_full_setup(db_session, num_attendees=1)
    meeting = ctx["meeting"]

    attempts = 0

    def flaky_send(recipient: str, subject: str, body: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("Temporary failure")

    monkeypatch.setattr("app.task_service.send_email", flaky_send)

    minutes = MeetingMinutes(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=MINUTES_STATUS_APPROVED,
        policy=MINUTES_POLICY_MANUAL_APPROVAL,
        content_summary=None,
        content_decisions=[],
        content_action_items=[],
    )
    db_session.add(minutes)
    db_session.flush()

    rec = MeetingMinutesRecipient(
        id=str(uuid.uuid4()),
        minutes_id=minutes.id,
        recipient_name="Attendee 1",
        recipient_email="attendee1@corp.example",
        delivery_status=MINUTES_DELIVERY_STATUS_PENDING,
        idempotency_key=f"minutes:{minutes.id}:attendee1@corp.example",
    )
    db_session.add(rec)
    db_session.commit()

    # First attempt — fails
    results_1 = send_meeting_minutes_to_recipients(db_session, meeting.id)
    assert results_1[0].delivery_status == MINUTES_DELIVERY_STATUS_FAILED
    assert attempts == 1

    # Without retry_failed=True, failed recipients are skipped
    results_2 = send_meeting_minutes_to_recipients(db_session, meeting.id)
    assert results_2[0].delivery_status == MINUTES_DELIVERY_STATUS_FAILED
    assert attempts == 1  # No new attempt

    # With retry_failed=True, re-attempts
    results_3 = send_meeting_minutes_to_recipients(
        db_session, meeting.id, retry_failed=True
    )
    assert results_3[0].delivery_status == MINUTES_DELIVERY_STATUS_SENT
    assert attempts == 2


# ---------------------------------------------------------------------------
# API-layer tests
# ---------------------------------------------------------------------------


def test_send_endpoint_requires_approval_before_sending(
    client: TestClient, db_session: Session, monkeypatch
):
    monkeypatch.setattr("app.task_service.send_email", lambda *_: None)
    ctx = _create_full_setup(db_session)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    # Generate but do NOT approve
    gen = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/generate?organizer_id={organizer_id}"
    )
    assert gen.status_code == 200

    # Attempt to send without approval → 400
    send_res = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )
    assert send_res.status_code == 400
    assert "approved" in send_res.json()["detail"].lower()


def test_send_endpoint_delivers_and_returns_summary(
    client: TestClient, db_session: Session, monkeypatch
):
    sent_to: list[str] = []
    monkeypatch.setattr(
        "app.meeting.minutes_service.send_meeting_minutes_to_recipients.__wrapped__"
        if hasattr(send_meeting_minutes_to_recipients, "__wrapped__")
        else "app.task_service.send_email",
        lambda recipient, subject, body: sent_to.append(recipient),
    )
    monkeypatch.setattr(
        "app.task_service.send_email",
        lambda recipient, subject, body: sent_to.append(recipient),
    )

    ctx = _create_full_setup(db_session, num_attendees=2)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    _generate_and_approve(client, meeting.id, organizer_id)

    send_res = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )
    assert send_res.status_code == 200
    data = send_res.json()

    assert data["meeting_id"] == meeting.id
    assert data["total"] == 2
    assert data["sent"] == 2
    assert data["failed"] == 0
    assert data["pending"] == 0
    assert len(data["recipients"]) == 2
    assert all(r["delivery_status"] == "sent" for r in data["recipients"])
    assert all(r["sent_at"] is not None for r in data["recipients"])


def test_send_endpoint_partial_failure_reported(
    client: TestClient, db_session: Session, monkeypatch
):
    call_count = 0

    def flaky_send(recipient: str, subject: str, body: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("SMTP down")

    monkeypatch.setattr("app.task_service.send_email", flaky_send)

    ctx = _create_full_setup(db_session, num_attendees=2)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    _generate_and_approve(client, meeting.id, organizer_id)

    send_res = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )
    assert send_res.status_code == 200
    data = send_res.json()
    assert data["total"] == 2
    assert data["sent"] == 1
    assert data["failed"] == 1
    assert data["pending"] == 0


def test_send_endpoint_retry_failed_param(
    client: TestClient, db_session: Session, monkeypatch
):
    attempts = 0

    def flaky_send(recipient: str, subject: str, body: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 1:
            raise OSError("Transient")

    monkeypatch.setattr("app.task_service.send_email", flaky_send)

    ctx = _create_full_setup(db_session, num_attendees=1)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    _generate_and_approve(client, meeting.id, organizer_id)

    # First send — fails
    send_res = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )
    assert send_res.status_code == 200
    assert send_res.json()["failed"] == 1

    # Repeat without retry_failed — still failed (no re-attempt)
    send_res2 = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )
    assert send_res2.status_code == 200
    assert send_res2.json()["failed"] == 1
    assert attempts == 1

    # With retry_failed=true — re-attempts and succeeds
    send_res3 = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send"
        f"?organizer_id={organizer_id}&retry_failed=true"
    )
    assert send_res3.status_code == 200
    assert send_res3.json()["sent"] == 1
    assert send_res3.json()["failed"] == 0
    assert attempts == 2


def test_unauthorized_user_cannot_send_minutes(
    client: TestClient, db_session: Session, monkeypatch
):
    monkeypatch.setattr("app.task_service.send_email", lambda *_: None)

    ctx = _create_full_setup(db_session)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    _generate_and_approve(client, meeting.id, organizer_id)

    send_res = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id=wrong-user"
    )
    assert send_res.status_code == 403


def test_get_recipients_endpoint_returns_delivery_status(
    client: TestClient, db_session: Session, monkeypatch
):
    monkeypatch.setattr("app.task_service.send_email", lambda *_: None)

    ctx = _create_full_setup(db_session, num_attendees=2)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    _generate_and_approve(client, meeting.id, organizer_id)

    # Before sending — all pending
    recs_before = client.get(
        f"/api/v1/meetings/{meeting.id}/minutes/recipients?organizer_id={organizer_id}"
    )
    assert recs_before.status_code == 200
    assert all(r["delivery_status"] == "pending" for r in recs_before.json())

    # Send
    client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )

    # After sending — all sent
    recs_after = client.get(
        f"/api/v1/meetings/{meeting.id}/minutes/recipients?organizer_id={organizer_id}"
    )
    assert recs_after.status_code == 200
    assert all(r["delivery_status"] == "sent" for r in recs_after.json())


def test_unauthorized_user_cannot_view_recipients(
    client: TestClient, db_session: Session
):
    ctx = _create_full_setup(db_session)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    # Generate minutes first
    client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/generate?organizer_id={organizer_id}"
    )

    res = client.get(
        f"/api/v1/meetings/{meeting.id}/minutes/recipients?organizer_id=not-the-organizer"
    )
    assert res.status_code == 403


def test_send_nonexistent_meeting_returns_404(client: TestClient):
    fake_id = str(uuid.uuid4())
    res = client.post(
        f"/api/v1/meetings/{fake_id}/minutes/send?organizer_id=any-org"
    )
    assert res.status_code == 404


def test_get_recipients_nonexistent_meeting_returns_404(client: TestClient):
    fake_id = str(uuid.uuid4())
    res = client.get(
        f"/api/v1/meetings/{fake_id}/minutes/recipients?organizer_id=any-org"
    )
    assert res.status_code == 404


def test_send_already_sent_is_idempotent_no_duplicate_smtp(
    client: TestClient, db_session: Session, monkeypatch
):
    sent_calls: list[str] = []
    monkeypatch.setattr(
        "app.task_service.send_email",
        lambda recipient, subject, body: sent_calls.append(recipient),
    )

    ctx = _create_full_setup(db_session, num_attendees=1)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    _generate_and_approve(client, meeting.id, organizer_id)

    # First send
    res1 = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )
    assert res1.status_code == 200
    assert res1.json()["sent"] == 1
    assert len(sent_calls) == 1

    # Second send — must not re-send SMTP
    res2 = client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )
    assert res2.status_code == 200
    assert res2.json()["sent"] == 1
    assert len(sent_calls) == 1  # No new SMTP call


def test_email_body_content_includes_meeting_details(
    client: TestClient, db_session: Session, monkeypatch
):
    """Verify that the email body sent to recipients includes key minutes content."""
    bodies: list[str] = []
    subjects: list[str] = []

    def capture_send(recipient: str, subject: str, body: str) -> None:
        subjects.append(subject)
        bodies.append(body)

    monkeypatch.setattr("app.task_service.send_email", capture_send)

    ctx = _create_full_setup(db_session, num_attendees=1)
    meeting = ctx["meeting"]
    organizer_id = meeting.organizer_id

    _generate_and_approve(client, meeting.id, organizer_id)
    client.post(
        f"/api/v1/meetings/{meeting.id}/minutes/send?organizer_id={organizer_id}"
    )

    assert len(bodies) == 1
    assert "Q4 Planning Meeting" in subjects[0]
    assert "Discussed roadmap priorities" in bodies[0]
    assert "Prioritise mobile first" in bodies[0]
    assert "Draft hiring plan" in bodies[0]
