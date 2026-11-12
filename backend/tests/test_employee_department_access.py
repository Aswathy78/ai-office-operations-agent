from datetime import datetime, timezone
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.auth_service import AuthenticatedPrincipal
from app.auth_service import provision_account
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.meeting.models import Base, Employee, Task


@pytest.fixture()
def access_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    db: Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    people = [
        Employee(
            id="manager",
            name="Manager",
            email="manager@example.test",
            team="Engineering",
            department="Operations",
            is_active=True,
        ),
        Employee(
            id="report-same-department",
            name="Report",
            email="report@example.test",
            team="Finance",
            department="Operations",
            manager_id="manager",
            is_active=True,
        ),
        Employee(
            id="same-department-other-line",
            name="Other Line",
            email="other-line@example.test",
            team="Engineering",
            department="Operations",
            manager_id="other-manager",
            is_active=True,
        ),
        Employee(
            id="cross-department-report",
            name="Cross Department",
            email="cross-department@example.test",
            team="Engineering",
            department="Finance",
            manager_id="manager",
            is_active=True,
        ),
        Employee(
            id="unknown-department-report",
            name="Unknown Department",
            email="unknown@example.test",
            team="Engineering",
            manager_id="manager",
            is_active=True,
        ),
        Employee(
            id="unassigned-same-team",
            name="Unassigned",
            email="unassigned@example.test",
            team="Engineering",
            department="Operations",
            is_active=True,
        ),
    ]
    db.add_all(people)
    db.flush()
    for employee in people:
        db.add(
            Task(
                id=f"task-{employee.id}",
                title=f"Task for {employee.name}",
                assignee_id=employee.id,
                creator_id=employee.id,
                source="assistant",
                idempotency_key=str(uuid.uuid4()),
                due_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
                priority="medium",
            )
        )
    db.commit()
    current = {"principal": _principal(people[0], "manager")}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_authenticated_principal] = lambda: current["principal"]
    try:
        with TestClient(app) as client:
            yield client, db, current, people
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_authenticated_principal, None)
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
        jti=f"session-{employee.id}",
    )


def _task_titles(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {task["title"] for task in response.json()}


def test_manager_scope_requires_same_known_department_and_reporting_visibility(access_api):
    client, _db, _current, people = access_api
    visible = {
        "Task for Manager",
        "Task for Report",
        "Task for Unassigned",
    }

    assert _task_titles(client.get("/api/v1/tasks/dashboard")) == visible
    directory = client.get("/api/v1/meetings/employees")
    assert directory.status_code == 200, directory.text
    assert {item["id"] for item in directory.json()} == {
        "manager",
        "report-same-department",
        "unassigned-same-team",
    }
    visible_directory_lookup = client.post(
        "/api/v1/assistant/tools/directory.lookup_employees/execute",
        json={"arguments": {"emails": ["report@example.test"]}},
    )
    assert visible_directory_lookup.status_code == 200
    assert [
        employee["id"]
        for employee in visible_directory_lookup.json()["result"]["employees"]
    ] == ["report-same-department"]

    for email in (
        "other-line@example.test",
        "cross-department@example.test",
        "unknown@example.test",
    ):
        denied = client.post(
            "/api/v1/assistant/tools/task.list/execute",
            json={"arguments": {"assignee_email": email}},
        )
        assert denied.status_code == 403
    denied_directory_lookup = client.post(
        "/api/v1/assistant/tools/directory.lookup_employees/execute",
        json={"arguments": {"emails": ["cross-department@example.test"]}},
    )
    assert denied_directory_lookup.status_code == 403

    same_department = client.post(
        "/api/v1/assistant/tools/task.list/execute",
        json={"arguments": {"assignee_email": "report@example.test"}},
    )
    assert same_department.status_code == 200
    assert {task["title"] for task in same_department.json()["result"]["tasks"]} == {
        "Task for Report"
    }


def test_unknown_manager_department_fails_closed_but_manager_keeps_own_access(access_api):
    client, db, current, people = access_api
    manager = db.get(Employee, "manager")
    manager.department = None
    db.commit()

    assert _task_titles(client.get("/api/v1/tasks/dashboard")) == {"Task for Manager"}
    directory = client.get("/api/v1/meetings/employees")
    assert directory.status_code == 200
    assert [item["id"] for item in directory.json()] == ["manager"]

    current["principal"] = _principal(manager, "manager")
    own = client.post(
        "/api/v1/assistant/tools/task.list/execute",
        json={"arguments": {"assignee_email": "manager@example.test"}},
    )
    assert own.status_code == 200


def test_operator_provisioning_sets_only_explicit_department(access_api):
    _client, db, _current, people = access_api
    manager = people[0]
    account = provision_account(
        db,
        manager.email,
        "ValidTestPassword123!",
        role="manager",
        department="Shared Services",
    )
    db.refresh(manager)
    assert account.employee_id == manager.id
    assert manager.department == "Shared Services"
    assert manager.team == "Engineering"


@pytest.mark.parametrize("role", ["hr_operations", "admin"])
def test_hr_and_admin_keep_cross_department_task_and_directory_access(access_api, role):
    client, _db, current, people = access_api
    current["principal"] = _principal(people[0], role)

    titles = _task_titles(client.get("/api/v1/tasks/dashboard"))
    assert len(titles) == len(people)
    directory = client.get("/api/v1/meetings/employees")
    assert directory.status_code == 200
    assert {item["id"] for item in directory.json()} == {employee.id for employee in people}
