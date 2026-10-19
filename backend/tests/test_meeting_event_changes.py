from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.meeting.event_change_service import (
    add_meeting_participant,
    add_meeting_participants,
    attendee_count,
    decide_meeting_cancellation,
    remove_meeting_participant,
    request_meeting_cancellation,
)
from app.meeting.calendar_provider import CalendarProviderError, LocalCalendarProvider
from app.meeting.models import (
    Base,
    Employee,
    Meeting,
    MeetingAuditLog,
    MeetingCancellationRequest,
    MeetingParticipant,
)
from app.meeting.schemas import (
    MeetingParticipantBulkChangeRequest,
    MeetingParticipantChangeRequest,
)
from app.meeting.service import create_meeting
from app.meeting.service import add_meeting_delegate
from app.meeting.schemas import MeetingCreate, ParticipantCreate
from app.db.database import get_db
from app.authentication import (
    authenticated_actor_id,
    require_authenticated_actor_id,
    require_authenticated_principal,
)
from app.auth_service import AuthenticatedPrincipal
from main import app


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
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

    def test_principal() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            user_id="employee-0",
            account_id="test-account",
            email="person0@example.test",
            name="Employee 0",
            role="employee",
            team="Engineering",
            jti="test-session",
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_actor_id] = lambda: "employee-0"
    app.dependency_overrides[require_authenticated_principal] = test_principal
    app.dependency_overrides[authenticated_actor_id] = lambda: "employee-0"
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.clear()


def make_meeting(db: Session, participant_count: int) -> Meeting:
    employees = [
        Employee(id=f"employee-{index}", name=f"Employee {index}", email=f"person{index}@example.test")
        for index in range(participant_count + 1)
    ]
    db.add_all(employees)
    db.commit()
    start = datetime.now(timezone.utc) + timedelta(days=3)
    meeting = create_meeting(
        db,
        MeetingCreate(
            title="Quarterly planning",
            meeting_type="scheduled",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            participants=[
                ParticipantCreate(user_id=employee.id, email=employee.email)
                for employee in employees[1:]
            ],
        ),
        organizer_id="employee-0",
    )
    return meeting


def audit_actions(db: Session, meeting_id: str) -> list[str]:
    return [
        entry.action
        for entry in db.query(MeetingAuditLog)
        .filter(MeetingAuditLog.meeting_id == meeting_id)
        .order_by(MeetingAuditLog.created_at, MeetingAuditLog.id)
        .all()
    ]


def mock_calendar_cancellation(monkeypatch, db: Session) -> list[str]:
    cancelled_ids: list[str] = []

    class MockCalendarProvider:
        def cancel_event(self, session: Session, event_id: str):
            meeting = session.query(Meeting).filter_by(id=event_id).one()
            meeting.status = "cancelled"
            session.flush()
            cancelled_ids.append(event_id)

    monkeypatch.setattr(
        "app.meeting.event_change_service.get_calendar_provider",
        lambda: MockCalendarProvider(),
    )
    return cancelled_ids


def test_cancellation_authorization_notifications_and_failure_audit(db_session: Session, monkeypatch):
    meeting = make_meeting(db_session, 2)
    notices: list[tuple[str, str, str]] = []

    def deliver(recipient: str, subject: str, body: str):
        if recipient == "person2@example.test":
            raise OSError("SMTP unavailable")
        notices.append((recipient, subject, body))
        return "smtp-message-id"

    monkeypatch.setattr("app.meeting.event_change_service.deliver_email", deliver)

    with pytest.raises(HTTPException) as unauthorized:
        request_meeting_cancellation(db_session, meeting.id, "employee-2")
    assert unauthorized.value.status_code == 403

    updated, request, sent, failures = request_meeting_cancellation(
        db_session, meeting.id, "employee-0", "No longer needed"
    )
    assert request is None
    assert updated.status == "cancelled"
    assert sent == ["person1@example.test"]
    assert failures == ["person2@example.test"]
    assert [notice[0] for notice in notices] == ["person1@example.test"]
    assert notices[0][1] == "Cancelled: Quarterly planning"
    entry = db_session.query(MeetingAuditLog).filter_by(meeting_id=meeting.id).one()
    assert entry.actor_id == "employee-0"
    assert entry.details["reason"] == "No longer needed"
    assert sorted(entry.details["notifications"], key=lambda result: result["recipient"]) == sorted([
        {"recipient": "person1@example.test", "status": "sent", "error": ""},
        {"recipient": "person2@example.test", "status": "failed", "error": "SMTP unavailable"},
    ], key=lambda result: result["recipient"])


