"""Tests for the date-ranged meeting calendar API."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.db.database import get_db
from app.meeting.models import Base, Employee, Meeting
from app.meeting.schemas import MeetingCreate, ParticipantCreate
from app.meeting.service import create_meeting


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
def client(db_session: Session, monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded_employees(db_session: Session):
    employees = [
        Employee(
            id="employee-1",
            name="Maya Thompson",
            email="maya@example.test",
            team="Engineering",
            is_active=True,
        ),
        Employee(
            id="employee-2",
            name="Ava Patel",
            email="ava@example.test",
            team="Engineering",
            is_active=True,
        ),
        Employee(
            id="employee-3",
            name="Isabella Lopez",
            email="isabella@example.test",
            team="Finance",
            is_active=True,
        ),
    ]
    db_session.add_all(employees)
    db_session.commit()
    return employees


def _create_meeting(
    db_session: Session,
    *,
    title: str,
    start_at: datetime,
    end_at: datetime,
    organizer_id: str,
    participants: list[ParticipantCreate],
) -> Meeting:
    if start_at.tzinfo is not None:
        start_at = start_at.astimezone(timezone.utc).replace(tzinfo=None)
    if end_at.tzinfo is not None:
        end_at = end_at.astimezone(timezone.utc).replace(tzinfo=None)
    return create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title=title,
            meeting_type="scheduled",
            start_at=start_at,
            end_at=end_at,
            participants=participants,
        ),
        organizer_id=organizer_id,
    )


def test_calendar_returns_only_visible_meetings_in_requested_month(
    client: TestClient,
    db_session: Session,
    seeded_employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("FRONTEND_BASE_URL", "https://office.example.test")
    kolkata = ZoneInfo("Asia/Kolkata")
    meeting_start = datetime(2026, 10, 22, 10, 0, tzinfo=kolkata)
    scheduled = _create_meeting(
        db_session,
        title="Project review",
        start_at=meeting_start,
        end_at=datetime(2026, 10, 22, 10, 45, tzinfo=kolkata),
        organizer_id="employee-1",
        participants=[
            ParticipantCreate(user_id="employee-2", email="ava@example.test"),
            ParticipantCreate(user_id="employee-2", email="ava@example.test"),
        ],
    )
    _create_meeting(
        db_session,
        title="Next month review",
        start_at=datetime(2026, 11, 1, 0, 15, tzinfo=kolkata),
        end_at=datetime(2026, 11, 1, 1, 0, tzinfo=kolkata),
        organizer_id="employee-1",
        participants=[],
    )

    response = client.get(
        "/api/v1/meetings/calendar",
        params={
            "year": 2026,
            "month": 10,
            "user_id": "employee-1",
            "email": "maya@example.test",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["timezone"] == "Asia/Kolkata"
    assert len(data["meetings"]) == 1
    result = data["meetings"][0]
    assert result["id"] == scheduled.id
    assert result["title"] == "Project review"
    assert result["start_at"] == "2026-10-22T04:30:00Z"
    assert result["end_at"] == "2026-10-22T05:15:00Z"
    assert result["join_url"] == scheduled.join_url
    assert result["attendees"] == [
        {
            "employee_id": "employee-2",
            "name": "Ava Patel",
            "email": "ava@example.test",
        }
    ]


def test_calendar_does_not_return_meetings_outside_existing_user_scope(
    client: TestClient,
    db_session: Session,
    seeded_employees: list[Employee],
):
    _create_meeting(
        db_session,
        title="Private finance meeting",
        start_at=datetime(2026, 10, 22, 4, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc),
        organizer_id="employee-3",
        participants=[],
    )

    response = client.get(
        "/api/v1/meetings/calendar",
        params={
            "year": 2026,
            "month": 10,
            "user_id": "employee-1",
            "email": "maya@example.test",
        },
    )

    assert response.status_code == 200
    assert response.json()["meetings"] == []


def test_calendar_includes_meetings_linked_by_participant_user_id(
    client: TestClient,
    db_session: Session,
    seeded_employees: list[Employee],
):
    _create_meeting(
        db_session,
        title="Employee-linked meeting",
        start_at=datetime(2026, 10, 22, 4, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc),
        organizer_id="employee-1",
        participants=[ParticipantCreate(user_id="employee-2")],
    )

    response = client.get(
        "/api/v1/meetings/calendar",
        params={
            "year": 2026,
            "month": 10,
            "user_id": "employee-2",
            "email": "ava@example.test",
        },
    )

    assert response.status_code == 200
    assert [meeting["title"] for meeting in response.json()["meetings"]] == [
        "Employee-linked meeting"
    ]
