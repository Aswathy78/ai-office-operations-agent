"""Administrator audit-log page: one read-only trail merged from the system's own records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import (
    AssistantAuditEvent,
    Base,
    EmailDraftEvent,
    Employee,
    MeetingAuditLog,
    TaskAuditLog,
    WorkflowEvent,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(Employee(id="emp-1", name="Ada Admin", email="ada@example.test", is_active=True))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _principal(role: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(user_id="emp-1", account_id="acc-1", email="ada@example.test",
                                  name="Ada", role=role, team=None, jti="jti")


@pytest.fixture()
def client(db_session: Session):
    current = {"role": "admin"}
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_authenticated_principal] = lambda: _principal(current["role"])
    with TestClient(app) as test_client:
        test_client.as_role = lambda role: current.update(role=role)
        yield test_client
    app.dependency_overrides.clear()


def _seed(db: Session) -> None:
    db.add_all([
        TaskAuditLog(id="t1", task_id="task-1", actor_id="emp-1", actor_role="manager", action="reassign",
                     outcome="applied", detail="Moved to Dan", created_at=NOW - timedelta(minutes=5)),
        TaskAuditLog(id="t2", task_id="task-2", actor_id="emp-1", actor_role="employee", action="change_due_date",
                     outcome="denied", detail="Not allowed", created_at=NOW - timedelta(minutes=1)),
        MeetingAuditLog(id="m1", meeting_id="mt-1", actor_id="emp-1", action="meeting_cancelled",
                        details={"reason": "Clash", "nested": {"x": 1}}, created_at=NOW - timedelta(minutes=3)),
        AssistantAuditEvent(id="a1", actor_id="emp-1", actor_role="admin", tool_name="send_email",
                            event="approval_requested", risk_level="high", detail="Needs approval",
                            data={"secret_payload": "TOP-SECRET-BODY"}, created_at=NOW - timedelta(minutes=2)),
        EmailDraftEvent(id="e1", draft_id="d1", sequence=1, from_status="sending", to_status="failed",
                        actor_id="system", detail="535 Username and Password not accepted",
                        created_at=NOW - timedelta(minutes=4)),
        WorkflowEvent(id="w1", workflow_run_id="run-1", sequence=1, event_type="dead_lettered",
                      step_key="summary", error="Groq request timed out", created_at=NOW - timedelta(minutes=6)),
    ])
    db.commit()


def test_only_an_administrator_can_read_the_audit_trail(client, db_session):
    _seed(db_session)
    for role in ("employee", "manager", "hr_operations"):
        client.as_role(role)
        response = client.get("/api/v1/admin/audit-logs")
        assert response.status_code == 403
        assert set(response.json()) == {"detail"}


def test_merges_every_source_newest_first_with_names(client, db_session):
    _seed(db_session)
    body = client.get("/api/v1/admin/audit-logs").json()
    assert [e["id"] for e in body["entries"]] == [
        "task:t2", "assistant:a1", "meeting:m1", "email:e1", "task:t1", "workflow:w1"]
    assert body["counts"] == {"task": 2, "meeting": 1, "assistant": 1, "email": 1, "workflow": 1, "auth": 0}
    first = body["entries"][0]
    assert first["actor_name"] == "Ada Admin" and first["outcome"] == "denied" and first["source"] == "task"
    assert next(e for e in body["entries"] if e["source"] == "email")["actor_name"] is None  # "system"


def test_filter_by_source_and_search(client, db_session):
    _seed(db_session)
    tasks = client.get("/api/v1/admin/audit-logs", params={"source": "task"}).json()
    assert {e["source"] for e in tasks["entries"]} == {"task"}
    assert tasks["counts"]["workflow"] == 1  # counts still describe every source
    found = client.get("/api/v1/admin/audit-logs", params={"q": "reassign"}).json()
    assert [e["id"] for e in found["entries"]] == ["task:t1"]
    assert client.get("/api/v1/admin/audit-logs", params={"q": "no-such-text"}).json()["entries"] == []
    assert client.get("/api/v1/admin/audit-logs", params={"q": "100%"}).json()["entries"] == []  # wildcard is literal


def test_limit_and_bad_source(client, db_session):
    _seed(db_session)
    assert len(client.get("/api/v1/admin/audit-logs", params={"limit": 2}).json()["entries"]) == 2
    assert client.get("/api/v1/admin/audit-logs", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/admin/audit-logs", params={"limit": 9999}).status_code == 422
    assert client.get("/api/v1/admin/audit-logs", params={"source": "bogus"}).status_code == 422


def test_secrets_and_raw_errors_are_not_exposed(client, db_session):
    _seed(db_session)
    response = client.get("/api/v1/admin/audit-logs")
    assert response.headers["cache-control"] == "no-store"
    assert "TOP-SECRET-BODY" not in response.text and "secret_payload" not in response.text
    assert "535" not in response.text and "Username and Password not accepted" not in response.text
    email = next(e for e in response.json()["entries"] if e["source"] == "email")
    assert "sign-in" in email["detail"].lower()


def test_long_detail_is_clipped_and_meeting_details_flattened(client, db_session):
    db_session.add(TaskAuditLog(id="long", task_id="t", actor_id="emp-1", actor_role="admin", action="create",
                                outcome="applied", detail="x" * 5000, created_at=NOW))
    db_session.add(MeetingAuditLog(id="m2", meeting_id="mt", actor_id="emp-1", action="participant_added",
                                   details={"email": "a@b.test", "nested": {"k": "v"}}, created_at=NOW))
    db_session.commit()
    entries = {e["id"]: e for e in client.get("/api/v1/admin/audit-logs").json()["entries"]}
    assert len(entries["task:long"]["detail"]) <= 300
    assert entries["meeting:m2"]["detail"] == "Email: a@b.test"


def test_routine_workflow_chatter_is_left_out(client, db_session):
    for index in range(5):
        db_session.add(WorkflowEvent(id=f"r{index}", workflow_run_id="run-9", sequence=index + 1,
                                     event_type="step_completed", created_at=NOW))
    db_session.add(WorkflowEvent(id="real", workflow_run_id="run-9", sequence=9, event_type="retry_scheduled",
                                 error="Groq request timed out", created_at=NOW - timedelta(minutes=1)))
    db_session.commit()
    body = client.get("/api/v1/admin/audit-logs").json()
    assert [e["id"] for e in body["entries"]] == ["workflow:real"]
    assert body["counts"]["workflow"] == 1
    only = client.get("/api/v1/admin/audit-logs", params={"source": "workflow"}).json()
    assert [e["id"] for e in only["entries"]] == ["workflow:real"]


def test_empty_trail(client):
    body = client.get("/api/v1/admin/audit-logs").json()
    assert body["entries"] == [] and sum(body["counts"].values()) == 0


def test_details_are_readable_and_carry_no_internal_ids(client, db_session):
    uuid = "da321cb4-d8c2-46e2-9939-16d9a2087b25"
    db_session.add_all([
        TaskAuditLog(id="r1", task_id="3fd8d0de-6617-49c5-a4d8-f9616b4c95cb", actor_id="system", actor_role="system",
                     action="reminder_sent", outcome="system_event", created_at=NOW,
                     detail="[task-reminder:3fd8d0de-6617:a1:d3:overdue16:sent] Reminder reminder_overdue sent to "
                            "john_doe@example.test."),
        TaskAuditLog(id="r2", task_id="t-2", actor_id="emp-1", actor_role="admin", action="create", outcome="applied",
                     created_at=NOW - timedelta(seconds=1),
                     detail=f"Created for Ava, due 2026-11-06T11:30:00+00:00 (tool execution {uuid})."),
        AssistantAuditEvent(id="a9", actor_id="emp-1", actor_role="admin", tool_name="meeting.schedule",
                            event="auto_approved", risk_level="low", approval_id=uuid, data={},
                            detail="Schedule at 2026-11-02T05:30:00+00:00 for 30 min.",
                            created_at=NOW - timedelta(seconds=2)),
        WorkflowEvent(id="w9", workflow_run_id="ae57b1c4-1111-2222-3333-444455556666", sequence=1,
                      event_type="manual_retry", step_key="task_reminders", message=None,
                      created_at=NOW - timedelta(seconds=3)),
    ])
    db_session.commit()
    entries = {e["id"]: e for e in client.get("/api/v1/admin/audit-logs").json()["entries"]}

    sent = entries["task:r1"]
    assert sent["action_label"] == "Reminder sent"
    assert sent["detail"] == "Reminder reminder overdue sent to john_doe@example.test."   # e-mail untouched
    assert "[" not in sent["detail"] and "reference" not in sent

    created = entries["task:r2"]
    assert created["detail"] == "Created for Ava, due 6 Nov 2026 11:30 UTC."
    assert uuid not in created["detail"]

    assistant = entries["assistant:a9"]
    assert (assistant["action_label"], assistant["target"]) == ("Auto-approved", "Schedule meeting")
    assert assistant["detail"] == "Schedule at 2 Nov 2026 05:30 UTC for 30 min."

    workflow = entries["workflow:w9"]
    assert workflow["action_label"] == "Retried by an administrator" and workflow["target"] == "Task reminders"
    assert workflow["detail"] == "The run was restarted from its first step."   # empty message gets a sentence


def test_unknown_actions_still_get_a_readable_label(client, db_session):
    db_session.add(TaskAuditLog(id="u1", task_id="t", actor_id="emp-1", actor_role="admin",
                                action="some_new_action", outcome="applied", detail="x", created_at=NOW))
    db_session.commit()
    entry = client.get("/api/v1/admin/audit-logs").json()["entries"][0]
    assert entry["action_label"] == "Some new action"