def test_meeting_creation_persists_unique_participants_and_lists_them(
    db_session: Session,
    client: TestClient,
    monkeypatch,
):
    deliveries: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        lambda recipient, subject, body: deliveries.append((recipient, subject, body)),
    )
    db_session.add(
        Employee(
            id="guest-employee",
            name="Guest Employee",
            email=" guest@example.test ",
        )
    )
    db_session.commit()
    response = client.post(
        "/api/v1/meetings/",
        json={
            "title": "Creation participants",
            "meeting_type": "instant",
            "participants": [
                {"user_id": "guest-employee", "email": "Guest@example.test"},
                {"email": " guest@EXAMPLE.test "},
                {"email": "second@example.test"},
            ],
        },
    )

    assert response.status_code == 200
    meeting_id = response.json()["id"]
    saved = (
        db_session.query(MeetingParticipant)
        .filter_by(meeting_id=meeting_id)
        .order_by(MeetingParticipant.email)
        .all()
    )
    assert [participant.email for participant in saved] == [
        "guest@example.test",
        "second@example.test",
    ]

    roster = client.get(f"/api/v1/meetings/{meeting_id}/participants")
    assert roster.status_code == 200
    assert sorted(participant["email"] for participant in roster.json()) == [
        "guest@example.test",
        "second@example.test",
    ]
    assert sorted(response.json()["invitations_sent"]) == [
        "guest@example.test",
        "second@example.test",
    ]
    assert response.json()["invitation_failures"] == []
    assert sorted(delivery[0] for delivery in deliveries) == [
        "guest@example.test",
        "second@example.test",
    ]
    assert all(delivery[1] == "Invitation: Creation participants" for delivery in deliveries)
    assert all("When:" in delivery[2] for delivery in deliveries)
    assert all(response.json()["join_url"] in delivery[2] for delivery in deliveries)


def test_meeting_creation_without_participants_persists_empty_roster(
    db_session: Session,
    client: TestClient,
    monkeypatch,
):
    deliveries: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        lambda recipient, subject, body: deliveries.append((recipient, subject, body)),
    )
    response = client.post(
        "/api/v1/meetings/",
        json={"title": "No participants", "meeting_type": "instant"},
    )

    assert response.status_code == 200
    meeting_id = response.json()["id"]
    assert (
        db_session.query(MeetingParticipant)
        .filter_by(meeting_id=meeting_id)
        .count()
        == 0
    )
    assert response.json()["invitations_sent"] == []
    assert response.json()["invitation_failures"] == []
    assert deliveries == []


def test_failed_meeting_creation_does_not_send_invitations(
    client: TestClient,
    monkeypatch,
):
    deliveries: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        lambda recipient, subject, body: deliveries.append((recipient, subject, body)),
    )

    def fail_create_meeting(*args, **kwargs):
        raise RuntimeError("meeting creation failed")

    monkeypatch.setattr("app.meeting.router.create_meeting", fail_create_meeting)
    with pytest.raises(RuntimeError, match="meeting creation failed"):
        client.post(
            "/api/v1/meetings/",
            json={
                "title": "Failed meeting",
                "meeting_type": "instant",
                "participants": [{"email": "guest@example.test"}],
            },
        )

    assert deliveries == []


