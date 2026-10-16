"""Unit tests for CalendarProvider and LocalCalendarProvider (Story 5.1)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.meeting.calendar_provider import (
    CalendarConflictError,
    CalendarEvent,
    LocalCalendarProvider,
    get_calendar_provider,
)
from app.meeting.models import Base, Employee, Meeting, MeetingParticipant


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


def _seed_employee(db: Session, name: str, email: str, team: str = "Engineering") -> Employee:
    emp = Employee(
        id=str(uuid.uuid4()),
        name=name,
        email=email,
        team=team,
        is_active=True,
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return emp


def test_calendar_provider_factory():
    provider = get_calendar_provider()
    assert isinstance(provider, LocalCalendarProvider)
    assert provider.provider_name == "local"


def test_create_and_read_back_event(db_session: Session):
    provider = LocalCalendarProvider()
    emp1 = _seed_employee(db_session, "Alice Developer", "alice@example.com")
    emp2 = _seed_employee(db_session, "Bob Engineer", "bob@example.com")

    start = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)

    event = provider.create_event(
        db=db_session,
        title="Sprint Sync",
        start_at=start,
        end_at=end,
        organizer_id=emp1.id,
        attendee_emails=[emp1.email, emp2.email],
        agenda="Discuss sprint goals",
    )

    assert event.id is not None
    assert event.title == "Sprint Sync"
    assert event.description == "Discuss sprint goals"
    assert "alice@example.com" in event.attendee_emails
    assert "bob@example.com" in event.attendee_emails
    assert event.join_url is not None
    assert f"/meetings/{event.id}" in event.join_url

    # Verify read back
    fetched = provider.get_event(db_session, event.id)
    assert fetched is not None
    assert fetched.id == event.id
    assert fetched.title == event.title


def test_availability_check_no_conflict(db_session: Session):
    provider = LocalCalendarProvider()
    emp = _seed_employee(db_session, "Charlie", "charlie@example.com")

    start = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)

    res = provider.check_availability(db_session, [emp.email], start, end)
    assert res.has_conflicts is False
    assert len(res.conflicts) == 0


def test_conflict_detection_and_creation_rejection(db_session: Session):
    provider = LocalCalendarProvider()
    emp = _seed_employee(db_session, "David", "david@example.com")

    start1 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
    end1 = start1 + timedelta(minutes=30)

    # First meeting
    provider.create_event(
        db=db_session,
        title="Existing Project Review",
        start_at=start1,
        end_at=end1,
        organizer_id=emp.id,
        attendee_emails=[emp.email],
    )

    # Check overlapping window: 10:15 - 10:45
    overlap_start = datetime(2026, 10, 20, 10, 15, 0, tzinfo=timezone.utc)
    overlap_end = overlap_start + timedelta(minutes=30)

    availability = provider.check_availability(db_session, [emp.email], overlap_start, overlap_end)
    assert availability.has_conflicts is True
    assert len(availability.conflicts) == 1
    conflict = availability.conflicts[0]
    assert conflict.attendee_email == "david@example.com"
    assert conflict.attendee_name == "David"
    assert conflict.conflicting_event_title == "Existing Project Review"

    # Attempting to create should raise CalendarConflictError
    with pytest.raises(CalendarConflictError) as exc_info:
        provider.create_event(
            db=db_session,
            title="Second Meeting",
            start_at=overlap_start,
            end_at=overlap_end,
            organizer_id=emp.id,
            attendee_emails=[emp.email],
        )

    assert "conflict detected for David" in str(exc_info.value)
