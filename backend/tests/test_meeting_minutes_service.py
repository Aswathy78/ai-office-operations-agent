from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.meeting.minutes_service import (
    approve_meeting_minutes,
    generate_or_get_meeting_minutes,
    get_eligible_attendance_recipients,
    get_meeting_minutes,
    is_valid_recipient_email,
)
from app.meeting.models import (
    ACTION_ITEM_REVIEW_STATUS_APPROVAL_REQUIRED,
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingAttendanceLog,
    MeetingMinutes,
    MeetingParticipant,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    MINUTES_DELIVERY_STATUS_PENDING,
    MINUTES_POLICY_MANUAL_APPROVAL,
    MINUTES_STATUS_APPROVED,
    MINUTES_STATUS_PENDING_APPROVAL,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _create_full_meeting_context(db: Session) -> dict:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="organizer-123",
        title="Sprint Planning",
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
        overview="Discussed Q4 deliverables and team capacity.",
        decisions=["Launch beta on Nov 1", "Freeze new feature requests"],
    )
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript.id,
        status="completed",
    )
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Marcus Chen",
        email="marcus@example.com",
        team="Engineering",
        is_active=True,
    )

    item1_deadline = now + timedelta(days=7)
    item1 = MeetingActionItem(
        id=str(uuid.uuid4()),
        extraction_id=extraction.id,
        task_description="Complete architecture review",
        owner_name="Marcus",
        confirmed_employee_id=employee.id,
        deadline_at=item1_deadline,
        deadline_text="next week",
        priority="high",
        confidence=0.95,
        source_segment_index=0,
        source_start_seconds=10.0,
        source_end_seconds=20.0,
        review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    )
    item2 = MeetingActionItem(
        id=str(uuid.uuid4()),
        extraction_id=extraction.id,
        task_description="Draft test cases",
        owner_name="Ava Patel",
        deadline_at=None,
        deadline_text="TBD",
        priority="medium",
        confidence=0.88,
        source_segment_index=1,
        source_start_seconds=25.0,
        source_end_seconds=35.0,
        review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    )
    item_unconfirmed = MeetingActionItem(
        id=str(uuid.uuid4()),
        extraction_id=extraction.id,
        task_description="Unconfirmed suggestion",
        owner_name=None,
        deadline_at=None,
        deadline_text=None,
        priority="low",
        confidence=0.5,
        source_segment_index=2,
        source_start_seconds=40.0,
        source_end_seconds=50.0,
        review_status=ACTION_ITEM_REVIEW_STATUS_PENDING_REVIEW,
    )

    attendee1 = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        participant_name="Marcus Chen",
        participant_email="Marcus@Example.COM",
        joined_at=now - timedelta(minutes=55),
    )
    attendee2 = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        participant_name="Sarah Developer",
        participant_email="sarah@example.com",
        joined_at=now - timedelta(minutes=50),
    )
    attendee_dup = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        participant_name="Marcus Chen (Rejoined)",
        participant_email="marcus@example.com",
        joined_at=now - timedelta(minutes=20),
    )
    attendee_no_email = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        participant_name="Guest Caller",
        participant_email=None,
        joined_at=now - timedelta(minutes=40),
    )
    attendee_invalid_email = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        participant_name="Bad Email",
        participant_email="not-an-email",
        joined_at=now - timedelta(minutes=30),
    )

    # An invitee who did NOT attend
    absent_invitee = MeetingParticipant(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        email="absent@example.com",
        role="participant",
    )

    db.add_all(
        [
            meeting,
            recording,
            transcript,
            summary,
            extraction,
            employee,
            item1,
            item2,
            item_unconfirmed,
            attendee1,
            attendee2,
            attendee_dup,
            attendee_no_email,
            attendee_invalid_email,
            absent_invitee,
        ]
    )
    db.commit()

    return {
        "meeting": meeting,
        "summary": summary,
        "employee": employee,
        "item1": item1,
        "item2": item2,
        "item1_deadline": item1_deadline,
    }


def test_is_valid_recipient_email():
    assert is_valid_recipient_email("user@example.com") is True
    assert is_valid_recipient_email("alice.smith+ops@corp.co.uk") is True
    assert is_valid_recipient_email("") is False
    assert is_valid_recipient_email("   ") is False
    assert is_valid_recipient_email(None) is False
    assert is_valid_recipient_email("invalid-email") is False
    assert is_valid_recipient_email("user@") is False
    assert is_valid_recipient_email("@domain.com") is False
    assert is_valid_recipient_email("user@domain") is False


