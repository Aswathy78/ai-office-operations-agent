from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app import task_router
from app.auth_service import AuthenticatedPrincipal
from app.authentication import get_authenticated_principal, require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import AuthAuditEvent, Base, Employee, Meeting, Task, TaskAuditLog


@pytest.fixture()
def http_access_api(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    db: Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    manager = Employee(
        id="manager-1", name="Manager", email="manager@example.test",
        team="Engineering", department="Operations", is_active=True,
    )
    employee = Employee(
        id="employee-1", name="Employee", email="employee@example.test",
        team="Engineering", department="Operations", manager_id=manager.id, is_active=True,
    )
    other = Employee(
        id="employee-2", name="Other", email="other@example.test",
        team="Finance", department="Finance", is_active=True,
    )
    db.add_all([manager, employee, other])
    task = Task(
        id="task-1", title="Employee task", assignee_id=employee.id,
        creator_id=employee.id, source="assistant", idempotency_key="key-1",
        due_at=datetime(2026, 11, 12, tzinfo=timezone.utc), priority="medium",
        status="pending",
    )
    other_task = Task(
        id="task-2", title="Other task", assignee_id=other.id,
        creator_id=other.id, source="assistant", idempotency_key="key-2",
        due_at=datetime(2026, 11, 12, tzinfo=timezone.utc), priority="medium",
        status="pending",
    )
    db.add_all([task, other_task])
    db.add(TaskAuditLog(
        id="audit-1", task_id=task.id, actor_id=employee.id,
        actor_role="employee", action="create", outcome="applied", detail="Created.",
    ))
    db.commit()
    current: dict[str, AuthenticatedPrincipal | None] = {"principal": None}

    def override_db():
        yield db

    def override_principal():
        return current["principal"]

    previous_overrides = app.dependency_overrides.copy()
    previous_startup = app.router.on_startup[:]
    previous_shutdown = app.router.on_shutdown[:]
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides.pop(require_authenticated_principal, None)
    app.dependency_overrides[get_authenticated_principal] = override_principal
    monkeypatch.setenv("EMAIL_BOUNCE_WEBHOOK_TOKEN", "test-token")
    try:
        with TestClient(app) as client:
            yield client, db, current, employee, other, manager
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        app.router.on_startup[:] = previous_startup
        app.router.on_shutdown[:] = previous_shutdown
        db.rollback()
        db.close()
        engine.dispose()


def _principal(employee: Employee, role: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=employee.id,
        account_id=f"account-{employee.id}",
        email=employee.email,
        name=employee.name,
        role=role,
        team=employee.team,
        jti=f"token-{employee.id}",
    )


def test_targeted_http_routes_require_auth_and_audit_denials(http_access_api):
    client, db, _current, employee, _other, _manager = http_access_api
    paths = [
        ("GET", "/api/v1/tasks/calendar.ics", {"assignee_email": employee.email}),
        ("POST", "/api/v1/tasks/task-1/assignment-email/retry", None),
        ("GET", "/api/v1/tasks/task-1/audit", None),
        ("GET", "/api/v1/tasks/task-1/notifications", None),
        ("GET", "/api/v1/tasks/reassignment-requests", None),
        ("GET", "/api/v1/meetings/employees", None),
        ("GET", "/api/v1/email-drafts/directory/groups", None),
    ]
    for method, path, params in paths:
        response = client.request(method, path, params=params)
        assert response.status_code == 401, path

    denials = db.query(AuthAuditEvent).filter(AuthAuditEvent.event == "access_denied").all()
    assert len(denials) == len(paths)
    assert all(event.user_id is None and event.actor_id is None for event in denials)
    assert {event.detail for event in denials} == {
        "HTTP 401 GET denied.", "HTTP 401 POST denied."
    }
    assert all("token" not in event.detail.casefold() for event in denials)


def test_caller_supplied_identity_and_role_do_not_grant_task_access(http_access_api):
    client, db, current, employee, other, _manager = http_access_api
    current["principal"] = _principal(employee, "employee")

    calendar = client.get(
        "/api/v1/tasks/calendar.ics",
        params={
            "assignee_email": other.email,
            "actor_id": other.id,
            "actor_role": "admin",
            "actor_email": other.email,
        },
    )
    audit = client.get(
        f"/api/v1/tasks/{'task-2'}/audit",
        params={"actor_id": other.id, "actor_role": "admin"},
    )
    assert calendar.status_code == 403
    assert audit.status_code == 403

    records = db.query(AuthAuditEvent).filter(AuthAuditEvent.event == "access_denied").all()
    assert len(records) == 2
    assert all(event.actor_id == employee.id for event in records)
    assert all("admin" not in event.detail.casefold() for event in records)


def test_existing_access_rules_allow_own_and_reporting_line_data(http_access_api):
    client, _db, current, employee, other, manager = http_access_api
    current["principal"] = _principal(employee, "employee")
    assert client.get(
        "/api/v1/tasks/calendar.ics", params={"assignee_email": employee.email}
    ).status_code == 200
    assert client.get("/api/v1/tasks/task-1/audit").status_code == 200
    assert client.get("/api/v1/tasks/task-1/notifications").status_code == 200

    current["principal"] = _principal(manager, "manager")
    assert client.get(
        "/api/v1/tasks/calendar.ics", params={"assignee_email": manager.email}
    ).status_code == 200
    assert client.get(
        "/api/v1/tasks/calendar.ics", params={"assignee_email": employee.email}
    ).status_code == 200
    assert client.get(
        "/api/v1/tasks/calendar.ics", params={"assignee_email": other.email}
    ).status_code == 403
    assert client.get("/api/v1/meetings/employees").status_code == 200
    assert client.get("/api/v1/email-drafts/directory/groups").status_code == 200


def test_manager_only_route_denials_are_audited_without_request_data(http_access_api):
    client, db, current, employee, _other, _manager = http_access_api
    current["principal"] = _principal(employee, "employee")
    paths = [
        ("GET", "/api/v1/tasks/escalations"),
        ("GET", "/api/v1/tasks/team-dashboard"),
        ("GET", "/api/v1/tasks/operational-monitoring"),
        ("GET", "/api/v1/tasks/operational-monitoring/export.csv"),
        ("POST", "/api/v1/tasks/reminders/run"),
        ("POST", "/api/v1/tasks/calendar-sync/run"),
    ]

    for method, path in paths:
        response = client.request(method, path, params={"team": "Sensitive Team"})
        assert response.status_code == 403, path

    records = (
        db.query(AuthAuditEvent)
        .filter(AuthAuditEvent.event == "access_denied")
        .order_by(AuthAuditEvent.created_at)
        .all()
    )
    assert len(records) == len(paths)
    assert all(record.actor_id == employee.id for record in records)
    assert all(
        record.detail == f"HTTP 403 {method} denied."
        for record, (method, _path) in zip(records, paths)
    )
    assert all("Sensitive Team" not in record.detail for record in records)


def test_meeting_report_denial_is_audited_without_meeting_data(http_access_api):
    client, db, current, employee, _other, manager = http_access_api
    meeting = Meeting(
        id="private-meeting",
        organizer_id=manager.id,
        title="Private meeting title",
        start_at=datetime(2026, 11, 12, 10, tzinfo=timezone.utc),
        end_at=datetime(2026, 11, 12, 11, tzinfo=timezone.utc),
        join_url="https://meet.example.test/private",
        status="completed",
    )
    db.add(meeting)
    db.commit()
    current["principal"] = _principal(employee, "employee")

    response = client.get(f"/api/v1/meetings/{meeting.id}/report")

    assert response.status_code == 403
    record = (
        db.query(AuthAuditEvent)
        .filter_by(event="access_denied", actor_id=employee.id)
        .one()
    )
    assert record.detail == "HTTP 403 GET denied."
    assert "private-meeting" not in record.detail
    assert "Private meeting title" not in record.detail


def test_meeting_lobby_listing_denial_is_audited(http_access_api):
    client, db, current, employee, _other, manager = http_access_api
    meeting = Meeting(
        id="private-lobby-meeting",
        organizer_id=manager.id,
        title="Private lobby",
        start_at=datetime(2026, 11, 12, 10, tzinfo=timezone.utc),
        end_at=datetime(2026, 11, 12, 11, tzinfo=timezone.utc),
        join_url="https://meet.example.test/private-lobby",
        status="completed",
    )
    db.add(meeting)
    db.commit()
    current["principal"] = _principal(employee, "employee")

    response = client.get(f"/api/v1/meetings/{meeting.id}/lobby")

    assert response.status_code == 403
    record = (
        db.query(AuthAuditEvent)
        .filter_by(event="access_denied", actor_id=employee.id)
        .one()
    )
    assert record.detail == "HTTP 403 GET denied."
    assert "private-lobby-meeting" not in record.detail


def test_assignment_retry_is_authenticated_and_scoped(http_access_api, monkeypatch):
    client, _db, current, employee, other, _manager = http_access_api
    attempts: list[str] = []
    monkeypatch.setattr(
        task_router, "attempt_assignment_email",
        lambda db, task_id, retry_failed=False: (
            attempts.append(task_id) or db.get(Task, task_id)
        ),
    )

    current["principal"] = _principal(employee, "employee")
    assert client.post("/api/v1/tasks/task-1/assignment-email/retry").status_code == 403
    current["principal"] = _principal(other, "admin")
    assert client.post("/api/v1/tasks/task-1/assignment-email/retry").status_code == 200
    assert attempts == ["task-1"]


def test_manager_calendar_sync_retry_respects_department_and_reporting_line(
    http_access_api, monkeypatch
):
    client, db, current, _employee, _other, manager = http_access_api
    current["principal"] = _principal(manager, "manager")
    attempts: list[str] = []
    monkeypatch.setattr(
        task_router, "retry_failed",
        lambda session, task_id: attempts.append(task_id),
    )

    response = client.post("/api/v1/tasks/task-2/calendar-sync/retry")

    assert response.status_code == 403
    assert attempts == []
    record = (
        db.query(AuthAuditEvent)
        .filter_by(event="access_denied", actor_id=manager.id)
        .one()
    )
    assert record.detail == "HTTP 403 POST denied."


def test_reassignment_requests_are_role_protected(http_access_api):
    client, _db, current, employee, _other, manager = http_access_api
    current["principal"] = _principal(employee, "employee")
    assert client.get("/api/v1/tasks/reassignment-requests").status_code == 403
    current["principal"] = _principal(manager, "manager")
    assert client.get("/api/v1/tasks/reassignment-requests").status_code == 200


def test_bounce_endpoint_requires_webhook_token_or_authenticated_admin(http_access_api):
    client, db, current, _employee, _other, manager = http_access_api
    payload = {"message_id": "<unknown@example.test>", "bounce_type": "hard"}

    spoofed = client.post(
        "/api/v1/email-drafts/bounces",
        params={"actor_role": "admin", "actor_id": manager.id},
        json=payload,
    )
    assert spoofed.status_code == 403, spoofed.text
    denial = db.query(AuthAuditEvent).filter(AuthAuditEvent.event == "access_denied").one()
    assert denial.actor_id is None
    assert denial.detail == "HTTP 403 POST denied."

    current["principal"] = _principal(manager, "admin")
    assert client.post(
        "/api/v1/email-drafts/bounces",
        params={"actor_role": "employee"},
        json=payload,
    ).status_code == 200

    current["principal"] = None
    assert client.post(
        "/api/v1/email-drafts/bounces",
        headers={"X-Bounce-Webhook-Token": "test-token"},
        json=payload,
    ).status_code == 200
