"""Backend tests for Meeting Rescheduling by Chat (Story 5.3)."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.db.database import get_db
from app.meeting.calendar_provider import (
    CalendarProviderError,
    LocalCalendarProvider,
    get_calendar_provider,
)
from app.meeting.models import (
    Base,
    Employee,
    Meeting,
    MeetingDelegate,
    MeetingParticipant,
    MeetingRescheduleRequest,
    RESCHEDULE_STATUS_APPROVED,
    RESCHEDULE_STATUS_PENDING,
    RESCHEDULE_STATUS_REJECTED,
)
from app.meeting.schemas import MeetingCreate, ParticipantCreate
from app.meeting.service import (
    add_meeting_delegate,
    create_meeting,
)
from app.meeting.scheduling_service import (
    decide_reschedule_request,
    find_target_meeting_to_reschedule,
    is_reschedule_intent,
    reschedule_meeting_from_natural_language,
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
def employees(db_session: Session):
    # Working days: Mon-Sun 09:00 - 18:00 to test business logic independently
    employees = [
        Employee(id="emp-1", name="Maya Thompson", email="maya@northstar.example", team="Engineering", is_active=True, working_days=[0, 1, 2, 3, 4, 5, 6], working_hours_start="09:00", working_hours_end="18:00"),
        Employee(id="emp-2", name="Ava Patel", email="ava.patel@northstar.example", team="Engineering", is_active=True, working_days=[0, 1, 2, 3, 4, 5, 6], working_hours_start="09:00", working_hours_end="18:00"),
        Employee(id="emp-3", name="Marcus Chen", email="marcus.chen@northstar.example", team="Engineering", is_active=True, working_days=[0, 1, 2, 3, 4, 5, 6], working_hours_start="09:00", working_hours_end="18:00"),
        Employee(id="emp-4", name="Daniel Brooks", email="daniel.brooks@northstar.example", team="Engineering", is_active=True, working_days=[0, 1, 2, 3, 4, 5, 6], working_hours_start="09:00", working_hours_end="18:00"),
        Employee(id="emp-5", name="Rahul", email="rahul@northstar.example", team="Engineering", is_active=True, working_days=[0, 1, 2, 3, 4, 5, 6], working_hours_start="09:00", working_hours_end="18:00"),
    ]
    for emp in employees:
        db_session.add(emp)
    db_session.commit()
    return employees


def test_is_reschedule_intent():
    assert is_reschedule_intent("Move tomorrow's meeting to 3 PM") is True
    assert is_reschedule_intent("Reschedule the 10 AM meeting to 3 PM") is True
    assert is_reschedule_intent("Shift tomorrow's sync to 4 PM") is True
    assert is_reschedule_intent("Push tomorrow's project meeting to 2 PM") is True
    assert is_reschedule_intent("Postpone the meeting to Friday at 11 AM") is True
    assert is_reschedule_intent("Change the time of tomorrow's meeting to 3 PM") is True
    # Normal scheduling or email drafting should NOT be reschedule intent
    assert is_reschedule_intent("Schedule a project meeting tomorrow at 10 AM with the development team") is False
    assert is_reschedule_intent("Book a sync with Ava tomorrow at 2 PM") is False
    assert is_reschedule_intent("Email the development team a reminder") is False


def test_organizer_reschedules_successfully(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    sent_emails = []
    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        lambda recipient, subject, body: sent_emails.append((recipient, subject, body)),
    )

    # Reference time: Monday Oct 19 2026, 04:30 UTC = 10:00 AM IST
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)
    # Target meeting tomorrow: Tuesday Oct 20 2026 at 10:00 AM IST (04:30 UTC) to 10:30 AM IST (05:00 UTC)
    meeting = create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Sprint Planning",
            agenda="Sprint review and planning",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[
                ParticipantCreate(user_id="emp-2", email="ava.patel@northstar.example"),
                ParticipantCreate(user_id="emp-3", email="marcus.chen@northstar.example"),
            ],
        ),
        organizer_id="emp-1",
    )

    # Organizer requests: "Move tomorrow's meeting to 3 PM"
    # 3 PM IST on Oct 20 = 09:30 UTC
    result = schedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 3 PM",
        organizer_id="emp-1",
        reference_time=ref_time,
    )

    assert result.status == "Completed"
    assert result.meeting is not None
    assert result.meeting.id == meeting.id
    # Verified new time: 15:00 IST = 09:30 UTC
    assert result.meeting.start_at == datetime(2026, 10, 20, 9, 30)
    assert result.meeting.end_at == datetime(2026, 10, 20, 10, 0)
    # Check notifications sent to attendees
    assert len(sent_emails) >= 2
    recipients = [r[0] for r in sent_emails]
    assert "ava.patel@northstar.example" in recipients
    assert "marcus.chen@northstar.example" in recipients
    assert "Rescheduled: Sprint Planning" in sent_emails[0][1]


def test_authorized_delegate_reschedules_successfully(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    sent_emails = []
    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        lambda recipient, subject, body: sent_emails.append((recipient, subject, body)),
    )

    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)
    meeting = create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Design Review",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[ParticipantCreate(user_id="emp-3", email="marcus.chen@northstar.example")],
        ),
        organizer_id="emp-1",
    )

    # emp-2 is authorized as delegate for organizer emp-1
    add_meeting_delegate(db_session, organizer_id="emp-1", delegate_id="emp-2")

    # Delegate emp-2 issues reschedule
    result = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 2 PM",
        requester_id="emp-2",
        reference_time=ref_time,
    )

    assert result.status == "Completed"
    assert result.meeting is not None
    # 2 PM IST = 08:30 UTC
    assert result.meeting.start_at == datetime(2026, 10, 20, 8, 30)


def test_participant_role_delegate_reschedules_successfully(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)

    meeting = create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Arch Sync",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[ParticipantCreate(user_id="emp-3", email="marcus.chen@northstar.example")],
        ),
        organizer_id="emp-1",
    )
    # Set participant role to delegate
    part = db_session.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting.id).first()
    part.role = "delegate"
    db_session.commit()

    result = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 11 AM",
        requester_id="emp-3",
        reference_time=ref_time,
    )
    assert result.status == "Completed"
    assert result.meeting.start_at == datetime(2026, 10, 20, 5, 30)


def test_ordinary_attendee_requests_approval(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)

    meeting = create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Team All Hands",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[ParticipantCreate(user_id="emp-4", email="daniel.brooks@northstar.example")],
        ),
        organizer_id="emp-1",
    )

    # emp-4 is an ordinary attendee (not organizer, not delegate)
    result = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 4 PM",
        requester_id="emp-4",
        reference_time=ref_time,
    )

    # Must require approval
    assert result.status == "pending_approval"
    assert result.reschedule_request_id is not None

    # Check meeting time was NOT modified yet
    refreshed_meeting = db_session.query(Meeting).filter(Meeting.id == meeting.id).first()
    assert refreshed_meeting.start_at == datetime(2026, 10, 20, 4, 30)

    # Check approval request in DB
    req = db_session.query(MeetingRescheduleRequest).filter(MeetingRescheduleRequest.id == result.reschedule_request_id).first()
    assert req is not None
    assert req.status == RESCHEDULE_STATUS_PENDING
    assert req.requested_by == "emp-4"
    assert req.meeting_id == meeting.id


def test_exactly_20_attendees_vs_21_attendees(db_session: Session, monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)

    # Create 25 employees
    for i in range(1, 26):
        db_session.add(Employee(id=f"emp-{i}", name=f"User {i}", email=f"user{i}@northstar.example", is_active=True, working_days=[0, 1, 2, 3, 4, 5, 6], working_hours_start="09:00", working_hours_end="18:00"))
    db_session.commit()

    # Meeting A: exactly 20 attendees (20 participants)
    participants_20 = [ParticipantCreate(user_id=f"emp-{i}", email=f"user{i}@northstar.example") for i in range(2, 21)]
    create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Meeting With 20",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=participants_20,
        ),
        organizer_id="emp-1",
    )

    # Organizer reschedules meeting with 20 attendees directly -> Completed
    res_20 = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 2 PM",
        requester_id="emp-1",
        reference_time=ref_time,
    )
    assert res_20.status == "Completed"

    # Meeting B: 21 attendees
    participants_21 = [ParticipantCreate(user_id=f"emp-{i}", email=f"user{i}@northstar.example") for i in range(2, 22)]
    create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Meeting With 21",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 21, 4, 30),
            end_at=datetime(2026, 10, 21, 5, 0),
            participants=participants_21,
        ),
        organizer_id="emp-1",
    )

    # Even organizer requires approval when attendees > 20
    res_21 = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move Wednesday's meeting to 3 PM",
        requester_id="emp-1",
        reference_time=ref_time,
    )
    assert res_21.status == "pending_approval"
    assert res_21.reschedule_request_id is not None
    assert "more than 20 attendees" in res_21.message


def test_conflicting_slot_causes_no_update(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)

    # Meeting 1: Target meeting on Tuesday 10:00 AM IST (04:30 UTC)
    target_meeting = create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Target Meeting",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[ParticipantCreate(user_id="emp-2", email="ava.patel@northstar.example")],
        ),
        organizer_id="emp-1",
    )

    # Meeting 2: Conflicting meeting on Tuesday at 3:00 PM IST (09:30 UTC) for emp-2
    create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Other Conflicting Event",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 9, 30),
            end_at=datetime(2026, 10, 20, 10, 0),
            participants=[ParticipantCreate(user_id="emp-2", email="ava.patel@northstar.example")],
        ),
        organizer_id="emp-3",
    )

    # Attempt to move target meeting to 3 PM (09:30 UTC)
    result = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 3 PM",
        requester_id="emp-1",
        reference_time=ref_time,
    )

    assert result.status == "conflict"
    assert len(result.conflicts) > 0
    assert result.conflicts[0].attendee_email == "ava.patel@northstar.example"
    assert "Other Conflicting Event" in result.conflicts[0].conflicting_event_title
    assert len(result.alternative_slots) > 0

    # Ensure meeting time was not changed
    db_session.refresh(target_meeting)
    assert target_meeting.start_at == datetime(2026, 10, 20, 4, 30)


def test_existing_event_does_not_conflict_with_itself(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)

    # Meeting currently 10:00 AM - 11:00 AM IST (04:30 - 05:30 UTC)
    meeting = create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Overlap Meeting",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 30),
            participants=[ParticipantCreate(user_id="emp-2", email="ava.patel@northstar.example")],
        ),
        organizer_id="emp-1",
    )

    # Move meeting to 10:30 AM IST (05:00 UTC), which overlaps with original 10:00-11:00 slot
    result = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 10:30 AM",
        requester_id="emp-1",
        reference_time=ref_time,
    )

    # Should succeed without reporting self-conflict!
    assert result.status == "Completed"
    assert result.meeting.start_at == datetime(2026, 10, 20, 5, 0)
    assert result.meeting.end_at == datetime(2026, 10, 20, 6, 0)


def test_multiple_matching_meetings_require_clarification(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)

    # Two meetings tomorrow
    create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Morning Sync",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[],
        ),
        organizer_id="emp-1",
    )
    create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Afternoon Demo",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 8, 30),
            end_at=datetime(2026, 10, 20, 9, 0),
            participants=[],
        ),
        organizer_id="emp-1",
    )

    # "Move tomorrow's meeting to 5 PM" is ambiguous
    result = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 5 PM",
        requester_id="emp-1",
        reference_time=ref_time,
    )

    assert result.status == "needs_clarification"
    assert "multiple matching meetings" in result.clarifying_question
    assert "Morning Sync" in result.clarifying_question
    assert "Afternoon Demo" in result.clarifying_question


def test_approval_can_be_granted_or_rejected(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    sent_emails = []
    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        lambda recipient, subject, body: sent_emails.append((recipient, subject, body)),
    )

    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)
    create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Weekly Discussion",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[ParticipantCreate(user_id="emp-4", email="daniel.brooks@northstar.example")],
        ),
        organizer_id="emp-1",
    )

    # emp-4 submits reschedule request
    res = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 3 PM",
        requester_id="emp-4",
        reference_time=ref_time,
    )
    assert res.status == "pending_approval"
    req_id = res.reschedule_request_id

    # Non-organizer (emp-3) attempts to decide -> 403 Forbidden
    with pytest.raises(Exception) as exc:
        decide_reschedule_request(
            db=db_session,
            request_id=req_id,
            decision="approve",
            decider_id="emp-3",
        )
    assert exc.value.status_code == 403

    # Authorized organizer (emp-1) approves
    req, updated_event, msg = decide_reschedule_request(
        db=db_session,
        request_id=req_id,
        decision="approve",
        decider_id="emp-1",
        decision_note="Approved by organizer",
    )
    assert req.status == RESCHEDULE_STATUS_APPROVED
    assert updated_event is not None
    # 3 PM IST = 09:30 UTC
    assert updated_event.start_at == datetime(2026, 10, 20, 9, 30)
    assert len(sent_emails) > 0


def test_approval_can_be_rejected(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)

    meeting = create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="One on One",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[ParticipantCreate(user_id="emp-4", email="daniel.brooks@northstar.example")],
        ),
        organizer_id="emp-1",
    )

    res = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 3 PM",
        requester_id="emp-4",
        reference_time=ref_time,
    )
    req_id = res.reschedule_request_id
    assert req_id is not None

    req, updated_event, msg = decide_reschedule_request(
        db=db_session,
        request_id=req_id,
        decision="reject",
        decider_id="emp-1",
        decision_note="Conflict with client demo",
    )
    assert req.status == RESCHEDULE_STATUS_REJECTED
    assert updated_event is None

    # Meeting remains unchanged
    db_session.refresh(meeting)
    assert meeting.start_at == datetime(2026, 10, 20, 4, 30)


def test_provider_update_failure_and_readback_mismatch(db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    ref_time = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)

    create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Important Sync",
            meeting_type="scheduled",
            start_at=datetime(2026, 10, 20, 4, 30),
            end_at=datetime(2026, 10, 20, 5, 0),
            participants=[ParticipantCreate(user_id="emp-2", email="ava.patel@northstar.example")],
        ),
        organizer_id="emp-1",
    )

    # Mock provider update_event to raise read-back verification failure
    def mock_fail_update(*args, **kwargs):
        raise CalendarProviderError("Provider read-back verification failed for meeting: timestamps do not match.")

    monkeypatch.setattr(LocalCalendarProvider, "update_event", mock_fail_update)

    result = reschedule_meeting_from_natural_language(
        db=db_session,
        prompt="Move tomorrow's meeting to 3 PM",
        requester_id="emp-1",
        reference_time=ref_time,
    )

    assert result.status == "error"
    assert "Provider read-back verification failed" in result.message


def test_reschedule_endpoints_via_api(client: TestClient, db_session: Session, employees: list[Employee], monkeypatch):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    org_tz = ZoneInfo("Asia/Kolkata")
    now_local = datetime.now(timezone.utc).astimezone(org_tz)
    tomorrow_date = (now_local + timedelta(days=1)).date()

    # Create meeting on tomorrow relative to now
    create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="API Reschedule Meeting",
            meeting_type="scheduled",
            start_at=datetime.combine(tomorrow_date, time(10, 0)),
            end_at=datetime.combine(tomorrow_date, time(10, 30)),
            participants=[ParticipantCreate(user_id="emp-2", email="ava.patel@northstar.example")],
        ),
        organizer_id="emp-1",
    )

    # 1. Non-organizer requests reschedule via POST /api/v1/meetings/schedule/natural-language
    resp = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": "Move tomorrow's meeting to 2 PM"},
        headers={"X-User-Id": "emp-2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_approval"
    req_id = data["reschedule_request_id"]
    assert req_id is not None

    # 2. Organizer lists pending requests via GET /api/v1/meetings/reschedule-requests
    list_resp = client.get(
        "/api/v1/meetings/reschedule-requests?status=pending",
        headers={"X-User-Id": "emp-1"},
    )
    assert list_resp.status_code == 200
    requests_list = list_resp.json()
    assert len(requests_list) == 1
    assert requests_list[0]["id"] == req_id
    assert requests_list[0]["requested_by"] == "emp-2"

    # 3. Organizer approves via POST /api/v1/meetings/reschedule-requests/{request_id}/decide
    decide_resp = client.post(
        f"/api/v1/meetings/reschedule-requests/{req_id}/decide",
        json={"decision": "approve", "note": "Looks good to me"},
        headers={"X-User-Id": "emp-1"},
    )
    assert decide_resp.status_code == 200
    decide_data = decide_resp.json()
    assert decide_data["request"]["status"] == "approved"
    assert decide_data["meeting"] is not None
    assert "14:00" in decide_data["meeting"]["start_at"] or "08:30" in decide_data["meeting"]["start_at"]


def test_chat_schedule_api_reschedules_existing_event_and_calendar_lists_it_once(
    client: TestClient,
    db_session: Session,
    employees: list[Employee],
    monkeypatch,
):
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    org_tz = ZoneInfo("Asia/Kolkata")
    tomorrow = (datetime.now(timezone.utc).astimezone(org_tz) + timedelta(days=1)).date()
    original_start = datetime.combine(tomorrow, time(10), tzinfo=org_tz)
    meeting = create_meeting(
        db=db_session,
        meeting_data=MeetingCreate(
            title="Planning meeting",
            meeting_type="scheduled",
            start_at=original_start.astimezone(timezone.utc).replace(tzinfo=None),
            end_at=(original_start + timedelta(minutes=30))
            .astimezone(timezone.utc)
            .replace(tzinfo=None),
            participants=[
                ParticipantCreate(user_id="emp-2", email="ava.patel@northstar.example"),
            ],
        ),
        organizer_id="emp-1",
    )

    response = client.post(
        "/api/v1/meetings/schedule/natural-language",
        json={"prompt": "Schedule the planning meeting with Ava at 3 PM instead of 10 AM"},
        headers={"X-User-Id": "emp-1"},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    expected_start = (
        datetime.combine(tomorrow, time(15), tzinfo=org_tz)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    assert result["status"] == "Completed"
    assert result["meeting"]["id"] == meeting.id
    assert result["meeting"]["start_at"] == expected_start
    assert "ava.patel@northstar.example" in result["invitations_sent"]
    assert db_session.query(Meeting).count() == 1

    calendar = client.get(
        "/api/v1/meetings/calendar",
        params={
            "year": tomorrow.year,
            "month": tomorrow.month,
            "user_id": "emp-1",
            "email": "maya@northstar.example",
        },
    )

    assert calendar.status_code == 200, calendar.text
    events = calendar.json()["meetings"]
    assert len(events) == 1
    assert events[0]["id"] == meeting.id
    assert events[0]["start_at"] == expected_start
    assert events[0]["start_at"] != (
        original_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
