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


@pytest.mark.parametrize("time_text", ["2.30 PM", "2 30 PM"])
def test_api_schedule_meeting_with_dot_or_spaced_minutes(
    client: TestClient,
    seeded_directory,
    monkeypatch,
    time_text: str,
):
    organization_timezone = ZoneInfo("Asia/Kolkata")
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", str(organization_timezone))
    target_date = datetime.now(timezone.utc).astimezone(organization_timezone).date() + timedelta(days=1)
    expected_start = datetime.combine(
        target_date,
        time(14, 30),
        tzinfo=organization_timezone,
    ).astimezone(timezone.utc)

    response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={
            "prompt": (
                f"Schedule a project meeting with Ava Patel tomorrow at {time_text}"
            )
        },
        headers={"X-User-Id": "emp-1"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "Completed"
    assert data["timezone"] == "Asia/Kolkata"
    assert datetime.fromisoformat(data["meeting"]["start_at"]) == expected_start
    assert datetime.fromisoformat(data["meeting"]["end_at"]) == (
        expected_start + timedelta(minutes=30)
    )


def test_api_schedules_available_half_hour_evening_slot(
    client: TestClient,
    seeded_directory,
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "UTC")
    target_date = datetime.now(timezone.utc).date() + timedelta(days=1)
    date_text = target_date.strftime("%d %b %Y")

    response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={
            "prompt": (
                f"Schedule a project meeting with Ava Patel on {date_text} at 8:30 PM"
            )
        },
        headers={"X-User-Id": "emp-1"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "Completed"
    assert datetime.fromisoformat(data["meeting"]["start_at"]) == datetime.combine(
        target_date,
        time(20, 30),
        tzinfo=timezone.utc,
    )


def test_api_reports_half_hour_conflict_and_schedules_after_it(
    client: TestClient,
    db_session: Session,
    seeded_directory,
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "UTC")
    target_date = datetime.now(timezone.utc).date() + timedelta(days=1)
    busy_meeting = Meeting(
        id="existing-evening-meeting",
        organizer_id="emp-1",
        title="Existing meeting",
        start_at=datetime.combine(target_date, time(20, 0)),
        end_at=datetime.combine(target_date, time(20, 50)),
        join_url="http://localhost:5173/meetings/existing-evening-meeting",
        status="scheduled",
    )
    db_session.add(busy_meeting)
    db_session.add(
        MeetingParticipant(
            id="existing-evening-meeting-ava",
            meeting_id=busy_meeting.id,
            user_id="emp-2",
            email="ava.patel@northstar.example",
            role="participant",
        )
    )
    db_session.commit()
    date_text = target_date.strftime("%d %b %Y")

    conflict_response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={
            "prompt": (
                f"Schedule a project meeting with Ava Patel on {date_text} at 8:30 PM"
            )
        },
        headers={"X-User-Id": "emp-1"},
    )

    assert conflict_response.status_code == 200
    conflict = conflict_response.json()
    assert conflict["status"] == "conflict"
    assert conflict["meeting"] is None
    assert "Existing meeting" in conflict["message"]
    assert any(
        item["attendee_email"] == "ava.patel@northstar.example"
        for item in conflict["conflicts"]
    )

    available_response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={
            "prompt": (
                f"Schedule a project meeting with Ava Patel on {date_text} at 9:00 PM"
            )
        },
        headers={"X-User-Id": "emp-1"},
    )

    assert available_response.status_code == 200
    available = available_response.json()
    assert available["status"] == "Completed"
    assert datetime.fromisoformat(available["meeting"]["start_at"]) == datetime.combine(
        target_date,
        time(21, 0),
        tzinfo=timezone.utc,
    )


def _next_weekday_at_ten_utc() -> datetime:
    target = datetime.now(timezone.utc).date() + timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return datetime.combine(target, time(10, 0), tzinfo=timezone.utc)


def _team_schedule_prompt(start_at: datetime) -> str:
    date_text = start_at.strftime("%d %b %Y")
    return f"Schedule a project meeting with Ava Patel and Marcus Chen on {date_text} at 10 AM"


def test_manual_meeting_with_organization_local_times_fits_working_hours(
    client: TestClient,
    seeded_directory,
    monkeypatch,
):
    organization_timezone = ZoneInfo("Asia/Kolkata")
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", str(organization_timezone))
    target_date = _next_weekday_at_ten_utc().date()
    local_start = datetime.combine(target_date, time(10, 1), tzinfo=organization_timezone)
    local_end = datetime.combine(target_date, time(12, 2), tzinfo=organization_timezone)

    response = client.post(
        "/api/v1/meetings/?organizer_id=emp-1",
        json={
            "title": "Local-time conversion",
            "meeting_type": "scheduled",
            "start_at": local_start.astimezone(timezone.utc).isoformat(),
            "end_at": local_end.astimezone(timezone.utc).isoformat(),
            "participants": [{"user_id": "emp-2"}],
        },
    )

    assert response.status_code == 200
    created = response.json()
    saved_start = datetime.fromisoformat(created["start_at"])
    saved_end = datetime.fromisoformat(created["end_at"])
    assert saved_start == local_start.astimezone(timezone.utc)
    assert saved_end == local_end.astimezone(timezone.utc)
    assert saved_start.astimezone(organization_timezone).hour == 10
    assert saved_start.astimezone(organization_timezone).minute == 1
    assert saved_end.astimezone(organization_timezone).hour == 12
    assert saved_end.astimezone(organization_timezone).minute == 2


