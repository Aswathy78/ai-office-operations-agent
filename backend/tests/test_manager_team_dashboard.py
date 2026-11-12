from datetime import datetime, timedelta, timezone
import uuid

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
    AuthAuditEvent,
    Base,
    Employee,
    Task,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
)


@pytest.fixture()
def dashboard_env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db: Session = session_factory()

    # Managers
    maya = Employee(
        id="emp-maya",
        name="Maya Thompson",
        email="manager@example.com",
        team="Engineering",
        department="Operations",
        manager_id=None,
        is_active=True,
    )
    isabella = Employee(
        id="emp-isabella",
        name="Isabella Lopez",
        email="isabella.lopez@northstar.example",
        team="Finance",
        department="Finance",
        manager_id=None,
        is_active=True,
    )
    lonely_mgr = Employee(
        id="emp-lonely-mgr",
        name="Solo Manager",
        email="solo.mgr@example.com",
        team="Research",
        department="Research",
        manager_id=None,
        is_active=True,
    )

    # Engineering reports
    alex = Employee(
        id="emp-alex",
        name="Alex Rivera",
        email="alex@northstar.example",
        team="Engineering",
        department="Operations",
        manager_id=maya.id,
        is_active=True,
    )
    marcus = Employee(
        id="emp-marcus",
        name="Marcus Chen",
        email="marcus@northstar.example",
        team="Engineering",
        department="Operations",
        manager_id=maya.id,
        is_active=True,
    )
    # Direct report with 0 tasks
    daniel = Employee(
        id="emp-daniel",
        name="Daniel Brooks",
        email="daniel@northstar.example",
        team="Engineering",
        department="Operations",
        manager_id=maya.id,
        is_active=True,
    )

    # Finance report
    noah = Employee(
        id="emp-noah",
        name="Noah Martin",
        email="noah@northstar.example",
        team="Finance",
        department="Finance",
        manager_id=isabella.id,
        is_active=True,
    )

    db.add_all([maya, isabella, lonely_mgr, alex, marcus, daniel, noah])
    db.flush()

    now = datetime.now(timezone.utc)

    # Alex tasks:
    # 1. Overdue pending task (due 3 hours ago)
    t1 = Task(
        id="task-alex-overdue",
        title="Alex Overdue Fix",
        assignee_id=alex.id,
        creator_id=maya.id,
        source="manual",
        status=TASK_STATUS_PENDING,
        due_at=now - timedelta(hours=3),
        priority="high",
        idempotency_key=str(uuid.uuid4()),
    )
    # 2. Due-soon pending task (due in 4 hours)
    t2 = Task(
        id="task-alex-due-soon",
        title="Alex Due Soon Review",
        assignee_id=alex.id,
        creator_id=maya.id,
        source="manual",
        status=TASK_STATUS_PENDING,
        due_at=now + timedelta(hours=4),
        priority="medium",
        idempotency_key=str(uuid.uuid4()),
    )
    # 3. Completed task (due 2 days ago, completed) -> NOT overdue!
    t3 = Task(
        id="task-alex-completed-past",
        title="Alex Completed Project",
        assignee_id=alex.id,
        creator_id=maya.id,
        source="manual",
        status=TASK_STATUS_COMPLETED,
        due_at=now - timedelta(days=2),
        priority="medium",
        idempotency_key=str(uuid.uuid4()),
    )

    # Marcus tasks:
    # 4. Future pending task (due in 5 days) -> not due soon, not overdue
    t4 = Task(
        id="task-marcus-future",
        title="Marcus Future Roadmap",
        assignee_id=marcus.id,
        creator_id=maya.id,
        source="manual",
        status=TASK_STATUS_PENDING,
        due_at=now + timedelta(days=5),
        priority="low",
        idempotency_key=str(uuid.uuid4()),
    )
    # 5. Completed task
    t5 = Task(
        id="task-marcus-completed",
        title="Marcus Completed Doc",
        assignee_id=marcus.id,
        creator_id=maya.id,
        source="manual",
        status=TASK_STATUS_COMPLETED,
        due_at=now + timedelta(days=1),
        priority="low",
        idempotency_key=str(uuid.uuid4()),
    )

    # Noah tasks (Finance):
    # 6. Overdue pending task in Finance
    t6 = Task(
        id="task-noah-overdue",
        title="Noah Budget Audit",
        assignee_id=noah.id,
        creator_id=isabella.id,
        source="manual",
        status=TASK_STATUS_PENDING,
        due_at=now - timedelta(hours=6),
        priority="high",
        idempotency_key=str(uuid.uuid4()),
    )

    db.add_all([t1, t2, t3, t4, t5, t6])
    db.commit()

    current_principal = {
        "value": AuthenticatedPrincipal(
            user_id=maya.id,
            account_id="account-maya",
            email=maya.email,
            name=maya.name,
            role="manager",
            team=maya.team,
            jti="session-maya",
        )
    }

    def override_get_db():
        yield db

    def override_principal():
        return current_principal["value"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_authenticated_principal] = override_principal
    try:
        with TestClient(app) as client:
            yield client, current_principal, db, {
                "maya": maya,
                "isabella": isabella,
                "lonely_mgr": lonely_mgr,
                "alex": alex,
                "marcus": marcus,
                "daniel": daniel,
                "noah": noah,
            }
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_authenticated_principal, None)
        db.rollback()
        db.close()
        engine.dispose()


