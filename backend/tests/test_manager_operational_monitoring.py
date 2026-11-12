"""Tests for Story 9.3 – Manager Operational Monitoring Portal.

Covers:
- Role-based access (employee → 403, manager → own team, admin → org-wide)
- Manager isolation (cannot see another manager's team data)
- Action items, task status rows, workflow failures, failure reasons
- CSV export endpoint
"""

from __future__ import annotations

import csv
import io
import uuid
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
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    Task,
    TaskCalendarSync,
    TaskNotification,
    WorkflowRun,
    TASK_STATUS_PENDING,
    TASK_STATUS_COMPLETED,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def monitoring_env():
    """Isolated in-memory SQLite DB with seeded data for monitoring tests.

    SQLite does NOT enforce FK constraints by default (PRAGMA foreign_keys is
    OFF), so placeholder IDs for transcript_id are acceptable here.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db: Session = session_factory()

    # --- Employees ---
    manager_a = Employee(
        id="mgr-a", name="Manager Alpha", email="mgr.a@example.com",
        team="Engineering", department="Operations", manager_id=None, is_active=True,
    )
    manager_b = Employee(
        id="mgr-b", name="Manager Beta", email="mgr.b@example.com",
        team="Finance", department="Finance", manager_id=None, is_active=True,
    )
    emp1 = Employee(
        id="emp-1", name="Alice Smith", email="alice@example.com",
        team="Engineering", department="Operations", manager_id="mgr-a", is_active=True,
    )
    emp2 = Employee(
        id="emp-2", name="Bob Jones", email="bob@example.com",
        team="Engineering", department="Operations", manager_id="mgr-a", is_active=True,
    )
    emp3 = Employee(
        id="emp-3", name="Carol Lane", email="carol@example.com",
        team="Finance", department="Finance", manager_id="mgr-b", is_active=True,
    )
    db.add_all([manager_a, manager_b, emp1, emp2, emp3])

    # --- Tasks (idempotency_key is NOT NULL unique) ---
    task1 = Task(
        id="task-1", title="Deploy service", assignee_id="emp-1",
        creator_id="mgr-a", status=TASK_STATUS_PENDING,
        priority="high", due_at=NOW - timedelta(hours=5),
        assignment_email_status="failed",
        assignment_email_error="SMTP auth failed",
        source="direct",
        idempotency_key="idem-task-1",
    )
    task2 = Task(
        id="task-2", title="Write report", assignee_id="emp-2",
        creator_id="mgr-a", status=TASK_STATUS_COMPLETED,
        priority="medium", due_at=NOW + timedelta(days=2),
        assignment_email_status="sent",
        assignment_email_error=None,
        source="direct",
        idempotency_key="idem-task-2",
    )
    task3 = Task(
        id="task-3", title="Finance review", assignee_id="emp-3",
        creator_id="mgr-b", status=TASK_STATUS_PENDING,
        priority="low", due_at=NOW + timedelta(days=1),
        assignment_email_status="sent",
        assignment_email_error=None,
        source="direct",
        idempotency_key="idem-task-3",
    )
    db.add_all([task1, task2, task3])

    # --- Calendar sync for task1 (failed) ---
    cal_sync = TaskCalendarSync(
        id=_uid(), task_id="task-1", provider="google",
        status="failed", attempt_count=3,
        last_error="Calendar API quota exceeded",
        created_at=NOW, updated_at=NOW,
    )
    db.add(cal_sync)

    # --- Notification failure for task1 ---
    notif = TaskNotification(
        id=_uid(), task_id="task-1",
        recipient_employee_id="emp-1",
        kind="assignment", subject="Your new task",
        body="Please complete Deploy service",
        idempotency_key=_uid(),
        status="failed",
        error="Connection timeout",
        created_at=NOW,
    )
    db.add(notif)

    # --- Meeting (manager_a is organiser) ---
    # Meeting.start_at / end_at / join_url are all required NOT NULL columns
    meeting = Meeting(
        id="meet-1", title="Sprint Planning",
        organizer_id="mgr-a", status="completed",
        start_at=NOW - timedelta(days=1),
        end_at=NOW - timedelta(days=1) + timedelta(hours=1),
        join_url="https://meet.example.com/sprint-planning",
    )
    db.add(meeting)

    # transcript_id FK is NOT NULL but SQLite won't enforce FK by default
    extraction = MeetingActionItemExtraction(
        id="extract-1", meeting_id="meet-1",
        transcript_id="trans-placeholder-1",
        status="completed",
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(extraction)

    action_item = MeetingActionItem(
        id="ai-1", extraction_id="extract-1",
        proposed_employee_id="emp-1",
        confirmed_employee_id="emp-1",
        owner_name="Alice Smith",
        task_description="Set up monitoring dashboard",
        deadline_at=NOW + timedelta(days=3),
        priority="high",
        ownership_status="confirmed",
        review_status="pending_review",
        confidence=0.9,
        source_segment_index=0,
        source_start_seconds=0.0,
        source_end_seconds=30.0,
        created_at=NOW,
    )
    db.add(action_item)

    # --- Workflow failure for manager_a's meeting ---
    wf_run = WorkflowRun(
        id="wf-1", workflow_type="meeting_processing",
        meeting_id="meet-1",
        idempotency_key="wf-key-1",
        status="failed",
        attempt_count=5, max_attempts=5,
        last_error="Transcription service unavailable",
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(minutes=30),
    )
    db.add(wf_run)

    db.commit()

    yield {
        "db": db,
        "session_factory": session_factory,
        "engine": engine,
    }

    db.close()
    engine.dispose()


@pytest.fixture()
def monitoring_client(monitoring_env):
    """Return a client factory injecting the in-memory DB and a given principal."""

    def _make(principal: AuthenticatedPrincipal) -> TestClient:
        def override_db():
            db: Session = monitoring_env["session_factory"]()
            try:
                yield db
            finally:
                db.close()

        def override_principal():
            return principal

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_authenticated_principal] = override_principal
        return TestClient(app, raise_server_exceptions=True)

    yield _make
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper principals
# ---------------------------------------------------------------------------

def _manager_a() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id="mgr-a",
        account_id="acc-mgr-a",
        name="Manager Alpha",
        email="mgr.a@example.com",
        role="manager",
        team="Engineering",
        jti="session-mgr-a",
    )


def _manager_b() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id="mgr-b",
        account_id="acc-mgr-b",
        name="Manager Beta",
        email="mgr.b@example.com",
        role="manager",
        team="Finance",
        jti="session-mgr-b",
    )


def _employee() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id="emp-1",
        account_id="acc-emp-1",
        name="Alice Smith",
        email="alice@example.com",
        role="employee",
        team="Engineering",
        jti="session-emp-1",
    )


def _admin() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id="admin-1",
        account_id="acc-admin-1",
        name="Admin",
        email="admin@example.com",
        role="admin",
        team=None,
        jti="session-admin-1",
    )


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:
    def test_employee_gets_403(self, monitoring_client):
        response = monitoring_client(_employee()).get("/api/v1/tasks/operational-monitoring")
        assert response.status_code == 403

    def test_manager_a_gets_200(self, monitoring_client):
        response = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring")
        assert response.status_code == 200

    def test_manager_b_gets_200(self, monitoring_client):
        response = monitoring_client(_manager_b()).get("/api/v1/tasks/operational-monitoring")
        assert response.status_code == 200

    def test_admin_gets_200(self, monitoring_client):
        response = monitoring_client(_admin()).get("/api/v1/tasks/operational-monitoring")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Manager isolation
# ---------------------------------------------------------------------------

class TestManagerIsolation:
    def test_manager_a_sees_own_team_tasks(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        task_ids = {r["task_id"] for r in data["task_status_rows"]}
        assert "task-1" in task_ids
        assert "task-2" in task_ids
        assert "task-3" not in task_ids

    def test_manager_b_sees_own_team_tasks(self, monitoring_client):
        data = monitoring_client(_manager_b()).get("/api/v1/tasks/operational-monitoring").json()
        task_ids = {r["task_id"] for r in data["task_status_rows"]}
        assert "task-3" in task_ids
        assert "task-1" not in task_ids
        assert "task-2" not in task_ids

    def test_manager_cannot_bypass_isolation_via_team_param(self, monitoring_client):
        data = monitoring_client(_manager_a()).get(
            "/api/v1/tasks/operational-monitoring?team=Finance"
        ).json()
        task_ids = {r["task_id"] for r in data["task_status_rows"]}
        assert "task-3" not in task_ids


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------

class TestActionItems:
    def test_manager_a_sees_action_items(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert "ai-1" in [ai["action_item_id"] for ai in data["action_items"]]

    def test_action_item_has_owner_and_review_state(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        ai = next(x for x in data["action_items"] if x["action_item_id"] == "ai-1")
        assert ai["owner_name"] == "Alice Smith"
        assert ai["review_status"] == "pending_review"
        assert ai["ownership_status"] == "confirmed"
        assert ai["meeting_title"] == "Sprint Planning"

    def test_action_item_has_deadline(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        ai = next(x for x in data["action_items"] if x["action_item_id"] == "ai-1")
        assert ai["deadline_at"] is not None

    def test_manager_b_sees_no_action_items(self, monitoring_client):
        data = monitoring_client(_manager_b()).get("/api/v1/tasks/operational-monitoring").json()
        assert data["action_items"] == []


# ---------------------------------------------------------------------------
# Task status (email + calendar)
# ---------------------------------------------------------------------------

class TestTaskStatus:
    def test_failed_email_shown(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        t1 = next(r for r in data["task_status_rows"] if r["task_id"] == "task-1")
        assert t1["email_status"] == "failed"
        assert "SMTP" in t1["email_error"]

    def test_sent_email_shown(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        t2 = next(r for r in data["task_status_rows"] if r["task_id"] == "task-2")
        assert t2["email_status"] == "sent"
        assert t2["email_error"] is None

    def test_failed_calendar_shown(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        t1 = next(r for r in data["task_status_rows"] if r["task_id"] == "task-1")
        assert t1["calendar_status"] == "failed"
        assert t1["calendar_provider"] == "google"
        assert t1["calendar_attempt_count"] == 3
        assert "quota" in t1["calendar_error"]

    def test_no_calendar_sync_returns_none(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        t2 = next(r for r in data["task_status_rows"] if r["task_id"] == "task-2")
        assert t2["calendar_status"] is None


# ---------------------------------------------------------------------------
# Workflow failures
# ---------------------------------------------------------------------------

class TestWorkflowFailures:
    def test_manager_a_sees_workflow_failure(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert "wf-1" in [wf["run_id"] for wf in data["workflow_failures"]]

    def test_workflow_failure_has_link_and_error(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        wf = next(x for x in data["workflow_failures"] if x["run_id"] == "wf-1")
        assert wf["run_link"] == "/admin/workflows#run=wf-1"
        assert "unavailable" in wf["last_error"]

    def test_manager_b_sees_no_workflow_failures(self, monitoring_client):
        data = monitoring_client(_manager_b()).get("/api/v1/tasks/operational-monitoring").json()
        assert data["workflow_failures"] == []


# ---------------------------------------------------------------------------
# Failure reasons
# ---------------------------------------------------------------------------

class TestFailureReasons:
    def test_includes_email_category(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert "email" in {r["category"] for r in data["failure_reasons"]}

    def test_includes_calendar_category(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert "calendar" in {r["category"] for r in data["failure_reasons"]}

    def test_includes_notification_category(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert "notification" in {r["category"] for r in data["failure_reasons"]}

    def test_each_reason_has_count_and_timestamp(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        for reason in data["failure_reasons"]:
            assert reason["count"] >= 1
            assert reason["last_occurred_at"] is not None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummary:
    def test_team_name(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert data["summary"]["team_name"] == "Engineering"

    def test_email_failed_count(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert data["summary"]["email_failed_count"] == 1

    def test_calendar_failed_count(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert data["summary"]["calendar_failed_count"] == 1

    def test_total_failures(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        # email(1) + calendar(1) + workflow(1) = 3
        assert data["summary"]["total_failures"] == 3

    def test_action_items_pending_review(self, monitoring_client):
        data = monitoring_client(_manager_a()).get("/api/v1/tasks/operational-monitoring").json()
        assert data["summary"]["action_items_pending_review"] == 1


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

class TestCsvExport:
    def test_employee_gets_403(self, monitoring_client):
        response = monitoring_client(_employee()).get(
            "/api/v1/tasks/operational-monitoring/export.csv"
        )
        assert response.status_code == 403

    def test_manager_gets_200_with_csv_content_type(self, monitoring_client):
        response = monitoring_client(_manager_a()).get(
            "/api/v1/tasks/operational-monitoring/export.csv"
        )
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]

    def test_csv_header_row(self, monitoring_client):
        response = monitoring_client(_manager_a()).get(
            "/api/v1/tasks/operational-monitoring/export.csv"
        )
        header = next(csv.reader(io.StringIO(response.text)))
        assert "task_id" in header
        assert "email_status" in header
        assert "calendar_status" in header

    def test_csv_contains_data_rows(self, monitoring_client):
        response = monitoring_client(_manager_a()).get(
            "/api/v1/tasks/operational-monitoring/export.csv"
        )
        rows = list(csv.reader(io.StringIO(response.text)))
        assert len(rows) >= 3  # header + task-1 + task-2

    def test_csv_isolates_manager_team(self, monitoring_client):
        response = monitoring_client(_manager_a()).get(
            "/api/v1/tasks/operational-monitoring/export.csv"
        )
        assert "task-3" not in response.text

    def test_csv_content_disposition(self, monitoring_client):
        response = monitoring_client(_manager_a()).get(
            "/api/v1/tasks/operational-monitoring/export.csv"
        )
        assert "attachment" in response.headers.get("content-disposition", "")