def test_meeting_creation_reports_invitation_delivery_failure(
    client: TestClient,
    monkeypatch,
):
    def fail_delivery(recipient: str, subject: str, body: str):
        raise OSError("SMTP unavailable")

    monkeypatch.setattr(
        "app.meeting.scheduling_service.deliver_email",
        fail_delivery,
    )
    response = client.post(
        "/api/v1/meetings/",
        json={
            "title": "Invitation failure",
            "meeting_type": "instant",
            "participants": [{"email": "guest@example.test"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["invitations_sent"] == []
    assert response.json()["invitation_failures"] == ["guest@example.test"]


def test_meeting_creation_rejects_invalid_participant_email(client: TestClient):
    response = client.post(
        "/api/v1/meetings/",
        json={
            "title": "Invalid participant",
            "meeting_type": "instant",
            "participants": [{"email": "not-an-email"}],
        },
    )

    assert response.status_code == 422


def test_meeting_creation_rejects_duplicate_email_with_conflicting_user_ids(
    client: TestClient,
):
    response = client.post(
        "/api/v1/meetings/",
        json={
            "title": "Conflicting participants",
            "meeting_type": "instant",
            "participants": [
                {"user_id": "employee-a", "email": "guest@example.test"},
                {"user_id": "employee-b", "email": "GUEST@example.test"},
            ],
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("participant_count", "expected_status"),
    [(19, "cancelled"), (20, "scheduled"), (21, "scheduled")],
)
def test_cancellation_threshold_exactly_20_21_and_above(
    db_session: Session,
    monkeypatch,
    participant_count: int,
    expected_status: str,
):
    meeting = make_meeting(db_session, participant_count)
    provider_cancellations = mock_calendar_cancellation(monkeypatch, db_session)
    assert attendee_count(db_session, meeting) == participant_count + 1
    sent: list[str] = []
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: sent.append(recipient),
    )

    updated, request, _, _ = request_meeting_cancellation(db_session, meeting.id, "employee-0")

    if expected_status == "cancelled":
        assert request is None
        assert updated.status == "cancelled"
        assert provider_cancellations == [meeting.id]
        assert len(sent) == 19
        assert audit_actions(db_session, meeting.id) == ["meeting_cancelled"]
    else:
        assert request is not None
        assert request.status == "pending"
        assert updated.status == "scheduled"
        assert provider_cancellations == []
        assert sent == []
        entry = db_session.query(MeetingAuditLog).filter_by(meeting_id=meeting.id).one()
        assert entry.action == "cancellation_requested"
        assert entry.details["attendee_count"] == participant_count + 1


def test_same_directory_user_with_multiple_email_aliases_counts_once(db_session: Session, monkeypatch):
    meeting = make_meeting(db_session, 19)
    db_session.add(
        MeetingParticipant(
            id="participant-alias",
            meeting_id=meeting.id,
            user_id="employee-1",
            email="employee.one.alias@example.test",
            role="participant",
        )
    )
    db_session.commit()

    assert attendee_count(db_session, meeting) == 20
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: "accepted",
    )
    updated, request, _, _ = request_meeting_cancellation(
        db_session,
        meeting.id,
        "employee-0",
    )
    assert request is None
    assert updated.status == "cancelled"


def test_duplicate_participant_emails_count_as_one_attendee(db_session: Session, monkeypatch):
    meeting = make_meeting(db_session, 18)
    db_session.add_all([
        MeetingParticipant(
            id="duplicate-email-original",
            meeting_id=meeting.id,
            user_id=None,
            email="external@example.test",
            role="participant",
        ),
        MeetingParticipant(
            id="duplicate-email-case-variant",
            meeting_id=meeting.id,
            user_id=None,
            email=" External@Example.Test ",
            role="participant",
        ),
    ])
    db_session.commit()

    assert attendee_count(db_session, meeting) == 20
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: None,
    )
    mock_calendar_cancellation(monkeypatch, db_session)
    _, request, _, _ = request_meeting_cancellation(
        db_session,
        meeting.id,
        "employee-0",
    )
    assert request is None
    assert meeting.status == "cancelled"


def test_removed_participant_is_excluded_from_cancellation_threshold(
    db_session: Session,
    monkeypatch,
):
    meeting = make_meeting(db_session, 20)
    participant = (
        db_session.query(MeetingParticipant)
        .filter_by(meeting_id=meeting.id, user_id="employee-20")
        .one()
    )
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: None,
    )
    _, removed, failures = remove_meeting_participant(
        db_session,
        meeting.id,
        participant.id,
        "employee-0",
    )
    assert removed == ["person20@example.test"]
    assert failures == []
    assert attendee_count(db_session, meeting) == 20

    mock_calendar_cancellation(monkeypatch, db_session)
    _, request, _, _ = request_meeting_cancellation(
        db_session,
        meeting.id,
        "employee-0",
    )
    assert request is None
    assert meeting.status == "cancelled"


def test_distinct_directory_users_count_separately(db_session: Session):
    meeting = make_meeting(db_session, 2)

    assert attendee_count(db_session, meeting) == 3


def test_unresolved_directory_identity_is_counted_conservatively(db_session: Session):
    meeting = make_meeting(db_session, 0)
    db_session.add(
        MeetingParticipant(
            id="unresolved-email",
            meeting_id=meeting.id,
            user_id="missing-directory-user",
            email="external@example.test",
            role="participant",
        )
    )
    db_session.add_all([
        MeetingParticipant(
            id="unresolved-no-email",
            meeting_id=meeting.id,
            user_id="another-missing-user",
            email=None,
            role="participant",
        ),
        MeetingParticipant(
            id="no-identity-data",
            meeting_id=meeting.id,
            user_id=None,
            email=None,
            role="participant",
        ),
    ])
    db_session.commit()

    assert attendee_count(db_session, meeting) == 4


def test_distinct_unresolved_user_ids_are_not_merged_by_matching_email(db_session: Session):
    meeting = make_meeting(db_session, 0)
    db_session.add_all([
        MeetingParticipant(
            id="unresolved-user-a",
            meeting_id=meeting.id,
            user_id="missing-user-a",
            email="same@example.test",
            role="participant",
        ),
        MeetingParticipant(
            id="unresolved-user-b",
            meeting_id=meeting.id,
            user_id="missing-user-b",
            email="same@example.test",
            role="participant",
        ),
    ])
    db_session.commit()

    assert attendee_count(db_session, meeting) == 3


def test_directory_email_conflicting_with_user_id_blocks_threshold_decision(db_session: Session):
    meeting = make_meeting(db_session, 2)
    participant = (
        db_session.query(MeetingParticipant)
        .filter_by(meeting_id=meeting.id, user_id="employee-1")
        .first()
    )
    assert participant is not None
    participant.email = "person2@example.test"
    db_session.commit()

    with pytest.raises(HTTPException) as inconsistent:
        attendee_count(db_session, meeting)
    assert inconsistent.value.status_code == 409


def test_pending_cancellation_is_not_applied_until_approval_and_can_be_rejected(
    db_session: Session,
    monkeypatch,
):
    meeting = make_meeting(db_session, 20)
    add_meeting_delegate(db_session, "employee-0", "employee-2")
    provider_cancellations = mock_calendar_cancellation(monkeypatch, db_session)
    sent: list[str] = []
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: sent.append(recipient),
    )

    _, request, _, _ = request_meeting_cancellation(db_session, meeting.id, "employee-0")
    assert request is not None
    assert meeting.status == "scheduled"
    assert sent == []

    rejected, no_meeting, _, _ = decide_meeting_cancellation(
        db_session, request.id, "reject", "employee-2", "Keep the meeting"
    )
    assert rejected.status == "rejected"
    assert rejected.decided_by == "employee-2"
    assert no_meeting is None
    assert meeting.status == "scheduled"
    assert provider_cancellations == []
    assert sent == []
    assert "cancellation_rejected" in audit_actions(db_session, meeting.id)
    with pytest.raises(HTTPException) as repeated_decision:
        decide_meeting_cancellation(
            db_session, request.id, "approve", "employee-2", "Second decision"
        )
    assert repeated_decision.value.status_code == 409
    assert meeting.status == "scheduled"
    assert provider_cancellations == []


