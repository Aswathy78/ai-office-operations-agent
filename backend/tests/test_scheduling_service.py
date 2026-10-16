"""Unit tests for Natural-Language Meeting Scheduling Service (Story 5.1)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.meeting.calendar_provider import LocalCalendarProvider
from app.meeting.models import Base, Employee, Meeting
from app.meeting.scheduling_service import (
    DEFAULT_DURATION_MINUTES,
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


def test_schedule_project_meeting_tomorrow_at_10am_with_dev_team(
    db_session: Session,
    dev_team_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    kolkata_tz = ZoneInfo("Asia/Kolkata")
    ref_time = datetime(2026, 10, 16, 4, 30, 0, tzinfo=timezone.utc)  # 10:00 AM IST on Oct 16

    prompt = "Schedule a project meeting tomorrow at 10 AM with the development team"
    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt=prompt,
        organizer_id="emp-1",
        reference_time=ref_time,
    )

    assert result.status == "scheduled"
    assert result.meeting is not None
    assert result.meeting.title == "Project Meeting"
    assert result.meeting.join_url is not None
    assert f"/meetings/{result.meeting.id}" in result.meeting.join_url

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
    assert res1.status == "scheduled"

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
