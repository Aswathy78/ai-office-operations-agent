"""API tests for Natural-Language Meeting Scheduling endpoint (Story 5.1)."""

from datetime import datetime, timezone
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
    assert data["status"] == "scheduled"
    assert data["meeting"] is not None
    assert data["meeting"]["title"] == "Project Meeting"
    assert "/meetings/" in data["meeting"]["join_url"]
    assert len(data["resolved_attendees"]) == 5
    assert any(a["name"] == "Ava Patel" for a in data["resolved_attendees"])


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
    assert res1.json()["status"] == "scheduled"

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
