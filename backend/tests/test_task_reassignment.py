"""Story 3.6 (FR-17): reassign tasks and change due dates."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    Task,
    TaskAuditLog,
    TaskNotification,
    TaskReassignmentRequest,
)

MANAGER = {"actor_id": "user-002", "actor_role": "manager", "actor_team": "Engineering"}
HR = {"actor_id": "user-003", "actor_role": "hr_operations", "actor_team": "People Operations"}
ADMIN = {"actor_id": "user-004", "actor_role": "admin", "actor_team": "Administration"}
EMPLOYEE = {"actor_id": "user-001", "actor_role": "employee", "actor_team": "Engineering"}


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

    def test_principal(request: Request) -> AuthenticatedPrincipal:
        query = request.query_params
        return AuthenticatedPrincipal(
            user_id=query.get("actor_id", "test-employee"),
            account_id="test-auth-account",
            email=query.get("actor_email", "test@example.test"),
            name="Test Actor",
            role=query.get("actor_role", "employee"),
            team=query.get("actor_team"),
            jti="test-session",
        )

    original_overrides = app.dependency_overrides.copy()
    startup_handlers = app.router.on_startup[:]
    shutdown_handlers = app.router.on_shutdown[:]
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = test_principal
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        app.router.on_startup[:] = startup_handlers
        app.router.on_shutdown[:] = shutdown_handlers


@pytest.fixture()
def sent_emails(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "app.task_reassignment_service.send_email",
        lambda recipient, subject, body: sent.append((recipient, subject, body)),
    )
    return sent


def _employee(db: Session, name: str, team: str | None, active: bool = True) -> Employee:
    employee = Employee(
        id=str(uuid.uuid4()),
        name=name,
        email=f"{name.lower().replace(' ', '.')}@northstar.example",
        team=team,
        is_active=active,
    )
    db.add(employee)
    db.commit()
    return employee


def _task(db: Session, assignee: Employee) -> Task:
    now = datetime.now(timezone.utc)
    meeting_id = str(uuid.uuid4())
    recording_id = str(uuid.uuid4())
    transcript_id = str(uuid.uuid4())
    meeting = Meeting(
        id=meeting_id,
        organizer_id=MANAGER["actor_id"],
        title="Sprint Planning",
        start_at=now,
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=recording_id, meeting_id=meeting_id, status=RECORDING_STATUS_COMPLETED
    )
    transcript = MeetingTranscript(
        id=transcript_id,
        recording_id=recording_id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
        transcript_text="Please prepare the release notes by Friday.",
        segments=[{"start": 1.0, "end": 4.0, "text": "Please prepare the release notes by Friday.", "speaker": "Maya"}],
        completed_at=now,
    )
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript_id,
        status=ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
        completed_at=now,
    )
    item = MeetingActionItem(
        id=str(uuid.uuid4()),
        owner_name=assignee.name,
        confirmed_employee_id=assignee.id,
        ownership_status="confirmed",
        task_description="Prepare the release notes.",
        deadline_at=now + timedelta(days=3),
        deadline_text="by Friday",
        priority="high",
        confidence=0.95,
        source_segment_index=0,
        source_start_seconds=1.0,
        source_end_seconds=4.0,
        source_quote="Please prepare the release notes by Friday.",
        review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    )
    extraction.items.append(item)
    task = Task(
        id=str(uuid.uuid4()),
        title="Prepare the release notes.",
        assignee_id=assignee.id,
        creator_id=MANAGER["actor_id"],
        source="meeting_action_item",
        source_action_item_id=item.id,
        idempotency_key=f"meeting-action-item:{item.id}",
        meeting_id=meeting_id,
        transcript_id=transcript_id,
        source_segment_index=0,
        source_timestamp_seconds=1.0,
        due_at=now + timedelta(days=3),
        priority="high",
        status="pending",
        assignment_email_status="sent",
    )
    db.add_all([meeting, recording, transcript, extraction, task])
    db.commit()
    return task


def _audit(db: Session, task: Task) -> list[TaskAuditLog]:
    db.expire_all()
    return (
        db.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task.id)
        .order_by(TaskAuditLog.created_at)
        .all()
    )


def test_manager_reassigns_within_team_and_both_assignees_are_notified(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    ava = _employee(db_session, "Ava Patel", "Engineering")
    task = _task(db_session, rahul)

    response = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": ava.id, "reason": "Rahul is on leave"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "applied"
    assert body["task"]["assignee_id"] == ava.id
    assert body["task"]["assignee_team"] == "Engineering"
    assert {email[0] for email in sent_emails} == {rahul.email, ava.email}
    assert {n["kind"] for n in body["notifications"]} == {"reassigned_away", "reassigned_to"}
    assert all(n["status"] == "sent" for n in body["notifications"])
    assignment_notification = (
        db_session.query(TaskNotification)
        .filter_by(task_id=task.id, recipient_employee_id=ava.id)
        .one()
    )
    assert assignment_notification.kind == "reassigned_to"
    assert assignment_notification.idempotency_key == (
        f"assignment:{task.id}:2:{ava.id}"
    )
    assert assignment_notification.status == "sent"
    assert f"/tasks?task_id={task.id}" in assignment_notification.body
    assert f"/meetings/{task.meeting_id}/report?" in assignment_notification.body
    assert body["task"]["assignment_email_status"] == "sent"

    entries = _audit(db_session, task)
    assert [(e.action, e.outcome) for e in entries] == [("reassign", "applied")]
    assert "Rahul is on leave" in entries[0].detail
    assert db_session.get(Task, task.id).assignee_id == ava.id


def test_manager_reassignment_outside_team_requires_approval(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    priya = _employee(db_session, "Priya Sharma", "Finance")
    task = _task(db_session, rahul)

    response = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": priya.id},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "pending_approval"
    assert "approval" in body["message"]
    assert body["task"]["assignee_id"] == rahul.id
    assert body["task"]["pending_reassignment"]["to_assignee_name"] == "Priya Sharma"
    assert sent_emails == []
    assert [(e.action, e.outcome) for e in _audit(db_session, task)] == [
        ("reassign", "pending_approval")
    ]

    duplicate = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": priya.id},
    )
    assert duplicate.status_code == 409

    pending = client.get("/api/v1/tasks/reassignment-requests").json()
    assert [request["task_id"] for request in pending] == [task.id]


def test_hr_approval_applies_cross_team_reassignment_and_notifies(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    priya = _employee(db_session, "Priya Sharma", "Finance")
    task = _task(db_session, rahul)
    request_id = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": priya.id},
    ).json()["reassignment_request"]["id"]

    response = client.post(
        f"/api/v1/tasks/reassignment-requests/{request_id}/decision",
        params=HR,
        json={"decision": "approve", "note": "Finance owns reporting"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["assignee_id"] == priya.id
    assert body["task"]["pending_reassignment"] is None
    assert body["reassignment_request"]["status"] == "approved"
    assert body["reassignment_request"]["decided_by"] == HR["actor_id"]
    assert {email[0] for email in sent_emails} == {rahul.email, priya.email}
    assert [e.outcome for e in _audit(db_session, task)] == ["pending_approval", "applied"]

    again = client.post(
        f"/api/v1/tasks/reassignment-requests/{request_id}/decision",
        params=HR,
        json={"decision": "approve"},
    )
    assert again.status_code == 409


def test_rejected_cross_team_request_leaves_task_unchanged(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    priya = _employee(db_session, "Priya Sharma", "Finance")
    task = _task(db_session, rahul)
    request_id = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": priya.id},
    ).json()["reassignment_request"]["id"]

    response = client.post(
        f"/api/v1/tasks/reassignment-requests/{request_id}/decision",
        params=ADMIN,
        json={"decision": "reject"},
    )

    assert response.status_code == 200
    assert response.json()["task"]["assignee_id"] == rahul.id
    assert response.json()["reassignment_request"]["status"] == "rejected"
    assert sent_emails == []
    assert [e.outcome for e in _audit(db_session, task)] == ["pending_approval", "rejected"]


def test_manager_cannot_approve_cross_team_request(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    priya = _employee(db_session, "Priya Sharma", "Finance")
    task = _task(db_session, rahul)
    request_id = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": priya.id},
    ).json()["reassignment_request"]["id"]

    response = client.post(
        f"/api/v1/tasks/reassignment-requests/{request_id}/decision",
        params=MANAGER,
        json={"decision": "approve"},
    )

    assert response.status_code == 403
    assert "HR / Operations or an Admin" in response.json()["detail"]
    assert db_session.get(TaskReassignmentRequest, request_id).status == "pending"
    assert [e.outcome for e in _audit(db_session, task)] == ["pending_approval", "denied"]


def test_employee_reassignment_is_denied_with_explanation_and_audit_entry(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    ava = _employee(db_session, "Ava Patel", "Engineering")
    task = _task(db_session, rahul)

    response = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=EMPLOYEE,
        json={"assignee_id": ava.id},
    )

    assert response.status_code == 403
    assert response.json()["detail"].startswith("Employees cannot reassign tasks")
    entries = _audit(db_session, task)
    assert [(e.actor_id, e.action, e.outcome) for e in entries] == [
        ("user-001", "reassign", "denied")
    ]
    assert entries[0].detail == response.json()["detail"]
    assert db_session.get(Task, task.id).assignee_id == rahul.id
    assert sent_emails == []


def test_manager_cannot_reassign_another_teams_task(
    client: TestClient, db_session: Session, sent_emails
):
    priya = _employee(db_session, "Priya Sharma", "Finance")
    noah = _employee(db_session, "Noah Martin", "Finance")
    task = _task(db_session, priya)

    response = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": noah.id},
    )

    assert response.status_code == 403
    assert "own team (Engineering)" in response.json()["detail"]
    assert [e.outcome for e in _audit(db_session, task)] == ["denied"]
    assert db_session.get(Task, task.id).assignee_id == priya.id


def test_hr_can_reassign_across_teams_directly(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    chloe = _employee(db_session, "Chloe Nguyen", "Operations")
    task = _task(db_session, rahul)

    response = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=HR,
        json={"assignee_id": chloe.id},
    )

    assert response.status_code == 200
    assert response.json()["task"]["assignee_id"] == chloe.id


@pytest.mark.parametrize(
    ("make_target", "status_code", "message"),
    [
        (lambda db, current: _employee(db, "Old Hand", "Engineering", active=False), 400, "inactive"),
        (lambda db, current: current, 400, "already assigned"),
    ],
)
def test_invalid_reassignment_targets_are_rejected(
    client: TestClient, db_session: Session, sent_emails, make_target, status_code, message
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    task = _task(db_session, rahul)
    target = make_target(db_session, rahul)

    response = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": target.id},
    )

    assert response.status_code == status_code
    assert message in response.json()["detail"]


def test_due_date_change_updates_calendar_and_notifies_assignee(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    task = _task(db_session, rahul)
    new_due = (datetime.now(timezone.utc) + timedelta(days=10)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )

    before = client.get("/api/v1/tasks/calendar.ics", params={"assignee_email": rahul.email}).text
    assert "SEQUENCE:0" in before

    response = client.patch(
        f"/api/v1/tasks/{task.id}/due-date",
        params=MANAGER,
        json={"due_at": new_due.isoformat(), "reason": "Client moved the review"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "applied"
    assert datetime.fromisoformat(body["task"]["due_at"]).replace(tzinfo=timezone.utc) == new_due
    assert body["task"]["calendar_sequence"] == 1
    assert [n["kind"] for n in body["notifications"]] == ["due_date_changed"]
    assert len(sent_emails) == 1
    recipient, subject, email_body = sent_emails[0]
    assert recipient == rahul.email
    assert subject.startswith("Due date changed")
    assert new_due.strftime("%d %b %Y %H:%M UTC") in email_body

    calendar = client.get("/api/v1/tasks/calendar.ics", params={"assignee_email": rahul.email}).text
    assert f"UID:{task.id}@ai-office-operations" in calendar
    assert "SEQUENCE:1" in calendar
    assert f"DTSTART:{new_due.strftime('%Y%m%dT%H%M%SZ')}" in calendar

    entries = _audit(db_session, task)
    assert [(e.action, e.outcome) for e in entries] == [("change_due_date", "applied")]
    assert "Client moved the review" in entries[0].detail


def test_due_date_in_the_past_is_rejected(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    task = _task(db_session, rahul)

    response = client.patch(
        f"/api/v1/tasks/{task.id}/due-date",
        params=MANAGER,
        json={"due_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 400
    assert "future" in response.json()["detail"]
    assert sent_emails == []


def test_employee_due_date_change_is_denied_and_audited(
    client: TestClient, db_session: Session, sent_emails
):
    rahul = _employee(db_session, "Rahul", "Engineering")
    task = _task(db_session, rahul)
    original_due = db_session.get(Task, task.id).due_at

    response = client.patch(
        f"/api/v1/tasks/{task.id}/due-date",
        params=EMPLOYEE,
        json={"due_at": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()},
    )

    assert response.status_code == 403
    assert "Employees cannot change due dates" in response.json()["detail"]
    assert [(e.action, e.outcome) for e in _audit(db_session, task)] == [
        ("change_due_date", "denied")
    ]
    assert db_session.get(Task, task.id).due_at == original_due

    audit = client.get(f"/api/v1/tasks/{task.id}/audit").json()
    assert audit[0]["outcome"] == "denied"


def test_failed_notification_is_recorded_without_undoing_reassignment(
    client: TestClient, db_session: Session, monkeypatch
):
    def failing_send(recipient, subject, body):
        raise OSError("SMTP unavailable")

    monkeypatch.setattr("app.task_reassignment_service.send_email", failing_send)
    rahul = _employee(db_session, "Rahul", "Engineering")
    ava = _employee(db_session, "Ava Patel", "Engineering")
    task = _task(db_session, rahul)

    response = client.patch(
        f"/api/v1/tasks/{task.id}/assignee",
        params=MANAGER,
        json={"assignee_id": ava.id},
    )

    assert response.status_code == 200
    assert response.json()["task"]["assignee_id"] == ava.id
    statuses = {n["status"] for n in response.json()["notifications"]}
    assert statuses == {"failed"}
    db_session.expire_all()
    stored = db_session.query(TaskNotification).filter_by(task_id=task.id).all()
    assert {n.error for n in stored} == {"SMTP unavailable"}
    assert db_session.get(Task, task.id).assignment_email_status == "failed"