def test_pending_cancellation_approval_cancels_notifies_and_audits(db_session: Session, monkeypatch):
    meeting = make_meeting(db_session, 20)
    add_meeting_delegate(db_session, "employee-0", "employee-2")
    provider_cancellations = mock_calendar_cancellation(monkeypatch, db_session)
    sent: list[str] = []
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: sent.append(recipient),
    )
    _, request, _, _ = request_meeting_cancellation(db_session, meeting.id, "employee-0")
    assert request is not None
    with pytest.raises(HTTPException) as unauthorized:
        decide_meeting_cancellation(db_session, request.id, "approve", "employee-3")
    assert unauthorized.value.status_code == 403
    with pytest.raises(HTTPException) as unauthorized_rejection:
        decide_meeting_cancellation(db_session, request.id, "reject", "employee-3")
    assert unauthorized_rejection.value.status_code == 403
    assert meeting.status == "scheduled"
    assert request.status == "pending"

    decided, updated, delivered, failures = decide_meeting_cancellation(
        db_session, request.id, "approve", "employee-2", "Approved"
    )
    assert decided.status == "approved"
    assert decided.requested_by == "employee-0"
    assert decided.decided_by == "employee-2"
    assert updated is not None and updated.status == "cancelled"
    assert provider_cancellations == [meeting.id]
    assert delivered == sent
    assert len(sent) == 20
    assert failures == []
    assert audit_actions(db_session, meeting.id) == [
        "cancellation_requested",
        "cancellation_approved",
    ]
    approval_audit = (
        db_session.query(MeetingAuditLog)
        .filter_by(meeting_id=meeting.id, action="cancellation_approved")
        .one()
    )
    assert approval_audit.actor_id == "employee-2"
    assert approval_audit.details["requested_by"] == "employee-0"
    with pytest.raises(HTTPException) as repeated_decision:
        decide_meeting_cancellation(
            db_session, request.id, "reject", "employee-2", "Second decision"
        )
    assert repeated_decision.value.status_code == 409
    assert provider_cancellations == [meeting.id]
    assert len(sent) == 20