def test_employee_cannot_access_team_dashboard(dashboard_env):
    client, principal, db, entities = dashboard_env
    alex = entities["alex"]
    principal["value"] = AuthenticatedPrincipal(
        user_id=alex.id,
        account_id=f"acc-{alex.id}",
        email=alex.email,
        name=alex.name,
        role="employee",
        team=alex.team,
        jti="session-alex",
    )

    resp = client.get("/api/v1/tasks/team-dashboard")
    assert resp.status_code == 403
    assert "Employees cannot access" in resp.json()["detail"]
    record = db.query(AuthAuditEvent).filter_by(event="access_denied").one()
    assert record.actor_id == alex.id
    assert record.detail == "HTTP 403 GET denied."


def test_manager_retrieves_team_dashboard(dashboard_env):
    client, principal, _, entities = dashboard_env
    # Authenticated as Maya (Engineering manager)
    resp = client.get("/api/v1/tasks/team-dashboard")
    assert resp.status_code == 200
    data = resp.json()

    summary = data["summary"]
    assert summary["team_name"] == "Engineering"
    # Maya has 3 direct reports: Alex, Marcus, Daniel
    assert summary["total_members"] == 3
    # Alex has 3 tasks, Marcus has 2 tasks, Daniel has 0 tasks -> total = 5
    assert summary["total_tasks"] == 5
    # Completed tasks: Alex (1), Marcus (1) -> 2
    assert summary["completed_tasks"] == 2
    # Open tasks: Alex (2), Marcus (1) -> 3
    assert summary["open_tasks"] == 3
    # Due soon tasks: Alex (1: t2) -> 1
    assert summary["due_soon_tasks"] == 1
    # Overdue tasks: Alex (1: t1) -> 1
    assert summary["overdue_tasks"] == 1
    # Completion rate: (2 / 5) * 100 = 40.0%
    assert summary["completion_rate"] == 40.0

    # Check members workload list
    members = {m["employee_id"]: m for m in data["members"]}
    assert len(members) == 3

    # Daniel has 0 tasks
    daniel_stats = members[entities["daniel"].id]
    assert daniel_stats["total_tasks"] == 0
    assert daniel_stats["open_tasks"] == 0
    assert daniel_stats["completed_tasks"] == 0
    assert daniel_stats["due_soon_tasks"] == 0
    assert daniel_stats["overdue_tasks"] == 0
    assert daniel_stats["completion_rate"] == 0.0

    # Alex has 3 tasks: 1 completed, 2 open (1 overdue, 1 due soon)
    alex_stats = members[entities["alex"].id]
    assert alex_stats["total_tasks"] == 3
    assert alex_stats["completed_tasks"] == 1
    assert alex_stats["open_tasks"] == 2
    assert alex_stats["overdue_tasks"] == 1
    assert alex_stats["due_soon_tasks"] == 1
    assert alex_stats["completion_rate"] == 33.3

    # Marcus has 2 tasks: 1 completed, 1 open (future)
    marcus_stats = members[entities["marcus"].id]
    assert marcus_stats["total_tasks"] == 2
    assert marcus_stats["completed_tasks"] == 1
    assert marcus_stats["open_tasks"] == 1
    assert marcus_stats["overdue_tasks"] == 0
    assert marcus_stats["due_soon_tasks"] == 0
    assert marcus_stats["completion_rate"] == 50.0

    # Check overdue tasks list
    overdue_tasks = data["overdue_tasks"]
    assert len(overdue_tasks) == 1
    assert overdue_tasks[0]["task_id"] == "task-alex-overdue"
    assert overdue_tasks[0]["assignee_id"] == entities["alex"].id
    assert overdue_tasks[0]["title"] == "Alex Overdue Fix"
    assert overdue_tasks[0]["overdue_hours"] >= 3
    assert overdue_tasks[0]["priority"] == "high"


def test_completed_tasks_with_past_due_date_are_not_overdue(dashboard_env):
    client, _, _, entities = dashboard_env
    # Task t3 ("Alex Completed Project") was due 2 days ago, but status is "completed".
    resp = client.get("/api/v1/tasks/team-dashboard")
    assert resp.status_code == 200
    data = resp.json()

    overdue_task_ids = [t["task_id"] for t in data["overdue_tasks"]]
    assert "task-alex-completed-past" not in overdue_task_ids
    assert data["summary"]["overdue_tasks"] == 1