def test_minutes_content_aggregation_and_defaults(db_session: Session):
    ctx = _create_full_meeting_context(db_session)
    meeting = ctx["meeting"]
    summary = ctx["summary"]
    item1_deadline = ctx["item1_deadline"]

    minutes = generate_or_get_meeting_minutes(db_session, meeting.id)

    assert minutes.meeting_id == meeting.id
    assert minutes.summary_id == summary.id
    assert minutes.status == MINUTES_STATUS_PENDING_APPROVAL
    assert minutes.policy == MINUTES_POLICY_MANUAL_APPROVAL
    assert minutes.approved_by is None
    assert minutes.approved_at is None

    # Overview and decisions aggregated from summary
    assert minutes.content_summary == "Discussed Q4 deliverables and team capacity."
    assert minutes.content_decisions == [
        "Launch beta on Nov 1",
        "Freeze new feature requests",
    ]

    # Only confirmed action items included; unconfirmed excluded
    assert len(minutes.content_action_items) == 2
    action1 = minutes.content_action_items[0]
    assert action1["task_description"] == "Complete architecture review"
    assert action1["owner_name"] == "Marcus Chen"
    assert action1["owner_email"] == "marcus@example.com"
    assert action1["deadline_at"] == item1_deadline.isoformat()
    assert action1["deadline_text"] == "next week"
    assert action1["priority"] == "high"

    action2 = minutes.content_action_items[1]
    assert action2["task_description"] == "Draft test cases"
    assert action2["owner_name"] == "Ava Patel"
    assert action2["deadline_at"] is None
    assert action2["deadline_text"] == "TBD"


def test_minutes_aggregation_with_missing_summary_and_action_items(db_session: Session):
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="org-empty",
        title="Ad-hoc Discussion",
        start_at=now,
        end_at=now + timedelta(minutes=30),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    db_session.add(meeting)
    db_session.commit()

    minutes = generate_or_get_meeting_minutes(db_session, meeting_id)

    assert minutes.content_summary is None
    assert minutes.content_decisions == []
    assert minutes.content_action_items == []
    assert minutes.summary_id is None
    assert minutes.status == MINUTES_STATUS_PENDING_APPROVAL
    assert minutes.policy == MINUTES_POLICY_MANUAL_APPROVAL


def test_recipient_selection_filters_and_deduplicates(db_session: Session):
    ctx = _create_full_meeting_context(db_session)
    meeting = ctx["meeting"]

    eligible = get_eligible_attendance_recipients(db_session, meeting.id)
    assert len(eligible) == 2

    emails = {item[1].lower() for item in eligible}
    assert emails == {"marcus@example.com", "sarah@example.com"}

    # absent@example.com must NOT be in eligible list (they did not attend)
    assert "absent@example.com" not in emails

    minutes = generate_or_get_meeting_minutes(db_session, meeting.id)
    assert len(minutes.recipients) == 2

    recipients_by_email = {
        rec.recipient_email.lower(): rec for rec in minutes.recipients
    }
    marcus_rec = recipients_by_email["marcus@example.com"]
    assert marcus_rec.delivery_status == MINUTES_DELIVERY_STATUS_PENDING
    assert marcus_rec.sent_at is None
    assert marcus_rec.attendance_log_id is not None
    assert marcus_rec.idempotency_key == f"minutes:{minutes.id}:marcus@example.com"


def test_repeated_generation_is_idempotent_no_duplicates(db_session: Session):
    ctx = _create_full_meeting_context(db_session)
    meeting = ctx["meeting"]

    minutes1 = generate_or_get_meeting_minutes(db_session, meeting.id)
    first_id = minutes1.id
    first_recipient_count = len(minutes1.recipients)

    minutes2 = generate_or_get_meeting_minutes(db_session, meeting.id)
    assert minutes2.id == first_id
    assert len(minutes2.recipients) == first_recipient_count

    all_minutes = (
        db_session.query(MeetingMinutes)
        .filter(MeetingMinutes.meeting_id == meeting.id)
        .all()
    )
    assert len(all_minutes) == 1


def test_stable_snapshot_persists_even_if_source_summary_changes_later(
    db_session: Session,
):
    ctx = _create_full_meeting_context(db_session)
    meeting = ctx["meeting"]
    summary = ctx["summary"]

    minutes = generate_or_get_meeting_minutes(db_session, meeting.id)
    assert minutes.content_summary == "Discussed Q4 deliverables and team capacity."

    # Later modification to summary
    summary.overview = "COMPLETELY CHANGED OVERVIEW"
    summary.decisions = ["New decision"]
    db_session.commit()

    retrieved = get_meeting_minutes(db_session, meeting.id)
    assert retrieved is not None
    # Snapshot remains unaffected
    assert retrieved.content_summary == "Discussed Q4 deliverables and team capacity."
    assert retrieved.content_decisions == [
        "Launch beta on Nov 1",
        "Freeze new feature requests",
    ]


def test_organizer_approval_transitions_status_without_dispatch(db_session: Session):
    ctx = _create_full_meeting_context(db_session)
    meeting = ctx["meeting"]

    minutes = generate_or_get_meeting_minutes(db_session, meeting.id)
    assert minutes.status == MINUTES_STATUS_PENDING_APPROVAL
    assert minutes.approved_by is None
    assert minutes.approved_at is None

    approved = approve_meeting_minutes(
        db_session,
        meeting.id,
        approver_id="organizer-123",
    )
    assert approved.status == MINUTES_STATUS_APPROVED
    assert approved.approved_by == "organizer-123"
    assert approved.approved_at is not None

    # In Phase 2, recipient delivery status remains pending (no SMTP dispatch yet)
    for rec in approved.recipients:
        assert rec.delivery_status == MINUTES_DELIVERY_STATUS_PENDING
        assert rec.sent_at is None


def test_approval_fails_if_minutes_not_found(db_session: Session):
    with pytest.raises(ValueError, match="Meeting minutes not found"):
        approve_meeting_minutes(db_session, "nonexistent-meeting", "org-1")
