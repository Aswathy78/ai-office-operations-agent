"""API tests for Natural-Language Meeting Scheduling endpoint (Story 5.1)."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.db.database import get_db
from app.meeting.models import Base, Employee


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


@pytest.fixture(autouse=True)
def prevent_real_invitation_delivery(monkeypatch):
    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        lambda recipient, subject, body: "test-message-id",
    )


@pytest.fixture()
def seeded_directory(db_session: Session):
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


def test_api_schedule_meeting_with_dev_team(client: TestClient, seeded_directory):
    response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": "Schedule a project meeting tomorrow at 10 AM with the development team"},
        headers={"X-User-Id": "emp-1"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "Completed"
    assert data["meeting"] is not None
    assert data["meeting"]["title"] == "Project Meeting"
    assert data["meeting"]["join_url"].endswith(
        f"/meetings/{data['meeting']['id']}"
    )
    assert len(data["resolved_attendees"]) == 5
    assert any(a["name"] == "Ava Patel" for a in data["resolved_attendees"])
    assert data["calendar_provider"] == "local"
    assert len(data["invitations_sent"]) == 5
    assert data["invitation_failures"] == []


def test_schedule_and_retrieve_10am_meeting_in_organization_timezone(
    client: TestClient,
    seeded_directory,
    monkeypatch,
):
    organization_timezone = ZoneInfo("Asia/Kolkata")
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", str(organization_timezone))
    now_local = datetime.now(timezone.utc).astimezone(organization_timezone)
    expected_start_local = datetime.combine(
        now_local.date() + timedelta(days=1),
        time(10, 0),
        tzinfo=organization_timezone,
    )
    expected_start_utc = expected_start_local.astimezone(timezone.utc)
    expected_end_utc = expected_start_utc + timedelta(minutes=30)

    response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={
            "prompt": "Schedule a project meeting tomorrow at 10 AM with the development team"
        },
        headers={"X-User-Id": "emp-1"},
    )

    assert response.status_code == 200
    created = response.json()
    assert created["status"] == "Completed"
    assert created["timezone"] == "Asia/Kolkata"
    assert created["meeting"]["timezone"] == "Asia/Kolkata"
    saved_start = datetime.fromisoformat(created["meeting"]["start_at"])
    saved_end = datetime.fromisoformat(created["meeting"]["end_at"])
    assert saved_start.utcoffset() == timedelta(0)
    assert saved_start == expected_start_utc
    assert saved_end == expected_end_utc
    assert saved_start.astimezone(organization_timezone).hour == 10
    assert saved_end - saved_start == timedelta(minutes=30)

    retrieved_response = client.get(
        "/api/v1/meetings/",
        params={"user_id": "emp-1", "email": "maya@northstar.example"},
    )
    assert retrieved_response.status_code == 200
    retrieved = next(
        item for item in retrieved_response.json()
        if item["id"] == created["meeting"]["id"]
    )
    assert retrieved["timezone"] == "Asia/Kolkata"
    assert datetime.fromisoformat(retrieved["start_at"]) == saved_start
    assert datetime.fromisoformat(retrieved["end_at"]) == saved_end


def test_meeting_create_normalizes_aware_and_naive_datetimes_to_utc():
    from app.meeting.schemas import MeetingCreate

    aware_meeting = MeetingCreate(
        title="Aware time",
        meeting_type="scheduled",
        start_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        end_at=datetime(2026, 10, 18, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    naive_meeting = MeetingCreate(
        title="Naive UTC time",
        meeting_type="scheduled",
        start_at=datetime(2026, 10, 18, 4, 30),
        end_at=datetime(2026, 10, 18, 5, 0),
    )

    assert aware_meeting.start_at == naive_meeting.start_at == datetime(2026, 10, 18, 4, 30)
    assert aware_meeting.end_at == naive_meeting.end_at == datetime(2026, 10, 18, 5, 0)


def test_api_schedule_missing_attendees_asks_clarification(client: TestClient, seeded_directory):
    response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": "Schedule a project meeting tomorrow at 10 AM"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["meeting"] is None
    assert data["clarifying_question"] is not None
    assert "Who should be invited" in data["clarifying_question"]


def test_api_schedule_conflict_detected(client: TestClient, seeded_directory):
    # Schedule first meeting
    res1 = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": "Schedule a project meeting tomorrow at 10 AM with the development team"},
        headers={"X-User-Id": "emp-1"},
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "Completed"

    # Attempt overlapping second meeting with Marcus
    res2 = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": "Schedule an architecture sync tomorrow at 10:15 AM with Marcus"},
        headers={"X-User-Id": "emp-1"},
    )
    assert res2.status_code == 200
    data = res2.json()
    assert data["status"] == "conflict"
    assert data["meeting"] is None
    assert len(data["conflicts"]) >= 1
    assert any(c["attendee_email"] == "marcus.chen@northstar.example" for c in data["conflicts"])
    assert "conflicts" in data["message"].lower()


def test_api_does_not_report_completed_when_invitation_delivery_fails(
    client: TestClient,
    seeded_directory,
    monkeypatch,
):
    def fail_delivery(recipient: str, subject: str, body: str):
        raise ValueError("SMTP unavailable")

    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        fail_delivery,
    )
    response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={
            "prompt": "Schedule a project meeting tomorrow at 10 AM with the development team"
        },
        headers={"X-User-Id": "emp-1"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert data["meeting"] is not None
    assert len(data["invitation_failures"]) == 5
    assert "invitations could not be delivered" in data["message"]
