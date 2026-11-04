"""Unit tests for Natural-Language Meeting Scheduling Service (Story 5.1)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.meeting.calendar_provider import FreeBusyResult, LocalCalendarProvider
from app.meeting.models import (
    Base,
    Employee,
    Meeting,
    MeetingAttendanceLog,
    MeetingParticipant,
)
from app.meeting.service import complete_meeting_if_empty
from app.meeting.scheduling_service import (
    DEFAULT_DURATION_MINUTES,
    find_alternative_slots,
    parse_duration_minutes,
    parse_meeting_date,
    parse_time_of_day,
    schedule_meeting_from_natural_language,
)


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


@pytest.fixture(autouse=True)
def prevent_real_invitation_delivery(monkeypatch):
    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        lambda recipient, subject, body: "test-message-id",
    )


@pytest.fixture()
def dev_team_employees(db_session: Session):
    employees = [
        Employee(id="emp-1", name="Maya Thompson", email="maya@northstar.example", team="Engineering", is_active=True),
        Employee(id="emp-2", name="Ava Patel", email="ava.patel@northstar.example", team="Engineering", is_active=True),
        Employee(id="emp-3", name="Marcus Chen", email="marcus.chen@northstar.example", team="Engineering", is_active=True),
        Employee(id="emp-4", name="Daniel Brooks", email="daniel.brooks@northstar.example", team="Engineering", is_active=True),
        Employee(id="emp-5", name="Rahul", email="rahul@northstar.example", team="Engineering", is_active=True),
        Employee(id="emp-6", name="Isabella Lopez", email="isabella.lopez@northstar.example", team="Finance", is_active=True),
    ]
    for emp in employees:
        db_session.add(emp)
    db_session.commit()
    return employees


def test_parse_duration_default_and_custom():
    assert parse_duration_minutes("Schedule a project meeting tomorrow at 10 AM") == 30
    assert parse_duration_minutes("Schedule a meeting for 1 hour tomorrow at 10 AM") == 60
    assert parse_duration_minutes("Schedule a 45 minute sync tomorrow at 10 AM") == 45


def test_parse_time_of_day():
    assert parse_time_of_day("meeting tomorrow at 10 AM") == (10, 0)
    assert parse_time_of_day("meeting tomorrow at 10:30am") == (10, 30)
    assert parse_time_of_day("meeting tomorrow at 2 PM") == (14, 0)
    assert parse_time_of_day("meeting tomorrow at 14:00") == (14, 0)


@pytest.mark.parametrize("time_text", ["2.30 PM", "2 30 PM"])
def test_parse_time_of_day_accepts_dot_and_spaced_minutes(time_text: str):
    assert parse_time_of_day(f"meeting tomorrow at {time_text}") == (14, 30)


def test_parse_explicit_month_day_date():
    reference_local = datetime(2026, 10, 17, 8, 52, tzinfo=ZoneInfo("Asia/Kolkata"))

    assert parse_meeting_date(
        "Schedule a presentation meeting at 12pm with Daniel Brooks on 25 Oct",
        reference_local,
    ) == datetime(2026, 10, 25).date()
    assert parse_meeting_date("Schedule it for Oct 25", reference_local) == datetime(2026, 10, 25).date()
    assert parse_meeting_date("Schedule it for October 25, 2027", reference_local) == datetime(2027, 10, 25).date()


def test_schedule_project_meeting_tomorrow_at_10am_with_dev_team(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("FRONTEND_BASE_URL", "https://meetings.example.test")
    ref_time = datetime(2026, 10, 16, 4, 30, 0, tzinfo=timezone.utc)  # 10:00 AM IST on Oct 16

    prompt = "Schedule a project meeting tomorrow at 10 AM with the development team"
    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt=prompt,
        organizer_id="emp-1",
        reference_time=ref_time,
    )

    assert result.status == "Completed"
    assert result.meeting is not None
    assert result.meeting.title == "Project Meeting"
    assert result.meeting.join_url.startswith("https://meetings.example.test/meetings/")
    assert f"/meetings/{result.meeting.id}" in result.meeting.join_url

    assert result.meeting.start_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 10, 17, 4, 30, tzinfo=timezone.utc
    )
    assert result.meeting.end_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 10, 17, 5, 0, tzinfo=timezone.utc
    )

    # Check duration: 30 minutes
    duration = result.meeting.end_at - result.meeting.start_at
    assert duration == timedelta(minutes=30)

    # Check attendees: all 5 engineering employees
    dev_emails = {"maya@northstar.example", "ava.patel@northstar.example", "marcus.chen@northstar.example", "daniel.brooks@northstar.example", "rahul@northstar.example"}
    assert set(result.meeting.attendee_emails) == dev_emails

    # Check read back from provider verified
    provider = LocalCalendarProvider()
    stored = provider.get_event(db_session, result.meeting.id)
    assert stored is not None
    assert stored.id == result.meeting.id
    assert stored.start_at == datetime(2026, 10, 17, 4, 30)
    assert stored.end_at == datetime(2026, 10, 17, 5, 0)


def test_future_meeting_with_attendance_is_not_completed_or_truncated(
    db_session: Session,
    monkeypatch,
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start_at = now + timedelta(days=1)
    end_at = start_at + timedelta(minutes=30)
    meeting = Meeting(
        id="future-attended-meeting",
        organizer_id="emp-1",
        title="Future meeting",
        agenda=None,
        start_at=start_at,
        end_at=end_at,
        join_url="http://localhost:5173/meetings/future-attended-meeting",
        status="active",
    )
    db_session.add(meeting)
    db_session.add(
        MeetingAttendanceLog(
            id="future-attendance",
            meeting_id=meeting.id,
            participant_name="Maya Thompson",
            is_external_guest=False,
            joined_at=now,
            left_at=now,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.meeting.service.signaling_manager.get_room_participants",
        lambda meeting_id: [],
    )

    assert complete_meeting_if_empty(db_session, meeting.id) is False

    db_session.refresh(meeting)
    assert meeting.status == "active"
    assert meeting.end_at == end_at


def test_unsupported_calendar_provider_fails_without_falling_back_to_local(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("CALENDAR_PROVIDER", "google")
    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Schedule a project meeting tomorrow at 10 AM with the development team",
        organizer_id="emp-1",
        reference_time=datetime(2026, 10, 16, 4, 30, tzinfo=timezone.utc),
    )

    assert result.status == "error"
    assert result.meeting is None
    assert result.message == (
        "The configured calendar provider is unavailable; the meeting was not created."
    )
    assert db_session.query(Meeting).count() == 0


def test_calendar_provider_creation_failure_is_not_reported_as_success(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    class FailingCalendarProvider:
        def check_availability(self, db, attendee_emails, start_at, end_at):
            return FreeBusyResult(has_conflicts=False)

        def create_event(
            self,
            db,
            title,
            start_at,
            end_at,
            organizer_id,
            attendee_emails,
            agenda=None,
        ):
            raise RuntimeError("calendar provider unavailable")

    monkeypatch.setattr(
        "app.meeting.scheduling_service.get_calendar_provider",
        lambda: FailingCalendarProvider(),
    )
    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Schedule a project meeting tomorrow at 10 AM with the development team",
        organizer_id="emp-1",
        reference_time=datetime(2026, 10, 16, 4, 30, tzinfo=timezone.utc),
    )

    assert result.status == "error"
    assert result.meeting is None
    assert result.message.startswith("Failed to create meeting:")
    assert db_session.query(Meeting).count() == 0


def test_missing_attendees_asks_clarifying_question(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    prompt = "Schedule a project meeting tomorrow at 10 AM"
    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt=prompt,
        organizer_id="emp-1",
    )

    assert result.status == "needs_clarification"
    assert result.meeting is None
    assert result.clarifying_question is not None
    assert "Who should be invited" in result.clarifying_question

    # Ensure no meeting was created in DB
    assert db_session.query(Meeting).count() == 0


def test_unresolvable_attendee_asks_clarifying_question(
    db_session: Session,
    dev_team_employees: list[Employee],
):
    prompt = "Schedule a project meeting tomorrow at 10 AM with the astronaut crew"
    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt=prompt,
        organizer_id="emp-1",
    )

    assert result.status == "needs_clarification"
    assert result.meeting is None
    assert result.clarifying_question is not None
    assert "astronaut crew" in result.clarifying_question

    assert db_session.query(Meeting).count() == 0


def test_conflict_detection_prevents_double_booking(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("FRONTEND_BASE_URL", "http://localhost:5173")
    kolkata_tz = ZoneInfo("Asia/Kolkata")
    ref_time = datetime(2026, 10, 16, 4, 30, 0, tzinfo=timezone.utc)

    # First meeting: Schedule meeting with development team tomorrow at 10 AM
    prompt1 = "Schedule a project meeting tomorrow at 10 AM with the development team"
    res1 = schedule_meeting_from_natural_language(
        db=db_session,
        prompt=prompt1,
        organizer_id="emp-1",
        reference_time=ref_time,
    )
    assert res1.status == "Completed"

    # Second meeting attempt: Try to book Ava Patel at the same time
    prompt2 = "Schedule a design sync tomorrow at 10:15 AM with Ava"
    res2 = schedule_meeting_from_natural_language(
        db=db_session,
        prompt=prompt2,
        organizer_id="emp-6",
        reference_time=ref_time,
    )

    assert res2.status == "conflict"
    assert res2.meeting is None
    assert len(res2.conflicts) >= 1
    assert any(c.attendee_email == "ava.patel@northstar.example" for c in res2.conflicts)
    assert "conflicts" in res2.message.lower()

    # Total meetings in DB should still be only 1
    assert db_session.query(Meeting).count() == 1


def test_october_25_request_does_not_conflict_with_october_18_meeting(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("FRONTEND_BASE_URL", "http://localhost:5173")
    existing_start = datetime(2026, 10, 18, 6, 30)
    existing_meeting = Meeting(
        id="october-18-project",
        organizer_id="emp-6",
        title="Project Meeting",
        agenda=None,
        start_at=existing_start,
        end_at=existing_start + timedelta(minutes=30),
        join_url="http://localhost:5173/meetings/october-18-project",
        status="scheduled",
    )
    db_session.add(existing_meeting)
    db_session.add(
        MeetingParticipant(
            id="october-18-daniel",
            meeting_id=existing_meeting.id,
            user_id="emp-4",
            email="daniel.brooks@northstar.example",
            role="participant",
        )
    )
    db_session.commit()

    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Schedule a presentation meeting at 12pm with Daniel Brooks on 25 Oct",
        organizer_id="emp-1",
        reference_time=datetime(2026, 10, 17, 3, 22, tzinfo=timezone.utc),
    )

    assert result.status == "Completed"
    assert result.meeting is not None
    assert result.meeting.title == "Presentation Meeting"
    assert result.meeting.start_at == datetime(2026, 10, 25, 6, 30)
    assert result.meeting.end_at == datetime(2026, 10, 25, 7, 0)
    assert result.meeting.start_at.date() == datetime(2026, 10, 25).date()
    assert result.meeting.end_at - result.meeting.start_at == timedelta(minutes=30)
    assert result.meeting.attendee_emails == ["daniel.brooks@northstar.example"]


def test_same_day_overlapping_interval_is_a_conflict(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("FRONTEND_BASE_URL", "http://localhost:5173")
    reference_time = datetime(2026, 10, 17, 3, 22, tzinfo=timezone.utc)

    first = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Schedule a project meeting on 25 Oct at 12 PM with Daniel Brooks",
        organizer_id="emp-1",
        reference_time=reference_time,
    )
    overlapping = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Schedule a presentation meeting on 25 Oct at 12:15 PM with Daniel Brooks",
        organizer_id="emp-6",
        reference_time=reference_time,
    )

    assert first.status == "Completed"
    assert first.meeting is not None
    assert first.meeting.start_at == datetime(2026, 10, 25, 6, 30)
    assert overlapping.status == "conflict"
    assert overlapping.meeting is None
    assert any(
        conflict.conflicting_event_title == "Project Meeting"
        and conflict.attendee_email == "daniel.brooks@northstar.example"
        for conflict in overlapping.conflicts
    )
    assert db_session.query(Meeting).count() == 1


def test_same_day_non_overlapping_intervals_are_not_a_conflict(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("FRONTEND_BASE_URL", "http://localhost:5173")
    reference_time = datetime(2026, 10, 17, 3, 22, tzinfo=timezone.utc)

    first = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Schedule a project meeting on 25 Oct at 12 PM with Daniel Brooks",
        organizer_id="emp-1",
        reference_time=reference_time,
    )
    later = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Schedule a presentation meeting on 25 Oct at 1 PM with Daniel Brooks",
        organizer_id="emp-6",
        reference_time=reference_time,
    )

    assert first.status == "Completed"
    assert later.status == "Completed"
    assert later.meeting is not None
    assert later.meeting.start_at == datetime(2026, 10, 25, 7, 30)
    assert later.meeting.end_at == datetime(2026, 10, 25, 8, 0)
    assert later.meeting.end_at - later.meeting.start_at == timedelta(minutes=30)
    assert db_session.query(Meeting).count() == 2


def test_alternative_slots_respect_employee_timezone_and_working_hours(
    db_session: Session,
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "UTC")
    attendee = Employee(
        id="timezone-attendee",
        name="Timezone Attendee",
        email="timezone@example.test",
        timezone_name="America/Los_Angeles",
        working_days=[0, 1, 2, 3, 4],
        working_hours_start="09:00",
        working_hours_end="17:00",
        is_active=True,
    )
    db_session.add(attendee)
    db_session.commit()

    alternatives = find_alternative_slots(
        db_session,
        datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc),
        30,
        [attendee.email],
        provider=LocalCalendarProvider(),
        limit=1,
    )

    assert alternatives == [(
        datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 19, 16, 30, tzinfo=timezone.utc),
    )]
    local_start = alternatives[0][0].astimezone(ZoneInfo("America/Los_Angeles"))
    local_end = alternatives[0][1].astimezone(ZoneInfo("America/Los_Angeles"))
    assert local_start.weekday() in attendee.working_days
    assert local_start.hour == 9
    assert local_end.hour == 9 and local_end.minute == 30


def test_unknown_attendee_alternatives_use_organization_working_hours(monkeypatch, db_session: Session):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "UTC")
    alternatives = find_alternative_slots(
        db_session,
        datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc),
        30,
        ["external@example.test"],
        provider=LocalCalendarProvider(),
        limit=1,
    )

    assert alternatives == [(
        datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 19, 9, 30, tzinfo=timezone.utc),
    )]


def test_send_meeting_invitations_formatting_and_delivery(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    sent_emails = []

    def mock_deliver(recipient: str, subject: str, body: str):
        sent_emails.append((recipient, subject, body))
        return "msg-123"

    monkeypatch.setattr("app.meeting.scheduling_service.deliver_email", mock_deliver)

    ref_time = datetime(2026, 10, 16, 4, 30, 0, tzinfo=timezone.utc)
    prompt = "Schedule a project meeting tomorrow at 10 AM with the development team"

    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt=prompt,
        organizer_id="emp-1",
        reference_time=ref_time,
    )

    assert result.status == "Completed"
    assert len(sent_emails) == 5
    assert len(result.invitations_sent) == 5
    assert result.invitation_failures == []

    # Check recipient and invitation content format
    recipient, subject, body = sent_emails[0]
    assert subject == "Invitation: Project Meeting"
    assert "10:00 AM - 10:30 AM (Asia/Kolkata)" in body
    assert "Join URL: http://localhost:5173/meetings/" in body
    assert "Maya Thompson" in body


def test_invitation_delivery_failure_is_not_reported_as_completed(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    def fail_delivery(recipient: str, subject: str, body: str):
        raise ValueError("SMTP is not configured")

    monkeypatch.setattr("app.meeting.scheduling_service.deliver_email", fail_delivery)
    ref_time = datetime(2026, 10, 16, 4, 30, 0, tzinfo=timezone.utc)

    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Schedule a project meeting tomorrow at 10 AM with the development team",
        organizer_id="emp-1",
        reference_time=ref_time,
    )

    assert result.status == "error"
    assert result.meeting is not None
    assert "invitations could not be delivered" in result.message
    assert len(result.invitation_failures) == 5
    assert result.invitations_sent == []
    assert db_session.query(Meeting).count() == 1