def test_cancellation_requester_cannot_approve_their_own_high_attendance_request(
    db_session: Session,
    monkeypatch,
):
    meeting = make_meeting(db_session, 20)
    provider_cancellations = mock_calendar_cancellation(monkeypatch, db_session)
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: pytest.fail("No email before an authorized approval."),
    )

    _, request, _, _ = request_meeting_cancellation(
        db_session,
        meeting.id,
        "employee-0",
    )
    assert request is not None

    with pytest.raises(HTTPException) as self_approval:
        decide_meeting_cancellation(
            db_session,
            request.id,
            "approve",
            "employee-0",
        )

    assert self_approval.value.status_code == 403
    db_session.refresh(meeting)
    db_session.refresh(request)
    assert meeting.status == "scheduled"
    assert request.status == "pending"
    assert request.decided_by is None
    assert provider_cancellations == []
    assert audit_actions(db_session, meeting.id) == ["cancellation_requested"]


def test_cancellation_requester_can_reject_own_request_through_api(
    db_session: Session,
    client: TestClient,
    monkeypatch,
):
    meeting = make_meeting(db_session, 20)
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: pytest.fail("Rejection must not send cancellation notices."),
    )
    headers = {"X-User-Id": "employee-0"}

    submitted = client.post(
        f"/api/v1/meetings/{meeting.id}/cancel",
        headers=headers,
        json={"reason": "Requesting review"},
    )
    assert submitted.status_code == 200
    request_id = submitted.json()["request_id"]
    assert submitted.json()["status"] == "pending_approval"
    assert submitted.json()["meeting"]["status"] == "scheduled"

    request_row = db_session.query(MeetingCancellationRequest).filter_by(id=request_id).one()
    db_session.refresh(meeting)
    assert request_row.status == "pending"
    assert meeting.status == "scheduled"

    app.dependency_overrides[require_authenticated_actor_id] = lambda: "employee-3"
    unauthorized = client.post(
        f"/api/v1/meetings/cancellation-requests/{request_id}/decide",
        headers={"X-User-Id": "employee-3"},
        json={"decision": "reject", "note": "Not an organizer or delegate"},
    )
    assert unauthorized.status_code == 403
    db_session.refresh(request_row)
    assert request_row.status == "pending"
    assert meeting.status == "scheduled"

    app.dependency_overrides[require_authenticated_actor_id] = lambda: "employee-0"
    rejected = client.post(
        f"/api/v1/meetings/cancellation-requests/{request_id}/decide",
        headers=headers,
        json={"decision": "reject", "note": "Keep the meeting"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["request"]["id"] == request_id
    assert rejected.json()["request"]["status"] == "rejected"
    assert rejected.json()["request"]["decided_by"] == "employee-0"
    assert rejected.json()["meeting"] is None
    assert rejected.json()["message"] == "Cancellation request rejected."

    db_session.refresh(request_row)
    db_session.refresh(meeting)
    assert request_row.status == "rejected"
    assert request_row.decided_by == "employee-0"
    assert meeting.status == "scheduled"
    assert audit_actions(db_session, meeting.id) == [
        "cancellation_requested",
        "cancellation_rejected",
    ]
    rejection_audit = (
        db_session.query(MeetingAuditLog)
        .filter_by(meeting_id=meeting.id, action="cancellation_rejected")
        .one()
    )
    assert rejection_audit.actor_id == "employee-0"
    assert rejection_audit.details["requested_by"] == "employee-0"

    pending = client.get("/api/v1/meetings/cancellation-requests", headers=headers)
    assert pending.status_code == 200
    assert pending.json() == []


def test_participant_add_remove_persists_notifies_and_audits(db_session: Session, monkeypatch):
    meeting = make_meeting(db_session, 1)
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: notices.append((recipient, subject)),
    )

    updated, sent, failures = add_meeting_participant(
        db_session,
        meeting.id,
        "employee-0",
        MeetingParticipantChangeRequest(email="guest@example.test"),
    )
    assert updated.status == "scheduled"
    assert sent == ["guest@example.test"]
    assert failures == []
    assert notices[-1] == ("guest@example.test", "Invitation: Quarterly planning")
    guest = db_session.query(MeetingParticipant).filter_by(email="guest@example.test").one()

    with pytest.raises(HTTPException) as duplicate:
        add_meeting_participant(
            db_session,
            meeting.id,
            "employee-0",
            MeetingParticipantChangeRequest(email="GUEST@example.test"),
        )
    assert duplicate.value.status_code == 409

    _, removed, remove_failures = remove_meeting_participant(
        db_session, meeting.id, guest.id, "employee-0"
    )
    assert removed == ["guest@example.test"]
    assert remove_failures == []
    assert notices[-1] == ("guest@example.test", "Meeting update: Quarterly planning")
    assert db_session.query(MeetingParticipant).filter_by(id=guest.id).first() is None
    assert audit_actions(db_session, meeting.id) == ["participant_added", "participant_removed"]


def test_participant_management_requires_authorized_actor(db_session: Session):
    meeting = make_meeting(db_session, 1)
    with pytest.raises(HTTPException) as add_denied:
        add_meeting_participant(
            db_session,
            meeting.id,
            "employee-1",
            MeetingParticipantChangeRequest(email="guest@example.test"),
        )
    assert add_denied.value.status_code == 403
    participant = db_session.query(MeetingParticipant).filter_by(meeting_id=meeting.id).first()
    with pytest.raises(HTTPException) as remove_denied:
        remove_meeting_participant(db_session, meeting.id, participant.id, "employee-1")
    assert remove_denied.value.status_code == 403


def test_bulk_participant_add_normalizes_skips_duplicates_and_reports_partial_failures(
    db_session: Session,
    monkeypatch,
):
    meeting = make_meeting(db_session, 1)
    deliveries: list[str] = []
    real_add_attendee = LocalCalendarProvider.add_attendee

    def add_attendee_with_provider_failure(provider, db, meeting_id, user_id, email):
        if email == "provider.fail@example.test":
            raise CalendarProviderError("Calendar provider unavailable.")
        return real_add_attendee(provider, db, meeting_id, user_id, email)

    def deliver_or_fail(recipient, subject, body):
        if recipient == "mail.fail@example.test":
            raise RuntimeError("SMTP unavailable.")
        deliveries.append(recipient)

    monkeypatch.setattr(LocalCalendarProvider, "add_attendee", add_attendee_with_provider_failure)
    monkeypatch.setattr("app.meeting.event_change_service.deliver_email", deliver_or_fail)

    _, outcomes = add_meeting_participants(
        db_session,
        meeting.id,
        "employee-0",
        MeetingParticipantBulkChangeRequest(
            emails=[
                " New.One@Example.Test ",
                "new.one@example.test",
                "not-an-email",
                "person1@example.test",
                "provider.fail@example.test",
                "mail.fail@example.test",
            ]
        ),
    )

    assert outcomes == {
        "added": ["new.one@example.test", "mail.fail@example.test"],
        "already_present": ["person1@example.test"],
        "duplicate_addresses": ["new.one@example.test"],
        "invalid_addresses": ["not-an-email"],
        "failed_addresses": ["provider.fail@example.test"],
        "notifications_sent": ["new.one@example.test"],
        "notification_failures": ["mail.fail@example.test"],
    }
    saved_emails = {
        row.email
        for row in db_session.query(MeetingParticipant)
        .filter_by(meeting_id=meeting.id)
        .all()
    }
    assert "new.one@example.test" in saved_emails
    assert "mail.fail@example.test" in saved_emails
    assert "provider.fail@example.test" not in saved_emails
    assert deliveries == ["new.one@example.test"]

    added_audits = (
        db_session.query(MeetingAuditLog)
        .filter_by(meeting_id=meeting.id, action="participant_added")
        .all()
    )
    assert {entry.details["email"] for entry in added_audits} == {
        "new.one@example.test",
        "mail.fail@example.test",
    }
    mail_failure_audit = next(
        entry for entry in added_audits
        if entry.details["email"] == "mail.fail@example.test"
    )
    assert mail_failure_audit.details["notifications"] == [{
        "recipient": "mail.fail@example.test",
        "status": "failed",
        "error": "SMTP unavailable.",
    }]

    _, retry_outcomes = add_meeting_participants(
        db_session,
        meeting.id,
        "employee-0",
        MeetingParticipantBulkChangeRequest(
            emails=["new.one@example.test", "mail.fail@example.test"]
        ),
    )
    assert retry_outcomes["added"] == []
    assert retry_outcomes["already_present"] == [
        "new.one@example.test",
        "mail.fail@example.test",
    ]
    assert deliveries == ["new.one@example.test"]
    assert len(added_audits) == 2


def test_bulk_participant_add_api_returns_each_outcome_and_audit_records(
    db_session: Session,
    client: TestClient,
    monkeypatch,
):
    meeting = make_meeting(db_session, 1)
    deliveries: list[str] = []
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: deliveries.append(recipient),
    )

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/participants/bulk",
        headers={"X-User-Id": "employee-0"},
        json={"emails": [" New.One@Example.Test ", "new.one@example.test", "bad"]},
    )

    assert response.status_code == 200
    assert response.json()["added"] == ["new.one@example.test"]
    assert response.json()["duplicate_addresses"] == ["new.one@example.test"]
    assert response.json()["invalid_addresses"] == ["bad"]
    assert response.json()["notifications_sent"] == ["new.one@example.test"]
    assert response.json()["message"] == (
        "1 participant added; 1 address was invalid or failed. "
        "1 duplicate or already invited address skipped."
    )
    assert deliveries == ["new.one@example.test"]
    history = client.get(
        f"/api/v1/meetings/{meeting.id}/audit",
        headers={"X-User-Id": "employee-0"},
    )
    assert [entry["action"] for entry in history.json()] == ["participant_added"]
    assert history.json()[0]["details"]["email"] == "new.one@example.test"