def test_manual_working_hours_conflicts_list_only_outside_attendees(
    client: TestClient,
    db_session: Session,
    seeded_directory,
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    attendee_with_later_hours = db_session.query(Employee).filter_by(id="emp-3").one()
    attendee_with_later_hours.working_hours_end = "23:00"
    db_session.commit()
    start_at = _next_weekday_at_ten_utc().replace(hour=13, minute=0)

    response = client.post(
        "/api/v1/meetings/?organizer_id=emp-1",
        json={
            "title": "Working-hours conflict detail",
            "meeting_type": "scheduled",
            "start_at": start_at.isoformat(),
            "end_at": (start_at + timedelta(minutes=30)).isoformat(),
            "participants": [
                {"user_id": "emp-2"},
                {"user_id": "emp-3"},
            ],
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert {
        conflict["attendee_email"] for conflict in detail["conflicts"]
    } == {"ava.patel@northstar.example"}


def test_conflict_returns_all_attendees_alternatives_and_requires_confirmation(
    client: TestClient,
    seeded_directory,
):
    prompt = _team_schedule_prompt(_next_weekday_at_ten_utc())
    initial = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": prompt},
        headers={"X-User-Id": "emp-1"},
    )
    assert initial.status_code == 200
    assert initial.json()["status"] == "Completed"
    assert initial.json()["meeting"] is not None

    conflict = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": prompt},
        headers={"X-User-Id": "emp-1"},
    ).json()
    assert conflict["status"] == "conflict"
    assert conflict["meeting"] is None
    assert {item["attendee_email"] for item in conflict["conflicts"]} == {
        "ava.patel@northstar.example",
        "marcus.chen@northstar.example",
    }
    assert {item["attendee_name"] for item in conflict["conflicts"]} == {
        "Ava Patel",
        "Marcus Chen",
    }
    assert conflict["alternative_slots"]
    assert len(client.get("/api/v1/meetings/?user_id=emp-1").json()) == 1

    selected = conflict["alternative_slots"][0]["start_at"]
    confirmed = client.post(
        "/api/v1/meetings/schedule/confirm",
        json={"prompt": prompt, "start_at": selected},
        headers={"X-User-Id": "emp-1"},
    ).json()
    assert confirmed["status"] == "Completed"
    assert confirmed["meeting"] is not None


def test_confirmation_rechecks_availability_and_returns_fresh_alternatives(
    client: TestClient,
    seeded_directory,
):
    start_at = _next_weekday_at_ten_utc()
    prompt = _team_schedule_prompt(start_at)
    client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": prompt},
        headers={"X-User-Id": "emp-1"},
    )
    conflict = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": prompt},
        headers={"X-User-Id": "emp-1"},
    ).json()
    selected = conflict["alternative_slots"][0]

    manual = client.post(
        "/api/v1/meetings/?organizer_id=emp-1",
        json={
            "title": "Another meeting",
            "meeting_type": "scheduled",
            "start_at": selected["start_at"],
            "end_at": selected["end_at"],
            "participants": [
                {"user_id": "emp-2", "email": "ava.patel@northstar.example"},
                {"user_id": "emp-3", "email": "marcus.chen@northstar.example"},
            ],
        },
    )
    assert manual.status_code == 200

    before_confirmation = client.get("/api/v1/meetings/?user_id=emp-1").json()
    stale = client.post(
        "/api/v1/meetings/schedule/confirm",
        json={"prompt": prompt, "start_at": selected["start_at"]},
        headers={"X-User-Id": "emp-1"},
    ).json()
    assert stale["status"] == "conflict"
    assert stale["meeting"] is None
    assert stale["conflicts"]
    assert stale["alternative_slots"]
    assert len(client.get("/api/v1/meetings/?user_id=emp-1").json()) == len(before_confirmation)


def test_manual_scheduled_meeting_rejects_double_booking(
    client: TestClient,
    seeded_directory,
):
    start_at = _next_weekday_at_ten_utc()
    payload = {
        "title": "Manual meeting",
        "meeting_type": "scheduled",
        "start_at": start_at.isoformat(),
        "end_at": (start_at + timedelta(minutes=30)).isoformat(),
        "participants": [{"user_id": "emp-2", "email": "ava.patel@northstar.example"}],
    }
    first = client.post("/api/v1/meetings/?organizer_id=emp-1", json=payload)
    assert first.status_code == 200
    duplicate = client.post("/api/v1/meetings/?organizer_id=emp-1", json=payload)
    assert duplicate.status_code == 409
    assert "ava.patel@northstar.example" in str(duplicate.json()["detail"])
    assert len(client.get("/api/v1/meetings/?user_id=emp-1").json()) == 1


def test_manual_conflict_detection_resolves_participant_by_user_id(
    client: TestClient,
    seeded_directory,
):
    start_at = _next_weekday_at_ten_utc()
    payload = {
        "title": "ID-only attendee meeting",
        "meeting_type": "scheduled",
        "start_at": start_at.isoformat(),
        "end_at": (start_at + timedelta(minutes=30)).isoformat(),
        "participants": [{"user_id": "emp-2"}],
    }
    assert client.post("/api/v1/meetings/?organizer_id=emp-1", json=payload).status_code == 200
    duplicate = client.post("/api/v1/meetings/?organizer_id=emp-1", json=payload)
    assert duplicate.status_code == 409
    assert "ava.patel@northstar.example" in str(duplicate.json()["detail"])


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