def test_manager_team_isolation_and_parameter_tampering(dashboard_env):
    client, principal, _, entities = dashboard_env
    # Maya (Engineering) requests with ?team=Finance (trying to inspect another team)
    resp = client.get("/api/v1/tasks/team-dashboard", params={"team": "Finance"})
    assert resp.status_code == 200
    data = resp.json()

    # Query param MUST be ignored for managers. Still shows Engineering!
    assert data["summary"]["team_name"] == "Engineering"
    member_ids = {m["employee_id"] for m in data["members"]}
    assert entities["noah"].id not in member_ids
    assert entities["alex"].id in member_ids

    # Switch principal to Isabella (Finance manager)
    isabella = entities["isabella"]
    principal["value"] = AuthenticatedPrincipal(
        user_id=isabella.id,
        account_id=f"acc-{isabella.id}",
        email=isabella.email,
        name=isabella.name,
        role="manager",
        team=isabella.team,
        jti="session-isabella",
    )

    resp_fin = client.get("/api/v1/tasks/team-dashboard")
    assert resp_fin.status_code == 200
    fin_data = resp_fin.json()
    assert fin_data["summary"]["team_name"] == "Finance"
    fin_members = {m["employee_id"] for m in fin_data["members"]}
    assert entities["noah"].id in fin_members
    assert entities["alex"].id not in fin_members
    assert fin_data["summary"]["total_tasks"] == 1
    assert fin_data["summary"]["overdue_tasks"] == 1


def test_manager_with_no_reports_receives_empty_dashboard(dashboard_env):
    client, principal, _, entities = dashboard_env
    lonely = entities["lonely_mgr"]
    principal["value"] = AuthenticatedPrincipal(
        user_id=lonely.id,
        account_id=f"acc-{lonely.id}",
        email=lonely.email,
        name=lonely.name,
        role="manager",
        team=lonely.team,
        jti="session-lonely",
    )

    resp = client.get("/api/v1/tasks/team-dashboard")
    assert resp.status_code == 200
    data = resp.json()

    assert data["summary"]["team_name"] == "Research"
    assert data["summary"]["total_members"] == 0
    assert data["summary"]["total_tasks"] == 0
    assert data["summary"]["open_tasks"] == 0
    assert data["summary"]["due_soon_tasks"] == 0
    assert data["summary"]["overdue_tasks"] == 0
    assert data["summary"]["completed_tasks"] == 0
    assert data["summary"]["completion_rate"] == 0.0
    assert data["members"] == []
    assert data["overdue_tasks"] == []


def test_admin_and_hr_can_filter_teams(dashboard_env):
    client, principal, _, entities = dashboard_env
    admin_id = str(uuid.uuid4())
    principal["value"] = AuthenticatedPrincipal(
        user_id=admin_id,
        account_id=f"acc-{admin_id}",
        email="admin@northstar.example",
        name="Admin",
        role="admin",
        team="Operations",
        jti="session-admin",
    )

    # Admin filtering Finance
    resp_fin = client.get("/api/v1/tasks/team-dashboard", params={"team": "Finance"})
    assert resp_fin.status_code == 200
    assert resp_fin.json()["summary"]["team_name"] == "Finance"
    member_names = [m["employee_name"] for m in resp_fin.json()["members"]]
    assert "Noah Martin" in member_names
    assert "Alex Rivera" not in member_names

    # Admin filtering Engineering
    resp_eng = client.get("/api/v1/tasks/team-dashboard", params={"team": "Engineering"})
    assert resp_eng.status_code == 200
    assert resp_eng.json()["summary"]["team_name"] == "Engineering"
    member_names_eng = [m["employee_name"] for m in resp_eng.json()["members"]]
    assert "Alex Rivera" in member_names_eng
    assert "Noah Martin" not in member_names_eng


def test_dashboard_data_freshness_on_task_completion(dashboard_env):
    client, principal, db, entities = dashboard_env
    # Initial state: Maya's team completion rate is 40.0% (2/5)
    r1 = client.get("/api/v1/tasks/team-dashboard")
    assert r1.status_code == 200
    assert r1.json()["summary"]["completed_tasks"] == 2
    assert r1.json()["summary"]["completion_rate"] == 40.0
    assert r1.json()["summary"]["overdue_tasks"] == 1

    # Mark Alex's overdue task as completed
    alex_overdue = db.get(Task, "task-alex-overdue")
    alex_overdue.status = TASK_STATUS_COMPLETED
    db.commit()

    # Immediate next call
    r2 = client.get("/api/v1/tasks/team-dashboard")
    assert r2.status_code == 200
    assert r2.json()["summary"]["completed_tasks"] == 3
    assert r2.json()["summary"]["completion_rate"] == 60.0  # 3/5
    assert r2.json()["summary"]["overdue_tasks"] == 0
    assert len(r2.json()["overdue_tasks"]) == 0