def test_api_cancellation_participant_changes_and_audit(
    db_session: Session,
    client: TestClient,
    monkeypatch,
):
    meeting = make_meeting(db_session, 1)
    deliveries: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: deliveries.append((recipient, subject)),
    )
    headers = {"X-User-Id": "employee-0"}

    added = client.post(
        f"/api/v1/meetings/{meeting.id}/participants",
        headers=headers,
        json={"email": "new.person@example.test"},
    )
    assert added.status_code == 200
    assert added.json()["status"] == "applied"
    assert added.json()["notifications_sent"] == ["new.person@example.test"]
    roster = client.get(f"/api/v1/meetings/{meeting.id}/participants", headers=headers)
    assert roster.status_code == 200
    assert any(row["email"] == "new.person@example.test" for row in roster.json())
    participant_id = (
        db_session.query(MeetingParticipant)
        .filter_by(meeting_id=meeting.id, email="new.person@example.test")
        .one()
        .id
    )

    removed = client.delete(
        f"/api/v1/meetings/{meeting.id}/participants/{participant_id}",
        headers=headers,
    )
    assert removed.status_code == 200
    assert removed.json()["notifications_sent"] == ["new.person@example.test"]

    cancelled = client.post(
        f"/api/v1/meetings/{meeting.id}/cancel",
        headers=headers,
        json={"reason": "Project complete"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["meeting"]["status"] == "cancelled"

    audit = client.get(f"/api/v1/meetings/{meeting.id}/audit", headers=headers)
    assert audit.status_code == 200
    assert [entry["action"] for entry in audit.json()] == [
        "meeting_cancelled",
        "participant_removed",
        "participant_added",
    ]
    assert all(entry["actor_id"] == "employee-0" for entry in audit.json())
    assert deliveries == [
        ("new.person@example.test", "Invitation: Quarterly planning"),
        ("new.person@example.test", "Meeting update: Quarterly planning"),
        ("person1@example.test", "Cancelled: Quarterly planning"),
    ]


def test_api_pending_cancellation_list_and_decision(
    db_session: Session,
    client: TestClient,
    monkeypatch,
):
    meeting = make_meeting(db_session, 20)
    add_meeting_delegate(db_session, "employee-0", "employee-2")
    provider_cancellations = mock_calendar_cancellation(monkeypatch, db_session)
    sent: list[str] = []
    monkeypatch.setattr(
        "app.meeting.event_change_service.deliver_email",
        lambda recipient, subject, body: sent.append(recipient),
    )
    headers = {"X-User-Id": "employee-0"}

    response = client.post(f"/api/v1/meetings/{meeting.id}/cancel", headers=headers, json={})
    assert response.status_code == 200
    assert response.json()["status"] == "pending_approval"
    assert response.json()["meeting"]["status"] == "scheduled"
    persisted_request = (
        db_session.query(MeetingCancellationRequest)
        .filter_by(id=response.json()["request_id"])
        .one()
    )
    assert persisted_request.meeting_id == meeting.id
    assert persisted_request.requested_by == "employee-0"
    assert persisted_request.status == "pending"
    db_session.refresh(meeting)
    assert meeting.status == "scheduled"
    assert sent == []

    repeated_request = client.post(
        f"/api/v1/meetings/{meeting.id}/cancel",
        headers=headers,
        json={"reason": "Duplicate submission"},
    )
    assert repeated_request.status_code == 200
    assert repeated_request.json()["request_id"] == persisted_request.id
    assert (
        db_session.query(MeetingCancellationRequest)
        .filter_by(meeting_id=meeting.id, status="pending")
        .count()
        == 1
    )

    pending = client.get("/api/v1/meetings/cancellation-requests", headers=headers)
    assert pending.status_code == 200
    assert len(pending.json()) == 1
    assert pending.json()[0]["id"] == persisted_request.id
    assert pending.json()[0]["attendee_count"] == 21
    request_id = pending.json()[0]["id"]
    app.dependency_overrides[require_authenticated_actor_id] = lambda: "employee-2"
    decision = client.post(
        f"/api/v1/meetings/cancellation-requests/{request_id}/decide",
        headers=headers,
        json={"decision": "approve", "note": "Approved"},
    )
    assert decision.status_code == 200
    assert decision.json()["request"]["status"] == "approved"
    assert decision.json()["request"]["requested_by"] == "employee-0"
    assert decision.json()["request"]["decided_by"] == "employee-2"
    assert decision.json()["meeting"]["status"] == "cancelled"
    assert provider_cancellations == [meeting.id]
    assert len(sent) == 20
    db_session.refresh(persisted_request)
    assert persisted_request.status == "approved"
    assert persisted_request.decided_by == "employee-2"


def test_delegate_meeting_list_includes_manage_access(db_session: Session, client: TestClient):
    meeting = make_meeting(db_session, 1)
    add_meeting_delegate(db_session, "employee-0", "employee-2")
    app.dependency_overrides[require_authenticated_actor_id] = lambda: "employee-2"
    app.dependency_overrides[require_authenticated_principal] = lambda: AuthenticatedPrincipal(
        user_id="employee-2",
        account_id="delegate-account",
        email="person2@example.test",
        name="Employee 2",
        role="employee",
        team="Engineering",
        jti="delegate-session",
    )
    app.dependency_overrides[authenticated_actor_id] = lambda: "employee-2"

    response = client.get(
        "/api/v1/meetings/",
        params={"user_id": "employee-2", "email": "person2@example.test"},
    )

    assert response.status_code == 200
    record = next(item for item in response.json() if item["id"] == meeting.id)
    assert record["can_manage"] is True


def test_meeting_list_does_not_derive_manage_access_from_spoofed_query_id(
    db_session: Session,
    client: TestClient,
):
    meeting = make_meeting(db_session, 1)
    app.dependency_overrides.pop(authenticated_actor_id)
    app.dependency_overrides.pop(require_authenticated_principal)

    response = client.get(
        "/api/v1/meetings/",
        params={"user_id": "employee-0", "email": "person0@example.test"},
    )

    assert response.status_code == 401


def test_unauthenticated_request_cannot_impersonate_meeting_organizer_or_delegate(
    db_session: Session,
    client: TestClient,
):
    meeting = make_meeting(db_session, 20)
    add_meeting_delegate(db_session, "employee-0", "employee-2")
    _, cancellation_request, _, _ = request_meeting_cancellation(
        db_session, meeting.id, "employee-0"
    )
    assert cancellation_request is not None
    participant = db_session.query(MeetingParticipant).filter_by(meeting_id=meeting.id).first()
    assert participant is not None

    app.dependency_overrides.pop(require_authenticated_actor_id)
    app.dependency_overrides.pop(require_authenticated_principal)
    app.dependency_overrides.pop(authenticated_actor_id)
    organizer_query = {"organizer_id": "employee-0"}
    for spoofed_actor in ("employee-0", "employee-2"):
        spoofed = {"X-User-Id": spoofed_actor}
        requests = [
            client.post(f"/api/v1/meetings/{meeting.id}/cancel", headers=spoofed, params=organizer_query, json={}),
            client.get("/api/v1/meetings/cancellation-requests", headers=spoofed, params=organizer_query),
            client.post(
                f"/api/v1/meetings/cancellation-requests/{cancellation_request.id}/decide",
                headers=spoofed,
                params=organizer_query,
                json={"decision": "approve"},
            ),
            client.post(
                f"/api/v1/meetings/{meeting.id}/participants",
                headers=spoofed,
                params=organizer_query,
                json={"email": "intruder@example.test"},
            ),
            client.delete(
                f"/api/v1/meetings/{meeting.id}/participants/{participant.id}",
                headers=spoofed,
                params=organizer_query,
            ),
            client.get(f"/api/v1/meetings/{meeting.id}/participants", headers=spoofed, params=organizer_query),
            client.get(f"/api/v1/meetings/{meeting.id}/audit", headers=spoofed, params=organizer_query),
        ]
        assert [response.status_code for response in requests] == [401] * 7

    db_session.refresh(meeting)
    assert meeting.status == "scheduled"


def test_trusted_authenticated_delegate_can_cancel_with_existing_authorization(
    db_session: Session,
    client: TestClient,
):
    meeting = make_meeting(db_session, 2)
    add_meeting_delegate(db_session, "employee-0", "employee-2")
    app.dependency_overrides[require_authenticated_actor_id] = lambda: "employee-2"

    response = client.post(f"/api/v1/meetings/{meeting.id}/cancel", json={})

    assert response.status_code == 200
    assert response.json()["meeting"]["status"] == "cancelled"
